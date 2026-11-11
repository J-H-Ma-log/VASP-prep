#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools.py —— 给智能体调用的 8 个 VASP 前处理工具（工具层，不重复科学逻辑）。

设计定位
--------
本模块只做三件事：**把已有能力包装成机器可调用的工具**、**做参数校验**、
**把异常转成结构化结果**。结构操作、INCAR 规则、POTCAR 拼接、结果核验
全部调用 vasp_core 已有函数，绝不在这里复制第二套物理逻辑——
两套逻辑一旦漂移，模型看到的校验结果就会和 CLI 实际写盘结果不一致。

安全边界（三条硬约束）
----------------------
1. 写盘工具只有 write_inputs，它必须显式 `confirm=true`，且目标目录必须
   此前不存在（绝不覆盖、绝不 --force）；POTCAR 只按 spec 记录，审计日志关闭。
2. submit_hint 只生成 cluster 的 `--dry-run` 命令文本，**不导入 subprocess、
   不执行 ssh/scp/qsub**；"提交作业"永远由人来做。
3. 其余 6 个工具只读；其中 validate_recipe 只会在工作区的
   `.tmp_build/tool_test` 下写临时配方并立即清理，不碰用户目录。

统一返回约定
------------
`dispatch(name, args)` 永不抛异常，返回：
    成功 {"ok": true, "tool": name, "mutating": bool, "result": {...}}
    失败 {"ok": false, "tool": name, "error": {code, message, field, detail}}
这样 OpenAI tool-calling 与 MCP tools/call 都能直接回传，模型看到的是
"工具被拒绝的原因"，而不是一段 Python traceback。
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cluster import DEFAULTS as CLUSTER_DEFAULTS
from .errors import (
    FileConflict,
    InputError,
    PolicyError,
    PrepError,
    StructureError,
    ValidationError,
)
from .incar import DANGEROUS_KEYS, INCAR_TYPES, build_incar, parse_incar
from .nl import text_to_recipe
from .poscar import read_poscar
from .prep import DEFAULT_AUDIT, build_parser as build_prep_parser, run as prep_run
from .recipe import load_recipe
from .report import make_diff
from .verify import find_outputs
from .verify import has_pymatgen
from .verify import render_report as render_verify_report
from .verify import verify_run as core_verify_run

__all__ = [
    "ToolError",
    "TOOLS",
    "dispatch",
    "tool_specs_openai",
    "tool_specs_mcp",
    "workspace_status",
    "read_structure",
    "read_incar",
    "propose_recipe",
    "validate_recipe",
    "write_inputs",
    "verify_run",
    "submit_hint",
]

# 工具层自己的临时落点：只允许工作区下 .tmp_build/tool_test（任务硬约束）。
# 为什么不用 tempfile.gettempdir()：用户要求所有测试/临时产物留在一个可清理的位置，
# 系统临时目录散落文件既难审计，也可能在受限环境里不可写。
TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_build" / "tool_test"

_MAX_READ_BYTES = 5 * 1024 * 1024  # 单文件读取上限：POSCAR/INCAR 正常不超过几百 KB

# 命令行参数的安全白名单：工具只生成命令文本，但文本会被复制到终端执行，
# 所以先在源头拒绝空白、分号、重定向等 shell 元字符，避免"提示词注入 → 命令注入"。
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\-]{0,127}$")
_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9_.@:/~+\-]{1,512}$")
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


class ToolError(Exception):
    """工具层的可预期错误：带稳定 code，供模型/宿主程序分支处理。"""

    def __init__(self, code: str, message: str, *, field: str | None = None,
                 detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.detail = detail


# ---------------------------------------------------------------------------
# JSON Schema 参数校验
# ---------------------------------------------------------------------------

_JSON_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    # bool 是 int 的子类，JSON 里却不是整数；不显式排除会把 true 当成 1。
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _type_name(value: Any) -> str:
    """给错误信息用的中文类型名（不是 Python 类名，模型更容易读懂）。"""
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "小数"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, dict):
        return "对象"
    if isinstance(value, list):
        return "数组"
    if value is None:
        return "null"
    return type(value).__name__


def _validate_value(field: str, spec: dict, value: Any) -> None:
    """按单个 JSON Schema 片段校验一个值；失败抛 ToolError。

    做什么：类型 → 枚举 → 长度/范围 → 数组元素，逐层检查。
    为什么：把校验放在 dispatch 入口，handler 里就不用到处写 if 判断，
            也保证 OpenAI/MCP 两条入口的校验完全一致。
    不这样：模型传错一个字段就会在 handler 深处抛 TypeError，用户看到 traceback。
    别的办法：用 jsonschema 第三方库，但项目要求纯标准库，且这里只需子集。
    """
    if "oneOf" in spec:
        # oneOf 只用于 recipe（路径字符串 或 配方对象）这类"二选一"参数；
        # 逐个试，任何一个片段完全通过就接受，全失败才报类型不匹配。
        for sub in spec["oneOf"]:
            try:
                _validate_value(field, sub, value)
                return
            except ToolError:
                continue
        raise ToolError("invalid-type", f"参数 {field} 的类型不符合要求（收到 {_type_name(value)}）",
                        field=field)

    expected = spec.get("type")
    if isinstance(expected, list):
        if not any(_JSON_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in expected):
            raise ToolError("invalid-type", f"参数 {field} 期望 {'/'.join(expected)}，收到 {_type_name(value)}",
                            field=field)
    elif expected in _JSON_TYPE_CHECKS:
        if not _JSON_TYPE_CHECKS[expected](value):
            raise ToolError("invalid-type", f"参数 {field} 期望 {expected}，收到 {_type_name(value)}",
                            field=field)

    if "enum" in spec and value not in spec["enum"]:
        allowed = "、".join(str(x) for x in spec["enum"])
        raise ToolError("invalid-enum", f"参数 {field} 只能是 {allowed}，收到 {value!r}", field=field)

    if isinstance(value, str):
        if len(value) < spec.get("minLength", 0):
            raise ToolError("invalid-length", f"参数 {field} 太短（至少 {spec['minLength']} 个字符）", field=field)
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise ToolError("invalid-length", f"参数 {field} 太长（最多 {spec['maxLength']} 个字符）", field=field)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolError("invalid-range", f"参数 {field} 不能小于 {spec['minimum']}（收到 {value}）", field=field)
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolError("invalid-range", f"参数 {field} 不能大于 {spec['maximum']}（收到 {value}）", field=field)

    if isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise ToolError("invalid-length", f"参数 {field} 至少需要 {spec['minItems']} 项", field=field)
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise ToolError("invalid-length", f"参数 {field} 最多 {spec['maxItems']} 项", field=field)
        item_spec = spec.get("items")
        if isinstance(item_spec, dict):
            for i, item in enumerate(value):
                _validate_value(f"{field}[{i}]", item_spec, item)


