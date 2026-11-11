#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""recipe.py —— 配方的定义、严格校验与执行顺序。

为什么要有"配方"这一层：
  自然语言（或人）说"把 Cu 扩成 2×2×1，把第 3 个原子换成 Co，截断能调到 520"，
  这句话必须先变成**机器可校验的结构化数据**，再交给脚本执行。
  配方就是这层中间语言：模型只负责产出 JSON，脚本负责校验与执行。
  好处是：模型说错话（漏字段、瞎编键名、把 1 写成 "一"）在配方层就被拦住，
  而不是等到 VASP 跑起来才发现。

配方 v2 的格式（所有字段都可省略，但 structure 必须有）：

    {
      "version": 2,
      "name": "cu_co_relax",
      "calc": "relax",                     # relax / static / band
      "base_dir": ".",                     # 相对配方文件所在目录
      "structure": {
        "file": "POSCAR",
        "ops": [
          {"op": "supercell", "scaling": [2, 2, 1]},
          {"op": "substitute", "index": 3, "species": "Co"},
          {"op": "vacuum", "axis": 2, "thickness": 15.0}
        ]
      },
      "incar": {"preset": "relax", "base": "INCAR", "overrides": {"ENCUT": 520}},
      "kpoints": {"mode": "gamma", "mesh": [4, 4, 1]},
      "potcar": {"dir": "~/Optpaw_PBE.54", "map": {"Cu": "Cu_pv"}, "mode": "auto"},
      "notes": "自由文本，只给人看"
    }

设计原则：
  1. 未知字段一律报错（不静默忽略）—— 否则模型写错键名，人以为生效了；
  2. 每个 op 的参数白名单化，`index` 一律 **1 基**（和 POSCAR 行号一致）；
  3. 结构操作按数组顺序执行，前面的结果就是后面的输入；
  4. 不在本文件里做 IO 写入，只做"读配方 → 校验 → 在内存里改结构"，
     写文件统一由 prep.py 负责（这样 dry-run 与真写走同一条路径）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import InputError, PolicyError
from .incar import PRESETS
from .kpoints import Kpoints, from_recipe as kpoints_from_recipe
from .ops_ext import EXT_OPS
from .poscar import (
    Structure,
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
)

# calc 名 → INCAR 预设名；不在这里的名字直接拒绝，避免"猜"
CALC_TO_PRESET = {
    "none": "none",   # 只改结构/指定键，不套任何预设（纯结构编辑的默认值）
    "relax": "relax",
    "scf": "static",
    "static": "static",
    "band": "band",
}

# 顶层允许的键；其余报错
TOP_KEYS = {"version", "name", "calc", "base_dir", "structure", "incar",
            "kpoints", "potcar", "notes"}

# 每个 op 允许的参数（白名单）；op 名 → (必需键, 可选键)
OP_SPECS: dict[str, tuple[set[str], set[str]]] = {
    "supercell": ({"scaling"}, set()),
    "substitute": ({"index", "species"}, set()),
    "substitute_all": ({"mapping"}, set()),
    "vacancy": (set(), {"indices", "index"}),
    "add_atom": ({"species"}, {"frac", "cart", "fixed"}),
    "strain": ({"matrix"}, {"mode"}),
    "vacuum": (set(), {"axis", "thickness", "place"}),
    "fix_atoms": ({"indices"}, {"axes", "value"}),
    "reorder_species": ({"order"}, set()),
    "translate": ({"vector"}, {"mode"}),
    "wrap": (set(), set()),
}

# 高阶操作（surface/adsorbate/fix_layers/ml_relax）的白名单来自 ops_ext：
# 这样"哪些 op 需要第三方库"只在一处声明，recipe 不重复维护。
for _ext_name, (_ext_fn, _ext_required, _ext_optional) in EXT_OPS.items():
    OP_SPECS[_ext_name] = (set(_ext_required), set(_ext_optional))
OP_SPECS.pop("slab", None)   # 旧名字统一由 _parse_ops 映射到 surface


@dataclass
class Recipe:
    path: Path
    raw: dict
    name: str
    calc: str
    base_dir: Path
    structure_file: Path
    ops: list[dict]
    incar_preset: str
    incar_base: str | None
    incar_overrides: dict
    kpoints: Kpoints | None
    potcar: dict
    notes: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "calc": self.calc,
            "base_dir": str(self.base_dir),
            "structure_file": str(self.structure_file),
            "ops": self.ops,
            "incar_preset": self.incar_preset,
            "incar_base": self.incar_base,
            "incar_overrides": self.incar_overrides,
            "kpoints": self.kpoints.as_dict() if self.kpoints else None,
            "potcar": self.potcar,
        }


