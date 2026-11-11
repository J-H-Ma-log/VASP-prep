"""机器学习势辅助：预弛豫、磁矩预测、MAGMOM 建议。

【这一层解决什么问题】
    VASP 前处理里有两件"体力活"：
      ① 手搭的表面/吸附构型往往离平衡位置很远，DFT 弛豫要几十步；
         先用 CHGNet 把结构松到 fmax≈0.05，再交给 VASP，能省一大半机时；
      ② 初始磁矩 MAGMOM 很难拍脑袋：元素、配位、自旋态都影响。
         CHGNet 能给出一个"合理的初猜"，比全填 0.6 更接近真实。
   但这两件事都**不能替代 VASP**：CHGNet 对强关联/稀土/磁性体系误差大。
   所以本模块的定位是"给出建议"，不是"下结论"：
     · pre_relax 返回的能量只是参考，报告里必须标"预弛豫"；
     · predict_magmom 的结果只写进 `MAGMOM.chgnet` 文件 + 提示，
       除非用户显式 `--ml-magmom`，否则**绝不自动写进 INCAR**。

【为什么磁矩不自动写】
    FM / AFM / FiM 是科学判断，不是数值优化：同一结构，铁磁与反铁磁的
    总能量差可能只有几十 meV，但物理结论完全相反。工具替用户选了，
    等于替他做了一个他不知道的假设。这也是 INCARBench 那类评测里
    "task-critical 正确率低"的典型来源。
"""

from __future__ import annotations

from typing import Any

from .backends import ase_backend, require_ase, require_pymatgen
from .errors import StructureError
from .poscar import Structure


def _chgnet_model(device: str = "cpu"):
    """加载 CHGNet 模型；加载一次约 1–6 秒、占几百 MB，调用方负责复用。"""
    try:
        from chgnet.model import CHGNet
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise StructureError(
            "磁矩预测需要 chgnet：pip install chgnet（含 torch）。\n"
            f"原始错误：{exc}"
        ) from exc
    return CHGNet.load(), device


def pre_relax(st: Structure, *, fmax: float = 0.05, steps: int = 100,
              cell_mask: tuple[int, ...] = (1, 1, 1, 0, 0, 0),
              model: str = "chgnet", device: str = "cpu") -> tuple[Structure, dict[str, Any]]:
    """用 ML 势做预弛豫，返回新结构与能量/收敛信息。

    【不收敛怎么办】不在这里抛异常，而是把 converged=False 放进 info：
        预弛豫没收敛并不致命（VASP 还会继续弛豫），但报告必须如实写出来，
        让人知道"这个初始结构并不在 ML 势的极小点"。
    """
    require_ase("ML 预弛豫（ml_relax / --ml relax）")
    out, info = ase_backend.relax(
        st, calculator=model, fmax=float(fmax), steps=int(steps),
        cell_mask=tuple(int(x) for x in cell_mask), device=device,
    )
    info["role"] = "pre-relaxation (not a final result)"
    return out, info


def predict_magmom(st: Structure, *, device: str = "cpu") -> dict[str, Any]:
    """用 CHGNet 预测每个原子的初始磁矩（μB）。

    【为什么用 pymatgen Structure】CHGNet 的 predict_structure 接受 pymatgen
        Structure；我们的 Structure 先经 pmg 后端转换，再送进去。
    【返回什么】per_atom（逐原子 μB，按 POSCAR 顺序）、by_species（各元素均值）、
        total（总量，粗略判断 FM/AFM 倾向）、note（使用限制）。
    """
    require_pymatgen("磁矩预测（--ml magmom）")
    from .backends.pmg_backend import to_pymatgen

    model, dev = _chgnet_model(device)
    pred = model.predict_structure(to_pymatgen(st))
    magmoms = [float(x) for x in pred.get("m", [])]
    symbols = st.atom_species()
    if len(magmoms) != len(symbols):
        raise StructureError(
            f"CHGNet 返回的磁矩数量 {len(magmoms)} 与原子数 {len(symbols)} 不一致"
        )
    by_species: dict[str, list[float]] = {}
    for s, m in zip(symbols, magmoms):
        by_species.setdefault(s, []).append(m)
    return {
        "per_atom": [round(x, 3) for x in magmoms],
        "by_species": {s: round(sum(v) / len(v), 3) for s, v in by_species.items()},
        "total_abs": round(sum(abs(x) for x in magmoms), 3),
        "note": "CHGNet 给出的初猜，只用于 MAGMOM 起点；FM/AFM 必须由人决定。",
    }


def suggest_incar(magmom_info: dict[str, Any], *, mode: str = "per_atom") -> dict[str, Any]:
    """把磁矩建议变成 INCAR 覆盖项（只返回建议，不直接写文件）。

    【为什么给两个 mode】
        per_atom：逐原子写，最精确，但 INCAR 行会很长（大体系可读性差）；
        by_species：每个元素一个值，简洁，适合快速试算。
    【为什么同时建议 ISPIN=2】有净磁矩时不打开自旋极化，等于强制非磁，
        能量会明显偏高，而且不会报错。
    """
    per_atom = magmom_info.get("per_atom") or []
    by_species = magmom_info.get("by_species") or {}
    if not per_atom:
        return {}
    if mode == "by_species":
        values = [by_species.get(s, 0.0) for s in magmom_info.get("symbols", [])]
        if not values:
            values = list(by_species.values())
        magmom = " ".join(f"{v:g}" for v in values)
    else:
        magmom = " ".join(f"{v:g}" for v in per_atom)
    return {
        "ISPIN": 2,
        "MAGMOM": magmom,
        "note": "MAGMOM 来自 CHGNet 初猜；铁磁/反铁磁需人工确认后再用。",
    }


def magmom_file_text(magmom_info: dict[str, Any]) -> str:
    """生成 `MAGMOM.chgnet` 的文本：前三行说明，后面一行就是可粘贴的 MAGMOM。

    【为什么单独一个文件】INCAR 是用户的东西，工具不该悄悄改；把建议放旁边，
        用户看一眼、认可了再自己粘过去，责任边界清楚。
    """
    atoms = len(magmom_info.get("per_atom", []))
    return (
        "# MAGMOM 建议（来自 CHGNet，仅作初猜）\n"
        f"# 原子数：{atoms}\n"
        "# 用法：确认磁序（FM/AFM）后，把下面一行粘进 INCAR，并设 ISPIN = 2\n"
        "MAGMOM = " + " ".join(f"{v:g}" for v in magmom_info.get("per_atom", [])) + "\n"
    )
