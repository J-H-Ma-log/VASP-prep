#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""nl2recipe.py —— 中文需求 → 配方 JSON → 校验（把"模型"和"脚本"接起来的胶水）。

流程（每一步失败都停，不会带病往下走）：
    1. 有模型：把 prompt + 用户原话喂给 llama-cli，用 GBNF 约束只许输出 JSON；
       没模型：用 vasp_core.nl 的规则翻译（-90% 场景够用，且完全可解释）。
    2. 抠出 JSON（模型偶尔仍会带前后空白/多余括号），解析失败直接退出。
    3. 调 `python -m vasp_core.prep <recipe> --dry-run --json` 做**真正的校验**：
       结构操作能不能执行、INCAR 键在不在白名单、类型对不对、K 点合法不合法。
    4. 校验通过才把配方写到你指定的位置，并打印下一步命令。

为什么不在这个脚本里直接改 VASP 文件：
  模型/翻译层只产出"候选配方"，改文件是 prep.py 的事。
  两层分开，才能做到"模型胡说 → 配方校验拦下 → 一个字节都不写"。

退出码：0 成功；2 输入/解析失败；3 策略拒绝；4 结构错误；6 校验未通过；7 模型调用失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from vasp_core.nl import merge_into, text_to_recipe  # noqa: E402


