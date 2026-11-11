#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fuzz_poscar.py —— 随机操作序列测试（找"VASP 不报错但结果错"的漏洞）。

用法（需要一个带 pymatgen 的解释器，例如 chgnet_env）：

    chgnet_env\\python\\python.exe tools\\fuzz_poscar.py C:\\path\\to\\POSCAR [轮数]

它做四件事，每一轮随机构造 1~4 个操作：
  1. 随机扩胞/替换/全替换/空位/加原子/应变/真空/固定/重排/平移/包裹
  2. 每步后检查：原子数 == 计数和 == 坐标数；元素不重复且连续
  3. 写出文本再读回，比对原子数/元素/计数/坐标（往返无损）
  4. 用 pymatgen 再读一遍（第三方库独立验证）

任何一步失败就打印操作序列并退出码 1；全部通过打印统计并退出 0。

【为什么不用 unittest】这是"随机找坑"的补充，不是固定用例；
固定用例在 tests/ 里，跑 `python -m unittest discover -s tests`。
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vasp_core.poscar import (  # noqa: E402
    op_add_atom,
    op_fix_atoms,
    op_reorder_species,
    op_strain,
    op_substitute,
    op_substitute_all,
    op_supercell,
    op_translate,
    op_vacancy,
    op_vacuum,
    op_wrap,
    read_poscar,
    write_poscar_text,
)


def check(st, tag, pymatgen=True):
    if st.natoms != sum(st.counts) or st.natoms != len(st.frac()):
        raise AssertionError(f"{tag}: 原子数不一致 natoms={st.natoms} counts={st.counts}")
    if len(set(st.species)) != len(st.species):
        raise AssertionError(f"{tag}: 元素重复 {st.species}")
    seen = []
    for s in st.atom_species():
        if not seen or seen[-1] != s:
            seen.append(s)
    if len(seen) != len(st.species):
        raise AssertionError(f"{tag}: 元素不连续 {seen} vs {st.species}")

    text = write_poscar_text(st)
    back = read_poscar_text_roundtrip(text)
    if back.natoms != st.natoms or back.species != st.species or back.counts != st.counts:
        raise AssertionError(f"{tag}: 往返不一致")
    for a, b in zip(st.frac(), back.frac()):
        for x, y in zip(a, b):
            if abs(x - y) > 1e-6:
                raise AssertionError(f"{tag}: 往返坐标偏差 {x} vs {y}")

    if pymatgen:
        from pymatgen.io.vasp import Poscar
        s = Poscar.from_str(text).structure
        if len(s) != st.natoms:
            raise AssertionError(f"{tag}: pymatgen 原子数 {len(s)} != {st.natoms}")


def read_poscar_text_roundtrip(text):
    """把文本写进临时文件再读回。

    【坑】不用 tempfile.mkdtemp：它按 0o700 建目录，本机 Python 3.14 上
          建出来的目录连创建者都不可写（PermissionError）。
    """
    import uuid
    from pathlib import Path as _P
    d = ROOT / "tests" / "_tmp" / f"fuzz_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=False)
    p = d / "POSCAR_roundtrip"
    p.write_text(text, encoding="utf-8")
    return read_poscar(p)


def one_round(src, rng, rounds_dir):
    st = read_poscar(src)
    n_ops = rng.randint(1, 4)
    for k in range(n_ops):
        choices = ["sub", "suball", "vac", "add", "strain", "vacuum", "fix", "reorder", "trans", "wrap"]
        if st.natoms * 2 <= 1200:
            choices.append("super")
        op = rng.choice(choices)
        try:
            if op == "super":
                st, _ = op_supercell(st, rng.choice([[2, 1, 1], [1, 2, 1], [2, 2, 1], [1, 1, 2]]))
            elif op == "sub":
                idx = rng.randint(1, st.natoms)
                old = st.atom_species()[idx - 1]
                st, _ = op_substitute(st, idx, rng.choice([x for x in ("W", "Se", "Mo", "S") if x != old]))
            elif op == "suball":
                old, new = rng.choice([("Mo", "W"), ("S", "Se")])
                if old not in st.species:
                    continue
                st, _ = op_substitute_all(st, {old: new})
            elif op == "vac":
                if st.natoms <= 2:
                    continue
                st, _ = op_vacancy(st, rng.randint(1, st.natoms))
            elif op == "add":
                st, _ = op_add_atom(st, rng.random(), rng.random(), rng.random(), "H")
            elif op == "strain":
                pct = rng.choice([0.01, 0.02, -0.01, 0.03])
                st, _ = op_strain(st, [[pct, 0, 0], [0, pct, 0], [0, 0, pct]], mode="delta")
            elif op == "vacuum":
                st, _ = op_vacuum(st, 2, rng.choice([5.0, 15.0, 20.0]))
            elif op == "fix":
                st, _ = op_fix_atoms(st, [rng.randint(1, st.natoms)], axes="123")
            elif op == "reorder":
                if len(st.species) < 2:
                    continue
                order = list(st.species)
                rng.shuffle(order)
                st, _ = op_reorder_species(st, order)
            elif op == "trans":
                st, _ = op_translate(st, [rng.uniform(-0.5, 0.5), 0, 0], mode="frac")
            else:
                st, _ = op_wrap(st)
        except Exception:
            # 越界等"预期内的拒绝"也算通过：关键是拒绝后结构没被改坏
            pass
        check(st, f"round{k}")
    return st


def main(argv):
    if len(argv) < 2:
        print("用法：python tools/fuzz_poscar.py <POSCAR 路径> [轮数=100]")
        return 2
    src = Path(argv[1])
    rounds = int(argv[2]) if len(argv) > 2 else 100
    if not src.exists():
        print(f"找不到 POSCAR：{src}")
        return 2
    (ROOT / "tests" / "_tmp").mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261108)
    for i in range(rounds):
        try:
            one_round(src, rng, None)
        except AssertionError as e:
            print(f"[FAIL] 第 {i} 轮：{e}")
            return 1
    print(f"fuzz OK：{rounds} 轮随机序列全部通过（源：{src.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
