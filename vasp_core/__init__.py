#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""vasp_core —— 纯标准库的 VASP 前处理内核。

模块分工：
    errors.py   统一的异常与退出码
    poscar.py   POSCAR 读写 + 无损结构操作（扩胞/替换/空位/加原子/应变/真空/约束/重排/平移）
    incar.py    INCAR 类型系统、预设、白名单覆盖、互斥规则
    kpoints.py  KPOINTS 四种模式（gamma/mp/line/explicit）
    potcar.py   赝势库扫描、按元素顺序拼接、TITEL 顺序校验
    recipe.py   配方 schema 校验 + 按顺序执行结构操作
    report.py   change_report.md / INCAR.diff / 审计日志
    prep.py     命令行入口（dry-run 默认，--write 才落盘）

对外常用：
    from vasp_core.prep import main
    python -m vasp_core.prep recipe.json --write
"""

__version__ = "2.0.0"

from .errors import (  # noqa: F401
    FileConflict,
    InputError,
    PolicyError,
    PrepError,
    StructureError,
    ValidationError,
)
from .incar import build_incar, check_consistency  # noqa: F401
from .kpoints import Kpoints, from_recipe as kpoints_from_recipe  # noqa: F401
from .potcar import check_order, choose, concat, scan_library, spec_text  # noqa: F401
from .poscar import Structure, read_poscar, write_poscar, write_poscar_text  # noqa: F401
from .recipe import Recipe, apply_ops, load_recipe  # noqa: F401
from .report import make_change_report, make_diff  # noqa: F401

__all__ = [
    "__version__",
    "PrepError", "InputError", "PolicyError", "StructureError", "FileConflict", "ValidationError",
    "Structure", "read_poscar", "write_poscar", "write_poscar_text",
    "build_incar", "check_consistency",
    "Kpoints", "kpoints_from_recipe",
    "scan_library", "choose", "concat", "spec_text", "check_order",
    "Recipe", "load_recipe", "apply_ops",
    "make_change_report", "make_diff",
]