def _validate_arguments(schema: dict, args: dict) -> dict:
    """按工具的 parameters schema 校验并补默认值，返回干净参数字典。

    做什么：拒绝未知字段、检查必填、逐字段校验、填 default。
    为什么：未知字段静默忽略最危险——模型以为改了 ENCUT，实际键名拼错没生效。
    不这样：配方层虽有未知键检查，但工具参数层不查会让"写错参数"看起来成功。
    别的办法：让每个 handler 自己校验；重复代码多且容易漏，集中一处更可靠。
    """
    props: dict = schema.get("properties", {})
    required = set(schema.get("required", []))
    cleaned: dict[str, Any] = {}

    for key in args:
        if key not in props:
            raise ToolError(
                "unknown-argument",
                f"未知参数 {key!r}；本工具只接受：{'、'.join(props) or '（无参数）'}",
                field=key,
            )

    for key, prop in props.items():
        if key in args:
            _validate_value(key, prop, args[key])
            cleaned[key] = args[key]
        elif key in required:
            raise ToolError("missing-argument", f"缺少必填参数 {key}", field=key)
        elif "default" in prop:
            cleaned[key] = prop["default"]

    return cleaned


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _require_file(path_text: str, *, field: str = "path") -> Path:
    """把参数解析成存在的文件路径；不存在/不是文件就抛结构化错误。"""
    path = Path(path_text).expanduser()
    if not path.exists():
        raise ToolError("file-not-found", f"文件不存在：{path}", field=field)
    if not path.is_file():
        raise ToolError("not-a-file", f"不是普通文件：{path}", field=field)
    if path.stat().st_size > _MAX_READ_BYTES:
        raise ToolError("file-too-large",
                        f"文件超过 {_MAX_READ_BYTES // 1024 // 1024} MB，拒绝读入：{path}", field=field)
    return path.resolve()


def _read_text(path: Path) -> str:
    """读文本：优先 utf-8-sig（Windows 记事本会带 BOM），非法字节替换而不是崩溃。"""
    return path.read_text(encoding="utf-8-sig", errors="replace")


@contextlib.contextmanager
def _scratch(tag: str):
    """在工作区 .tmp_build/tool_test 下开一个临时目录，退出时无条件清理。

    做什么：mkdtemp 的替代品，位置固定在任务允许的区域。
    为什么：validate_recipe 收到"配方 dict"时要落一个临时 JSON 才能复用 load_recipe，
            写完必须删掉，否则每次校验都给工作区留垃圾。
    不这样：把临时配方写进用户当前目录，可能与真实文件重名、覆盖用户数据。
    别的办法：改 load_recipe 支持 dict（会动核心模块，任务禁止）。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{tag}_{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _recipe_path(recipe: Any, scratch: Path) -> Path:
    """把 recipe 参数（路径字符串或配方对象）统一成可交给 load_recipe 的文件路径。

    配方的 base_dir 是相对"配方文件所在目录"解析的。模型给的 dict 通常来自
    propose_recipe，其 base_dir 是 "."，若原样写到临时目录，结构文件就会去
    .tmp_build 里找——所以这里把 "."（或缺省）改成本进程当前目录，符合
    "用户在工作目录下操作"的直觉。
    """
    if isinstance(recipe, str):
        path = Path(recipe).expanduser()
        if not path.exists():
            raise ToolError("recipe-not-found", f"配方文件不存在：{path}", field="recipe")
        if not path.is_file():
            raise ToolError("not-a-file", f"配方不是普通文件：{path}", field="recipe")
        return path.resolve()

    if not isinstance(recipe, dict) or not recipe:
        raise ToolError("invalid-type", "recipe 必须是配方文件路径或非空配方对象", field="recipe")

    data = json.loads(json.dumps(recipe))  # 深拷贝：不改调用方对象
    base_dir = data.get("base_dir")
    if base_dir in (None, "", "."):
        data["base_dir"] = str(Path.cwd())
    target = scratch / "recipe.json"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _prep_namespace(recipe_path: Path, *, write: bool, out: Path | None,
                    pp_dir: str | None, potcar_mode: str):
    """用 prep 自己的 argparse 构造 Namespace，保证与 CLI 默认值永远一致。

    为什么不用手写 Namespace：prep.py 会持续加参数（--ml/--symmetry/--verify…），
    手写字段一旦漏掉，工具层就在别人升级内核后崩掉。parse_args 自动带上默认值。
    不这样：硬编码 Namespace 字段，属于"复制核心接口"，升级即失效。
    别的办法：给 prep 增加程序化入口（改核心模块，本任务不允许）。
    """
    argv = [str(recipe_path), "--json", "--quiet", "--potcar-mode", potcar_mode]
    ns = build_prep_parser().parse_args(argv)
    ns.write = write
    ns.out = str(out) if out is not None else None
    ns.pp_dir = pp_dir
    ns.no_audit = True          # 工具层不写全局审计日志，避免悄悄改用户 home
    ns.audit = str(DEFAULT_AUDIT)
    return ns


# 异常 → 稳定错误码。顺序有讲究：子类必须排在 PrepError 前面。
_ERROR_CODE_MAP: list[tuple[type[BaseException], str]] = [
    (FileConflict, "out-dir-exists"),
    (ValidationError, "validation-failed"),
    (StructureError, "invalid-structure"),
    (PolicyError, "policy-violation"),
    (InputError, "invalid-input"),
    (PrepError, "prep-error"),
]


def _map_exception(exc: BaseException) -> tuple[str, str, str | None]:
    """把内核异常翻译成 (code, message, detail)，供 dispatch 统一回传。"""
    for cls, code in _ERROR_CODE_MAP:
        if isinstance(exc, cls):
            return code, str(exc), None
    return "internal-error", f"工具内部错误：{type(exc).__name__}: {exc}", None


def _run_prep(*, recipe_path: Path, write: bool, out: Path | None,
              pp_dir: str | None, potcar_mode: str) -> dict:
    """调用 prep.run，并把 PrepError 转成 ToolError（保持错误码语义）。"""
    try:
        return prep_run(_prep_namespace(recipe_path, write=write, out=out,
                                        pp_dir=pp_dir, potcar_mode=potcar_mode))
    except PrepError as exc:
        code, message, _ = _map_exception(exc)
        raise ToolError(code, message) from exc


def _incar_diff_preview(recipe_path: Path, natoms: int) -> str:
    """复算一遍 INCAR，产出与写盘时同源的 unified diff（不写文件）。

    为什么单独算：prep.run 的 dry-run 摘要只给"改了哪些键"，不给 diff；
    而人审阅时需要看到"底稿 → 新 INCAR"的逐行变化，才能判断有没有意外改动。
    会不会和写盘不一致：两边都调用同一个 build_incar/make_diff，只有底稿来源
    这一处需要保持一致（这里按 prep.run 的顺序：incar.base 优先，其次 base_dir/INCAR）。
    """
    recipe = load_recipe(recipe_path)
    base_text: str | None = None
    if recipe.incar_base:
        base_path = (recipe.base_dir / recipe.incar_base).resolve()
        if base_path.exists():
            base_text = _read_text(base_path)
    else:
        default_base = recipe.base_dir / "INCAR"
        if default_base.exists():
            base_text = _read_text(default_base)

    incar_text, _changes, _issues, _lines = build_incar(
        base_text=base_text,
        preset=recipe.incar_preset,
        overrides=recipe.incar_overrides,
        natoms=natoms,
    )
    return make_diff(base_text or "", incar_text, "INCAR")


def _atomic_text(path: Path, text: str) -> None:
    """测试/文档示例用不到；保留给未来扩展（原子写）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 工具 1：workspace_status（只读）