def _req_str(obj: dict, key: str, where: str, *, allow_empty: bool = False) -> str:
    if key not in obj:
        raise InputError(f"{where} 缺少字段 {key!r}")
    val = obj[key]
    if not isinstance(val, str) or (not allow_empty and not val.strip()):
        raise InputError(f"{where}.{key} 必须是非空字符串，收到 {val!r}")
    return val


def _check_keys(obj: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise InputError(
            f"{where} 出现未知字段 {extra}；允许的字段是 {sorted(allowed)}。\n"
            f"（未知字段一律报错，是因为模型最容易在这里写错键名，静默忽略会让你以为生效了）"
        )


def _parse_ops(raw_ops) -> list[dict]:
    if raw_ops is None:
        return []
    if not isinstance(raw_ops, list):
        raise InputError(f"structure.ops 必须是数组，收到 {type(raw_ops).__name__}")
    out: list[dict] = []
    for i, item in enumerate(raw_ops, 1):
        if not isinstance(item, dict):
            raise InputError(f"structure.ops 第 {i} 项必须是对象，收到 {item!r}")
        name = item.get("op")
        if not isinstance(name, str) or not name:
            raise InputError(f"structure.ops 第 {i} 项缺少 op 字段")
        # 旧配方里的 alias：slab 就是 surface，参数名也统一（miller 等）
        if name == "slab":
            item = dict(item)
            item["op"] = name = "surface"
        if name not in OP_SPECS:
            raise InputError(
                f"不支持的结构操作 {name!r}。可用：{sorted(OP_SPECS)}\n"
                f"提示：surface/adsorbate/ml_relax 需要 pymatgen 或 ase，"
                f"装不上时把这些 op 去掉，只做几何操作。"
            )
        required, optional = OP_SPECS[name]
        _check_keys(item, {"op"} | required | optional, f"structure.ops[{i}]({name})")
        missing = sorted(required - set(item))
        if missing:
            raise InputError(f"structure.ops[{i}]({name}) 缺少必需参数 {missing}")
        out.append(item)
    return out


def load_recipe(path: str | Path) -> Recipe:
    """读配方并做严格校验；任何问题都抛 InputError/PolicyError，消息里带定位。"""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise InputError(f"配方文件不存在：{p}")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = p.read_text(encoding="gbk", errors="replace")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"配方不是合法 JSON：{p}\n第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}\n"
            f"常见原因：用了中文引号、多了逗号、少了大括号。"
        ) from exc
    if not isinstance(raw, dict):
        raise InputError("配方最外层必须是 JSON 对象 {...}")

    _check_keys(raw, TOP_KEYS, "配方顶层")

    version = raw.get("version", 2)
    if version != 2:
        raise InputError(f"配方 version 必须是 2（旧版请用 migrate 或手改），收到 {version!r}")

    name = str(raw.get("name") or p.stem)
    calc = str(raw.get("calc") or "relax").lower()
    if calc not in CALC_TO_PRESET:
        raise InputError(
            f"calc={calc!r} 不支持。可用：{sorted(CALC_TO_PRESET)}\n"
            f"（md/neb 等复杂任务请直接改 INCAR 或在组里模板上手工调整）"
        )

    base_dir_raw = str(raw.get("base_dir") or ".")
    base_dir = (p.parent / base_dir_raw).resolve()

    structure = raw.get("structure")
    if not isinstance(structure, dict):
        raise InputError("缺少 structure 段（至少要有 {\"file\": \"POSCAR\"}）")
    _check_keys(structure, {"file", "ops"}, "structure")
    struct_file = (base_dir / str(structure.get("file") or "POSCAR")).resolve()
    ops = _parse_ops(structure.get("ops"))

    incar_raw = raw.get("incar") or {}
    if not isinstance(incar_raw, dict):
        raise InputError("incar 必须是对象")
    _check_keys(incar_raw, {"preset", "base", "overrides"}, "incar")
    preset = str(incar_raw.get("preset") or CALC_TO_PRESET[calc]).lower()
    if preset not in PRESETS:
        raise InputError(f"incar.preset={preset!r} 不存在；可用 {sorted(PRESETS)}")
    incar_base = incar_raw.get("base")
    if incar_base is not None and not isinstance(incar_base, str):
        raise InputError("incar.base 必须是路径字符串或省略")
    overrides = incar_raw.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise InputError("incar.overrides 必须是对象（键=INCAR 参数名，值=数值）")

    kp = kpoints_from_recipe(raw.get("kpoints"))

    potcar_raw = raw.get("potcar") or {}
    if not isinstance(potcar_raw, dict):
        raise InputError("potcar 必须是对象")
    _check_keys(potcar_raw, {"dir", "map", "mode"}, "potcar")
    mode = str(potcar_raw.get("mode") or "auto").lower()
    if mode not in ("auto", "spec", "skip"):
        raise InputError(f"potcar.mode={mode!r} 不支持；可用 auto / spec / skip")
    pmap = potcar_raw.get("map") or {}
    if not isinstance(pmap, dict):
        raise InputError("potcar.map 必须是对象，如 {\"Cu\": \"Cu_pv\"}")

    return Recipe(
        path=p,
        raw=raw,
        name=name,
        calc=calc,
        base_dir=base_dir,
        structure_file=struct_file,
        ops=ops,
        incar_preset=preset,
        incar_base=incar_base,
        incar_overrides=overrides,
        kpoints=kp,
        potcar={"dir": potcar_raw.get("dir"), "map": pmap, "mode": mode},
        notes=str(raw.get("notes") or ""),
    )


