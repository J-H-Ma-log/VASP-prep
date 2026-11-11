#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""nl.py —— 不依赖任何模型的中文规则翻译器（"能听懂"的第一层）。

为什么要有这一层：
  1. 80% 的日常请求是固定句式（"扩成 2x2x1""把截断能调到 520""真空层 15 Å"），
     用正则就能翻译成配方，比让 7B 模型去猜更准、更快、可解释；
  2. 模型只负责"规矩不出来的那句"，所以即使模型说胡话，常见请求仍然可用；
  3. 每条命中都记录"哪条规则、原文哪一段、产生了什么"，出问题能追溯。

能力边界（保守）：
  · 只翻"改结构/改 INCAR 常见键/生成 KPOINTS"这类确定性动作；
  · 切表面（slab）、按层固定原子、DFT+U/泛函、赝势选择、收敛测试 —— 一律进 needs_human，
    不猜、不默认，因为猜错不会报错，只会算错体系。

用法：
    python -m vasp_core.nl "把 Cu 扩成 2x2x1，第 3 个原子换成 Co，截断能调到 520"
    python -m vasp_core.nl "..." --base recipes/cu.json --out recipes/cu_v2.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .incar import IVDW_NAMES

ELL = r"([A-Z][a-z]?)"

# (名字, 正则, 处理函数)；顺序 = 匹配优先级，先匹配到的先处理
RULES: list[tuple[str, re.Pattern, str]] = [
    ("substitute_all",
     re.compile(r"(?:把|将)?\s*所有(?:的)?\s*(?:原子\s*)?([A-Z][a-z]?)\s*(?:原子)?\s*(?:换|改|替换)(?:成|为)\s*([A-Z][a-z]?)"),
     "把某元素全部替换成另一元素"),
    ("substitute",
     re.compile(r"第\s*(\d+)\s*个原子\s*(?:换|改|替换)(?:成|为)\s*([A-Z][a-z]?)"),
     "按序号替换单个原子"),
    ("vacancy",
     re.compile(r"(?:删除|删掉|去掉|移除)\s*第\s*(\d+)\s*个原子"),
     "删除指定原子（空位）"),
    ("add_atom",
     re.compile(r"(?:在|于)?\s*(?:分数坐标)?\s*\(?\s*(-?\d+\.?\d*)\s*[,，\s]\s*(-?\d+\.?\d*)\s*[,，\s]\s*(-?\d+\.?\d*)\s*\)?\s*(?:处)?\s*(?:加|添加|放)\s*(?:一个|1个)?\s*([A-Z][a-z]?)"),
     "在给定分数坐标处加一个原子"),
    ("vacuum",
     re.compile(r"真空(?:层|区)?\s*(?:厚度)?\s*(?:为|到|加|设|改成|取)?\s*(\d+(?:\.\d+)?)\s*(?:Å|A|埃|angstrom)?"),
     "加真空层（默认沿 c 轴、居中）"),
    ("supercell",
     re.compile(r"(?:扩胞|扩成|扩展|超胞|扩到)[^0-9]{0,8}(\d+)\s*[x×X*]\s*(\d+)\s*[x×X*]\s*(\d+)|(\d+)\s*[x×X*]\s*(\d+)\s*[x×X*]\s*(\d+)\s*(?:的)?\s*(?:超胞|扩胞)"),
     "扩成 nx×ny×nz 超胞"),
    ("strain",
     re.compile(r"(?:拉伸|压缩|施加|加)\s*(?:一个)?\s*(?:各向同性|均匀)?\s*(\d+(?:\.\d+)?)\s*%\s*(?:的)?\s*(?:应变|形变)?|(?:各向同性|均匀)?\s*应变\s*(?:为|=|:|：)?\s*(\d+(?:\.\d+)?)\s*%"),
     "各向同性应变（百分比）"),
    ("kpoints",
     re.compile(r"[Kk]\s*(?:点|网格|点数|网格数)\s*(?:为|用|取|设|:|：)?\s*(\d+)\s*[x×X*]\s*(\d+)\s*[x×X*]\s*(\d+)"),
     "Gamma 中心 K 网格"),
    ("encut",
     re.compile(r"(?:截断能|ENCUT)\s*(?:调到|改为|设为|设置为|=|到|取)?\s*(\d{3,4})"),
     "设置截断能 ENCUT"),
    ("sigma",
     re.compile(r"(?:SIGMA|sigma|展宽)\s*(?:调到|改为|设为|=|到|取)?\s*(0?\.\d+)"),
     "设置 SIGMA"),
    ("magmom",
     re.compile(r"(?:磁矩|MAGMOM)\s*(?:设为|=|到|取)?\s*(-?\d+(?:\.\d+)?)"),
     "设置 MAGMOM（标量，脚本会按原子数广播）"),
    ("ispin",
     re.compile(r"(?:自旋极化|考虑自旋|ISPIN\s*(?:=)?\s*2)"),
     "开启自旋极化 ISPIN=2"),
    # vdW 色散：按"说的方法"映射到 VASP 的 IVDW 数值（对照 vasp.at/wiki/index.php/IVDW）
    ("ivdw_value",
     re.compile(r"IVDW\s*(?:=|设为|改成|调到|取|为)?\s*(\d{1,3})"),
     "显式设置 IVDW 数值"),
    ("ivdw_d3bj",
     re.compile(r"(?:DFT[-\s]?D3\s*[（(]?\s*BJ|\bD3BJ\b|D3\s*[（(]\s*BJ|Becke[-\s]?Johnson|BJ\s*阻尼)"),
     "DFT-D3(BJ) → IVDW=12"),
    ("ivdw_d3zero",
     re.compile(r"(?:DFT[-\s]?D3|D3)\s*(?:零阻尼|zero|0\s*阻尼)"),
     "DFT-D3 零阻尼 → IVDW=11"),
    ("ivdw_d3",
     re.compile(r"DFT[-\s]?D3|\bD3\b"),
     "DFT-D3（默认 BJ）→ IVDW=12"),
    ("ivdw_d2",
     re.compile(r"DFT[-\s]?D2|\bD2\b"),
     "DFT-D2 → IVDW=1"),
    ("ivdw_ts",
     re.compile(r"Tkatchenko[-\s]?Scheffler|\bTS\b"),
     "TS 方法 → IVDW=20"),
    ("ivdw_mbd",
     re.compile(r"\bMBD\b|多体色散"),
     "MBD → IVDW=202"),
    ("ivdw_d4",
     re.compile(r"DFT[-\s]?D4|\bD4\b"),
     "DFT-D4 → IVDW=13"),
    ("calc_relax", re.compile(r"(?:结构优化|几何优化|弛豫|relax|离子步)"), "计算类型 = relax"),
    ("calc_static", re.compile(r"(?:静态|单点|scf|自洽|能量计算)"), "计算类型 = static"),
    ("calc_band", re.compile(r"(?:能带|band\s*structure|高对称路径)"), "计算类型 = band"),
]

