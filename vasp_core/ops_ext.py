"""高阶操作：表面、吸附质、按层固定、机器学习势预弛豫。

【为什么单独一个模块，而不是塞进 poscar.py】
    poscar.py 的 12 个 op 是"纯几何、零依赖、可离线"的——扩胞/替换/应变/真空
    只用到三角函数和矩阵，集群上没有 pymatgen/ase 也能跑。
    本模块的 4 个 op 需要外部库（pymatgen 切表面、ase 加吸附质/弛豫）。
    分开放，好处是：
      ① 核心测试永远不依赖第三方包；
      ② 没装包时，只有配方里真的用了 surface/ml_relax 才报错，
         不会影响"只做几何操作"的用户；
      ③ 每个 op 都能单独标"需要什么后端"，工具层可以据此提示安装。

【接口约定】和 poscar.py 的 op 完全一致：
    op_xxx(st, **参数) -> (新 Structure, 中文一句话说明)
    纯函数：不修改传入对象；出错抛 StructureError / PolicyError。
"""

from __future__ import annotations

from typing import Any, Sequence

from .backends import ase_backend, pmg_backend
from .errors import StructureError
from .poscar import Structure


def op_surface(st: Structure, *, miller: Sequence[int], layers: int = 4,
               vacuum: float = 15.0, center: bool = True,
               min_slab_size: float | None = None,
               fix_layers: int = 0) -> tuple[Structure, str]:
    """切 (hkl) 表面（需要 pymatgen）。

    【参数】miller=[1,1,1] 这类三整数；layers 是"保留几层"；
        vacuum 是真空厚度（Å）；min_slab_size 若给出则优先（单位 Å）。
    【为什么默认 vacuum=15】表面能随真空收敛的经验值：10 Å 通常够，
        15 Å 对带偶极/吸附的体系更保险；代价是 c 方向变长、计算变贵一点。
    【不这样做】真空 5 Å 会让上下表面周期性相互作用，吸附能算出来差 0.1–0.3 eV。
    """
    if len(list(miller)) != 3:
        raise StructureError(f"miller 必须是 3 个整数，收到 {miller!r}")
    try:
        hkl = tuple(int(x) for x in miller)
    except (TypeError, ValueError) as exc:
        raise StructureError(f"miller 必须是整数，收到 {miller!r}") from exc
    if all(x == 0 for x in hkl):
        raise StructureError("miller 不能是 (0,0,0)")
    out, info = pmg_backend.slab(
        st, hkl, layers=int(layers), vacuum=float(vacuum),
        min_slab_size=min_slab_size, center=bool(center),
        fix_layers=int(fix_layers),
    )
    msg = (f"切 {hkl} 表面：{st.natoms} → {out.natoms} 原子，"
           f"请求 {layers} 层 / 真空 {vacuum} Å，实际真空 {info['vacuum_actual']} Å")
    if info.get("n_terminations", 1) > 1:
        msg += f"（该方向有 {info['n_terminations']} 种终止面，已取第一种）"
    if fix_layers:
        msg += f"；{info.get('fix_message', '')}"
    return out, msg


def op_adsorbate(st: Structure, *, species: str, site: str | None = "ontop",
                 position: Sequence[float] | None = None,
                 height: float = 2.0) -> tuple[Structure, str]:
    """在表面加吸附质（需要 ase）。

    【为什么需要"表面"】吸附质必须放在一个已有真空的 slab 上；
        对体相加吸附质没有物理意义（会嵌进晶格里）。
        这里不强行检查——用户可能想先在体相上摆位置再切表面，由他决定。
    """
    if not species or not species.isalpha():
        raise StructureError(f"吸附质元素符号不合法：{species!r}")
    return ase_backend.add_adsorbate(
        st, species, site=site,
        position=list(position) if position is not None else None,
        height=float(height),
    )


def op_fix_layers(st: Structure, *, n_layers: int = 2, tol: float = 0.1,
                  axes: str = "123") -> tuple[Structure, str]:
    """固定底部 n 层（纯几何，不需要第三方库）。

    【为什么放在这里是安全的】只按 z 坐标聚类 + 改 flags，没有物理假设；
        即使没有 pymatgen/ase 也能用。
    """
    return pmg_backend.op_fix_layers(st, n_layers=int(n_layers), tol=float(tol), axes=axes)


def op_ml_relax(st: Structure, *, fmax: float = 0.05, steps: int = 100,
                cell_mask: Sequence[int] = (1, 1, 1, 0, 0, 0),
                model: str = "chgnet", device: str = "cpu") -> tuple[Structure, str]:
    """用机器学习势/EMT 预弛豫结构（需要 ase，chgnet 模型还需 chgnet+torch）。

    【为什么值得做】DFT 直接弛豫一个手搭的表面/吸附构型，常常要几十到上百个
        离子步；先用 CHGNet 弛豫到 fmax≈0.05，再交给 VASP，通常能把 VASP 的
        离子步砍掉一半以上，而且不太会掉进错误的局部极小。
    【为什么不能只靠它】CHGNet 对磁性/强关联/稀土体系误差大；它的能量不能当
        最终结论，最终必须 VASP 复核。配方里用它，报告里也会明确写"预弛豫"。
    【参数】cell_mask 六个数对应 a/b/c/α/β/γ；全 0 表示只动原子不动晶胞
        （表面/吸附体系通常如此）；(1,1,1,0,0,0) 允许体积变化但不变角度。
    """
    mask = [int(x) for x in cell_mask]
    if len(mask) != 6 or any(x not in (0, 1) for x in mask):
        raise StructureError("cell_mask 必须是 6 个 0/1（a b c α β γ）")
    out, info = ase_backend.relax(
        st, calculator=model, fmax=float(fmax), steps=int(steps),
        cell_mask=mask, device=device,
    )
    if not info["converged"]:
        raise StructureError(
            f"预弛豫在 {steps} 步内没有收敛（fmax={info['fmax_actual']} > {fmax}）。\n"
            "把 steps 调大（如 300），或先检查初始结构是否合理（原子是否重叠）。"
        )
    msg = (f"{model} 预弛豫：{info['n_steps']} 步收敛，"
           f"E = {info['energy_eV']:.4f} eV（{info['energy_per_atom_eV']:.4f} eV/atom），"
           f"最大力 {info['fmax_actual']} eV/Å，体积 {info['volume_before']} → {info['volume_after']} Å³")
    return out, msg


# op 名字 → (函数, 必填参数集合, 可选参数集合)
# recipe.py 用这份表做白名单校验；新增 op 只改这里和 recipe 的分发一行。
EXT_OPS: dict[str, tuple[Any, set[str], set[str]]] = {
    "surface": (op_surface, {"miller"}, {"layers", "vacuum", "center", "min_slab_size", "fix_layers"}),
    "adsorbate": (op_adsorbate, {"species"}, {"site", "position", "height"}),
    "fix_layers": (op_fix_layers, set(), {"n_layers", "tol", "axes"}),
    "ml_relax": (op_ml_relax, set(), {"fmax", "steps", "cell_mask", "model", "device"}),
}