def apply_ops(st: Structure, ops: list[dict]) -> tuple[Structure, list[dict]]:
    """按顺序执行结构操作，返回 (新结构, 操作日志)。

    每一步都在新对象上做（poscar 的 op_* 都是纯函数，不改入参），
    所以中途出错时原结构完好，不会出现"改了一半"的状态。
    """
    log: list[dict] = []
    cur = st
    for i, item in enumerate(ops, 1):
        name = item["op"]
        try:
            if name == "supercell":
                cur, detail = op_supercell(cur, item["scaling"])
            elif name == "substitute":
                cur, detail = op_substitute(cur, item["index"], item["species"])
            elif name == "substitute_all":
                cur, detail = op_substitute_all(cur, item["mapping"])
            elif name == "vacancy":
                idx = item.get("indices", item.get("index"))
                if idx is None:
                    raise InputError("vacancy 需要 indices（列表）或 index（单个）")
                cur, detail = op_vacancy(cur, idx)
            elif name == "add_atom":
                cur, detail = op_add_atom(
                    cur, frac=item.get("frac"), cart=item.get("cart"),
                    species=item["species"], fixed=bool(item.get("fixed", False)),
                )
            elif name == "strain":
                cur, detail = op_strain(cur, item["matrix"], mode=item.get("mode", "delta"))
            elif name == "vacuum":
                cur, detail = op_vacuum(
                    cur, axis=int(item.get("axis", 2)),
                    thickness=float(item.get("thickness", 15.0)),
                    place=str(item.get("place", "center")),
                )
            elif name == "fix_atoms":
                cur, detail = op_fix_atoms(
                    cur, item["indices"], axes=str(item.get("axes", "123")),
                    value=bool(item.get("value", True)),
                )
            elif name == "reorder_species":
                cur, detail = op_reorder_species(cur, item["order"])
            elif name == "translate":
                cur, detail = op_translate(cur, item["vector"], mode=str(item.get("mode", "direct")))
            elif name == "wrap":
                cur, detail = op_wrap(cur)
            elif name in EXT_OPS:
                # 高阶 op：参数已经过白名单校验，这里原样透传。
                # 不逐个写分支，是为了让"新增 op"只需要改 ops_ext.EXT_OPS 一处。
                fn = EXT_OPS[name][0]
                params = {k: v for k, v in item.items() if k != "op"}
                cur, detail = fn(cur, **params)
            else:  # pragma: no cover - _parse_ops 已拦住
                raise InputError(f"未知操作 {name}")
        except (InputError, PolicyError):
            raise
        except Exception as exc:  # 把底层异常翻译成"第几步失败"
            raise InputError(f"structure.ops[{i}]({name}) 执行失败：{exc}") from exc
        log.append({"step": i, "op": name, "detail": detail})
    return cur, log
