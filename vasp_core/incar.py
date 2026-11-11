#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""incar.py —— INCAR 的解析、类型校验、白名单与"可审阅的修改"。

和 v1 的区别（为什么重写）：
  v1 只是"按键名白名单 + 字符串替换"，会把 MAGMOM 的列表写成
  `MAGMOM = [0.6, 0.6]`（VASP 不认），也不检查 ISMEAR=-5 配 NSW>0 这类致命组合。
  v2 给每个键定义类型/取值范围，并在写盘前做跨键、跨文件校验。

设计原则：
  1. 白名单内的键才允许"自动改"；白名单外一律拒绝（防止模型瞎写）。
  2. 危险键（DFT+U、泛函、电荷、vdW）永远拒绝自动改，只提示人工。
  3. 已有 INCAR 作为底稿时，未知键、注释、行序原样保留，只调整目标键。
  4. 每一处修改都产出 (key, old, new)，写进 diff 与 change_report。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import PolicyError

# ---------------------------------------------------------------------------
# 键的类型定义
# ---------------------------------------------------------------------------

# 每个键：type（float/int/bool/enum/str/float_list）、range、enum、unit、note
# 【因】类型写错是 INCAR 最常见的"沉默错误"（VASP 要么忽略、要么用默认值继续算）
INCAR_TYPES: dict[str, dict[str, Any]] = {
    "ENCUT":   {"type": "float", "range": (50.0, 3000.0), "unit": "eV", "note": "平面波截断能"},
    "EDIFF":   {"type": "float", "range": (1e-9, 1.0), "unit": "eV", "note": "电子步收敛判据"},
    "EDIFFG":  {"type": "float", "range": (-10.0, 1.0), "unit": "eV/Å", "note": "离子步判据；负值=力"},
    "PREC":    {"type": "enum", "enum": ["Low", "Normal", "Accurate", "Single"]},
    "ALGO":    {"type": "enum", "enum": ["Normal", "Fast", "VeryFast", "All", "Damped", "Subrot", "Eigenval", "None", "CHI", "BSE"]},
    "ISPIN":   {"type": "int", "enum": [1, 2]},
    "MAGMOM":  {"type": "float_list", "note": "逐原子或按元素展开的初始磁矩"},
    "LREAL":   {"type": "enum", "enum": ["Auto", "TRUE", "FALSE", ".TRUE.", ".FALSE."]},
    "NELM":    {"type": "int", "range": (1, 10000)},
    "KSPACING": {"type": "float", "range": (0.01, 2.0), "unit": "Å^-1"},
    "LWAVE":   {"type": "bool"},
    "LCHARG":  {"type": "bool"},
    "ISMEAR":  {"type": "int", "enum": [-5, -4, -1, 0, 1, 2]},
    "SIGMA":   {"type": "float", "range": (0.001, 2.0), "unit": "eV"},
    "ISIF":    {"type": "int", "enum": [0, 1, 2, 3, 4, 5, 6, 7]},
    "IBRION":  {"type": "int", "enum": [-1, 0, 1, 2, 3, 5, 6, 7, 8, 44]},
    "NSW":     {"type": "int", "range": (0, 100000)},
    "LORBIT":  {"type": "int", "enum": [0, 1, 2, 5, 10, 11, 12]},
    "ICHARG":  {"type": "int", "enum": [0, 1, 2, 3, 4, 5, 10, 11, 12]},
    "LMAXMIX": {"type": "int", "enum": [0, 2, 4, 6]},
    "LASPH":   {"type": "bool"},
    "NCORE":   {"type": "int", "range": (1, 256)},
    "NPAR":    {"type": "int", "range": (1, 256)},
    "KPAR":    {"type": "int", "range": (1, 256)},
    "GGA":     {"type": "enum", "enum": ["PE", "PS", "CA", "RP", "AM05", "BO", "OR"]},
    "AMIX":    {"type": "float", "range": (0.01, 1.0)},
    "BMIX":    {"type": "float", "range": (0.001, 5.0)},
    "ISYM":    {"type": "int", "enum": [0, 1, 2, 3]},
    "SYMPREC": {"type": "float", "range": (1e-8, 1e-2)},
    "ADDGRID": {"type": "bool"},
    "LVTOT":   {"type": "bool"},
    "LVHAR":   {"type": "bool"},
    "LAECHG":  {"type": "bool"},
    "EMIN":    {"type": "float", "range": (-50.0, 20.0), "unit": "eV"},
    "EMAX":    {"type": "float", "range": (-20.0, 50.0), "unit": "eV"},
    "NEDOS":   {"type": "int", "range": (1, 100000)},
    "POTIM":   {"type": "float", "range": (1e-4, 10.0)},
    "SMASS":   {"type": "float", "range": (-3.0, 3.0)},
    "TEBEG":   {"type": "float", "range": (0.0, 10000.0), "unit": "K"},
    "TEEND":   {"type": "float", "range": (0.0, 10000.0), "unit": "K"},
    "MDALGO":  {"type": "int", "enum": [0, 1, 2, 3, 11, 21, 13]},
    # vdW 色散校正。取值来自 VASP 官方 wiki（https://vasp.at/wiki/index.php/IVDW）：
    #   0=不加；1/10=DFT-D2；11=D3 零阻尼；12=D3(BJ)；20=TS；21=TS/HI；
    #   202=MBD@rsSCS；263=MBD@rSC/FI；3=DFT-ulg；4=dDsC；13=D4；14=libMBD；15=simple-D3。
    # 【为什么不放进危险键】你们组日常就改它，属于"常用设置"；
    #   但它又是科学选择（用哪种色散），所以规则是"必须你明确点名才写"（见 EXPLICIT_KEYS）。
    "IVDW":    {"type": "int", "enum": [0, 1, 2, 3, 4, 10, 11, 12, 13, 14, 15, 20, 21, 202, 263],
                "note": "vdW 色散校正方法；11=D3 零阻尼，12=D3(BJ)，20=TS，202=MBD"},
}

