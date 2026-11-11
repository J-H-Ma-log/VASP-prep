"""db.py 的单元测试：不联网、不依赖 pymatgen。

【怎么做到不联网】把 `db._http_json` 换成假函数，返回手写的 OPTIMADE 记录；
检索/下载的解析、落盘、命名、错误提示全部照真跑。
真网络只在用户实际使用时才走，测试不该因为断网/库临时故障变红。
"""

from __future__ import annotations

import json
import shutil
import unittest
import uuid
import zipfile
from pathlib import Path

from vasp_core import db, paths
from vasp_core.config import ROOT, load
from vasp_core.errors import PrepError

POSCAR_CU = """Cu fcc primitive
   1.00000000000000
     2.5527000000    0.0000000000    0.0000000000
     1.2763500000    2.2107000000    0.0000000000
     1.2763500000    0.7369000000    2.0842000000
   Cu
     1
Direct
     0.0000000000    0.0000000000    0.0000000000
"""

# 一条最小的 OPTIMADE 记录：正交晶格 + 2 个原子（Mo、S）
OPTIMADE_ENTRY = {
    "id": "mp-999",
    "attributes": {
        "lattice_vectors": [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 10.0]],
        "cartesian_site_positions": [[0.0, 0.0, 0.0], [1.5, 1.5, 5.0]],
        "species_at_sites": ["Mo", "S"],
        "nsites": 2,
        "chemical_formula_reduced": "MoS",
    },
}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        root = ROOT / "tests" / "_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = root / f"db_{uuid.uuid4().hex[:10]}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # 把 materials 指到测试目录，别污染仓库
        self.cfg = load()
        self.cfg.paths.materials_dir = str(self.tmp / "materials")
        self.cfg.paths.cache_dir = str(self.tmp / "cache")

    def fake_db(self, entry=OPTIMADE_ENTRY, calls=None):
        """替换 _http_json：记录调用 URL，返回预置记录。"""
        def _fake(url, timeout=30):
            if calls is not None:
                calls.append(url)
            return {"data": [entry], "meta": {"data_returned": 1}}
        orig = db._http_json
        db._http_json = _fake
        self.addCleanup(lambda: setattr(db, "_http_json", orig))


class TestHelpers(Base):
    def test_elements_of(self):
        self.assertEqual(db.elements_of("MoS2"), ["Mo", "S"])
        self.assertEqual(db.elements_of("Bi2O3"), ["Bi", "O"])
        self.assertEqual(db.elements_of("CO"), ["C", "O"])  # 不能误判成 Co
        with self.assertRaises(PrepError):
            db.elements_of("123")

    def test_hill_formula(self):
        self.assertEqual(db._hill_formula(["Mo", "S", "S"]), "MoS2")
        self.assertEqual(db._hill_formula(["S", "Mo"]), "MoS")
        self.assertEqual(db._hill_formula(["C", "H", "O", "H"]), "CH2O")

    def test_norm_formula(self):
        self.assertEqual(db._norm_formula("Mo S2"), "mos2")
        self.assertEqual(db._norm_formula("S2Mo"), "s2mo")

    def test_poscar_from_optimade(self):
        text = db.poscar_from_optimade(OPTIMADE_ENTRY)
        from vasp_core.poscar import read_poscar_text
        st = read_poscar_text(text)
        self.assertEqual(st.species, ["Mo", "S"])
        self.assertEqual(st.counts, [1, 1])
        self.assertAlmostEqual(st.volume(), 90.0)
        # 第二个原子分数坐标应为 (0.5,0.5,0.5)
        self.assertAlmostEqual(st.frac()[1][0], 0.5, places=6)

    def test_poscar_from_optimade_missing_fields(self):
        with self.assertRaises(PrepError):
            db.poscar_from_optimade({"id": "x", "attributes": {}})