# ---------------------------------------------------------------------------

def workspace_status(args: dict) -> dict:
    """盘点工作区：有哪些配方/结构/算例、赝势与超算环境变量、Python 版本。

    做什么：只读扫描目录（默认当前目录，深度 2），按文件类型归类，并汇总环境。
    为什么先做这一步：模型不知道用户在哪个目录、有什么文件，直接读 POSCAR 会瞎猜路径；
            先盘点能把"相对路径"变成有依据的绝对路径。
    不这样做：模型频繁试错读文件，既浪费轮次也容易把用户目录结构说错。
    还有别的办法：让用户手填路径；但对话式使用时用户往往只说"帮我扩胞"。
    """
    root = Path(args.get("workspace", ".")).expanduser().resolve()
    if not root.exists():
        raise ToolError("workspace-not-found", f"工作区目录不存在：{root}", field="workspace")
    if not root.is_dir():
        raise ToolError("not-a-directory", f"工作区不是目录：{root}", field="workspace")

    max_depth = int(args.get("max_depth", 2))
    max_entries = int(args.get("max_entries", 200))

    # 跳过版本库/缓存/本工具自己的临时区：它们又大又与当前任务无关。
    skip_dirs = {".git", ".hg", ".svn", "__pycache__", ".tmp_build", ".tmp_nl",
                 "node_modules", ".venv", "venv"}

    entries: list[dict] = []
    counts: dict[str, int] = {}
    truncated = False
    stack: list[tuple[Path, int]] = [(root, 0)]

    while stack:
        current, depth = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue  # 无权限的目录跳过，不让盘点整体失败
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                break
            name = child.name
            if child.is_dir():
                if name in skip_dirs or name.startswith("."):
                    continue
                if depth < max_depth:
                    stack.append((child, depth + 1))
                continue
            if not child.is_file():
                continue
            kind = _classify_entry(child)
            if kind is None:
                continue
            try:
                size = child.stat().st_size
            except OSError:
                size = None
            entries.append({"path": str(child), "kind": kind, "size": size})
            counts[kind] = counts.get(kind, 0) + 1
        if len(entries) >= max_entries:
            truncated = True
            break

    return {
        "workspace": str(root),
        "entries": entries,
        "counts": counts,
        "truncated": truncated,
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "env": {
            "VASP_HOST": os.environ.get("VASP_HOST", ""),
            "PMG_VASP_PSP_DIR": os.environ.get("PMG_VASP_PSP_DIR", ""),
            "VASP_PP_PATH": os.environ.get("VASP_PP_PATH", ""),
        },
        "cluster_defaults": dict(CLUSTER_DEFAULTS),
        "tools_version": __version__,
        "note": "路径均为绝对路径；读结构用 read_structure，校验配方用 validate_recipe。",
    }


def _classify_entry(path: Path) -> str | None:
    """按文件名给条目归类；返回 None 表示不关心的普通文件（不列进结果）。"""
    lower = path.name.lower()
    if lower in ("poscar", "contcar") or lower.endswith(".vasp"):
        return "structure"
    if lower.startswith("incar"):
        return "incar"
    if lower.startswith("kpoints"):
        return "kpoints"
    if lower == "potcar":
        return "potcar"
    if lower.endswith(".spec"):
        return "potcar-spec"
    if lower in ("outcar", "oszicar", "vasprun.xml", "vasprun.xml.gz", "oszicar.gz"):
        return "output"
    if lower.endswith(".json"):
        # 只看一眼文件名已不够：小型 JSON 读进来判断是不是配方；
        # 读失败/超大就当普通 JSON，不因为一个坏文件让整个盘点失败。
        try:
            if path.stat().st_size <= 256 * 1024:
                data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
                if isinstance(data, dict) and ("structure" in data or data.get("version") == 2):
                    return "recipe"
                return "json"
        except (OSError, ValueError):
            return "json"
        return "json"
    if lower.endswith((".sh", ".slurm", ".pbs")):
        return "job-script"
    return None