# 危险键：任何情况下都不自动写，只提示人工
DANGEROUS_KEYS = {
    "LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ", "LDAUPRINT",
    "NELECT", "NELMIN", "LHFCALC", "AEXX", "HFSCREEN", "METAGGA",
    "VDW_RADIUS", "VDW_SCALING", "LUSE_VDW", "GGA",
}

# 显式键：只有用户原话里明确提到（"加 D3 修正"/"IVDW=12"）才允许写；
# 模型自己"顺手加一个"会被服务层拦掉（service.py 里检查原话关键词）。
EXPLICIT_KEYS = {"IVDW"}

# 允许"自然语言自动改"的键：有类型定义的键，去掉危险键
ALLOWED_OVERRIDE_KEYS = set(INCAR_TYPES) - DANGEROUS_KEYS

# 预设：保守、可解释，不包含任何危险键
PRESETS: dict[str, dict[str, Any]] = {
    "relax": {
        "ENCUT": 520, "EDIFF": 1e-5, "EDIFFG": -0.02, "PREC": "Accurate",
        "ALGO": "Normal", "ISMEAR": 0, "SIGMA": 0.05, "ISIF": 3,
        "IBRION": 2, "NSW": 100, "LREAL": "Auto", "LWAVE": False, "LCHARG": True,
    },
    "static": {
        "ENCUT": 520, "EDIFF": 1e-6, "PREC": "Accurate", "ALGO": "Normal",
        "ISMEAR": -5, "SIGMA": 0.05, "IBRION": -1, "NSW": 0,
        "LREAL": "Auto", "LWAVE": False, "LCHARG": True,
    },
    "band": {
        "ENCUT": 520, "EDIFF": 1e-6, "PREC": "Accurate", "ALGO": "Normal",
        "ISMEAR": 0, "SIGMA": 0.05, "ICHARG": 11, "IBRION": -1, "NSW": 0,
        "LORBIT": 11, "LWAVE": False, "LCHARG": False,
    },
    # "none" = 不套任何预设，只改用户明确要求的键。
    # 【为什么需要它】"扩成 2x2x1"这种纯结构需求不应该顺手往 INCAR 里塞
    #   ISIF/IBRION/NSW 等 10 个键——那是改计算设置，不是改结构。
    # 【不这么干会怎样】用户只想换原子，结果 INCAR 被整篇改写，还得逐行核对。
    "none": {},
}

