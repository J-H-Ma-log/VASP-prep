#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""potcar.py —— 从赝势库目录拼 POTCAR，并校验"元素顺序"这个 VASP 头号沉默错误。

VASP 的规则（务必记住）：
  POTCAR 里的赝势顺序，必须与 POSCAR 第 6 行（元素行）的顺序完全一致。
  顺序错了 VASP 不会报错，它会用错的赝势把体系算完 —— 结果看起来正常，物理上全错。

所以本模块做三件事：
  1. 扫描赝势库（递归找名为 POTCAR 的文件，读 TITEL 得到元素与变体）；
  2. 按 POSCAR 的元素顺序挑文件并拼接；
  3. 拼完再读一遍 TITEL，逐行核对顺序，不一致就报错。

变体（Cu vs Cu_pv）是科学选择，默认按 Materials Project 习惯优先 `_pv`；
配方里可以用 potcar.map 显式指定，避免"默认帮我选了但选错"。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import StructureError

# 变体优先级：先找带 _pv 的（过渡金属常用），再找完全同名的，再其他
DEFAULT_PRIORITY = ["_pv", "", "_sv", "_d", "_h", "_3", "_2", "_s"]

_TITEL_RE = re.compile(r"^\s*(?:TITEL\s*=\s*)?([A-Za-z_]+)\s+([A-Za-z0-9_]+)\s+(\S+)?\s*$")
_ENMAX_RE = re.compile(r"ENMAX\s*=\s*([0-9.]+)")


@dataclass
class Entry:
    element: str      # 元素符号，如 Cu
    variant: str      # 变体后缀，如 _pv（无后缀就是 ""）
    title: str        # TITEL 的完整符号，如 Cu_pv
    path: Path
    enmax: float | None = None

    def as_dict(self) -> dict:
        return {"element": self.element, "variant": self.variant,
                "title": self.title, "path": str(self.path), "enmax": self.enmax}


def parse_potcar_header(path: str | Path) -> Entry | None:
    """读 POTCAR 头部，返回 (元素, 变体, TITEL, ENMAX)。

    POTCAR 头部两种写法都要认：
      `  PAW_PBE Cu_pv 06Sep2000`            （旧）
      `  TITEL  = PAW_PBE Cu_pv 06Sep2000`   （新）
    """
    p = Path(path)
    try:
        # 只读文件头 20 KB 足够找到 TITEL/ENMAX；不要用 [next(f)] 循环，
        # 因为文件短于 60 行时 StopIteration 会被外层 except 吞掉，导致整段解析被跳过。
        with p.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read(20000)
    except OSError:
        return None
    head = text.splitlines()[:60]
    enmax = None
    m = _ENMAX_RE.search(text)
    if m:
        try:
            enmax = float(m.group(1))
        except ValueError:
            enmax = None

    for line in head:
        s = line.strip()
        if not s or s.startswith("!"):
            continue
        m = _TITEL_RE.match(line)
        if not m:
            continue
        # 需要第二段像 "Cu_pv" 或 "Cu"，第一段像 PAW_PBE / PAW_GGA / US
        func, sym = m.group(1), m.group(2)
        if not func.upper().startswith(("PAW", "US", "NC")):
            continue
        if not re.match(r"^[A-Z][a-z]?", sym):
            continue
        element = sym.split("_")[0]
        variant = sym[len(element):]
        return Entry(element=element, variant=variant, title=sym, path=p, enmax=enmax)
    return None