# 明确"我不猜"的说法 → 进 needs_human
IVDW_VALUES = (0, 1, 2, 3, 4, 10, 11, 12, 13, 14, 15, 20, 21, 202, 263)

HUMAN_REQUIRED: list[tuple[str, re.Pattern]] = [
    ("切表面/slab", re.compile(r"(?:切|生成|做|建)\s*(?:一个)?\s*(?:[\w\-（）()]*\s*)?表面|slab|米勒指数")),
    ("按层固定原子", re.compile(r"固定[^，。；]{0,8}层|固定表面|固定底部|固定最下")),
    ("DFT+U / 杂化泛函", re.compile(r"DFT\s*\+\s*U|LDAU|HSE|杂化泛函")),
    ("非局域 vdW-DF 泛函", re.compile(r"非局域\s*vdW|vdW-DF|LUSE_VDW")),
    ("vdW 色散（未指明方法）", re.compile(r"色散|范德华|vdW")),
    ("赝势选择", re.compile(r"赝势|POTCAR\s*(?:用|选)|_pv|_sv")),
    ("收敛测试", re.compile(r"收敛测试|做\s*\d+\s*个\s*ENCUT|K\s*点收敛|测试收敛")),
    ("磁序/反铁磁", re.compile(r"反铁磁|铁磁|AFM|FM|磁序|自旋构型")),
    ("对称性/原胞切换", re.compile(r"原胞|惯用胞|primitive|conventional|对称化")),
]


def _mk_recipe() -> dict:
    return {"version": 2, "name": "from_nl", "calc": "relax", "base_dir": ".",
            "structure": {"file": "POSCAR", "ops": []}, "incar": {"overrides": {}}}