DEFAULT_INCAR: dict[str, Any] = dict(PRESETS["relax"])


# ---------------------------------------------------------------------------
# 行模型与解析
# ---------------------------------------------------------------------------

@dataclass
class Line:
    """一行 INCAR：要么是注释/空行（key=None），要么是 key = value。"""

    key: str | None
    value: str | None
    raw: str
    # 是否被本次修改过：没改过的行写回时用 raw（保留原缩进/对齐），
    # 否则 diff 里会出现"整文件重排版"的假改动，掩盖真正改了哪一行。
    changed: bool = False
    # 原行的前导空白与行内注释：改值时只替换数字，保留 "   ENCUT = 520  # 说明" 的样子
    indent: str = ""
    note: str = ""


@dataclass
class Change:
    key: str
    old: str
    new: str
    reason: str = "override"

    def as_dict(self) -> dict:
        return {"key": self.key, "old": self.old, "new": self.new, "reason": self.reason}


def parse_incar(text: str) -> list[Line]:
    """解析 INCAR，保留注释与行序。

    规则：含 "=" 的行是参数；以 # 或 ! 开头的是注释；其余原样保留。
    【坑】VASP 的续行（下一行以空格开头）这里不合并 —— 续行只出现在长列表里，
          本工具写 MAGMOM 时会自己控制格式，不依赖用户原文件的续行。
    """
    out: list[Line] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            out.append(Line(None, None, raw))
            continue
        if "=" not in raw:
            out.append(Line(None, None, raw))
            continue
        k, v = raw.split("=", 1)
        key = k.strip()
        indent = raw[: len(raw) - len(raw.lstrip())]
        # 行内注释（"ENCUT = 400 # 说明"）单独存起来，改值时原样带回
        pos = [p for p in (v.find("#"), v.find("!")) if p >= 0]
        cut = min(pos) if pos else -1
        note = v[cut:].rstrip() if cut >= 0 else ""
        val = (v if cut < 0 else v[:cut]).strip()
        out.append(Line(key, val, raw, False, indent, note))
    return out


def render_incar(lines: Iterable[Line]) -> str:
    """写回文本：改过的参数行用 "KEY = VALUE"，其余行逐字节保留原文。

    【因】diff 是给人看的。如果没改的行也重新排版（去缩进、去行尾空格），
         一份 INCAR 会整篇飘红，真正改动的那一行反而被淹没。
    【坑】行尾空格、制表符、末尾空行都算原文的一部分，必须原样写回。
    """
    parts = []
    for ln in lines:
        if ln.key is None or not ln.changed:
            parts.append(ln.raw)
        else:
            note = f"  {ln.note}" if ln.note else ""
            parts.append(f"{ln.indent}{ln.key} = {ln.value}{note}")
    if not parts:
        return ""
    text = "\n".join(parts)
    if parts[-1].strip() == "":
        # 原文末尾有空行：join 只补了分隔符，这里再补一个换行才是原来的两个 \n
        return text + "\n"
    return text if text.endswith("\n") else text + "\n"


def upsert(lines: list[Line], key: str, value: str, reason: str = "override") -> tuple[list[Line], Change]:
    """存在则替换值（保留位置与大小写），不存在则追加到末尾。"""
    ku = key.upper()
    for i, ln in enumerate(lines):
        if ln.key is not None and ln.key.upper() == ku:
            old = ln.value or ""
            note = f"  {ln.note}" if ln.note else ""
            lines[i] = Line(ln.key, value, f"{ln.indent}{ln.key} = {value}{note}", True, ln.indent, ln.note)
            return lines, Change(ku, old, value, reason)
    lines.append(Line(ku, value, f"{ku} = {value}", True))
    return lines, Change(ku, "(不存在)", value, reason)


