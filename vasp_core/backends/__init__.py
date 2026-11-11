"""可选后端：pymatgen（表面/对称性/能带路径/读 vasprun）与 ASE（吸附/约束/弛豫）。

【为什么要有"可选后端"】
    本项目的核心哲学是"纯标准库也能跑"：集群登录节点常常没有 pymatgen/ase，
    或者装不上（无外网、Python 太老）。所以核心模块 vasp_core/poscar.py 自己
    实现了 POSCAR 读写和 12 个几何操作，不依赖任何第三方包。
    但有两件事自己写不划算：
      · 任意 (hkl) 切表面：要枚举平移、正交化、去重复层，pymatgen 已写得很成熟；
      · 力场/机器学习势弛豫：ASE 的 BFGS、FIRE、cell filter 是事实标准。
    于是把它们放进 backends/，谁需要谁导入：
      - 需要 surface/symmetry/kpath/verify 时 → 用 pymatgen 后端
      - 需要 adsorbate/ml_relax 时 → 用 ASE 后端
    没装对应包时，只让"用到它的那一步"报错并告诉用户装什么，
    而不是整个工具一启动就 ImportError。

【不这样做的后果】
    若把 pymatgen 写进核心 import，集群上 `python -m vasp_core.prep --help`
    都会直接崩；用户会以为整套工具装坏了，其实只是少了一个可选依赖。

【还有别的办法吗】
    可以做一个纯标准库的表面生成器，但任意 hkl 的正交化与层去重很容易出错，
    而且错了 VASP 不会报错（只会算错体系）——不值得重写。
"""

from __future__ import annotations

from importlib.util import find_spec
from typing import Any

# 只做"包在不在"的判断，不真正 import：import pymatgen 要几百毫秒，
# 而 --help / 只读操作不该付这个代价。
_HAS_PMG: bool | None = None
_HAS_ASE: bool | None = None


def has_pymatgen() -> bool:
    """判断当前解释器能不能用 pymatgen。结果缓存，避免每次调用都查。"""
    global _HAS_PMG
    if _HAS_PMG is None:
        _HAS_PMG = find_spec("pymatgen") is not None
    return _HAS_PMG


def has_ase() -> bool:
    """判断当前解释器能不能用 ase。"""
    global _HAS_ASE
    if _HAS_ASE is None:
        _HAS_ASE = find_spec("ase") is not None
    return _HAS_ASE


def describe() -> dict[str, Any]:
    """给 --doctor 和 /health 用的能力快照。

    【为什么返回 dict 而不是布尔】
        调用方（CLI、MCP 工具、智能体）需要把"哪些能力可用/缺什么包"原样告诉
        用户，而不是只说"不可用"。
    """
    return {
        "pymatgen": {
            "available": has_pymatgen(),
            "used_for": ["surface", "symmetry", "kpath", "read_vasprun", "fetch_mp"],
            "install": 'pip install pymatgen   # 或 conda install -c conda-forge pymatgen',
        },
        "ase": {
            "available": has_ase(),
            "used_for": ["adsorbate", "fix_layers", "ml_relax"],
            "install": 'pip install ase   # chgnet 模型另需 pip install chgnet',
        },
    }


def require_pymatgen(what: str) -> None:
    """在调用 pymatgen 后端前显式检查，给一句能照做的安装提示。

    【为什么不在函数内部 try/except ImportError】
        ImportError 的原始信息是 "No module named 'pymatgen'"，用户看不出
        是"这一步需要它"，也看不出该装哪个包。统一在这里抛，信息更可操作。
    """
    if not has_pymatgen():
        from ..errors import PolicyError

        raise PolicyError(
            f"{what} 需要 pymatgen，但当前 Python 里没有。\n"
            "装它：pip install pymatgen\n"
            "或者跳过这一步（例如用 --ml off，或只做几何操作）。",
            detail={"backend": "pymatgen", "required_by": what},
        )


def require_ase(what: str) -> None:
    """在调用 ASE 后端前显式检查。"""
    if not has_ase():
        from ..errors import PolicyError

        raise PolicyError(
            f"{what} 需要 ase，但当前 Python 里没有。\n"
            "装它：pip install ase\n"
            "机器学习势还要：pip install chgnet（或换成 --ml off）。",
            detail={"backend": "ase", "required_by": what},
        )