def text_to_recipe(text: str, base: dict | None = None) -> dict:
    """把一段中文翻译成配方（或把修改叠加到 base 配方上）。

    返回 {"recipe": {...}, "matched": [...], "needs_human": [...], "unmatched": [...]}
    """
    text = (text or "").strip()
    recipe = json.loads(json.dumps(base)) if base else _mk_recipe()
    recipe.setdefault("version", 2)
    recipe.setdefault("name", "from_nl")
    recipe.setdefault("calc", "relax")
    recipe.setdefault("base_dir", ".")
    recipe.setdefault("structure", {}).setdefault("file", "POSCAR")
    recipe["structure"].setdefault("ops", [])
    recipe.setdefault("incar", {}).setdefault("overrides", {})

    matched: list[dict] = []
    needs_human: list[dict] = []
    consumed: list[tuple[int, int]] = []

    for name, pattern in HUMAN_REQUIRED:
        for m in pattern.finditer(text):
            needs_human.append({"topic": name, "text": m.group(0), "why": "需要人工决定，脚本不猜"})
            consumed.append(m.span())

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in consumed)

    pending: list[tuple[int, int, str, tuple, str]] = []
    for name, pattern, _desc in RULES:
        for m in pattern.finditer(text):
            if not free(m.span()):
                continue
            pending.append((m.start(), m.end(), name, m.groups(), m.group(0)))

    # 按"在句子里出现的先后"执行：中文语序基本就是操作顺序，
    # 例如"扩成 2x2x1，第 3 个原子换成 Co"——第 3 个原子指的是扩胞之后的第 3 个。
    # 如果强行按固定顺序重排，索引就会指到另一个原子上（这类错 VASP 不会报）。
    desc_of = {n: d for n, _p, d in RULES}
    # 只说了"加色散/范德华"但没说是 D2/D3/TS/MBD 时，才算"需要人工决定"；
    # 说清楚了方法（或直接给 IVDW 数值）就不该再提示人工。
    ivdw_rule_names = {"ivdw_value", "ivdw_d3bj", "ivdw_d3zero", "ivdw_d3", "ivdw_d2",
                       "ivdw_ts", "ivdw_mbd", "ivdw_d4", "ivdw_d3"}
    for _start, _end, name, g, raw in sorted(pending, key=lambda x: x[0]):
        effect = _apply(name, g, recipe)
        if effect is None:
            continue
        matched.append({"rule": name, "text": raw, "effect": effect, "desc": desc_of[name]})
        consumed.append((_start, _end))

    matched_names = {m["rule"] for m in matched}
    if matched_names & {"ivdw_value", "ivdw_d3bj", "ivdw_d3zero", "ivdw_d3", "ivdw_d2", "ivdw_ts", "ivdw_mbd", "ivdw_d4"}:
        needs_human = [n for n in needs_human if n["topic"] != "vdW 色散（未指明方法）"]

    unmatched = _unmatched_fragments(text, consumed)
    return {"recipe": recipe, "matched": matched, "needs_human": needs_human,
            "unmatched": unmatched}


def _apply(name: str, g, recipe: dict) -> str | None:
    """把一条规则命中的分组写进配方；返回人类可读的效果说明。"""
    ops = recipe["structure"]["ops"]
    ov = recipe["incar"]["overrides"]

    if name == "substitute_all":
        a, b = g[0], g[1]
        ops.append({"op": "substitute_all", "mapping": {a: b}})
        return f"所有 {a} → {b}"

    if name == "substitute":
        idx, sp = int(g[0]), g[1]
        ops.append({"op": "substitute", "index": idx, "species": sp})
        return f"第 {idx} 个原子 → {sp}"

    if name == "vacancy":
        idx = int(g[0])
        ops.append({"op": "vacancy", "indices": [idx]})
        return f"删除第 {idx} 个原子"

    if name == "add_atom":
        x, y, z, sp = float(g[0]), float(g[1]), float(g[2]), g[3]
        ops.append({"op": "add_atom", "frac": [x, y, z], "species": sp})
        return f"加 {sp} 于分数坐标 ({x}, {y}, {z})"

    if name == "vacuum":
        t = float(g[0])
        if t <= 0 or t > 100:
            return None
        ops.append({"op": "vacuum", "axis": 2, "thickness": t, "place": "center"})
        return f"沿 c 轴加 {t:g} Å 真空（居中）"

    if name == "supercell":
        nums = [x for x in g if x]
        if len(nums) == 3:
            sc = [int(x) for x in nums]
            if min(sc) < 1 or max(sc) > 12:
                return None
            ops.append({"op": "supercell", "scaling": sc})
            return f"扩胞 {sc[0]}×{sc[1]}×{sc[2]}"
        return None

    if name == "strain":
        raw = g[0] if g and g[0] else (g[1] if len(g) > 1 else None)
        if raw is None:
            return None
        pct = float(raw)
        if pct == 0 or pct > 20:
            return None
        d = pct / 100.0
        ops.append({"op": "strain", "matrix": [[d, 0, 0], [0, d, 0], [0, 0, d]], "mode": "delta"})
        return f"各向同性应变 {pct:g}%"

    if name == "kpoints":
        mesh = [int(x) for x in g[:3]]
        if min(mesh) < 1 or max(mesh) > 64:
            return None
        recipe["kpoints"] = {"mode": "gamma", "mesh": mesh}
        return f"K 网格 {mesh}"

    if name == "ivdw_value":
        v = int(g[0])
        if v not in IVDW_VALUES:
            return None
        ov["IVDW"] = v
        return f"IVDW = {v}（{IVDW_NAMES.get(v, '')}）"

    if name in ("ivdw_d3bj", "ivdw_d3zero", "ivdw_d3", "ivdw_d2", "ivdw_ts", "ivdw_mbd", "ivdw_d4"):
        # 同一个句子可能命中多条形如 "D3" / "D3 零阻尼" 的规则：
        # 先命中的说了算（规则顺序 + 出现位置已经保证"更具体的在前"）。
        if "IVDW" in ov:
            return None
        v = {"ivdw_d3bj": 12, "ivdw_d3zero": 11, "ivdw_d3": 12,
             "ivdw_d2": 1, "ivdw_ts": 20, "ivdw_mbd": 202, "ivdw_d4": 13}[name]
        ov["IVDW"] = v
        return f"IVDW = {v}（{IVDW_NAMES.get(v, '')}）"

    if name == "encut":
        v = int(g[0])
        if v < 100 or v > 2000:
            return None
        ov["ENCUT"] = v
        return f"ENCUT = {v}"

    if name == "sigma":
        v = float(g[0])
        if v <= 0 or v > 1:
            return None
        ov["SIGMA"] = v
        return f"SIGMA = {v}"

    if name == "magmom":
        v = float(g[0])
        if abs(v) > 20:
            return None
        ov["MAGMOM"] = v
        ov["ISPIN"] = 2
        return f"MAGMOM = {v}（标量，按原子数展开）+ ISPIN=2"

    if name == "ispin":
        ov["ISPIN"] = 2
        return "ISPIN = 2"

    if name == "calc_relax":
        recipe["calc"] = "relax"
        return "calc = relax"
    if name == "calc_static":
        recipe["calc"] = "static"
        return "calc = static"
    if name == "calc_band":
        recipe["calc"] = "band"
        return "calc = band"
    return None