# ---------------------------------------------------------------------------
# 工具 2：read_structure（只读）
# ---------------------------------------------------------------------------

def read_structure(args: dict) -> dict:
    """读 POSCAR/CONTCAR：晶格、元素、原子数、分数坐标、约束标志。

    做什么：调用 vasp_core.poscar.read_poscar，返回摘要 + 前 N 个原子明细。
    为什么不让模型读原文：POSCAR 的缩放因子/笛卡尔/Selective 标记很容易被读错，
            结构化输出能把"这个数到底是什么意思"固定下来。
    不这样做：模型可能把缩放因子当成 1、把笛卡尔当分数，后续扩胞/掺杂直接算错。
    还有别的办法：返回原始文本；对长结构浪费 token，且无类型信息。
    """
    path = _require_file(args["path"])
    st = read_poscar(path)
    summary = st.summary()
    max_atoms = int(args.get("max_atoms", 20))

    # VASP4 没有元素行，atom_species() 会抛错；这里退化成"未知元素"，仍然给出坐标，
    # 让模型能读到结构，同时在 notes 里明确提示不能做元素相关判断。
    notes: list[str] = []
    try:
        species = st.atom_species()
    except StructureError as exc:
        species = ["?"] * st.natoms
        notes.append(f"元素信息缺失（VASP4 格式）：{exc}")

    fracs = st.frac()
    flags = st.flags if st.selective else None
    atoms: list[dict] = []
    for i in range(min(st.natoms, max_atoms)):
        atom = {
            "index": i + 1,               # 1 基：与配方 index、POSCAR 行号一致
            "species": species[i] if i < len(species) else "?",
            "frac": [round(x, 8) for x in fracs[i]],
        }
        if flags is not None and i < len(flags):
            atom["fixed"] = [bool(x) for x in flags[i]]
        atoms.append(atom)

    if st.selective:
        notes.append("结构带 Selective dynamics：fixed=true 表示该方向固定。")
    if summary.get("has_velocities"):
        notes.append("结构带速度行；扩胞/替换后速度不会自动保持物理意义。")

    return {
        "path": str(path),
        "comment": st.comment,
        "summary": summary,
        "lattice": [[round(x, 8) for x in row] for row in st.lattice],
        "atoms": atoms,
        "atoms_returned": len(atoms),
        "truncated": st.natoms > len(atoms),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 工具 3：read_incar（只读）
# ---------------------------------------------------------------------------

def read_incar(args: dict) -> dict:
    """解析 INCAR：键值、重复键、未知键、危险键。

    做什么：parse_incar 解析成 {KEY: value}，再对照内核的 INCAR_TYPES 标出未知/危险键。
    为什么：INCAR 的坑不是"语法错"，而是"拼错键名 VASP 静默忽略""写了 LDAU 但工具
            不支持"。先标出来，比等模型自己 grep 更可靠。
    不这样做：模型可能把 LDAU 之类的键当成普通覆盖写进配方，被内核拒绝时又说不出原因。
    还有别的办法：直接检查一致性（check_consistency）——那需要结构/赝势上下文，
            适合 validate_recipe，不适合单读一个 INCAR。
    """
    path = _require_file(args["path"])
    text = _read_text(path)
    lines = parse_incar(text)
    keys: dict[str, str] = {}
    duplicates: list[str] = []
    for line in lines:
        if line.key in keys and line.key not in duplicates:
            duplicates.append(line.key)
        keys[line.key] = line.value

    unknown = sorted(k for k in keys if k not in INCAR_TYPES)
    dangerous = sorted(k for k in keys if k in DANGEROUS_KEYS or _is_dangerous_like(k))
    include_lines = bool(args.get("include_lines", False))

    result = {
        "path": str(path),
        "n_keys": len(keys),
        "keys": keys,
        "duplicates": duplicates,
        "unknown_keys": unknown,
        "dangerous_keys": dangerous,
        "has_species_hint": any(k in keys for k in ("MAGMOM", "LDAUU", "NELECT")),
        "notes": [],
    }
    if duplicates:
        result["notes"].append(f"重复键：{'、'.join(duplicates)}（VASP 通常取最后一次，容易误判）")
    if unknown:
        result["notes"].append(f"未知键：{'、'.join(unknown)}（VASP 会静默忽略，检查是否拼错）")
    if dangerous:
        result["notes"].append(f"危险/策略键：{'、'.join(dangerous)}（工具层不允许覆盖，需人工处理）")
    if include_lines:
        result["lines"] = [{"key": ln.key, "value": ln.value, "raw": ln.raw} for ln in lines]
    return result


def _is_dangerous_like(key: str) -> bool:
    """DANGEROUS_KEYS 里带通配（LDAU* / IVDW* / VDW_*），这里补一层前缀判断。"""
    upper = key.upper()
    return any(upper.startswith(prefix) for prefix in ("LDAU", "IVDW", "VDW_"))


# ---------------------------------------------------------------------------
# 工具 4：propose_recipe（只读，产候选）
# ---------------------------------------------------------------------------

def propose_recipe(args: dict) -> dict:
    """把中文需求转成候选 recipe 对象（规则/模型只给候选，不写文件）。

    做什么：调用 nl.text_to_recipe；可带一个基准配方路径，把新要求合并上去。
    为什么先给候选、不直接写盘：自然语言里大量歧义（slab 取哪个面、固定几层、
            是否 DFT+U），工具只做"能确定的"，其余进 needs_human。
    不这样做：模型擅自猜科学参数并直接生成算例，用户拿到的是看似合理但错的输入。
    还有别的办法：让 LLM 直接生成 JSON；已有 nl2recipe 做这件事，但那里要联网，
            本工具保持离线可复现。
    """
    text = args["text"].strip()
    if not text:
        raise ToolError("invalid-input", "text 不能为空", field="text")

    base = None
    base_path_text = args.get("base_recipe")
    if base_path_text:
        base_path = Path(base_path_text).expanduser()
        if not base_path.exists():
            raise ToolError("recipe-not-found", f"基准配方不存在：{base_path}", field="base_recipe")
        base = load_recipe(base_path).raw

    parsed = text_to_recipe(text, base=base)
    recipe = parsed["recipe"]
    # base_dir 默认 "." 是相对"配方文件"的；候选还没落盘，明确告诉模型这是工作目录语义。
    hints: list[str] = []
    if parsed.get("needs_human"):
        topics = "、".join(item.get("topic", "?") for item in parsed["needs_human"])
        hints.append(f"以下需求需要人决定，工具没有擅自处理：{topics}")
    if parsed.get("unmatched"):
        hints.append("有未识别的片段，请确认是否漏了需求：" + "；".join(parsed["unmatched"]))

    return {
        "recipe": recipe,
        "recipe_json": json.dumps(recipe, ensure_ascii=False, indent=2),
        "matched": parsed.get("matched", []),
        "needs_human": parsed.get("needs_human", []),
        "unmatched": parsed.get("unmatched", []),
        "hints": hints,
        "next_step": "把 recipe_json 与 needs_human 给用户确认，然后调用 validate_recipe 做真校验。",
    }


# ---------------------------------------------------------------------------
# 工具 5：validate_recipe（只读用户目录；临时文件立即清理）
# ---------------------------------------------------------------------------

def validate_recipe(args: dict) -> dict:
    """对配方做完整 dry-run 校验，返回结构变化、INCAR diff、KPOINTS、问题清单。

    做什么：调用 prep.run(write=False) 得到与写盘完全同源的摘要，再复算 INCAR diff。
    为什么必须真跑一遍内核：只查 JSON 语法看不出"ENCUT < 赝势 ENMAX""ISPIN=2 没有
            MAGMOM"这类会算错的组合；这些只有执行到一致性检查才暴露。
    不这样做：把无效配方写进算例目录，VASP 跑完才发现，浪费机时。
    还有别的办法：让模型自己逐条对规则；规则有 12 条一致性检查，模型背不全。
    """
    include_diff = bool(args.get("include_diff", True))
    potcar_mode = args.get("potcar_mode", "spec")
    pp_dir = args.get("pp_dir")

    with _scratch("validate") as scratch:
        recipe_path = _recipe_path(args["recipe"], scratch)
        # dry-run 也要给一个明确且不存在的 out：否则 prep 用时间戳默认值，
        # 同一秒内两次调用可能撞上已存在目录而抛 FileConflict（误报）。
        out_preview = scratch / "out_preview"
        user_out = args.get("out_dir")
        out_path = Path(user_out).expanduser().resolve() if user_out else out_preview
        summary = _run_prep(recipe_path=recipe_path, write=False, out=out_path,
                            pp_dir=pp_dir, potcar_mode=potcar_mode)

        incar_diff = ""
        diff_note = ""
        if include_diff:
            try:
                incar_diff = _incar_diff_preview(recipe_path, summary["structure"]["natoms"][1])
                if not incar_diff:
                    diff_note = "新 INCAR 与底稿完全一致（没有行级改动）。"
            except (PrepError, OSError, ValueError) as exc:
                # diff 只是展示层，算不出来不应让"校验"整体失败；明确标注原因。
                diff_note = f"diff 预览生成失败（不影响校验结论）：{exc}"

    issues = summary.get("issues", [])
    result = {
        "ok": bool(summary.get("ok")),
        "dry_run": True,
        "recipe": summary["recipe"],
        "structure": summary["structure"],
        "ops": summary["ops"],
        "incar_changes": summary["incar_changes"],
        "incar_diff": incar_diff,
        "diff_note": diff_note,
        "kpoints": summary["kpoints"],
        "potcar": summary["potcar"],
        "issues": issues,
        "error_details": [i for i in issues if i.get("level") == "ERROR"],
        "errors": summary["errors"],
        "warnings": summary["warnings"],
        "out_dir_preview": summary["out_dir"],
        "message": summary.get("message", ""),
        "next_step": ("把 incar_diff、结构变化、issues 给用户看；确认无误后才调用 "
                      "write_inputs(confirm=true)。"),
    }
    if not result["ok"]:
        result["hint"] = "存在 ERROR：先改配方/结构，不要写盘。"
    return result


# ---------------------------------------------------------------------------
# 工具 6：write_inputs（mutating，必须 confirm=true）
# ---------------------------------------------------------------------------

def write_inputs(args: dict) -> dict:
    """把已确认的配方写成一个全新的算例目录（POSCAR/INCAR/KPOINTS/POTCAR.spec/报告）。

    做什么：confirm 必须为 true；目标目录必须不存在；调用 prep.run(write=True)。
    为什么两道闸：① confirm 是"人已看过 validate 结果"的显式声明，模型不能自己默认；
            ② 拒绝已存在目录，保证不覆盖任何已有算例（用户数据无价）。
    不这样做：模型一次幻觉就能改写用户跑了一半的算例目录。
    还有别的办法：失败时要求 --force；本工具故意不暴露 force，覆盖只能由人用 CLI 做。
    """
    if args.get("confirm") is not True:
        raise ToolError("confirm-required",
                        "write_inputs 会写文件，必须由人确认后显式传 confirm=true",
                        field="confirm")

    out_dir = Path(args["out_dir"]).expanduser().resolve()
    if out_dir.exists():
        raise ToolError("out-dir-exists",
                        f"输出目录已存在，拒绝覆盖：{out_dir}（请换一个新目录名）",
                        field="out_dir")

    potcar_mode = args.get("potcar_mode", "spec")
    pp_dir = args.get("pp_dir")

    with _scratch("write") as scratch:
        recipe_path = _recipe_path(args["recipe"], scratch)
        summary = _run_prep(recipe_path=recipe_path, write=True, out=out_dir,
                            pp_dir=pp_dir, potcar_mode=potcar_mode)

    diff_text = ""
    report_text = ""
    report_truncated = False
    diff_path = out_dir / "INCAR.diff"
    report_path = out_dir / "change_report.md"
    if diff_path.exists():
        diff_text = _read_text(diff_path)
    if report_path.exists():
        report_text = _read_text(report_path)
        if len(report_text) > 8000:
            report_text = report_text[:8000]
            report_truncated = True

    result = {
        "ok": True,
        "dry_run": False,
        "out_dir": str(out_dir),
        "files": summary.get("files", []),
        "structure": summary["structure"],
        "ops": summary["ops"],
        "incar_changes": summary["incar_changes"],
        "incar_diff": diff_text,
        "change_report": report_text,
        "change_report_truncated": report_truncated,
        "change_report_path": str(report_path) if report_path.exists() else None,
        "kpoints": summary["kpoints"],
        "potcar": summary["potcar"],
        "issues": summary.get("issues", []),
        "errors": summary["errors"],
        "warnings": summary["warnings"],
        "message": summary.get("message", ""),
        "next_step": ("人工检查 INCAR.diff 与 change_report.md；确认无误后再让用户决定是否提交。"
                      "本工具不会提交作业。"),
    }
    return result


# ---------------------------------------------------------------------------
# 工具 7：verify_run（只读）
# ---------------------------------------------------------------------------

def verify_run(args: dict) -> dict:
    """核验一次 VASP 计算是否真的收敛（进程完成 / 电子收敛 / 离子收敛三层）。

    做什么：调用 vasp_core.verify.verify_run，附中文报告。
    为什么单独一层："跑完了"不等于"收敛了"；能量未收敛时拿来比较会得出错误科学结论。
    不这样做：模型看到 OUTCAR 有 General timing 就宣布成功，用户拿未收敛能量去画趋势。
    还有别的办法：只 grep 关键词；verify 已经把误判陷阱（ZBRENT/EDIFF 等）处理过了。
    """
    run_dir = Path(args["run_dir"]).expanduser()
    if not run_dir.exists():
        raise ToolError("run-dir-not-found", f"算例目录不存在：{run_dir}", field="run_dir")
    if not run_dir.is_dir():
        raise ToolError("not-a-directory", f"不是目录：{run_dir}", field="run_dir")

    strict_ionic = bool(args.get("strict_ionic", True))
    try:
        files = find_outputs(run_dir)
        # 核心 verify_run 在"没有任何可用数据源"时会因为没有 final_energy 键而抛
        # KeyError（这是内核的一个边角缺陷，本任务禁止改核心模块）。工具层先判断
        # 数据源是否可用，不可用时自己给出"无法判定"的结构化结果，避免把 KeyError
        # 暴露成 internal-error，也避免让模型误以为脚本崩了。
        usable = bool(files["outcar"] or files["oszicar"]
                      or (files["vasprun"] and has_pymatgen()))
        if usable:
            info = core_verify_run(run_dir, strict_ionic=strict_ionic)
        else:
            info = {
                "run_dir": str(run_dir.resolve()),
                "files": {k: (str(v) if v else None) for k, v in files.items()},
                "source": None,
                "converged": False,
                "notes": ["目录里没有可用的 vasprun.xml / OUTCAR / OSZICAR，无法判定。"],
            }
    except InputError as exc:
        raise ToolError("invalid-input", str(exc), field="run_dir") from exc

    result = dict(info)
    result.setdefault("final_energy", None)
    result["verdict"] = "已收敛" if info.get("converged") else "未收敛或无法判定"
    result["status"] = "converged" if info.get("converged") else "not-converged"
    if strict_ionic and info.get("n_ionic_steps") == 0:
        result.setdefault("notes", []).append(
            "该算例没有离子步；若是静态计算，请用 strict_ionic=false 重新核验。")
    if args.get("include_report", True):
        result["report"] = render_verify_report(info)
    result["next_step"] = ("收敛≠结论正确：还要检查 ENCUT、K 点密度、真空层、磁序是否合理；"
                           "未收敛时不要用能量做比较。")
    return result


# ---------------------------------------------------------------------------
# 工具 8：submit_hint（不执行任何命令）
# ---------------------------------------------------------------------------

def submit_hint(args: dict) -> dict:
    """生成把算例推送到超算并提交的 cluster dry-run 命令文本（纯文本，绝不执行）。

    做什么：校验 run_dir/主机/路径的安全字符，拼出 `python -m vasp_core.cluster up …
            --dry-run` 命令，附人工检查清单。
    为什么只给 dry-run：工具没有任何执行 ssh/scp/qsub 的代码路径，从源头杜绝"模型把
            作业提交出去"；cluster 的 dry-run 本身也只打印、不执行。
    不这样做：一旦工具能执行命令，提示词注入或模型误判就可能直接占用机时/覆盖远端数据。
    还有别的办法：给 MCP 服务器加 ask 权限；但本项目要求零依赖、离线可复现，纯文本最稳。
    """
    run_dir = Path(args["run_dir"]).expanduser().resolve()
    if not run_dir.exists():
        raise ToolError("run-dir-not-found", f"算例目录不存在：{run_dir}", field="run_dir")
    if not run_dir.is_dir():
        raise ToolError("not-a-directory", f"不是目录：{run_dir}", field="run_dir")

    host = args["host"].strip()
    if not _HOST_RE.match(host):
        raise ToolError("invalid-host",
                        f"host 只允许 user@hostname / IP 形式（不能含空格与 shell 元字符）：{host!r}",
                        field="host")

    remote = args.get("remote", CLUSTER_DEFAULTS["remote"])
    pp_dir = args.get("pp_dir", CLUSTER_DEFAULTS["pp_dir"])
    script = args.get("script", "vasp.sh")
    for field, value, regex in (("remote", remote, _REMOTE_PATH_RE),
                                ("pp_dir", pp_dir, _REMOTE_PATH_RE),
                                ("script", script, _SCRIPT_RE)):
        if not regex.match(value):
            raise ToolError("invalid-path",
                            f"{field} 含不允许的字符（空白/分号/引号等）：{value!r}", field=field)

    # 命令拼接：只有 run_dir 是本地路径（可能含空格），用 shlex.quote；
    # 其余字段已通过白名单，不需要也不应该加引号（避免引号破坏超算端的 ~ 展开）。
    command = (
        "python -m vasp_core.cluster up "
        f"{shlex.quote(str(run_dir))} "
        f"--host {host} --remote {remote} --pp-dir {pp_dir} --script {script} --dry-run"
    )

    preflight = {
        "has_poscar": (run_dir / "POSCAR").exists(),
        "has_incar": (run_dir / "INCAR").exists(),
        "has_kpoints": (run_dir / "KPOINTS").exists(),
        "has_potcar": (run_dir / "POTCAR").exists(),
        "has_potcar_spec": (run_dir / "POTCAR.spec").exists(),
    }
    missing = [name for name in ("has_poscar", "has_incar") if not preflight[name]]

    return {
        "run_dir": str(run_dir),
        "host": host,
        "executed": False,
        "command": {
            "shell": "bash / PowerShell",
            "preview": command,
            "note": "这是 --dry-run 预览命令：cluster 只会打印它将要执行的远程操作，不会真的连超算。",
        },
        "preflight": preflight,
        "missing_required": missing,
        "checklist": [
            "已经用 write_inputs 生成新目录，且 INCAR.diff / change_report.md 已人工看过",
            "确认 POTCAR 的来源与元素顺序（本地只有 POTCAR.spec 时，超算会按 spec 拼）",
            "确认超算上作业脚本名、队列/核数、赝势目录路径",
            "先跑 --dry-run 核对命令，再由人手动执行真正的提交",
        ],
        "warning": ("本工具不会执行任何命令；真正的提交必须由人完成。"
                    if not missing else f"目录里缺少必需文件：{missing}，建议先写算例再提交。"),
        "manual_after_review": "人工复核预检结果和 dry-run 输出后，再由人去掉 --dry-run 执行；本工具不会代为提交任何作业。",
    }


# ---------------------------------------------------------------------------
# 工具注册表 + OpenAI/MCP 两种 schema 视图
# ---------------------------------------------------------------------------

def _schema(properties: dict[str, dict], required: list[str] | None = None) -> dict:
    """构造本项目统一的 JSON Schema：对象、字段表、必填、禁止额外字段。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: dict[str, dict] = {}


def _register(name: str, description: str, parameters: dict,
              handler: Callable[[dict], dict], *, mutating: bool) -> None:
    """注册一个工具；注册表是 dispatch 与两种 schema 输出的唯一数据源。"""
    TOOLS[name] = {
        "name": name,
        "description": description,
        "parameters": parameters,
        "handler": handler,
        "mutating": mutating,
    }


_register(
    "workspace_status",
    "盘点工作区（只读）：列出配方 JSON、POSCAR/CONTCAR、INCAR、KPOINTS、算例输出目录，"
    "并给出 Python、VASP_HOST、赝势目录环境变量与 cluster 默认值。"
    "不确定该读哪个文件时先调它。",
    _schema({
        "workspace": {"type": "string", "minLength": 1, "default": ".",
                      "description": "要盘点的目录（默认当前目录）"},
        "max_depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2,
                      "description": "递归深度（默认 2）"},
        "max_entries": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200,
                        "description": "最多列出的文件数（默认 200）"},
    }),
    workspace_status,
    mutating=False,
)

_register(
    "read_structure",
    "读 POSCAR/CONTCAR（只读）：返回结构摘要、晶格矩阵、前 N 个原子的分数坐标与固定标志。"
    "配方里的 index 一律 1 基，与本工具 index 一致。",
    _schema({
        "path": {"type": "string", "minLength": 1, "description": "POSCAR/CONTCAR 文件路径"},
        "max_atoms": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 20,
                      "description": "返回多少个原子的明细（默认 20）"},
    }, ["path"]),
    read_structure,
    mutating=False,
)

_register(
    "read_incar",
    "解析 INCAR（只读）：返回键值、重复键、未知键、危险键（如 LDAU/NELECT）。"
    "用于判断现有 INCAR 能不能作为配方底稿。",
    _schema({
        "path": {"type": "string", "minLength": 1, "description": "INCAR 文件路径"},
        "include_lines": {"type": "boolean", "default": False,
                          "description": "是否附带逐行原始内容（默认 false）"},
    }, ["path"]),
    read_incar,
    mutating=False,
)

_register(
    "propose_recipe",
    "把中文需求转成候选 recipe 对象（只读、不写文件）。只处理工具能确定的规则；"
    "slab/DFT+U/赝势/收敛测试等需要人决策的内容会放进 needs_human，不要自行补全。",
    _schema({
        "text": {"type": "string", "minLength": 1, "maxLength": 4000,
                 "description": "用户的中文需求，如『把铜扩成 2x2x2 超胞，ENCUT 600』"},
        "base_recipe": {"type": "string", "minLength": 1,
                        "description": "可选：已有配方 JSON 路径，新要求合并到它上面"},
    }, ["text"]),
    propose_recipe,
    mutating=False,
)

_register(
    "validate_recipe",
    "对配方做完整 dry-run 校验（只读用户目录）：输出结构变化、INCAR 行级 diff、KPOINTS、"
    "POTCAR spec 与全部 issues。这是写盘前的最后一道检查，ERROR 未清除就不要写。",
    _schema({
        "recipe": {"oneOf": [{"type": "string", "minLength": 1}, {"type": "object"}],
                   "description": "配方 JSON 路径，或配方对象（通常来自 propose_recipe）"},
        "out_dir": {"type": "string", "minLength": 1,
                    "description": "可选：预览将来要写入的目录（不会创建）"},
        "pp_dir": {"type": "string", "minLength": 1,
                   "description": "可选：赝势库目录，覆盖配方/环境变量"},
        "potcar_mode": {"type": "string", "enum": ["auto", "spec", "skip"], "default": "spec",
                        "description": "auto=本地拼 POTCAR；spec=只给 spec（默认）；skip=不碰赝势"},
        "include_diff": {"type": "boolean", "default": True,
                         "description": "是否生成 INCAR 行级 diff（默认 true）"},
    }, ["recipe"]),
    validate_recipe,
    mutating=False,
)

_register(
    "write_inputs",
    "把已确认的配方写成全新算例目录（会写文件！）。必须 confirm=true，且 out_dir 必须"
    "尚不存在；不会覆盖、不会提交作业。写完后返回 INCAR.diff 与 change_report.md 内容供人复核。",
    _schema({
        "recipe": {"oneOf": [{"type": "string", "minLength": 1}, {"type": "object"}],
                   "description": "配方 JSON 路径或配方对象"},
        "out_dir": {"type": "string", "minLength": 1,
                    "description": "新的输出目录（必须不存在）"},
        "confirm": {"type": "boolean",
                    "description": "必须为 true：表示人已看过 validate 结果并同意写盘"},
        "pp_dir": {"type": "string", "minLength": 1,
                   "description": "可选：赝势库目录"},
        "potcar_mode": {"type": "string", "enum": ["auto", "spec", "skip"], "default": "spec",
                        "description": "auto=本地拼 POTCAR；spec=只给 spec（默认）；skip=不碰赝势"},
    }, ["recipe", "out_dir", "confirm"]),
    write_inputs,
    mutating=True,
)

_register(
    "verify_run",
    "核验算例结果（只读）：分『进程完成 / 电子收敛 / 离子收敛』三层判断，给出能量、"
    "每原子能量、残余力与中文报告。跑完 ≠ 收敛，未收敛不要比较能量。",
    _schema({
        "run_dir": {"type": "string", "minLength": 1, "description": "算例目录"},
        "strict_ionic": {"type": "boolean", "default": True,
                         "description": "静态计算（NSW=0）传 false，否则会因无离子步判为未收敛"},
        "include_report": {"type": "boolean", "default": True,
                           "description": "是否附带中文 markdown 报告（默认 true）"},
    }, ["run_dir"]),
    verify_run,
    mutating=False,
)

_register(
    "submit_hint",
    "生成把算例推到超算的 cluster --dry-run 命令文本（绝不执行 ssh/scp/qsub）。"
    "只输出预览命令与人工检查清单；真正提交必须由人完成。",
    _schema({
        "run_dir": {"type": "string", "minLength": 1, "description": "已生成好的本机算例目录"},
        "host": {"type": "string", "minLength": 1, "maxLength": 128,
                 "description": "超算登录串 user@host"},
        "remote": {"type": "string", "minLength": 1, "default": CLUSTER_DEFAULTS["remote"],
                   "description": "超算工作根目录（默认 ~/vasp-work）"},
        "pp_dir": {"type": "string", "minLength": 1, "default": CLUSTER_DEFAULTS["pp_dir"],
                   "description": "超算赝势目录（默认 ~/Optpaw_PBE.54）"},
        "script": {"type": "string", "minLength": 1, "default": "vasp.sh",
                   "description": "作业脚本文件名（默认 vasp.sh）"},
    }, ["run_dir", "host"]),
    submit_hint,
    mutating=False,
)


def tool_specs_openai() -> list[dict]:
    """OpenAI /chat/completions 的 tools 数组（函数调用格式）。

    做什么：把注册表转成 [{"type": "function", "function": {name, description, parameters}}]。
    为什么单独一个函数：注册表是唯一数据源，OpenAI 与 MCP 两种视图从这里派生，
            以后加工具只改 _register，不会出现两套 schema 漂移。
    不这样做：手写两份 schema，改一处漏一处，模型按旧 schema 传参直接被拒。
    还有别的办法：运行时从 MCP tools/list 反推；多一层序列化，没必要。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in TOOLS.values()
    ]