def _reconfigure():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def extract_json(text: str) -> str:
    """从模型输出里抠出第一个**括号配平**的 JSON 对象。

    为什么不用正则：JSON 里有嵌套数组，正则匹配到第一个 `}` 就断了。
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("输出里没有 {，模型没有按格式回答")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("JSON 括号没有闭合（可能被 max tokens 截断）")


def call_llama(args, user_text: str, workdir: Path) -> str:
    """调用 llama-cli，返回它的原始输出。"""
    llama = args.llama
    if not llama:
        raise FileNotFoundError("没有指定 --llama（llama-cli 的路径）")
    if not Path(llama).exists():
        raise FileNotFoundError(f"找不到 llama-cli：{llama}")
    prompt = Path(args.prompt).read_text(encoding="utf-8")
    full = f"{prompt}\n\n用户：{user_text}\n输出：\n"
    pf = workdir / "prompt_full.txt"
    pf.write_text(full, encoding="utf-8")
    cmd = [
        llama, "-m", args.model,
        "-f", str(pf),
        "--grammar-file", args.grammar,
        "--temp", "0",              # 温度 0：同样的输入给同样的输出，便于复现与评测
        "-n", str(args.max_tokens),
        "--no-display-prompt",
        "-no-cnv",
        "--threads", str(args.threads),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=args.timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"llama-cli 退出码 {proc.returncode}：{(proc.stderr or '')[-800:]}")
    return proc.stdout


def validate_recipe(recipe_path: Path, pp_dir: str | None) -> tuple[int, dict, str]:
    """调 prep.py 做 dry-run 校验，返回 (退出码, 解析到的 JSON, stderr 文本)。"""
    cmd = [sys.executable, "-m", "vasp_core.prep", str(recipe_path), "--json"]
    if pp_dir:
        cmd += ["--pp-dir", pp_dir]
    proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    data: dict = {}
    if out.startswith("{"):
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {}
    return proc.returncode, data, (proc.stderr or "").strip()


def main(argv: list[str] | None = None) -> int:
    _reconfigure()
    p = argparse.ArgumentParser(
        prog="nl2recipe",
        description="中文需求 → 配方 JSON（可选本地模型，带校验；不直接改 VASP 文件）",
    )
    p.add_argument("text", nargs="?", help="一句中文需求")
    p.add_argument("--text-file", default=None, help="从文件读需求（避免引号转义麻烦）")
    p.add_argument("--base", default=None, help="已有配方：改动叠加在它上面")
    p.add_argument("--out", default=None, help="校验通过后把配方写到这个路径")
    p.add_argument("--no-llm", action="store_true", help="只用规则翻译，不调用模型")
    p.add_argument("--llama", default=os.environ.get("LLAMA_CLI", ""), help="llama-cli 路径")
    p.add_argument("--model", default=os.environ.get("LLAMA_MODEL", ""), help="GGUF 模型路径")
    p.add_argument("--prompt", default=str(HERE / "llm" / "prompt_zh.txt"))
    p.add_argument("--grammar", default=str(HERE / "llm" / "recipe.gbnf"))
    p.add_argument("--pp-dir", default=None, help="赝势库（校验 POTCAR 顺序用，可省略）")
    p.add_argument("--threads", type=int, default=int(os.environ.get("LLAMA_THREADS", "16")))
    p.add_argument("--max-tokens", type=int, default=700)
    p.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args(argv)

    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8-sig").strip()
    if not text:
        print("[错误] 需要一句中文需求（位置参数或 --text-file）", file=sys.stderr)
        return 2

    base = None
    if args.base:
        base = json.loads(Path(args.base).read_text(encoding="utf-8-sig"))

    work_root = HERE / ".tmp_nl"
    work_root.mkdir(parents=True, exist_ok=True)
    work = work_root / ("nl2r_" + __import__("uuid").uuid4().hex[:10])
    work.mkdir(parents=True, exist_ok=False)
    try:
        if args.no_llm:
            r = text_to_recipe(text, base=base)
            recipe = r["recipe"] if base is None else merge_into(base, r["recipe"])
            print("规则翻译命中：")
            for m in r["matched"]:
                print(f"  · {m['desc']}：\"{m['text']}\" → {m['effect']}")
            if r["needs_human"]:
                print("必须人工决定（脚本不猜）：")
                for h in r["needs_human"]:
                    print(f"  ! {h['topic']}：\"{h['text']}\"")
            if r["unmatched"]:
                print("没看懂的片段：", " / ".join(r["unmatched"]))
            model_ok = True
        else:
            print(f"调用模型：{args.model or '(未设置 --model)'}")
            raw = call_llama(args, text, work)
            js = extract_json(raw)
            recipe = json.loads(js)
            if base is not None:
                recipe = merge_into(base, recipe)
            model_ok = True

        # base_dir 必须指到"结构文件所在目录"：配方的相对路径是相对配方文件解析的，
        # 而候选配方写在临时目录里，不改成绝对路径就会在临时目录里找 POSCAR。
        recipe["base_dir"] = str(Path(args.base).resolve().parent) if args.base else str(Path.cwd())
        rp = work / "recipe.json"
        rp.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        code, data, err = validate_recipe(rp, args.pp_dir)
        if code != 0:
            print(f"[校验未通过] 退出码 {code}", file=sys.stderr)
            if err:
                print(err, file=sys.stderr)
            for issue in data.get("issues", []) if isinstance(data, dict) else []:
                print(f"  [{issue.get('level')}] {issue.get('code')}: {issue.get('message')}", file=sys.stderr)
            print(f"\n配方已保留在：{rp}", file=sys.stderr)
            return code

        st = data.get("structure", {})
        print("\n校验通过：")
        print(f"  结构 {st.get('natoms')}，元素 {st.get('species')}")
        print(f"  INCAR 变更：{len(data.get('incar_changes', []))} 项")
        warns = [i for i in data.get("issues", []) if i.get("level") == "WARN"]
        for w in warns:
            print(f"  [WARN] {w['code']}: {w['message']}")

        dest = Path(args.out) if args.out else (HERE / "runs" / f"{recipe.get('name', 'recipe')}.json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rp, dest)
        print(f"\n配方：{dest}")
        print("下一步（确认无误后）：")
        print(f"  python -m vasp_core.prep {dest} --write --pp-dir <赝势库>")
        return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[解析失败] {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"[模型调用失败] {exc}", file=sys.stderr)
        print("提示：没有模型时加 --no-llm，规则翻译能覆盖大部分常见说法。", file=sys.stderr)
        return 7
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
