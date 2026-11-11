"""批量前处理（v3）的测试。

【为什么单独一个文件】批量是"一句话 × N 个材料 × M 个参数"，出错的组合比单文件多
（重名、坏 POSCAR、参数非法、目录已存在）；这些都要有用例，不能只靠手点网页。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
import uuid
from pathlib import Path

from vasp_core import batch, config
from vasp_core.errors import PrepError

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = ROOT / "tests" / "_tmp"

POSCAR_CU = """Cu1
1.0
3.61 0 0
0 3.61 0
0 0 3.61
Cu
1
Direct
0 0 0
"""

# 真实 MoS2 的简化版：元素行故意重复（Mo S Mo S），批量里必须能正确处理
POSCAR_MOS2 = """MoS2
3.18013
3.191 0 0
-1.592 2.765 0
0 0 24.837
Mo S Mo S
1 2 1 2
Direct
0 0 0.1
0.333 0.667 0.15
0.667 0.333 0.15
0.5 0.5 0.2
0.833 0.167 0.25
0.167 0.833 0.25
"""

INCAR_MOS2 = """SYSTEM = MoS2
IVDW = 11
ENCUT = 400
"""


class Base(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = TMP_ROOT / f"batch_{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # 配置里把 batches 指到临时目录，避免污染真实 batches/
        self.cfg = config.Config()
        self.cfg.paths.batches_dir = str(self.tmp / "batches")
        self.root = self.tmp / "mats"
        self.root.mkdir()
        (self.tmp / ".tmp_service").mkdir(exist_ok=True)

    def mat(self, name: str, poscar: str = POSCAR_CU, incar: str | None = None) -> Path:
        d = self.root / name
        d.mkdir()
        (d / "POSCAR").write_text(poscar, encoding="utf-8")
        if incar is not None:
            (d / "INCAR").write_text(incar, encoding="utf-8")
        return d

    def preview(self, text="扩成 2x2x1", ivdw="", **kw):
        return batch.preview_batch(str(self.root), text, cfg=self.cfg, ivdw=ivdw, **kw)


class TestFind(Base):
    def test_find_materials_depth0(self):
        self.mat("Cu")
        self.mat("MoS2", POSCAR_MOS2, INCAR_MOS2)
        found = batch.find_materials(self.root)
        self.assertEqual(sorted(p.name for p in found), ["Cu", "MoS2"])

    def test_find_materials_ignores_non_material_dirs(self):
        self.mat("Cu")
        (self.root / "empty").mkdir()
        self.assertEqual([p.name for p in batch.find_materials(self.root)], ["Cu"])

    def test_find_materials_depth1(self):
        inner = self.root / "groupA" / "MoS2"
        inner.mkdir(parents=True)
        (inner / "POSCAR").write_text(POSCAR_MOS2, encoding="utf-8")
        self.mat("Cu")
        self.assertEqual(len(batch.find_materials(self.root, depth=0)), 1)
        self.assertEqual(len(batch.find_materials(self.root, depth=1)), 2)

    def test_find_materials_missing_root(self):
        with self.assertRaises(PrepError):
            batch.find_materials(self.tmp / "nope")

    def test_scan_marks_broken_material(self):
        self.mat("Cu")
        bad = self.root / "broken"
        bad.mkdir()
        (bad / "POSCAR").write_text("这不是 POSCAR\n", encoding="utf-8")
        scan = batch.scan_materials(self.root, cfg=self.cfg)
        self.assertEqual(scan["n_found"], 2)
        self.assertEqual(scan["n_ok"], 1)
        self.assertFalse(next(x for x in scan["materials"] if x["name"] == "broken")["ok"])


class TestNaming(Base):
    def test_parse_values(self):
        self.assertEqual(batch.parse_values("0,11,12"), ["0", "11", "12"])
        self.assertEqual(batch.parse_values("11，11 12"), ["11", "12"])
        self.assertEqual(batch.parse_values(""), [""])
        self.assertEqual(batch.parse_values("  "), [""])

    def test_batch_name_user_and_default(self):
        self.assertEqual(batch.make_batch_name("随便", "我的批次"), "我的批次")
        auto = batch.make_batch_name("扩成 2x2x1，加 DFT-D3(BJ)", "")
        self.assertTrue(auto.startswith("batch_"))
        self.assertIn("2x2x1", auto)

    def test_safe_batch_name_strips_illegal(self):
        self.assertEqual(batch.make_batch_name("x", 'a/b:c*"d'), "a_b_c__d")

    def test_plan_targets_all_combinations(self):
        rows = batch.plan_targets(Path("B"), ["Cu", "MoS2"], ["", "11"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["target"], Path("B/Cu"))
        self.assertEqual(rows[1]["target"], Path("B/Cu/ivdw_11"))

    def test_plan_targets_dedupes_names(self):
        rows = batch.plan_targets(Path("B"), ["Cu", "Cu"], [""])
        self.assertEqual([r["target"].name for r in rows], ["Cu", "Cu_2"])


class TestPreview(Base):
    def test_preview_counts_and_no_write(self):
        self.mat("Cu")
        self.mat("MoS2", POSCAR_MOS2, INCAR_MOS2)
        pv = self.preview(ivdw="0,11,12")
        self.assertEqual(pv["n_total"], 6)
        self.assertEqual(pv["n_ok"], 6)
        self.assertFalse(Path(pv["batch_dir"]).exists(), "预览不应该创建批次目录")

    def test_preview_sweep_ivdw_each_value(self):
        self.mat("MoS2", POSCAR_MOS2, INCAR_MOS2)
        pv = self.preview(text="加色散修正", ivdw="0,11,12")
        vals = {}
        for it in pv["items"]:
            ch = {c["key"]: c["new"] for c in it["incar_changes"]}
            # 底稿里本来就是 IVDW=11，扫 11 时不会产生"改动"（等同值被过滤）；
            # 这不算错：结果文件里仍然是 11。
            vals[it["value"]] = ch.get("IVDW", "11") if "IVDW" not in ch else ch["IVDW"]
        self.assertEqual(vals, {"0": "0", "11": "11", "12": "12"})

    def test_preview_multi_material_same_recipe(self):
        self.mat("Cu")
        self.mat("MoS2", POSCAR_MOS2, INCAR_MOS2)
        pv = self.preview()
        self.assertEqual(pv["n_total"], 2)
        for it in pv["items"]:
            self.assertEqual(Path(it["recipe"]["base_dir"]).name, it["material"])
            self.assertTrue(any("supercell" in json.dumps(o) for o in it["ops"]))

    def test_preview_empty_text(self):
        self.mat("Cu")
        with self.assertRaises(PrepError):
            self.preview(text="  ")

    def test_preview_no_materials(self):
        with self.assertRaises(PrepError) as ctx:
            self.preview()
        self.assertIn("没有找到", str(ctx.exception))

    def test_preview_max_materials(self):
        for i in range(3):
            self.mat(f"m{i}")
        self.cfg.batch.max_materials = 2
        with self.assertRaises(PrepError) as ctx:
            self.preview()
        self.assertIn("材料太多", str(ctx.exception))

    def test_preview_bad_ivdw_only_breaks_that_item(self):
        self.mat("Cu")
        pv = self.preview(text="加色散修正", ivdw="11,99")
        status = {it["value"]: it["status"] for it in pv["items"]}
        self.assertEqual(status["11"], "ok")
        self.assertEqual(status["99"], "error")
        self.assertIn("IVDW", pv["items"][1]["issue"])


class TestWrite(Base):
    def test_write_creates_files_and_summary(self):
        self.mat("Cu")
        self.mat("MoS2", POSCAR_MOS2, INCAR_MOS2)
        pv = self.preview(ivdw="0,12")
        pv = batch.write_batch(pv)
        self.assertEqual(pv["written"], 4)
        bdir = Path(pv["batch_dir"])
        for name in ("Cu", "MoS2"):
            for v in ("ivdw_0", "ivdw_12"):
                d = bdir / name / v
                for f in ("POSCAR", "INCAR", "INCAR".replace("INCAR", "INCAR"), "INCAR.diff",
                          "change_report.md", "recipe.json"):
                    self.assertTrue((d / f).is_file(), f"{d / f} 缺失")
        self.assertTrue((bdir / "batch_summary.csv").is_file())
        self.assertTrue((bdir / "batch_report.md").is_file())
        csv_text = (bdir / "batch_summary.csv").read_text(encoding="utf-8-sig")
        self.assertIn("MoS2", csv_text)

    def test_write_source_untouched(self):
        cu = self.mat("Cu")
        before = (cu / "POSCAR").stat().st_mtime_ns
        pv = self.preview()
        batch.write_batch(pv)
        self.assertEqual((cu / "POSCAR").stat().st_mtime_ns, before)
        self.assertEqual(sorted(p.name for p in cu.iterdir()), ["POSCAR"])

    def test_write_twice_creates_second_batch(self):
        """同名批次第二次预览会落到 <批次>_2，不覆盖上一次结果。"""
        self.mat("Cu")
        pv = self.preview()
        batch.write_batch(pv)
        pv2 = self.preview()
        self.assertNotEqual(pv["batch_dir"], pv2["batch_dir"])
        self.assertTrue(Path(pv2["batch_dir"]).name.endswith("_2"))
        pv2 = batch.write_batch(pv2)
        self.assertEqual(pv2["written"], 1)

    def test_write_skips_existing_target_without_force(self):
        """目标目录已存在且非空时跳过；force 才重写。"""
        self.mat("Cu")
        pv = self.preview()
        target = Path(pv["items"][0]["target"])
        target.mkdir(parents=True)
        (target / "已有文件.txt").write_text("x", encoding="utf-8")
        pv = batch.write_batch(pv, force=False)
        self.assertEqual(pv["skipped"], 1)
        pv2 = self.preview()
        # 新预览会给出 <批次>_2，这里手动把目标改回刚才那个目录，验证 force 覆盖
        pv2["items"][0]["target"] = str(target)
        pv2 = batch.write_batch(pv2, force=True)
        self.assertEqual(pv2["written"], 1)

    def test_write_material_dir_names_deduped(self):
        self.mat("MoS2")
        # 第二个材料同名但在别的子目录：depth=1 下才会都找到
        inner = self.root / "sub" / "MoS2"
        inner.mkdir(parents=True)
        (inner / "POSCAR").write_text(POSCAR_CU, encoding="utf-8")
        pv = self.preview(depth=1)
        names = [it["material"] for it in pv["items"]]
        self.assertIn("MoS2", names)
        self.assertIn("MoS2_2", names)

    def test_cli_preview_json(self):
        self.mat("Cu")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = batch.main(["--root", str(self.root), "--text", "扩成 2x2x1",
                               "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["n_total"], 1)
        self.assertEqual(data["items"][0]["status"], "ok")

    def test_cli_error_returns_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = batch.main(["--root", str(self.root), "--text", "扩成 2x2x1"])
        self.assertEqual(code, 2)
        self.assertIn("没有找到", err.getvalue())


if __name__ == "__main__":
    unittest.main()
