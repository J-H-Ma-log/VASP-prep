#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_core.py —— 纯标准库 unittest，覆盖"改错会静默算错体系"的那些点。

为什么用 unittest 而不是 pytest：
  集群上不需要额外装任何包（python3 -m unittest 就能跑）。
  测试重点不是覆盖率，而是**VASP 不会报错但结果错**的情形：
    · 元素顺序与 POTCAR 不一致
    · counts 与坐标数量不一致
    · scale 与 Cartesian 坐标的换算
    · Selective dynamics 的 T/F 丢失
    · MAGMOM 写成 Python 列表
    · KSPACING 与 KPOINTS 同时存在

跑法（在 vasp-prep-v2 目录下）：
    python -m unittest discover -s tests -v
"""

import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vasp_core import (  # noqa: E402
    FileConflict,
    InputError,
    PolicyError,
    StructureError,
    build_incar,
    check_order,
    choose,
    kpoints_from_recipe,
    load_recipe,
    read_poscar,
)
from vasp_core.incar import check_consistency, parse_incar  # noqa: E402
from vasp_core.kpoints import Kpoints, check_kpoints, render_kpoints  # noqa: E402
from vasp_core.potcar import Entry, scan_library, spec_text  # noqa: E402
from vasp_core.poscar import (  # noqa: E402
    op_fix_atoms,
    op_reorder_species,
    op_strain,
    op_substitute,
    op_supercell,
    op_vacancy,
    op_vacuum,
    op_wrap,
    write_poscar_text,
)
from vasp_core.recipe import apply_ops  # noqa: E402

POSCAR_CU = """Cu fcc primitive
1.0
  2.5527  0.0000  0.0000
  1.2763  2.2107  0.0000
  1.2763  0.7369  2.0842
  Cu
  1
Direct
  0.0000000000  0.0000000000  0.0000000000
"""

POSCAR_SEL = """Pt slab test
1.0
  2.7750  0.0000  0.0000
  1.3875  2.4035  0.0000
  0.0000  0.0000 20.0000
  Pt
  2
Selective dynamics
Direct
  0.0000000000  0.0000000000  0.1000000000   T T F
  0.3333333333  0.3333333333  0.1500000000   F F T
"""

POSCAR_SCALE = """scale test
2.0
  2.0  0.0  0.0
  0.0  2.0  0.0
  0.0  0.0  2.0
  Cu
  1
Cartesian
  1.0  1.0  1.0
