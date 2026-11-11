"""service.py 的测试：读源目录、规则模式预览、写入、拒绝覆盖。

【为什么这些用例重要】
    网页上"点一下"背后就是这几步；这里错一处，用户看到的是"预览成功但文件没写"
    或者更糟——"源文件被改了"。所以：
      1) 预览阶段必须不碰源目录；
      2) 写入必须拒绝覆盖已有目录；
      3) 规则模式（模型离线）必须给出同样的结果，不能"没网就废"。

运行：python -m unittest discover -s tests
（只用系统 Python 也行：这里不依赖 pymatgen/ase）
"""

from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

from vasp_core import config as config_mod
from vasp_core import service
from vasp_core.errors import PrepError

ROOT = Path(__file__).resolve().parent.parent


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


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # 不用 tempfile.mkdtemp：它按 0o700 建目录，本机 Python 3.14 上连创建者都不可写
        tmp_root = ROOT / "tests" / "_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_root / f"svc_{uuid.uuid4().hex[:10]}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(service.cleanup_all)

        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "POSCAR").write_text(POSCAR_CU, encoding="utf-8")
        (self.src / "INCAR").write_text("SYSTEM = test\nENCUT = 400\n", encoding="utf-8")
        self.out = self.tmp / "out"

        # 测试一律走规则模式：不依赖本机是否开着 Ollama，结果可复现
        self.cfg = config_mod.load()
        self.cfg.llm.enabled = False