def scan_library(root: str | Path, *, max_depth: int = 5) -> dict[str, list[Entry]]:
    """递归扫描赝势库，返回 {元素: [Entry, ...]}。

    为什么限制深度：赝势库可能有几万个文件，全盘 os.walk 会很慢；
    正常布局（potpaw_PBE.54/Cu/POTCAR）深度不超过 3。
    """
    base = Path(root)
    if not base.exists():
        raise StructureError(f"赝势库目录不存在：{base}")
    lib: dict[str, list[Entry]] = {}
    base_depth = len(base.parts)
    for dirpath, dirnames, filenames in os.walk(base):
        depth = len(Path(dirpath).parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            if fn.upper() != "POTCAR":
                continue
            e = parse_potcar_header(Path(dirpath) / fn)
            if e is None:
                continue
            lib.setdefault(e.element, []).append(e)
    if not lib:
        raise StructureError(
            f"在 {base} 下没找到任何 POTCAR 文件。\n"
            f"检查：ls {base} 看是否有 potpaw_PBE.54/ 之类的子目录；\n"
            f"或者用 --potcar spec 只生成清单，人工拼接。"
        )
    # 同一元素可能有多个版本（不同泛函/日期），按路径排序保证可复现
    for k in lib:
        lib[k].sort(key=lambda e: (e.variant, str(e.path)))
    return lib


def choose(lib: dict[str, list[Entry]], species: list[str], *,
           mapping: dict[str, str] | None = None,
           priority: list[str] | None = None) -> list[Entry]:
    """按 POSCAR 元素顺序选出每个元素对应的 POTCAR。"""
    mapping = {str(k): str(v) for k, v in (mapping or {}).items()}
    prio = priority or DEFAULT_PRIORITY
    out: list[Entry] = []
    missing: list[str] = []
    for sym in species:
        cands = lib.get(sym, [])
        if sym in mapping:
            want = mapping[sym]
            hit = [e for e in cands if e.title == want]
            if not hit:
                raise StructureError(
                    f"potcar.map 指定了 {sym} → {want}，但库里没有这个 POTCAR。\n"
                    f"该元素可用：{[e.title for e in cands] or '（无）'}"
                )
            out.append(hit[0])
            continue
        if not cands:
            missing.append(sym)
            continue
        if len(cands) == 1:
            out.append(cands[0])
            continue
        picked = None
        for suffix in prio:
            hit = [e for e in cands if e.variant == suffix]
            if hit:
                picked = hit[0]
                break
        if picked is None:
            picked = cands[0]
        out.append(picked)
    if missing:
        raise StructureError(
            "赝势库里缺少这些元素的 POTCAR：" + ", ".join(missing) + "\n"
            "解决：① 用 --potcar spec 只生成清单；② 用 potcar.map 指定已有变体；"
            "③ 检查 --pp-dir 是否指到了 potpaw_PBE.54 这一层。"
        )
    return out


def concat(entries: list[Entry]) -> str:
    """把多个 POTCAR 拼成一个。每段之间保证有换行。"""
    parts: list[str] = []
    for e in entries:
        text = e.path.read_text(encoding="utf-8", errors="replace")
        parts.append(text.rstrip("\n") + "\n")
    return "\n".join(parts)


def spec_text(entries: list[Entry]) -> str:
    """POTCAR.spec：每行一个 TITEL 符号，人工拼 POTCAR 时的核对清单。"""
    return "".join(f"{e.title}\n" for e in entries)


def total_enmax(entries: list[Entry]) -> float | None:
    vals = [e.enmax for e in entries if e.enmax]
    return max(vals) if vals else None


def check_order(entries: list[Entry], species: list[str]) -> list[dict]:
    """核对 POTCAR 顺序与 POSCAR 元素顺序。返回 issue 列表。"""
    out: list[dict] = []
    titles = [e.title for e in entries]
    if len(entries) != len(species):
        out.append({"level": "ERROR", "code": "potcar-count",
                    "message": f"POTCAR 段数 {len(entries)} ≠ 元素种数 {len(species)}"})
        return out
    for i, (e, sym) in enumerate(zip(entries, species), 1):
        if e.element != sym:
            out.append({"level": "ERROR", "code": "potcar-order",
                        "message": f"第 {i} 段 POTCAR 是 {e.title}，POSCAR 第 6 行第 {i} 个元素是 {sym}"})
    if not out:
        out.append({"level": "PASS", "code": "potcar-order-ok",
                    "message": f"POTCAR 顺序与 POSCAR 一致：{titles}"})
    return out