class TestSearch(Base):
    def test_search_dispatch_unknown(self):
        with self.assertRaises(PrepError) as ctx:
            db.search("nope", "MoS2", cfg=self.cfg)
        self.assertIn("不认识的数据源", str(ctx.exception))

    def test_search_c2db_says_import(self):
        with self.assertRaises(PrepError) as ctx:
            db.search("c2db", "MoS2", cfg=self.cfg)
        self.assertIn("导入", str(ctx.exception))

    def test_search_mp_uses_page_limit_and_filters(self):
        calls: list[str] = []
        self.fake_db(calls=calls)
        items = db.search("mp", "MoS", limit=5, cfg=self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "mp-999")
        self.assertEqual(items[0].formula, "MoS")
        self.assertIn("page_limit=5", calls[0])
        self.assertIn("chemical_formula_reduced", calls[0])

    def test_search_oqmd_provider_field(self):
        self.fake_db()
        items = db.search("oqmd", "MoS", limit=2, cfg=self.cfg)
        self.assertEqual(items[0].provider, "oqmd")


class TestDownload(Base):
    def test_download_writes_poscar_and_source(self):
        self.fake_db()
        cand = db.Candidate(provider="mp", id="mp-999", formula="MoS")
        dest = db.download(cand, self.cfg)
        self.assertEqual(dest.name, "mp_mp-999")
        self.assertTrue((dest / "POSCAR").exists())
        meta = json.loads((dest / "SOURCE.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["provider"], "mp")
        self.assertEqual(meta["id"], "mp-999")
        from vasp_core.poscar import read_poscar
        st = read_poscar(dest / "POSCAR")
        self.assertEqual(st.counts, [1, 1])

    def test_download_skips_existing(self):
        self.fake_db()
        cand = db.Candidate(provider="mp", id="mp-999")
        dest = db.download(cand, self.cfg)
        before = (dest / "POSCAR").stat().st_mtime_ns
        again = db.download(cand, self.cfg)
        self.assertEqual(again, dest)
        self.assertEqual((dest / "POSCAR").stat().st_mtime_ns, before)

    def test_download_subdir_and_unsupported(self):
        self.fake_db()
        cand = db.Candidate(provider="mp", id="mp-999")
        dest = db.download(cand, self.cfg, subdir="二维材料")
        self.assertEqual(dest.parent.name, "二维材料")
        with self.assertRaises(PrepError):
            db.download(db.Candidate(provider="aflow", id="aflow-1"), self.cfg)


class TestImport(Base):
    def test_import_poscar(self):
        src = self.tmp / "MoS2"
        src.mkdir()
        (src / "POSCAR").write_text(POSCAR_CU, encoding="utf-8")
        dest = db.import_file(src / "POSCAR", cfg=self.cfg)
        self.assertTrue((dest / "POSCAR").exists())
        self.assertEqual(json.loads((dest / "SOURCE.json").read_text(encoding="utf-8"))["provider"],
                         "import")

    def test_import_zip(self):
        src = self.tmp / "x.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("inner/POSCAR", POSCAR_CU)
        dest = db.import_file(src, cfg=self.cfg)
        self.assertTrue((dest / "POSCAR").exists())

    def test_import_folder_mixed(self):
        d = self.tmp / "exported"
        d.mkdir()
        (d / "a.vasp").write_text(POSCAR_CU, encoding="utf-8")
        (d / "b.poscar").write_text(POSCAR_CU, encoding="utf-8")
        (d / "note.txt").write_text("忽略我", encoding="utf-8")
        out = db.import_folder(d, cfg=self.cfg)
        self.assertEqual(len(out), 2)

    def test_import_missing(self):
        with self.assertRaises(PrepError):
            db.import_file(self.tmp / "nope.vasp", cfg=self.cfg)


class TestCli(Base):
    def test_cli_search_json(self):
        self.fake_db()
        # 直接调 main()，避免起子进程
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = db.main(["search", "--provider", "mp", "--formula", "MoS", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["items"][0]["id"], "mp-999")

    def test_cli_unknown_provider_exit2(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = db.main(["search", "--provider", "mp", "--formula", "123"])
        self.assertEqual(rc, 2)
        self.assertIn("[错误]", err.getvalue())


if __name__ == "__main__":
    unittest.main()
