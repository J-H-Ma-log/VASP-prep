#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_eval.py —— 评测"中文 → 配方"这层懂了多少。

为什么要有它：
  模型换成 3B 还是 7B、规则改了一条，效果好不好不能靠感觉。这里固定 50 句真实
  需求，逐条比对"期望的 op / INCAR 键 / K 点 / 计算类型 / 是否必须人工"，
  分数低于阈值就退出码 1 —— 以后改规则或换模型，先看这个分数。

判分口径（宽松但可追溯）：
  · ops：期望的操作名必须都出现（顺序也要对，因为索引依赖顺序）；
  · incar：期望的键值都要被子集命中（数值允许 1e-9 误差）；
  · kpoints：mesh 必须完全相等；
  · calc：必须相等；
  · needs_human：标了 true 的句子必须至少产生一条 needs_human 提示。

用法：
    python eval/run_eval.py [--no-llm] [--threshold 0.9]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from vasp_core.nl import text_to_recipe  # noqa: E402


def check_one(case: dict) -> list[str]:
    """跑一条用例，返回失败原因列表（空 = 通过）。"""
    r = text_to_recipe(case["text"])
    recipe = r["recipe"]
    fails: list[str] = []

    want_ops = case.get("ops", [])
    got_ops = [o["op"] for o in recipe.get("structure", {}).get("ops", [])]
    if want_ops:
        if got_ops[:len(want_ops)] != want_ops:
            fails.append(f"ops 期望 {want_ops}，实际 {got_ops}")

    want_incar = case.get("incar", {})
    got_incar = recipe.get("incar", {}).get("overrides", {})
    for k, v in want_incar.items():
        if k not in got_incar:
            fails.append(f"INCAR 缺 {k}")
            continue
        try:
            if abs(float(got_incar[k]) - float(v)) > 1e-9:
                fails.append(f"INCAR {k} 期望 {v}，实际 {got_incar[k]}")
        except (TypeError, ValueError):
            if got_incar[k] != v:
                fails.append(f"INCAR {k} 期望 {v}，实际 {got_incar[k]}")

    want_kp = case.get("kpoints")
    if want_kp:
        got_kp = recipe.get("kpoints") or {}
        if got_kp.get("mesh") != want_kp:
            fails.append(f"K 点期望 {want_kp}，实际 {got_kp.get('mesh')}")

    want_calc = case.get("calc")
    if want_calc and recipe.get("calc") != want_calc:
        fails.append(f"calc 期望 {want_calc}，实际 {recipe.get('calc')}")

    if case.get("needs_human") and not r["needs_human"]:
        fails.append("应该进 needs_human 却直接执行了")

    return fails


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="评测中文→配方规则层")
    p.add_argument("--cases", default=str(HERE / "cases.jsonl"))
    p.add_argument("--threshold", type=float, default=0.9, help="通过率下限，默认 0.9")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    cases = [json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines() if line.strip()]
    passed = 0
    for case in cases:
        fails = check_one(case)
        if not fails:
            passed += 1
            if args.verbose:
                print(f"[PASS] #{case['id']} {case['text']}")
        else:
            print(f"[FAIL] #{case['id']} {case['text']}")
            for f in fails:
                print(f"        - {f}")

    rate = passed / len(cases) if cases else 0.0
    print(f"\n{passed}/{len(cases)} 通过，通过率 {rate:.1%}（阈值 {args.threshold:.0%}）")
    return 0 if rate >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