# ---------------------------------------------------------------------------
# 值规范化与校验
# ---------------------------------------------------------------------------

_TRUE = {"TRUE", ".TRUE.", "T", "YES", "Y", "1"}
_FALSE = {"FALSE", ".FALSE.", "F", "NO", "N", "0"}


def _fmt_value(v: Any, spec: dict, natoms: int | None) -> str:
    """把 JSON 里的值格式化成 VASP 能读的字符串；同时做类型/范围检查。"""
    t = spec["type"]
    if t == "float":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            try:
                v = float(str(v).strip())
            except ValueError as exc:
                raise PolicyError(f"该键需要数值，收到 {v!r}") from exc
        lo, hi = spec.get("range", (float("-inf"), float("inf")))
        if not (lo <= float(v) <= hi):
            raise PolicyError(f"数值 {v} 超出允许范围 [{lo}, {hi}]")
        # ENCUT/NCORE 这类整数语义的键，用 int 更自然
        return f"{float(v):g}"

    if t == "int":
        if isinstance(v, bool):
            raise PolicyError("该键需要整数，收到布尔值")
        try:
            iv = int(str(v).strip())
        except (TypeError, ValueError) as exc:
            raise PolicyError(f"该键需要整数，收到 {v!r}") from exc
        if "enum" in spec and iv not in spec["enum"]:
            raise PolicyError(f"取值 {iv} 不在允许集合 {spec['enum']} 中")
        if "range" in spec:
            lo, hi = spec["range"]
            if not (lo <= iv <= hi):
                raise PolicyError(f"整数 {iv} 超出允许范围 [{lo}, {hi}]")
        return str(iv)

    if t == "bool":
        if isinstance(v, bool):
            return ".TRUE." if v else ".FALSE."
        s = str(v).strip().upper()
        if s in _TRUE:
            return ".TRUE."
        if s in _FALSE:
            return ".FALSE."
        raise PolicyError(f"该键需要布尔值（True/False/.TRUE./.FALSE.），收到 {v!r}")

    if t == "enum":
        sv = str(v).strip()
        for cand in spec["enum"]:
            if str(cand).lower() == sv.lower():
                return str(cand)
        raise PolicyError(f"取值 {v!r} 不在允许集合 {spec['enum']} 中")

    if t == "float_list":
        return _fmt_magmom(v, natoms)

    return str(v)


def _fmt_magmom(v: Any, natoms: int | None) -> str:
    """MAGMOM 的三种写法：
      - 数字：所有原子同一个初始磁矩；
      - 数字列表：长度 = 原子数（逐原子）或元素数（按元素，需 natoms 才能展开）；
      - 字符串：原样写（高级用户），但会做基本合法性检查。
    输出统一的空格分隔字符串 —— VASP 只认这种。
    """
    if isinstance(v, str):
        toks = v.replace(",", " ").split()
        if not toks or any(_to_float(t) is None for t in toks):
            raise PolicyError(f"MAGMOM 字符串里有非数字：{v!r}")
        return " ".join(toks)
    if isinstance(v, bool):
        raise PolicyError("MAGMOM 不能是布尔值")
    if isinstance(v, (int, float)):
        if natoms is None:
            raise PolicyError("MAGMOM 用标量展开时需要知道原子数")
        return " ".join(f"{float(v):g}" for _ in range(natoms))
    if isinstance(v, (list, tuple)):
        vals = []
        for x in v:
            if isinstance(x, bool):
                raise PolicyError("MAGMOM 列表里不能有布尔值")
            fx = _to_float(x)
            if fx is None:
                raise PolicyError(f"MAGMOM 列表里有非数字：{x!r}")
            vals.append(fx)
        if not vals:
            raise PolicyError("MAGMOM 列表是空的")
        return " ".join(f"{x:g}" for x in vals)
    raise PolicyError(f"MAGMOM 的类型不支持：{type(v).__name__}")