class TestSummarize(Base):
    def test_reads_poscar(self):
        info = service.summarize_source(self.src)
        self.assertEqual(info["natoms"], 1)
        self.assertEqual(info["species"], ["Cu"])
        self.assertAlmostEqual(info["lattice_abc"][0], 2.5527, places=3)
        self.assertAlmostEqual(info["volume"], 11.7617, places=2)
        self.assertTrue(info["has_incar"])
        self.assertFalse(info["has_kpoints"])

    def test_missing_dir(self):
        with self.assertRaises(PrepError):
            service.summarize_source(self.tmp / "nope")

    def test_missing_poscar(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(PrepError):
            service.summarize_source(empty)


class TestPreview(Base):
    def test_rules_mode_struct_edit(self):
        """规则模式：扩胞 + 替换。不套 INCAR 预设，只改结构。"""
        job = service.preview(str(self.src), str(self.out),
                              "扩成 2x2x1，第 3 个原子换成 Co", self.cfg)
        self.assertTrue(job.summary["ok"])
        self.assertEqual(job.summary["structure"]["natoms"], [1, 4])
        ops = [o["op"] for o in job.summary["ops"]]
        self.assertEqual(ops, ["supercell", "substitute"])
        # 没有点名的 INCAR 键不该被改（尤其不能套 relax 预设）
        self.assertEqual(job.summary["incar_changes"], [])
        self.assertIn("POSCAR", job.diffs)
        self.assertNotIn("INCAR", job.diffs)
        self.assertFalse(self.out.exists(), "预览阶段不能创建最终输出目录")

    def test_rules_mode_encut(self):
        job = service.preview(str(self.src), str(self.out), "ENCUT 调到 520", self.cfg)
        keys = [c["key"] for c in job.summary["incar_changes"]]
        self.assertEqual(keys, ["ENCUT"])
        self.assertIn("INCAR", job.diffs)

    def test_unknown_text_no_crash(self):
        """规则模式听不懂也不该崩：返回空 ops + 提示。"""
        job = service.preview(str(self.src), str(self.out), "把体系调得更准一点", self.cfg)
        self.assertEqual(job.summary["structure"]["natoms"], [1, 1])

    def test_empty_text(self):
        with self.assertRaises(PrepError):
            service.preview(str(self.src), str(self.out), "   ", self.cfg)

    def test_source_untouched(self):
        before = (self.src / "POSCAR").read_bytes()
        mtime = (self.src / "POSCAR").stat().st_mtime_ns
        service.preview(str(self.src), str(self.out), "扩成 2x2x1", self.cfg)
        self.assertEqual((self.src / "POSCAR").read_bytes(), before)
        self.assertEqual((self.src / "POSCAR").stat().st_mtime_ns, mtime)


class TestWrite(Base):
    def test_write_then_conflict(self):
        job = service.preview(str(self.src), str(self.out), "扩成 2x2x1", self.cfg)
        job2 = service.write(job.id)
        self.assertEqual(job2.written_to, str(self.out.resolve()))
        for name in ("POSCAR", "INCAR", "change_report.md"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertIn("POSCAR", job2.files)

        # 第二次写同一目录必须拒绝（除非 force）
        job3 = service.preview(str(self.src), str(self.out), "扩成 3x1x1", self.cfg)
        with self.assertRaises(PrepError):
            service.write(job3.id)
        # force 可以覆盖
        job4 = service.write(job3.id, force=True)
        self.assertIn("POSCAR", job4.files)

    def test_write_unknown_job(self):
        with self.assertRaises(PrepError):
            service.write("does-not-exist")


class TestRecipePolicy(Base):
    def test_calc_detection(self):
        self.assertEqual(service._detect_calc("扩成 2x2x1"), "none")
        self.assertEqual(service._detect_calc("做个静态计算"), "static")
        self.assertEqual(service._detect_calc("算能带"), "band")
        self.assertEqual(service._detect_calc("结构优化一下"), "relax")

    def test_preset_only_when_asked(self):
        job = service.preview(str(self.src), str(self.out), "结构优化", self.cfg)
        keys = [c["key"] for c in job.summary["incar_changes"]]
        self.assertIn("ENCUT", keys)       # relax 预设有 ENCUT
        self.assertIn("ISIF", keys)


class TestVdw(unittest.TestCase):
    """IVDW：说话点名才改、方法名映射、模型擅自加的要丢掉。"""

    def test_method_mapping(self):
        from vasp_core import nl

        cases = {"加 DFT-D3(BJ)": 12, "加 D3 零阻尼": 11, "加 DFT-D3": 12,
                 "加 D2": 1, "加 TS 修正": 20, "加 MBD": 202, "加 D4": 13}
        for text, want in cases.items():
            got = nl.text_to_recipe(text)["recipe"]["incar"]["overrides"].get("IVDW")
            self.assertEqual(got, want, f"{text!r} 应映射到 IVDW={want}")

    def test_bare_dispersion_needs_human(self):
        from vasp_core import nl

        r = nl.text_to_recipe("加色散校正")
        self.assertNotIn("IVDW", r["recipe"]["incar"]["overrides"])
        self.assertTrue(any("vdW" in n["topic"] for n in r["needs_human"]))

    def test_strip_unrequested_model_ivdw(self):
        logs: list[dict] = []
        recipe = {"incar": {"overrides": {"IVDW": 12}}}
        service._strip_unrequested_ivdw(recipe, "扩成 2x2x1", logs)
        self.assertNotIn("IVDW", recipe["incar"]["overrides"])
        self.assertTrue(any("IVDW" in x["msg"] for x in logs))

        keep = {"incar": {"overrides": {"IVDW": 12}}}
        service._strip_unrequested_ivdw(keep, "加 D3 后扩胞", logs)
        self.assertIn("IVDW", keep["incar"]["overrides"])


class TestPreflight(unittest.TestCase):
    """写入前自检：能写就过，不能写要把系统原因翻译成人话。"""

    def test_ok_in_workspace(self):
        root = Path(__file__).resolve().parent.parent
        target = root / "tests" / "_tmp" / f"pf_{uuid.uuid4().hex[:8]}" / "out"
        service._preflight_out(target)
        self.assertTrue(target.parent.is_dir())
        shutil.rmtree(target.parent, ignore_errors=True)

    def test_bad_parent_gives_readable_error(self):
        root = Path(__file__).resolve().parent.parent
        base = root / "tests" / "_tmp" / f"pf_{uuid.uuid4().hex[:8]}"
        base.mkdir(parents=True)
        fake_file = base / "not_a_dir"
        fake_file.write_text("x", encoding="utf-8")
        try:
            with self.assertRaises(PrepError) as ctx:
                service._preflight_out(fake_file / "out")
            msg = str(ctx.exception)
            self.assertIn("无法", msg)
            self.assertIn("pid=", msg)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestDefaultOutput(unittest.TestCase):
    """输出目录留空时，一定写到程序自己的 runs 下（那里有写权限）。"""

    def test_empty_output_goes_to_repo_runs(self):
        from vasp_core.config import ROOT

        cfg = config_mod.load()
        cfg.llm.enabled = False
        root = Path(__file__).resolve().parent.parent
        src = root / "tests" / "_tmp" / f"defsrc_{uuid.uuid4().hex[:8]}"
        src.mkdir(parents=True)
        (src / "POSCAR").write_text(
            "Cu\n1.0\n3.61 0 0\n0 3.61 0\n0 0 3.61\nCu\n1\nDirect\n0 0 0\n",
            encoding="utf-8",
        )
        try:
            job = service.preview(str(src), "", "扩成 2x2x1", cfg)
            self.assertEqual(Path(job.output).parent, ROOT / "runs")
            self.assertTrue(Path(job.output).name.endswith("_out"))
            job2 = service.write(job.id)
            self.assertTrue(Path(job2.written_to).joinpath("POSCAR").is_file())
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(ROOT / "runs" / f"{src.name}_out", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
