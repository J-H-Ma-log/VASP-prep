"""ASE 后端：吸附质、按层固定、机器学习势/EMT 预弛豫。

【为什么吸附用 ASE 而不是 pymatgen】
    pymatgen 也能做吸附，但要自己算"顶位/桥位/空位"的坐标；ASE 的
    add_adsorbate(slab, "N", height=2.0, position="ontop") 直接支持
    ontop / bridge / fcc / hcp / hollow 这些位点语义，出错概率低得多。

【为什么弛豫用 ASE 的 BFGS 而不是 CHGNet 自带的 StructOptimizer】
    StructOptimizer 用 FIRE，会把每一步日志刷到 stdout，而且返回的 dict 里
    收敛信息不完整；ASE 的 BFGS + FrechetCellFilter 是同一套物理，
    但 logfile=None 就能安静跑，opt.converged() 直接告诉你收没收敛。

【这一层的输出永远是 our Structure】
    上层（prep/report）只认一种结构类型；后端转换在边界处一次完成，
    避免"有的 op 返回 pymatgen Structure、有的返回我们的"这种混乱。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..backends import require_ase
from ..errors import StructureError
from ..poscar import Structure
from .pmg_backend import _structure_from_atoms  # 纯几何工具，不触发 pymatgen 导入

# 六自由度顺序：a b c α β γ。1 表示"跟着变"，0 表示"固定"。
DEFAULT_CELL_MASK = (1, 1, 1, 0, 0, 0)


def to_atoms(st: Structure):
    """our Structure → ase.Atoms（笛卡尔坐标 + 晶胞 + 三维周期）。

    【坑】ase.Atoms 的 cell 是行向量、positions 是笛卡尔，和我们的约定一致；
        但 ase 默认 pbc=(False,False,False)，不设 True 的话弛豫时晶胞不周期，
        表面会塌缩，能量完全不对。
    """
    require_ase("转换到 ase.Atoms")
    from ase import Atoms

    symbols = st.atom_species()
    return Atoms(symbols=symbols, positions=st.cart(), cell=st.lattice, pbc=True)


def from_atoms(atoms, *, comment: str = "from ase") -> Structure:
    """ase.Atoms → our Structure，并把 ASE 的约束翻译成 POSCAR 的 T/F。

    【为什么要翻译约束】ASE 的 FixAtoms 只是一个 Python 对象，写 POSCAR 时没人
        会替你写 Selective dynamics；不翻译的话"固定底层"在 VASP 里等于没固定。
    【怎么翻】遍历 atoms.constraints，收集 FixAtoms 的 index；
        被固定的原子写 F F F，其余写 T T T，并打开 selective。
    """
    require_ase("从 ase.Atoms 转换")
    fixed: set[int] = set()
    try:
        from ase.constraints import FixAtoms

        for con in atoms.constraints:
            if isinstance(con, FixAtoms):
                idx = getattr(con, "index", None)
                if idx is None:
                    mask = getattr(con, "mask", None)
                    if mask is not None:
                        idx = [i for i, m in enumerate(mask) if m]
                if idx is not None:
                    fixed.update(int(i) for i in idx)
    except Exception:  # noqa: BLE001 - 约束类型不认识就当作没有约束
        fixed = set()

    flags = None
    if fixed:
        flags = [([False, False, False] if i in fixed else [True, True, True])
                 for i in range(len(atoms))]
    return _structure_from_atoms(
        list(atoms.get_chemical_symbols()),
        [list(map(float, p)) for p in atoms.get_positions()],
        [list(map(float, row)) for row in atoms.get_cell()],
        comment=comment,
        flags=flags,
    )


def add_adsorbate(st: Structure, species: str, *, site: str | None = "ontop",
                  position: Sequence[float] | None = None,
                  height: float = 2.0) -> tuple[Structure, str]:
    """在表面上加一个吸附质。

    【参数语义】
        site：ase 的位点名（ontop/bridge/fcc/hcp/hollow），只对常见表面有意义；
        position：直接给面上分数坐标 (x, y)，优先于 site——用户说"在 (0.5,0.5) 加 O"
                  时用这个；
        height：吸附质到表面最高原子的垂直距离（Å）。
    【为什么 height 默认 2.0】过渡金属表面吸附小分子通常在 1.8–2.2 Å；
        给 2.0 只是初值，后面要用 DFT/ML 弛豫。
    【不这样做】直接按笛卡尔坐标猜一个位置，很容易嵌进表面里（距离 <1 Å），
        VASP 直接报 "two atoms are too close" 或算出荒谬能量。
    """
    require_ase("加吸附质（adsorbate）")
    from ase.build import add_adsorbate as _ase_add

    atoms = to_atoms(st)
    pos: Any = position if position is not None else (site or "ontop")
    if isinstance(pos, (list, tuple)) and len(pos) == 2:
        # ase 的 (x, y) 是"面内分数坐标"；这里保持用户直觉，原样传。
        pos = (float(pos[0]), float(pos[1]))
    _ase_add(atoms, species, height=float(height), position=pos)
    out = from_atoms(atoms, comment=f"adsorbate {species} @ {pos} h={height}")
    # 吸附后元素顺序会变（新元素插在末尾），from_atoms 内部的 _regroup 已处理。
    msg = (f"在表面加 {species}：位点 {pos}，高度 {height} Å，"
           f"{st.natoms} → {out.natoms} 原子")
    return out, msg


def relax(st: Structure, *, calculator: str = "emt", fmax: float = 0.05,
          steps: int = 100, cell_mask: Sequence[int] = DEFAULT_CELL_MASK,
          logfile: str | None = None,
          device: str = "cpu") -> tuple[Structure, dict[str, Any]]:
    """用 ASE 做几何优化（可选变胞），返回新结构与收敛信息。

    【calculator 的选择】
        emt：ASE 自带、极快、只支持少数金属（Cu/Ag/Au/Ni/Pd/Pt/Al…），
             用来验证流程/做冒烟测试，**不能用于科研结论**；
        chgnet：通用机器学习势，元素覆盖广，精度接近 PBE，适合预弛豫；
             仍需 VASP 复核，因为 ML 势对磁性/强关联体系不可靠。
    【为什么默认只用 cell_mask 变 a/b/c，不变角度】
        变角度（α/β/γ）在表面/低维体系里容易把晶胞拧成畸形；
        体积优化用 FrechetCellFilter 的 1 1 1 0 0 0 就够，也更稳。
    【不这样做】不弛豫直接算静态能量，等于在算一个手搭的、可能不合理初始结构，
        能量比真实基态高几百 meV，后续所有结论都偏。
    """
    require_ase("弛豫（relax）")
    from ase.filters import FrechetCellFilter
    from ase.optimize import BFGS

    atoms = to_atoms(st)
    if calculator == "emt":
        from ase.calculators.emt import EMT

        atoms.calc = EMT()
    elif calculator == "chgnet":
        try:
            from chgnet.model import CHGNet
            from chgnet.model.dynamics import CHGNetCalculator
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise StructureError(
                "要用 chgnet 预弛豫，需要 pip install chgnet（以及 torch）。\n"
                f"原始错误：{exc}"
            ) from exc
        atoms.calc = CHGNetCalculator(model=CHGNet.load(), use_device=device)
    else:
        raise StructureError(f"未知的 calculator：{calculator}（支持 emt / chgnet）")

    if any(int(x) for x in cell_mask):
        target = FrechetCellFilter(atoms, mask=[int(x) for x in cell_mask])
    else:
        target = atoms
    opt = BFGS(target, logfile=logfile)
    opt.run(fmax=float(fmax), steps=int(steps))
    converged = bool(opt.converged())

    forces = atoms.get_forces()
    fmax_actual = max((math.sqrt(sum(x * x for x in f)) for f in forces), default=0.0)
    energy = float(atoms.get_potential_energy())
    out = from_atoms(atoms, comment=f"relaxed by {calculator}")
    info = {
        "calculator": calculator,
        "converged": converged,
        "fmax_target": float(fmax),
        "fmax_actual": round(fmax_actual, 4),
        "n_steps": int(getattr(opt, "get_number_of_steps", lambda: 0)()),
        "energy_eV": round(energy, 6),
        "energy_per_atom_eV": round(energy / max(out.natoms, 1), 6),
        "volume_before": round(st.volume(), 4),
        "volume_after": round(out.volume(), 4),
        "cell_mask": [int(x) for x in cell_mask],
    }
    return out, info
