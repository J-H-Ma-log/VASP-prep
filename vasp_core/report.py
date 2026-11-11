#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""report.py —— 把"改了什么、为什么改、有什么风险"写成可复核的文件。

为什么必须写这个文件：
  自然语言改输入，最怕的是"改完了但不知道改了哪一行"。
  所以每次运行都产出两样东西，随输入文件一起留在算例目录里：
    change_report.md  —— 人读：结构怎么变、INCAR 哪个键从多少到多少、风险提示；
    INCAR.diff        —— 机读：unified diff，`git apply`/`patch` 能直接反向撤回。
  另外往全局审计日志追加一行 JSON（谁、何时、哪个配方、写到哪、改了哪些键），
  方便以后回答"这个参数是谁改的"。

原则：报告只描述**脚本实际做了什么**，不做任何推测；风险只说不是"算得好不好"。
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
from pathlib import Path

from .poscar import Structure


def _fmt(x: float, nd: int = 6) -> str:
    s = f"{float(x):.{nd}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def structure_delta(before: Structure, after: Structure) -> dict:
    """结构层面的硬指标：原子数、元素顺序、体积、晶格矩阵。"""
    return {
        "natoms_before": before.natoms,
        "natoms_after": after.natoms,
        "species_before": list(dict.fromkeys(before.atom_species())),
        "species_after": list(dict.fromkeys(after.atom_species())),
        "volume_before": before.volume(),
        "volume_after": after.volume(),
        "lattice_before": before.lattice,
        "lattice_after": after.lattice,
    }


def make_diff(old_text: str, new_text: str, filename: str = "INCAR") -> str:
    """生成 unified diff；内容相同时返回空字符串。"""
    old = old_text.splitlines(keepends=True)
    new = new_text.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old, new, fromfile=f"a/{filename}", tofile=f"b/{filename}"))
    if not diff and old_text == new_text:
        return ""
    return "".join(diff)


def _table(rows: list[tuple[str, str, str, str]], headers: tuple[str, str, str, str]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(lines)


def make_change_report(
    *,
    recipe_name: str,
    recipe_path: Path,
    source_structure: Path,
    before: Structure,
    after: Structure,
    ops_log: list[dict],
    incar_changes: list,
    incar_issues: list[dict],
    potcar_issues: list[dict],
    kpoints_text: str | None,
    potcar_titles: list[str] | None,
    out_dir: Path,
    potcar_note: str = "",
) -> str:
    """生成 change_report.md 的完整文本。"""
    delta = structure_delta(before, after)
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append(f"# 输入变更报告：{recipe_name}")
    lines.append("")
    lines.append(f"- 生成时间：{ts}")
    lines.append(f"- 配方文件：`{recipe_path}`")
    lines.append(f"- 源结构：`{source_structure}`")
    lines.append(f"- 输出目录：`{out_dir}`")
    lines.append("")

    # 1 结构
    lines.append("## 1. 结构变化")
    lines.append("")
    rows = [
        ("原子数", str(delta["natoms_before"]), str(delta["natoms_after"]),
         "扩胞/加原子/空位会改这一项"),
        ("元素顺序", " ".join(delta["species_before"]), " ".join(delta["species_after"]),
         "POTCAR 顺序必须与此一致"),
        ("体积 (Å³)", _fmt(delta["volume_before"], 3), _fmt(delta["volume_after"], 3),
         "应变/真空层会改这一项"),
    ]
    lines.append(_table(rows, ("指标", "改前", "改后", "说明")))
    lines.append("")
    lat_rows = []
    for i in range(3):
        b = " ".join(_fmt(x, 4) for x in delta["lattice_before"][i])
        a = " ".join(_fmt(x, 4) for x in delta["lattice_after"][i])
        lat_rows.append((f"a{i + 1}", b, a, ""))
    lines.append(_table(lat_rows, ("晶格矢量", "改前 (Å)", "改后 (Å)", "")))
    lines.append("")

    # 2 操作
    lines.append("## 2. 按顺序执行的结构操作")
    lines.append("")
    if ops_log:
        for item in ops_log:
            lines.append(f"{item['step']}. `{item['op']}` —— {item['detail']}")
    else:
        lines.append("（配方里没有结构操作，POSCAR 按原样复制）")
    lines.append("")

    # 3 INCAR
    lines.append("## 3. INCAR 变更")
    lines.append("")
    if incar_changes:
        rows = [(c.key, str(c.old), str(c.new), c.reason) for c in incar_changes]
        lines.append(_table(rows, ("参数", "改前", "改后", "原因")))
    else:
        lines.append("（没有覆盖任何 INCAR 参数，沿用预设模板）")
    lines.append("")

    # 4 一致性/风险
    issues = list(incar_issues) + list(potcar_issues)
    errs = [i for i in issues if i.get("level") == "ERROR"]
    warns = [i for i in issues if i.get("level") == "WARN"]
    lines.append("## 4. 检查结果")
    lines.append("")
    if not errs and not warns:
        lines.append("- 全部检查通过。")
    for i in errs:
        lines.append(f"- **ERROR** `{i['code']}`：{i['message']}")
    for i in warns:
        lines.append(f"- WARN `{i['code']}`：{i['message']}")
    lines.append("")

    # 5 文件
    lines.append("## 5. 生成的文件")
    lines.append("")
    produced = ["POSCAR", "INCAR"]
    if kpoints_text is not None:
        produced.append("KPOINTS")
    if potcar_titles:  # 只有真生成了 spec 才写这一行；skip 模式不该出现
        produced.append("POTCAR.spec")
    lines.append("- " + "、".join(produced))
    if potcar_titles:
        lines.append(f"- POTCAR 顺序（spec）：{' '.join(potcar_titles)}")
    if potcar_note:
        lines.append(f"- {potcar_note}")
    lines.append("")

    lines.append("## 6. 下一步（请人工确认后再提交）")
    lines.append("")
    lines.append("```bash")
    lines.append(f"cd {out_dir}")
    lines.append("cat change_report.md          # 先读这一份")
    if potcar_titles:
        lines.append("cat POTCAR.spec                # 元素顺序应与 POSCAR 第 6 行一致")
        lines.append("# 到超算上按 spec 拼 POTCAR 后再提交")
    else:
        lines.append("# POTCAR 由你按元素顺序自行准备（本工具不碰赝势）")
    lines.append("qsub vasp.sh                   # 或你的提交脚本")
    lines.append("```")
    lines.append("")
    lines.append("> 本报告由脚本生成，只记录实际写入的内容；不判断参数的科学合理性。")
    return "\n".join(lines) + "\n"


def write_audit(path: Path, entry: dict) -> None:
    """向全局审计日志追加一行 JSON（不存在则创建）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