def _unmatched_fragments(text: str, consumed: list[tuple[int, int]]) -> list[str]:
    """把没被任何规则吃掉的片段挑出来（去掉标点和空白），提醒人"这句没懂"。"""
    if not consumed:
        return [text] if text else []
    spans = sorted(consumed)
    rest: list[str] = []
    pos = 0
    for a, b in spans:
        if a > pos:
            rest.append(text[pos:a])
        pos = max(pos, b)
    if pos < len(text):
        rest.append(text[pos:])
    out = []
    for chunk in rest:
        cleaned = re.sub(r"[\s，,。；;：:、的了吧呢啊把将]", "", chunk)
        # 单独出现的元素名（如"把 Cu 扩成…"里的 Cu）通常是主语，不算"没听懂"
        if re.fullmatch(r"[A-Z][a-z]?", cleaned):
            continue
        if len(cleaned) >= 2:
            out.append(cleaned)
    return out


def merge_into(base: dict, add: dict) -> dict:
    """把 add 叠加到 base 上：ops 追加、overrides 合并、kpoints/potcar 覆盖。"""
    out = json.loads(json.dumps(base))
    out.setdefault("structure", {}).setdefault("ops", [])
    out["structure"]["ops"].extend(add.get("structure", {}).get("ops", []))
    out.setdefault("incar", {}).setdefault("overrides", {})
    out["incar"]["overrides"].update(add.get("incar", {}).get("overrides", {}))
    for key in ("kpoints", "potcar", "calc", "name"):
        if key in add:
            out[key] = add[key]
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vasp_core.nl", description="中文 → 配方 JSON（规则版，不用模型）")
    p.add_argument("text", help="一句中文需求，例如：把第 3 个原子换成 Co，扩成 2x2x1")
    p.add_argument("--base", default=None, help="已有配方，修改会叠加在它上面")
    p.add_argument("--out", default=None, help="把结果配方写到这个文件（不写则打印）")
    p.add_argument("--quiet", action="store_true", help="只输出配方 JSON")
    args = p.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    base = None
    if args.base:
        base = json.loads(Path(args.base).read_text(encoding="utf-8-sig"))
    r = text_to_recipe(args.text, base=base)
    recipe = r["recipe"] if base is None else merge_into(base, r["recipe"])

    if args.out:
        Path(args.out).write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        if not args.quiet:
            print(f"已写出配方：{args.out}")
    if not args.quiet:
        print("命中规则：")
        for m in r["matched"]:
            print(f"  · {m['desc']}：\"{m['text']}\" → {m['effect']}")
        if r["needs_human"]:
            print("必须人工决定（脚本不猜）：")
            for h in r["needs_human"]:
                print(f"  ! {h['topic']}：\"{h['text']}\"")
        if r["unmatched"]:
            print("没看懂的片段：", " / ".join(r["unmatched"]))
    if not args.out:
        print(json.dumps(recipe, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