def tool_specs_mcp() -> list[dict]:
    """MCP tools/list 的 tools 数组（name/description/inputSchema）。

    做什么：同一份注册表转成 MCP 2024-11-05 的 Tool 结构。
    为什么 inputSchema 直接复用 JSON Schema：MCP 的 inputSchema 本来就是 JSON Schema；
            复用能保证参数校验与模型看到的 schema 完全一致。
    不这样做：另写一套"简化 schema"，校验和展示不一致，模型会按展示的传参。
    还有别的办法：MCP 也支持 outputSchema（本版不必要）。
    """
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["parameters"],
        }
        for tool in TOOLS.values()
    ]


# ---------------------------------------------------------------------------
# 统一分发
# ---------------------------------------------------------------------------

def _failure(name: str, code: str, message: str, *, field: str | None = None,
             detail: str | None = None) -> dict:
    return {
        "ok": False,
        "tool": name,
        "error": {"code": code, "message": message, "field": field, "detail": detail},
    }


def dispatch(name: str, args: dict | None = None) -> dict:
    """校验并执行一个工具；任何情况下都返回 dict，不抛异常。

    做什么：查表 → 校验参数 → 调 handler → 捕获 ToolError/PrepError/未知异常。
    为什么绝不抛异常：工具结果要原样回传给模型/宿主，抛异常会让整个对话回合崩掉，
            而且模型看不到"为什么被拒绝"，只会反复重试同一个错误调用。
    不这样做：一个拼错的参数就中断 agent 循环，用户还得重跑。
    还有别的办法：让宿主捕获异常再包装；MCP 和 OpenAI 两个宿主各写一遍，容易不一致。
    """
    tool = TOOLS.get(name)
    if tool is None:
        allowed = "、".join(TOOLS)
        return _failure(name, "unknown-tool", f"未知工具 {name!r}；可用工具：{allowed}")

    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _failure(name, "invalid-arguments", "工具参数必须是 JSON 对象")

    try:
        cleaned = _validate_arguments(tool["parameters"], args)
    except ToolError as exc:
        return _failure(name, exc.code, exc.message, field=exc.field, detail=exc.detail)

    try:
        result = tool["handler"](cleaned)
    except ToolError as exc:
        return _failure(name, exc.code, exc.message, field=exc.field, detail=exc.detail)
    except PrepError as exc:
        code, message, detail = _map_exception(exc)
        return _failure(name, code, message, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 工具层必须把未知异常转成结构化结果
        code, message, detail = _map_exception(exc)
        return _failure(name, code, message, detail=detail)

    return {"ok": True, "tool": name, "mutating": bool(tool["mutating"]), "result": result}
