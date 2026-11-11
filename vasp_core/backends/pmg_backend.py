"""pymatgen 后端：任意 (hkl) 表面、对称性、能带路径、vasprun 解析、MP 取结构。

【这一层负责什么】
    vasp_core 自己的 Structure 只认 POSCAR 文本；pymatgen 的 Structure 会做
    规范化（把坐标 wrap 回晶胞、识别等价位置、支持 SlabGenerator）。
    所以这里做两件事：
      1. 两个方向的结构转换：our Structure ↔ pymatgen Structure；
      2. 用 pymatgen 的成熟算法实现我们不想重写的高阶操作。

【为什么不直接全程用 pymatgen】
    ① 集群上常常没有（Python 3.10 的老环境、无外网）；
    ② pymatgen 的 POSCAR 写出会把 scale 归一、丢掉原有的 Selective 标记与注释，
       而"前处理"最要紧的恰恰是**别动用户原文件里不懂的东西**。
    所以只在"确实需要它的算法"时调用，调用完立刻转回我们自己的无损结构。

【转换为什么走文本，而不是逐字段翻译】
    POSCAR 文本是两边都能严格解析的共同语言，而且能把 Selective dynamics、
    速度块、坐标模式一起带过去；逐字段翻译要处理 lattice/scale/coord 三者的
    耦合，写错一处就静默算错体系。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..backends import require_pymatgen
from ..errors import StructureError
from ..poscar import Structure, _regroup, op_fix_atoms, read_poscar_text, write_poscar_text


# ---------------------------------------------------------------- 结构转换


def to_pymatgen(st: Structure):
    """our Structure → pymatgen Structure。

    【B 案】把 POSCAR 文本交给 Poscar.from_str，让 pymatgen 自己解析。
    【不这样做的后果】若手工拼 pymatgen 的 Lattice/coords，遇到负 scale、
        Cartesian 坐标、速度块时很容易把单位搞错。
    """
    require_pymatgen("转换到 pymatgen Structure")
    from pymatgen.io.vasp import Poscar

    text = write_poscar_text(st)
    return Poscar.from_str(text).structure


def from_pymatgen(pmg_structure: Any, *, comment: str = "", keep_flags: bool = True) -> Structure:
    """pymatgen Structure → our Structure，保留选择性标记。

    【为什么要保留 flags】pymatgen 的 Poscar.get_str() 会写出 Selective dynamics
        与 T/F；如果丢掉，用户原本固定的底层原子会被放开，弛豫结果就变了。
    """
    require_pymatgen("从 pymatgen Structure 转换")
    from pymatgen.io.vasp import Poscar

    text = Poscar(pmg_structure, comment=comment or "from pymatgen").get_str()
    return read_poscar_text(text, source="<pymatgen>")


def _structure_from_atoms(symbols: list[str], cart: list[list[float]],
                          lattice: list[list[float]], *, comment: str,
                          flags: list[list[bool]] | None = None) -> Structure:
    """从"逐原子元素 + 笛卡尔坐标 + 晶格"拼出 our Structure（内部工具）。

    【为什么要 _regroup】VASP 要求同元素原子连续，而 pymatgen/ase 交回来的
        原子顺序不保证连续；不重排的话 VASP 会把后面的原子当成前一种元素。
    """
    fracs = []
    inv = _inv3(lattice)
    for c in cart:
        fracs.append([sum(c[k] * inv[k][j] for k in range(3)) for j in range(3)])
    base = Structure(
        comment=comment,
        scale_raw=1.0,
        lattice_raw=[list(r) for r in lattice],
        species=None,
        counts=[],
        coords=[],
        coord_mode="direct",
        selective=flags is not None,
        flags=None,
        velocities=None,
        tail=[],
    )
    return _regroup(base, list(symbols), fracs, flags, None)


def _inv3(m: list[list[float]]) -> list[list[float]]:
    """3x3 求逆（行向量约定）。只给本模块内部转换用，独立实现避免循环 import。"""
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise StructureError("晶格矩阵行列式为 0，无法求逆")
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


# ---------------------------------------------------------------- 表面


def _d_spacing(lattice: list[list[float]], miller: tuple[int, int, int]) -> float:
    """给定晶格与 Miller 指数，算面间距 d_hkl（Å）。

    【为什么要 d】
        pymatgen 的 SlabGenerator 用"最小板厚（Å）"而不是"层数"作参数；
        用户说"4 层 (111)"，我们得先知道一层多厚，才能折算成 Å。
    【公式】倒格矢 g = h·b1 + k·b2 + l·b3，d = 1/|g|，其中 b_j 是 inv(A) 的列。
    """
    h, k, l = miller
    inv = _inv3(lattice)
    g = [sum(coef * inv[i][j] for i, coef in enumerate((h, k, l))) for j in range(3)]
    norm = math.sqrt(sum(x * x for x in g))
    if norm < 1e-12:
        raise StructureError(f"Miller 指数 {miller} 非法（倒格矢长度为 0）")
    return 1.0 / norm


def slab(st: Structure, miller: tuple[int, int, int], *,
         layers: int = 4, vacuum: float = 15.0, min_slab_size: float | None = None,
         center: bool = True, fix_layers: int = 0) -> tuple[Structure, dict[str, Any]]:
    """用 pymatgen 的 SlabGenerator 切任意 (hkl) 表面。

    【为什么用 pymatgen 而不是 ase.build.fcc111】
        ase 的 fcc111/bcc110 只对"已命名"的简单体系可靠；用户说 Pt(111) 可以，
        但 (211)、(10-10) 或六方晶系就没现成函数。pymatgen 的 SlabGenerator
        对任意 hkl 做平移枚举 + 正交化，是通用解。
    【参数的语义】
        layers：希望保留多少层（我们折算成 Å 传给 min_slab_size）；
        vacuum：真空层厚度（Å）——必须显式给，太薄会周期性粘在一起；
        min_slab_size：直接按 Å 指定（优先于 layers）；
        center：把板放到晶胞中间（两边真空对称，避免偶极）；
        fix_layers：底部固定几层（写 Selective dynamics 的 F F F）。
    【不这样做会怎样】
        · 真空 < 10 Å：上下表面互相作用，表面能算出来偏高且不稳定；
        · 不 center：板贴着一侧，VASP 周期性下真空实际只有一半；
        · 不固定底层：体相部分也会被弛豫，等于在算一个"纳米板"而不是表面。
    """
    require_pymatgen("切表面（surface）")
    from pymatgen.core.surface import SlabGenerator

    pmg = to_pymatgen(st)
    d = _d_spacing(st.lattice, miller)
    if min_slab_size is None:
        # 层数 → 厚度：留一点余量，避免浮点误差少切一层。
        min_slab_size = max(layers * d, d * 1.01)
    gen = SlabGenerator(
        pmg,
        miller_index=miller,
        min_slab_size=float(min_slab_size),
        min_vacuum_size=float(vacuum),
        center_slab=center,
        lll_reduce=True,
        primitive=True,
    )
    slabs = gen.get_slabs()
    if not slabs:
        raise StructureError(f"无法生成 {miller} 表面（可能该方向没有可行的终止面）")
    # 取第一个终止面：对金属/简单氧化物通常就是最稳定那个；
    # 若要枚举所有终止面，交给调用方再筛（这里不猜）。
    slab_struct = from_pymatgen(slabs[0], comment=f"slab {miller} {layers}L vac={vacuum}")

    info: dict[str, Any] = {
        "miller": list(miller),
        "layers_requested": layers,
        "d_spacing": round(d, 4),
        "min_slab_size": round(float(min_slab_size), 4),
        "vacuum_requested": float(vacuum),
        "n_terminations": len(slabs),
        "natoms": slab_struct.natoms,
    }

    if fix_layers > 0:
        slab_struct, fix_msg = op_fix_layers(slab_struct, n_layers=int(fix_layers))
        info["fix_layers"] = int(fix_layers)
        info["fix_message"] = fix_msg

    info["vacuum_actual"] = round(_vacuum_gap(slab_struct), 4)
    return slab_struct, info


def _vacuum_gap(st: Structure) -> float:
    """算 c 方向实际真空（晶胞高度 − 原子 z 跨度）。

    【为什么要实际值】用户给 15 Å，但 SlabGenerator 可能加到 15.2；
        真正决定物理的是"原子之间隔多远空"，要如实报告。
    """
    zs = [c[2] for c in st.cart()]
    if not zs:
        return 0.0
    return max(st.lattice[2][2], 0.0) - (max(zs) - min(zs))


def op_fix_layers(st: Structure, *, n_layers: int = 2, tol: float = 0.1,
                  axes: str = "123") -> tuple[Structure, str]:
    """固定最底下 n_layers 层原子（按 z 去重分层）。

    【为什么要按"层"而不是按"索引"】POSCAR 里的原子顺序不等于层序；
        用户说"固定下面两层"，只能按 z 坐标聚类来判断。
    【为什么不直接用 ASE 的 FixAtoms】ASE 约束活在 ase.Atoms 上；
        写回 POSCAR 时只有 Selective dynamics 能表达它。这里直接改 flags，
        不引入 ase 依赖，纯几何操作在核心层完成。
    """
    if n_layers <= 0:
        raise StructureError("n_layers 必须 ≥ 1")
    zs = [c[2] for c in st.cart()]
    uniq: list[float] = []
    for z in sorted(zs):
        if not uniq or abs(z - uniq[-1]) > tol:
            uniq.append(z)
    if n_layers > len(uniq):
        n_layers = len(uniq)
    cutoff = uniq[n_layers - 1] + tol
    idxs = [i + 1 for i, z in enumerate(zs) if z <= cutoff]  # 1 基，与 POSCAR 行号一致
    st2, msg = op_fix_atoms(st, idxs, axes=axes)
    return st2, f"固定底部 {n_layers} 层（z ≤ {cutoff:.3f} Å），共 {len(idxs)} 个原子：{msg}"


# ---------------------------------------------------------------- 对称性 / 能带


def symmetry(st: Structure) -> dict[str, Any]:
    """用 spglib（经 pymatgen）算空间群、点群、晶系。

    【为什么值得单独一层】空间群决定了很多后续选择：
        立方 Fm-3m 的 k 点可以很稀；低对称 P1 往往要加密；
        判断"这是不是原胞"也靠它（惯用胞 vs 原胞）。
    【不这样做】用户可以自己查，但容易看错；而且写 INCAR/KPOINTS 时
        用错对称性假设会静默给出偏高的能量。
    """
    require_pymatgen("对称性分析（symmetry）")
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    pmg = to_pymatgen(st)
    sga = SpacegroupAnalyzer(pmg, symprec=1e-3)
    out: dict[str, Any] = {
        "space_group_symbol": sga.get_space_group_symbol(),
        "space_group_number": sga.get_space_group_number(),
        "point_group": sga.get_point_group_symbol(),
        "crystal_system": str(sga.get_crystal_system()),
        "international": sga.get_space_group_symbol(),
    }
    try:
        out["lattice_type"] = str(sga.get_lattice_type())
        out["n_symmetry_ops"] = len(sga.get_symmetry_operations())
        out["is_primitive"] = bool(sga.is_primitive())
        conventional = sga.get_conventional_standard_structure()
        out["conventional_atoms"] = len(conventional)
    except Exception as exc:  # noqa: BLE001 - 对称性库对畸形输入会抛各种异常
        out["warning"] = f"部分对称性信息取不到：{exc}"
    return out


def kpath(st: Structure, *, line_density: int = 20) -> dict[str, Any]:
    """生成能带计算的 KPOINTS（line mode）与高对称点标签。

    【为什么不用 KSPACING】能带要沿特定路径取点，自动网格做不出来。
    【为什么能带必须用 pymatgen 的 HSE06 路径】不同晶系的高对称点名字/位置不同，
        自己写易错；HighSymmKpath 内置 Setyawan-Curtarolo 与 Latimer-Munro 两套约定。
    """
    require_pymatgen("能带路径（kpath）")
    from pymatgen.symmetry.bandstructure import HighSymmKpath

    pmg = to_pymatgen(st)
    hk = HighSymmKpath(pmg, path_type="setyawan_curtarolo")
    literal = hk.kpath
    labels: dict[str, list[float]] = {}
    for name, frac in literal["kpoints"].items():
        labels[name] = [round(float(x), 6) for x in frac]
    segments: list[tuple[str, str]] = []
    for seg in literal["path"]:
        if len(seg) != 2:
            continue
        a, b = seg
        if a in labels and b in labels:
            segments.append((a, b))
    lines: list[str] = []
    for a, b in segments:
        lines.append(f"{a} → {b}")
    return {
        "path_type": "setyawan_curtarolo",
        "labels": labels,
        "segments": segments,
        "n_segments": len(segments),
        "line_density": line_density,
        "summary": "; ".join(lines) if lines else "(未找到路径)",
    }


def kpoints_line_text(st: Structure, *, line_density: int = 20) -> str:
    """把 kpath 渲染成 VASP 的 line-mode KPOINTS 文本。

    【格式要点】第一行注释，第二行 0，第三行 "Line-mode"，第四行 reciprocal，
        之后每两行是"点坐标 + 标签"。点数由 VASP 按 line_density 自动插值。
    """
    info = kpath(st, line_density=line_density)
    out = [
        "K-Path generated by vasp-prep (pymatgen HighSymmKpath)",
        "0",
        "Line-mode",
        "reciprocal",
    ]
    for a, b in info["segments"]:
        out.append(" ".join(f"{x:.8f}" for x in info["labels"][a]))
        out.append(f"! {a}")
        out.append(" ".join(f"{x:.8f}" for x in info["labels"][b]))
        out.append(f"! {b}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- vasprun / MP


def read_vasprun(path: str | Path) -> dict[str, Any]:
    """读 vasprun.xml：收敛性、总能、离子步、末态结构与最大残余力。

    【为什么 parse_potcar_file=False】默认会去找 POTCAR 并解析，集群上常缺
        赝势文件，于是读结果时抛异常——但我们要的只是能量/力，跟 POTCAR 无关。
    【converged 的语义】pymatgen 的 converged 是"电子步 + 离子步都收敛"；
        只看进程是否正常退出是不够的（OUTCAR 正常结束但 ionic 没收敛很常见）。
    """
    require_pymatgen("读取 vasprun.xml（verify）")
    from pymatgen.io.vasp.outputs import Vasprun

    p = Path(path)
    if not p.exists():
        raise StructureError(f"找不到 vasprun.xml：{p}")
    vr = Vasprun(str(p), parse_potcar_file=False, exception_on_bad_xml=False)
    info: dict[str, Any] = {
        "converged": bool(getattr(vr, "converged", False)),
        "converged_electronic": bool(getattr(vr, "converged_electronic", False)),
        "converged_ionic": bool(getattr(vr, "converged_ionic", False)),
        "final_energy": float(vr.final_energy) if vr.final_energy is not None else None,
        "n_ionic_steps": len(getattr(vr, "ionic_steps", []) or []),
        "is_spin": bool(getattr(vr, "is_spin", False)),
    }
    fs = getattr(vr, "final_structure", None)
    if fs is not None:
        ours = from_pymatgen(fs, comment="final structure from vasprun")
        info["final_structure"] = ours
        info["final_volume"] = round(ours.volume(), 4)
        info["final_natoms"] = ours.natoms
    # 末步最大残余力：这是判断"结构优化够不够"的直接指标
    try:
        steps = vr.ionic_steps or []
        if steps:
            forces = steps[-1].get("forces")
            if forces:
                info["max_force"] = round(max(
                    math.sqrt(sum(x * x for x in f)) for f in forces
                ), 4)
    except Exception:  # noqa: BLE001
        info["max_force"] = None
    return info