def _to_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Issue:
    level: str   # PASS / WARN / ERROR
    code: str
    message: str


def validate_overrides(overrides: dict[str, Any], natoms: int | None) -> list[Issue]:
    """只校验"用户配方里显式给出的键"，返回问题列表（不抛异常的用 Issue）。"""
    issues: list[Issue] = []
    for k, v in overrides.items():
        ku = str(k).upper()
        if ku in DANGEROUS_KEYS:
            issues.append(Issue("ERROR", "dangerous-key", f"{ku} 属于危险键，禁止自动写入（DFT+U/泛函/电荷/vdW）"))
            continue
        if ku not in INCAR_TYPES:
            issues.append(Issue("ERROR", "unknown-key", f"{ku} 不在允许的键表里"))
            continue
        try:
            _fmt_value(v, INCAR_TYPES[ku], natoms)
        except PolicyError as exc:
            issues.append(Issue("ERROR", f"bad-value:{ku}", str(exc)))
    return issues


# ---------------------------------------------------------------------------
# 构建最终 INCAR
# ---------------------------------------------------------------------------

def build_incar(
    *,
    base_text: str | None,
    preset: str | None,
    overrides: dict[str, Any],
    natoms: int,
) -> tuple[str, list[Change], list[Issue], list[Line]]:
    """默认值 → 预设 → 用户覆盖，全部经过白名单和类型检查。

    返回：(最终文本, 变更列表, 问题列表, 最终行列表)
    【因】调用方（prep.py）要拿"变更列表"写 diff/报告，拿"问题列表"决定是否中止
    """
    issues: list[Issue] = []
    if base_text is not None:
        lines = parse_incar(base_text)
        base_note = "以现有 INCAR 为底稿（未知键/注释/顺序保留）"
    else:
        lines = [Line(k, _fmt_value(v, INCAR_TYPES[k], natoms), f"{k} = {_fmt_value(v, INCAR_TYPES[k], natoms)}")
                 for k, v in DEFAULT_INCAR.items()]
        base_note = "没有底稿 INCAR，使用内置保守默认值（relax 预设）"

    changes: list[Change] = []

    if preset is not None:
        if preset not in PRESETS:
            raise PolicyError(f"未知 preset：{preset}（可用：{sorted(PRESETS)}）")
        for k, v in PRESETS[preset].items():
            sval = _fmt_value(v, INCAR_TYPES[k], natoms)
            lines, ch = upsert(lines, k, sval, reason=f"preset:{preset}")
            changes.append(ch)

    for k, v in overrides.items():
        ku = str(k).upper()
        if ku in DANGEROUS_KEYS:
            raise PolicyError(
                f"拒绝自动写入危险键 {ku}：DFT+U/泛函/电荷/vdW 类设置必须人工确认\n"
                f"（这类参数写错不会报错，但结果物理上不可信）"
            )
        if ku not in INCAR_TYPES:
            raise PolicyError(
                f"键 {ku} 不在白名单里。允许的键：{sorted(INCAR_TYPES)}"
            )
        try:
            sval = _fmt_value(v, INCAR_TYPES[ku], natoms)
        except PolicyError as exc:
            # 把键名带上：只说"取值 99 不在允许集合"用户不知道是哪个键
            raise PolicyError(f"{ku}：{exc}") from exc
        lines, ch = upsert(lines, ku, sval, reason="override")
        changes.append(ch)
    if base_text is not None:
        issues.append(Issue("PASS", "base-incar", base_note))
    return render_incar(lines), _merge_changes(changes), issues, lines


def _same_value(a: str, b: str) -> bool:
    """判断两个 INCAR 值是否等价：先按数值比，再按大小写不敏感文本比。"""
    if a.strip() == b.strip():
        return True
    try:
        return abs(float(a.split()[0]) - float(b.split()[0])) < 1e-12
    except (ValueError, IndexError):
        return a.strip().lower() == b.strip().lower()


