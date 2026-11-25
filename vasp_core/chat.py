"""终端聊天入口：`python -m vasp_core.chat`。

适合：SSH 登录、没有浏览器、或者想快速试一句话的时候。
它和网页调的是同一个 service.py，所以行为完全一致——
网页能做的事这里都能做，只是没有按钮，用 y/n 代替。

用法：
    python -m vasp_core.chat
    然后按提示输入"源目录""输出名称"，再输入一句中文需求；
    看到 diff 后输入 y 写入，n 放弃，r 换一句需求重来，q 退出。

【为什么要单独留一个终端入口】
    网页服务要起端口、开浏览器；在服务器上（或想批量脚本化时）这两个都是负担。
    终端版只依赖标准输入输出，零端口。
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import config as config_mod
from .config import ROOT
from . import llm as llm_mod
from .service import preview, write, summarize_source


def _ask(prompt: str, default: str = "") -> str:
    """问一句；直接回车表示用默认值。空输入且无默认值时返回空串（由调用方判断）。"""
    hint = f"（默认 {default}）" if default else ""
    try:
        s = input(f"{prompt}{hint}：").strip()
    except EOFError:
        print()
        return ""
    return s or default


def _print_logs(logs: list[dict]) -> None:
    """按时间线打印；颜色用 ANSI，Windows 10+ 的终端支持（不支持时会显示转义符，但不影响功能）。"""
    colors = {"step": "\033[36m", "ok": "\033[32m", "warn": "\033[33m",
              "err": "\033[31m", "info": "\033[90m"}
    for item in logs:
        c = colors.get(item["level"], "")
        print(f"  {item['t']} {c}{item['level']:<4}\033[0m {item['msg']}")


def _print_diffs(diffs: dict[str, str]) -> None:
    if not diffs:
        print("\n（没有文件差异：结构或参数没有变化）")
        return
    for name, text in diffs.items():
        print(f"\n===== {name} =====")
        print(text)


def main(argv: list[str] | None = None) -> int:
    cfg = config_mod.load()
    print("=" * 66)
    print("VASP 前处理 · 终端版")
    print(f"配置文件：{cfg.source_file or '（未找到，使用默认值）'}")
    pr = llm_mod.probe(cfg)
    if pr.available:
        print(f"模型：{cfg.llm.model} 在线（{pr.elapsed_ms} ms）")
    else:
        print(f"模型：不可用（{pr.reason}）→ 使用规则模式")
    print("=" * 66)

    source = _ask("源目录（含 POSCAR 的文件夹）", cfg.paths.default_source)
    if not source:
        print("没有源目录，退出。")
        return 2
    try:
        info = summarize_source(source)
    except Exception as exc:
        print(f"[错误] 读不了源目录：{exc}")
        return 2
    print(f"读到：{''.join(info['species'])}，{info['natoms']} 个原子，"
          f"体积 {info['volume']} Å³")

    src_name = Path(source).name or "run"
    name = _ask("输出名称（结果写到 vasp-prep\\runs\\<名称>，留空=" + src_name + "_out）", "")
    output = str((ROOT / cfg.paths.default_output / (name.strip() or f"{src_name}_out")).resolve())

    while True:
        text = _ask("\n需求（例：扩成 2x2x1，第 3 个原子换成 Co，ENCUT 调到 520）")
        if text in ("q", "quit", "exit", ":q"):
            return 0
        if not text:
            continue
        try:
            job = preview(source, output, text, cfg)
        except Exception as exc:
            print(f"\n[错误] {exc}")
            continue

        print("\n--- 过程 ---")
        _print_logs(job.logs)
        print("\n--- 预览结果 ---")
        s = job.summary
        print(f"原子数：{s['structure']['natoms'][0]} → {s['structure']['natoms'][1]}；"
              f"体积：{s['structure']['volume'][0]} → {s['structure']['volume'][1]} Å³")
        if s.get("incar_changes"):
            print("INCAR 变更：")
            for ch in s["incar_changes"]:
                print(f"  {ch['key']}: {ch['old']} → {ch['new']}（{ch.get('reason', '')}）")
        _print_diffs(job.diffs)

        ans = _ask("\n写入这个输出目录吗？y=写入 / n=放弃 / 直接回车=放弃", "n")
        if ans.lower() in ("y", "yes"):
            try:
                job = write(job.id)
                print(f"\n[完成] 已写入：{job.written_to}")
                print("文件：" + "、".join(job.files))
                print("\n把这个目录传到超算即可；POTCAR 按你的习惯在超算上拼。")
            except Exception as exc:
                print(f"\n[错误] 写入失败：{exc}")
        else:
            print("已放弃本次写入（源目录里的文件没有改动）。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