"""


class Base(unittest.TestCase):
    def setUp(self):
        # 不用 tempfile 默认目录：本机 TEMP 在沙箱外，写会 PermissionError；
        # 统一放到仓库内的 tests/_tmp，跑完自动删。
        tmp_root = ROOT / "tests" / "_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        # 不能用 tempfile.mkdtemp：它按 0o700 建目录，本机 Python 3.14 上
        # 建出来的目录连创建者自己都不可写（已知怪癖），改用普通 mkdir。
        self.tmp = tmp_root / f"case_{uuid.uuid4().hex[:10]}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class TestPoscar(Base):
    def test_roundtrip_preserves_numbers(self):
        st = read_poscar(self.write("POSCAR", POSCAR_CU))
        self.assertEqual(st.natoms, 1)
        self.assertAlmostEqual(st.volume(), 11.7617, places=3)
        text = write_poscar_text(st)
        st2 = read_poscar(self.write("POSCAR2", text))
        self.assertEqual(st2.natoms, st.natoms)
        self.assertAlmostEqual(st2.volume(), st.volume(), places=8)
        self.assertEqual(st2.atom_species(), st.atom_species())

    def test_selective_flags_kept(self):
        st = read_poscar(self.write("POSCAR", POSCAR_SEL))
        self.assertTrue(st.selective)
        self.assertEqual(st.flags[0], [True, True, False])
        self.assertEqual(st.flags[1], [False, False, True])
        text = write_poscar_text(st)
        self.assertIn("Selective dynamics", text)
        self.assertIn("T   T   F", text)
        st2 = read_poscar(self.write("P2", text))
        self.assertEqual(st2.flags, st.flags)

    def test_scale_and_cartesian_conversion(self):
        st = read_poscar(self.write("POSCAR", POSCAR_SCALE))
        # 晶格 2*2=4 Å 边长；Cartesian 坐标 1,1,1 → 分数 0.25
        self.assertAlmostEqual(st.lattice[0][0], 4.0)
        self.assertAlmostEqual(st.frac()[0][0], 0.5)
        self.assertAlmostEqual(st.cart()[0][0], 2.0)

    def test_vasp4_rejected(self):
        text = "vasp4\n1.0\n1 0 0\n0 1 0\n0 0 1\n1\nDirect\n0 0 0\n"
        st = read_poscar(self.write("POSCAR", text))
        with self.assertRaises(StructureError):
            st.atom_species()

    def test_counts_mismatch_caught(self):
        bad = POSCAR_CU.replace("  Cu\n  1\n", "  Cu\n  2\n")
        with self.assertRaises(StructureError):
            read_poscar(self.write("POSCAR", bad))


class TestOps(Base):
    def setUp(self):
        super().setUp()
        self.st = read_poscar(self.write("POSCAR", POSCAR_CU))

    def test_supercell(self):
        st2, msg = op_supercell(self.st, [2, 2, 1])
        self.assertEqual(st2.natoms, 4)
        self.assertEqual(st2.counts, [4])
        self.assertAlmostEqual(st2.volume(), self.st.volume() * 4, places=6)
        self.assertIn("1 → 4", msg)

    def test_supercell_with_repeated_species(self):
        """POSCAR 元素顺序写成 Mo S Mo S 时，扩胞不能把元素铺两遍。

        这是真实 MoS2 POSCAR 的写法（同元素在元素行里出现多次）；
        旧实现 `for sym in st.species` 会把 Mo、S 各铺两次，坐标数是 counts 的 2 倍。
        """
        mos2 = (
            "MoS2\n    3.18013\n"
            "     1.0000000000    0.0000000000    0.0000000000\n"
            "     0.5000000000    0.8660254038    0.0000000000\n"
            "     0.0000000000    0.0000000000    7.8099800000\n"
            "   Mo   S    Mo   S\n"
            "     1     2     1     2\n"
            "Selective dynamics\n"
            "Direct\n"
            "  0.333333  0.333333  0.449841  T T T\n"
            "  0.666667  0.666667  0.512585  T T T\n"
            "  0.666667  0.666667  0.387146  T T T\n"
            "  0.666667  0.666667  0.449841  T T T\n"
            "  0.333333  0.333333  0.512585  T T T\n"
            "  0.333333  0.333333  0.387146  T T T\n"
        )
        st = read_poscar(self.write("POSCAR_mos2", mos2))
        self.assertEqual(st.natoms, 6)
        # 读取时就把重复元素合并了：Mo S Mo S (1 2 1 2) → Mo S (2 4)
        self.assertEqual(st.species, ["Mo", "S"])
        self.assertEqual(st.counts, [2, 4])

        st2, msg = op_supercell(st, [2, 2, 1])
        self.assertEqual(st2.natoms, 6 * 4)
        self.assertEqual(st2.counts, [8, 16])
        self.assertEqual(len(st2.frac()), 24)
        self.assertIn("6 → 24", msg)

    def test_substitute_regroups(self):
        st2, _ = op_supercell(self.st, [2, 2, 1])
        st3, msg = op_substitute(st2, 3, "Co")
        self.assertEqual(st3.counts, [3, 1])
        self.assertEqual(st3.atom_species(), ["Cu", "Cu", "Cu", "Co"])
        self.assertIn("重新分组", msg)

    def test_substitute_bad_index(self):
        with self.assertRaises(StructureError):
            op_substitute(self.st, 5, "Co")

    def test_vacancy(self):
        st2, _ = op_supercell(self.st, [2, 2, 1])
        st3, msg = op_vacancy(st2, [1])
        self.assertEqual(st3.natoms, 3)
        self.assertEqual(st3.counts, [3])

    def test_strain_delta_vs_absolute(self):
        d, _ = op_strain(self.st, [[0.02, 0, 0], [0, 0.02, 0], [0, 0, 0.02]], mode="delta")
        a, _ = op_strain(self.st, [[1.02, 0, 0], [0, 1.02, 0], [0, 0, 1.02]], mode="absolute")
        for i in range(3):
            self.assertAlmostEqual(d.lattice[i][i], a.lattice[i][i], places=9)

    def test_strain_volume_cubic(self):
        st2, _ = op_strain(self.st, [[0.10, 0, 0], [0, 0, 0], [0, 0, 0]], mode="delta")
        self.assertAlmostEqual(st2.volume() / self.st.volume(), 1.10, places=6)

    def test_vacuum_gap(self):
        st2, _ = op_vacuum(self.st, axis=2, thickness=15.0, place="bottom")
        # 投影方向上，原子范围与晶格长度之差应约等于厚度
        L = st2.lattice
        import math
        clen = math.sqrt(sum(x * x for x in L[2]))
        n = [x / clen for x in L[2]]
        proj = [sum(c[k] * n[k] for k in range(3)) for c in st2.cart()]
        self.assertAlmostEqual(clen - (max(proj) - min(proj)), 15.0, places=5)

    def test_fix_atoms_sets_selective(self):
        # VASP 语义：T=允许移动，F=固定。所以"固定 z"要写成 F，即 [T, T, F]
        st2, msg = op_fix_atoms(self.st, [1], axes="3")
        self.assertTrue(st2.selective)
        self.assertEqual(st2.flags[0], [True, True, False])
        self.assertIn("固定", msg)

    def test_reorder_species(self):
        st2, _ = op_supercell(self.st, [2, 2, 1])
        st3, _ = op_substitute(st2, 3, "Co")
        st4, _ = op_reorder_species(st3, ["Co", "Cu"])
        self.assertEqual(st4.species, ["Co", "Cu"])
        self.assertEqual(st4.counts, [1, 3])

    def test_wrap(self):
        st2, _ = op_strain(self.st, [[0, 0, 0], [0, 0, 0], [0, 0, 0]], mode="delta")
        st2.coords[0] = [1.2, -0.3, 0.5]
        st3, _ = op_wrap(st2)
        self.assertTrue(all(0 <= x < 1 for x in st3.frac()[0]))


class TestIncar(Base):
    def test_preset_applied(self):
        text, changes, issues, lines = build_incar(
            base_text=None, preset="relax", overrides={"ENCUT": 480}, natoms=4
        )
        self.assertIn("ENCUT = 480", text)
        self.assertIn("ISIF = 3", text)
        keys = {c.key: c for c in changes}
        self.assertIn("ENCUT", keys)

    def test_dangerous_key_rejected(self):
        with self.assertRaises(PolicyError):
            build_incar(base_text=None, preset="relax", overrides={"LDAUU": [4, 0]}, natoms=2)

    def test_unknown_key_rejected(self):
        with self.assertRaises(PolicyError):
            build_incar(base_text=None, preset="relax", overrides={"NOT_A_KEY": 1}, natoms=1)

    def test_type_range_rejected(self):
        with self.assertRaises(PolicyError):
            build_incar(base_text=None, preset="relax", overrides={"ENCUT": 99999}, natoms=1)

    def test_magmom_list_not_python_repr(self):
        text, _, _, _ = build_incar(
            base_text=None, preset="relax",
            overrides={"ISPIN": 2, "MAGMOM": [1.0, 2.0, 3.0]}, natoms=3,
        )
        self.assertIn("MAGMOM = 1 2 3", text)
        self.assertNotIn("[", text.split("MAGMOM")[1].splitlines()[0])

    def test_base_incar_preserved(self):
        base = "SYSTEM = x\n# 注释保留\nENCUT = 400\n"
        text, changes, _, _ = build_incar(
            base_text=base, preset="relax", overrides={"ENCUT": 480}, natoms=1
        )
        self.assertIn("# 注释保留", text)
        self.assertIn("ENCUT = 480", text)
        self.assertNotIn("ENCUT = 400", text)
        # 同一键只应合并成一条 400 → 480
        enc = [c for c in changes if c.key == "ENCUT"]
        self.assertEqual(len(enc), 1)
        self.assertEqual(enc[0].old, "400")

    def test_consistency_ismear5_with_relax(self):
        issues = check_consistency(
            incar={"ISMEAR": "-5", "NSW": "100"}, natoms=1, calc_type="relax"
        )
        self.assertTrue(any(i.level == "ERROR" and i.code == "ismear-tetra-relax" for i in issues))

    def test_consistency_magmom_length(self):
        issues = check_consistency(
            incar={"ISPIN": "2", "MAGMOM": "1 1"}, natoms=3, calc_type="relax"
        )
        self.assertTrue(any(i.level == "ERROR" for i in issues))

    def test_consistency_encut_below_enmax(self):
        issues = check_consistency(
            incar={"ENCUT": "300"}, natoms=1, potcar_enmax=368.0, calc_type="relax"
        )
        self.assertTrue(any(i.code == "encut-below-enmax" for i in issues))


    def test_ivdw_allowed_when_named(self):
        """用户点名色散时 IVDW 能写；且只改这一行。"""
        text, changes, issues, _ = build_incar(
            base_text=None, preset="none", overrides={"IVDW": 12}, natoms=6
        )
        self.assertIn("IVDW = 12", text)
        self.assertEqual([c.key for c in changes], ["IVDW"])
        issues = check_consistency(incar={"IVDW": "12"}, natoms=6)
        self.assertTrue(any(i.code == "ivdw-on" and i.level == "PASS" for i in issues))

    def test_ivdw_bad_value_rejected(self):
        """IVDW=99 这种不存在的方法必须在写盘前就被拒绝（VASP 会直接忽略或报错）。"""
        with self.assertRaises(PolicyError):
            build_incar(base_text=None, preset="none", overrides={"IVDW": 99}, natoms=6)

    def test_ivdw_not_in_presets(self):
        """预设里绝不能自带 IVDW：色散是科学选择，只能由人点名。"""
        text, _, _, _ = build_incar(base_text=None, preset="relax", overrides={}, natoms=1)
        self.assertNotIn("IVDW", text)

    def test_ivdw_external_package_warned(self):
        """13/14/15 依赖 VASP 编译外部包，要给出 WARN 而不是静默通过。"""
        build_incar(base_text=None, preset="none", overrides={"IVDW": 14}, natoms=2)
        issues = check_consistency(incar={"IVDW": "14"}, natoms=2)
        self.assertTrue(any(i.code == "ivdw-external-package" and i.level == "WARN" for i in issues))


class TestKpoints(Base):
    def test_gamma_render(self):
        kp = kpoints_from_recipe({"mode": "gamma", "mesh": [4, 4, 1]})
        text = render_kpoints(kp)
        self.assertIn("Gamma", text)
        self.assertIn("4  4  1", text)

    def test_mp_render(self):
        kp = kpoints_from_recipe({"mode": "mp", "mesh": [8, 8, 8]})
        self.assertIn("Monkhorst-Pack", render_kpoints(kp))

    def test_line_render(self):
        kp = kpoints_from_recipe({
            "mode": "line", "points_per_segment": 30,
            "segments": [
                {"from": [0, 0, 0], "to": [0.5, 0, 0], "label_from": "G", "label_to": "X"},
                {"from": [0.5, 0, 0], "to": [0.5, 0.5, 0], "label_from": "X", "label_to": "M"},
            ],
        })
        text = render_kpoints(kp)
        self.assertIn("Line-mode", text)
        self.assertIn("! G", text)
        self.assertIn("! M", text)

    def test_bad_mesh(self):
        with self.assertRaises(StructureError):
            kpoints_from_recipe({"mode": "gamma", "mesh": [0, 4, 4]})

    def test_mesh_too_large(self):
        with self.assertRaises(StructureError):
            kpoints_from_recipe({"mode": "gamma", "mesh": [80, 80, 1]})

    def test_slab_kz_warning(self):
        kp = kpoints_from_recipe({"mode": "gamma", "mesh": [4, 4, 2]})
        issues = check_kpoints(kp, is_slab=True)
        self.assertTrue(any(i["code"] == "slab-kz" for i in issues))


class TestPotcar(Base):
    def _lib(self):
        d = self.tmp / "pp"
        (d / "Cu").mkdir(parents=True)
        (d / "Co").mkdir(parents=True)
        (d / "Cu" / "POTCAR").write_text(
            "  PAW_PBE Cu_pv 06Sep2000\n   ENMAX  =  368.000; ENMIN  =  276.000 eV\n",
            encoding="utf-8")
        (d / "Co" / "POTCAR").write_text(
            "  TITEL  = PAW_PBE Co 06Sep2000\n   ENMAX  =  267.700; ENMIN  =  200.000 eV\n",
            encoding="utf-8")
        return d

    def test_scan_and_choose(self):
        lib = scan_library(self._lib())
        self.assertEqual(set(lib), {"Cu", "Co"})
        entries = choose(lib, ["Cu", "Co"])
        self.assertEqual([e.title for e in entries], ["Cu_pv", "Co"])
        self.assertEqual(spec_text(entries), "Cu_pv\nCo\n")

    def test_missing_element(self):
        lib = scan_library(self._lib())
        with self.assertRaises(StructureError):
            choose(lib, ["Cu", "S"])

    def test_order_check(self):
        lib = scan_library(self._lib())
        entries = choose(lib, ["Co", "Cu"])
        issues = check_order(entries, ["Cu", "Co"])
        self.assertTrue(any(i["level"] == "ERROR" and i["code"] == "potcar-order" for i in issues))

    def test_map_override(self):
        lib = scan_library(self._lib())
        with self.assertRaises(StructureError):
            choose(lib, ["Cu"], mapping={"Cu": "Cu"})  # 库里没有纯净 Cu


class TestRecipe(Base):
    def _mk(self, obj: dict) -> Path:
        self.write("POSCAR", POSCAR_CU)
        p = self.tmp / "recipe.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8-sig")  # 故意带 BOM
        return p

    def base(self, **kw):
        obj = {
            "version": 2, "name": "t", "calc": "relax", "base_dir": ".",
            "structure": {"file": "POSCAR", "ops": [{"op": "supercell", "scaling": [2, 1, 1]}]},
            "incar": {"preset": "relax"},
            "kpoints": {"mode": "gamma", "mesh": [1, 1, 1]},
        }
        obj.update(kw)
        return obj

    def test_load_with_bom(self):
        r = load_recipe(self._mk(self.base()))
        self.assertEqual(r.name, "t")
        self.assertEqual(len(r.ops), 1)

    def test_unknown_top_key(self):
        with self.assertRaises(InputError):
            load_recipe(self._mk(self.base(bad_key=1)))

    def test_unknown_op(self):
        obj = self.base()
        obj["structure"]["ops"] = [{"op": "teleport"}]
        with self.assertRaises(InputError):
            load_recipe(self._mk(obj))

    def test_op_missing_param(self):
        obj = self.base()
        obj["structure"]["ops"] = [{"op": "substitute", "index": 1}]
        with self.assertRaises(InputError):
            load_recipe(self._mk(obj))

    def test_slab_alias_needs_miller(self):
        # slab 现在是 surface 的别名：缺 miller 时应在"配方校验"阶段就报清楚，
        # 而不是等到执行阶段才报缺 pymatgen（那样用户不知道到底缺哪个参数）。
        obj = self.base()
        obj["structure"]["ops"] = [{"op": "slab"}]
        with self.assertRaises(InputError) as ctx:
            load_recipe(self._mk(obj))
        self.assertIn("miller", str(ctx.exception))

    def test_bad_json_message(self):
        self.write("POSCAR", POSCAR_CU)
        p = self.tmp / "recipe.json"
        p.write_text('{"a": 1,}', encoding="utf-8")
        with self.assertRaises(InputError) as ctx:
            load_recipe(p)
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_apply_ops_chain(self):
        r = load_recipe(self._mk(self.base()))
        st = read_poscar(r.structure_file)
        st2, log = apply_ops(st, r.ops)
        self.assertEqual(st2.natoms, 2)
        self.assertEqual(len(log), 1)


class TestCli(Base):
    def _fixture(self):
        self.write("POSCAR", POSCAR_CU)
        self.write("INCAR", "ENCUT = 400\nEDIFF = 1e-5\n")
        (self.tmp / "pp" / "Cu").mkdir(parents=True)
        (self.tmp / "pp" / "Cu" / "POTCAR").write_text(
            "  PAW_PBE Cu_pv 06Sep2000\n   ENMAX  =  368.000\n", encoding="utf-8")
        recipe = {
            "version": 2, "name": "cu", "calc": "relax", "base_dir": ".",
            "structure": {"file": "POSCAR", "ops": [{"op": "supercell", "scaling": [2, 2, 2]}]},
            "incar": {"preset": "static", "overrides": {"ENCUT": 520}},
            "kpoints": {"mode": "gamma", "mesh": [4, 4, 4]},
            "potcar": {"dir": "pp", "mode": "auto"},
        }
        return self.write("recipe.json", json.dumps(recipe))

    def _run(self, *args):
        cmd = [sys.executable, "-m", "vasp_core.prep", *args]
        return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")

    def test_dry_run_writes_nothing(self):
        r = self._fixture()
        out = self.tmp / "run"
        proc = self._run(str(r), "--out", str(out), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["dry_run"])
        self.assertFalse(out.exists())

    def test_write_then_conflict(self):
        r = self._fixture()
        out = self.tmp / "run"
        proc = self._run(str(r), "--write", "--out", str(out), "--audit", str(self.tmp / "a.jsonl"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "change_report.md", "INCAR.diff"):
            self.assertTrue((out / name).exists(), name)
        proc2 = self._run(str(r), "--write", "--out", str(out))
        self.assertEqual(proc2.returncode, 5, proc2.stderr)

    def test_validation_error_blocks_write(self):
        r = self._fixture()
        data = json.loads(r.read_text(encoding="utf-8-sig"))
        data["incar"]["overrides"] = {"ISMEAR": -5, "NSW": 100}  # 静态 ISMEAR 配弛豫
        r.write_text(json.dumps(data), encoding="utf-8")
        out = self.tmp / "run"
        proc = self._run(str(r), "--write", "--out", str(out))
        self.assertEqual(proc.returncode, 6, proc.stderr)
        self.assertFalse(out.exists())


class TestRealWorldRegressions(Base):
    """用真实 POSCAR 踩出来的坑，逐条钉死，防止回退。"""

    def setUp(self):
        super().setUp()
        self.st = read_poscar(self.write("POSCAR_cu", POSCAR_CU))

    def test_vacuum_does_not_shrink_existing_gap(self):
        """已有真空比要求更厚时，必须原样保留，不能把 slab 挤小。"""
        big, _ = op_vacuum(self.st, axis=2, thickness=20.0)
        c_big = big.lattice[2][2]
        same, msg = op_vacuum(big, axis=2, thickness=5.0)
        self.assertAlmostEqual(same.lattice[2][2], c_big, places=9)
        self.assertIn("保持不变", msg)

    def test_velocities_roundtrip_separated_by_blank_line(self):
        """速度块：写盘时前面必须留空行，读回要能逐原子还原。

        ASE 的 read_vasp 会先读一行判断 "Lattice velocities"：
        没有空行时第 1 个原子速度被当判断行吞掉，随后 EOF → assert 失败。
        """
        st = self.st
        st.velocities = [[0.01, -0.02, 0.03]]
        text = write_poscar_text(st)
        lines = text.splitlines()
        # 元素计数行 + 坐标行之后应是一个空行，再是 natoms 行速度
        coord_start = next(i for i, ln in enumerate(lines) if ln.strip().lower().startswith(("direct", "cartesian")))
        vel_start = coord_start + 1 + st.natoms
        self.assertEqual(lines[vel_start].strip(), "", "速度块前必须有一个空行")
        self.assertEqual(len(lines[vel_start + 1: vel_start + 1 + st.natoms]), st.natoms)

        p = self.write("POSCAR_vel", text)
        back = read_poscar(p)
        self.assertIsNotNone(back.velocities)
        self.assertEqual(len(back.velocities), 1)
        for got, want in zip(back.velocities[0], [0.01, -0.02, 0.03]):
            self.assertAlmostEqual(got, want, places=6)

    def test_axes_legacy_111_means_all_three(self):
        """旧写法 axes="111" 曾被理解为"只固定 x 三遍"，现在等价于 "123"。"""
        st2, _ = op_fix_atoms(self.st, [1], axes="111")
        self.assertEqual(st2.flags[0], [False, False, False])
        st3, _ = op_fix_atoms(self.st, [1], axes="123")
        self.assertEqual(st2.flags[0], st3.flags[0])

    def test_incar_no_changes_means_empty_diff(self):
        """没有任何 INCAR 改动时，变更列表必须为空（prep 据此原样复制底稿，避免假 diff）。"""
        base = "# 组里模板\n  ENCUT = 400\n  ISMEAR = 0\n"
        text, changes, issues, _ = build_incar(
            base_text=base, preset=None, overrides={}, natoms=1
        )
        self.assertEqual(changes, [])
        # 键值不能被改：重新排版可以有缩进差异，但语义必须一致
        before = {ln.key: ln.value for ln in parse_incar(base)}
        after = {ln.key: ln.value for ln in parse_incar(text)}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