def _merge_changes(changes: list["Change"]) -> list["Change"]:
    """同一个键被"预设 + 覆盖"连续改两次时，合并成一行 old → new。

    【因】报告里出现两行 ENCUT 会让人误以为改了两次；
         真正要告诉人的是"从文件里的原值，最终变成了什么"。
    """
    merged: dict[str, Change] = {}
    order: list[str] = []
    for ch in changes:
        if ch.key not in merged:
            merged[ch.key] = Change(ch.key, ch.old, ch.new, ch.reason)
            order.append(ch.key)
        else:
            prev = merged[ch.key]
            merged[ch.key] = Change(prev.key, prev.old, ch.new, ch.reason)
    out: list[Change] = []
    for k in order:
        ch = merged[k]
        if _same_value(ch.old, ch.new):
            continue
        out.append(ch)
    return out


# ---------------------------------------------------------------------------
# 跨键 / 跨文件校验
# ---------------------------------------------------------------------------

def incar_dict(lines: list[Line]) -> dict[str, str]:
    return {ln.key.upper(): (ln.value or "") for ln in lines if ln.key is not None}


def check_consistency(
    *,
    incar: dict[str, str],
    natoms: int,
    counts: list[int] | None = None,
    species: list[str] | None = None,
    calc_type: str = "relax",
    potcar_enmax: float | None = None,
    has_kpoints_file: bool = False,
) -> list[Issue]:
    """写盘前的确定性检查：这些组合错了不会报错，但结果不可信。"""
    issues: list[Issue] = []

    def num(key: str) -> float | None:
        v = incar.get(key)
        if v is None:
            return None
        try:
            return float(v.split()[0])
        except (ValueError, IndexError):
            return None

    ispin = num("ISPIN")
    magmom = incar.get("MAGMOM")
    if ispin == 2:
        if not magmom:
            issues.append(Issue("ERROR", "ispin2-no-magmom",
                                "ISPIN=2 但没有 MAGMOM：VASP 会用默认初值，磁性结果可能完全错"))
        else:
            n = _count_values(magmom)
            if n not in (natoms, len(counts) if counts else -1):
                issues.append(Issue(
                    "ERROR", "magmom-length",
                    f"MAGMOM 个数 {n} ≠ 原子数 {natoms}（也不等于元素种数）。"
                    f"替位/空位/扩胞后必须重算 MAGMOM"))
    elif ispin is None:
        issues.append(Issue("WARN", "ispin-missing", "没有设置 ISPIN（默认 1，非自旋极化）"))

    ismear = num("ISMEAR")
    nsw = num("NSW") or 0
    if ismear == -5 and nsw > 0:
        issues.append(Issue("ERROR", "ismear-tetra-relax",
                            "ISMEAR=-5（四面体法）不能用于弛豫（NSW>0），力不准；静态计算才用它"))

    isif = num("ISIF")
    ibrion = num("IBRION")
    if isif is not None and isif > 0 and ibrion is not None and ibrion <= 0:
        issues.append(Issue("WARN", "isif-without-ibrion",
                            f"ISIF={isif:g}（会动晶胞）但 IBRION={ibrion:g}（不动离子），设置互相矛盾"))
    if nsw > 0 and ibrion == -1:
        issues.append(Issue("WARN", "nsw-with-ibrion-minus1",
                            "NSW>0 但 IBRION=-1：不会做离子弛豫，NSW 没有意义"))

    encut = num("ENCUT")
    if encut is None:
        issues.append(Issue("WARN", "encut-missing", "没有设置 ENCUT：VASP 会用 POTCAR 的最大 ENMAX"))
    elif encut < 200 or encut > 1000:
        issues.append(Issue("WARN", "encut-range", f"ENCUT={encut:g} eV 偏离常见区间 [200, 1000]"))
    if encut is not None and potcar_enmax is not None and encut < potcar_enmax:
        issues.append(Issue("ERROR", "encut-below-enmax",
                            f"ENCUT={encut:g} < POTCAR 的 ENMAX={potcar_enmax:g}：会严重欠收敛"))

    if incar.get("KSPACING") and has_kpoints_file:
        issues.append(Issue("ERROR", "kspacing-vs-kpoints",
                            "INCAR 有 KSPACING，同时目录里有 KPOINTS：两者冲突，VASP 优先用 KPOINTS"))

    icharg = num("ICHARG")
    if icharg == 11 and calc_type != "band":
        issues.append(Issue("WARN", "icharg11-not-band",
                            "ICHARG=11（读 CHGCAR 做非自洽）通常用于能带；当前 calc_type="
                            f"{calc_type}"))

    ldau_keys = [k for k in ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ") if k in incar]
    if ldau_keys and len(ldau_keys) < 4:
        issues.append(Issue("ERROR", "ldau-incomplete",
                            f"DFT+U 参数不全：只给了 {ldau_keys}，LDAU 必须同时给 LDAUTYPE/LDAUL/LDAUU/LDAUJ"))

    if "NELECT" in incar:
        issues.append(Issue("WARN", "nelect-manual",
                            "手动设置 NELECT（带电体系）：确认电荷量与补偿背景设置正确"))

    if species and incar.get("MAGMOM") and len(species) > 1:
        issues.append(Issue("WARN", "magmom-check-by-species",
                            f"MAGMOM 需要按 POSCAR 元素顺序（{species}）逐原子对齐；"
                            f"若是按元素写的，确认元素个数与 counts 一致"))

    # ---- vdW 色散校正（IVDW）相关 ----
    ivdw = num("IVDW")
    if ivdw is not None:
        if ivdw not in (0, 1, 2, 3, 4, 10, 11, 12, 13, 14, 15, 20, 21, 202, 263):
            issues.append(Issue("ERROR", "ivdw-invalid",
                                f"IVDW={ivdw:g} 不是 VASP 认识的取值（见 vasp.at/wiki/index.php/IVDW）"))
        luse_vdw = str(incar.get("LUSE_VDW", "")).strip().upper().strip(".")
        if ivdw > 0 and luse_vdw in ("TRUE", "T"):
            issues.append(Issue("WARN", "ivdw-vs-luse-vdw",
                                "同时开了 IVDW（原子对/多体色散校正）和 LUSE_VDW（非局域 vdW-DF 泛函）："
                                "两者是不同路线，通常只选一种，确认不是叠加误设"))
        if ivdw in (13, 14, 15):
            issues.append(Issue("WARN", "ivdw-external-package",
                                f"IVDW={ivdw:g} 依赖 VASP 编译时链接的外部包（DFT-D4 / libMBD / simple-D3）；"
                                "先确认集群上的 VASP 编了它，否则运行时会停"))
        if ivdw > 0:
            issues.append(Issue("PASS", "ivdw-on",
                                f"已启用色散校正 IVDW={ivdw:g}（{IVDW_NAMES.get(int(ivdw), '')}）"))

    if not [i for i in issues if i.level == "ERROR"]:
        issues.append(Issue("PASS", "incar-consistency", "INCAR 跨键检查通过"))
    return issues


# IVDW 取值 → 人话（用于日志/报告；与 VASP wiki 对照）
IVDW_NAMES = {
    0: "不校正", 1: "DFT-D2", 10: "DFT-D2", 2: "TS", 20: "TS", 21: "TS/HI",
    3: "DFT-ulg", 4: "dDsC", 11: "DFT-D3（零阻尼）", 12: "DFT-D3(BJ)",
    13: "DFT-D4", 14: "libMBD", 15: "simple-DFT-D3",
    202: "MBD@rsSCS", 263: "MBD@rSC/FI",
}


def _count_values(text: str) -> int:
    return len(text.replace(",", " ").split())
