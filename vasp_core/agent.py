#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent.py —— OpenAI 兼容 /chat/completions 的 tool-calling 智能体循环。

它做什么
--------
把一个自然语言任务交给模型，模型可以调用 vasp_core.tools 里的 8 个工具
（读结构 / 读 INCAR / 提配方 / 校验 / 写算例 / 核验结果 / 生成超算命令提示）。
循环完全用标准库 urllib 实现，不依赖 openai SDK，方便离线环境部署。

安全边界
--------
* 工具层里只有 write_inputs 是 mutating；**不加 --yes-write 时一律拒绝**，
  即使模型自己填了 confirm=true。
* 写盘仍然要求参数 confirm=true（两道闸，见 vasp_core/tools.py 的说明）。
* 本程序没有任何提交作业的代码路径；submit_hint 只返回命令文本。
* 模型不可达/返回格式错误时直接报错退出，**绝不伪造工具结果或最终答案**。

退出码
------
0  完成（模型给出最终答复，且所有工具调用正常结束）
2  参数/配置问题，或超过 --max-turns 仍未结束
7  模型服务不可达 / HTTP 错误 / 响应不是 OpenAI 兼容格式

典型用法
--------
    set OPENAI_API_KEY=sk-...
    python -m vasp_core.agent "把 C:\\work\\cu\\POSCAR 扩成 2x2x2，ENCUT 600" \
        --base-url https://api.deepseek.com/v1 --model deepseek-chat

    # 只读演练（不写盘）：
    python -m vasp_core.agent --task-file task.txt --base-url ... --model ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .tools import TOOLS, dispatch, tool_specs_openai

__all__ = ["AgentError", "main", "call_model", "run_once"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MODEL = 7

SYSTEM_PROMPT = """你是 VASP 前处理助手，负责把用户的中文需求变成可复核的 VASP 算例输入。

严格遵守下面的工作顺序与边界：
1. 信息不足时先调用 workspace_status / read_structure / read_incar 了解现状，
   不要猜测文件路径和结构内容。
2. 用 propose_recipe 得到候选配方；needs_human 里的内容（slab 切面、固定层、
   DFT+U、赝势选择、磁序、收敛测试等）必须交给人决定，不要自行补全。
3. 用 validate_recipe 做真校验。把 incar_diff、结构变化（natoms/体积/ops）、
   issues 如实转述给人看；有 ERROR 时先改配方，不要写盘。
4. 只有用户在看到校验结果后明确确认，才调用 write_inputs，且必须传 confirm=true。
   写完后把 INCAR.diff 和 change_report.md 的关键内容给人复核。
5. 生成算例后可以用 verify_run 核验已有结果；submit_hint 只给出 --dry-run 命令
   文本，绝不自动提交作业，也不要声称已经提交。
6. 所有数值以工具结果为准；工具失败就如实说明失败原因，绝不编造结果或路径。

回复使用中文，简洁、分点，明确区分"文件里记录的 / 程序计算的 / 你的推断"。"""


class AgentError(Exception):
    """智能体循环的可预期失败：带退出码与中文原因，供 main 统一处理。"""

    def __init__(self, message: str, *, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class TraceWriter:
    """把每一步事件写成 JSONL（一行一个 JSON），同时负责终端打印。

    为什么单独一个类：终端展示要短（截断长文本），落盘要全（便于事后排查幻觉）；
    两件事放在一处，保证"看到的"和"存档的"来自同一次调用。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # 每次启动覆盖旧 trace：一次会话一份记录，避免多次运行混在一起难以对照。
        self._fh = path.open("w", encoding="utf-8")
        self.run_index = 0

    def log(self, event: str, **data: Any) -> dict:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "run": self.run_index,
            "event": event,
            **data,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        return record

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _preview(text: str, limit: int = 400) -> str:
    """终端预览：长文本截断，避免一次工具输出刷屏到看不见关键信息。"""
    text = (text or "").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} 字符)"


def _chat_url(base_url: str) -> str:
    """拼 /chat/completions；允许用户直接给完整 endpoint（本地网关常见）。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def call_model(*, base_url: str, api_key: str | None, model: str,
               messages: list[dict], tools: list[dict], timeout: float) -> dict:
    """请求一次 chat/completions，返回 assistant message。

    做什么：标准库 urllib POST，检查 HTTP 状态、JSON 结构与 choices[0].message。
    为什么不用 openai SDK：任务要求仅标准库；而且失败时我们要拿到原始 HTTP 文本，
            SDK 的异常信息有时会吞掉服务端返回的具体错误。
    不这样做：把网络异常抛成 traceback，用户分不清"没网/密钥错/模型名错"。
    还有别的办法：requests/aiohttp（第三方依赖，离线环境可能装不上）。
    """
    url = _chat_url(base_url)
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.2,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
        except Exception:  # noqa: BLE001 - 读错误体失败不应掩盖原始 HTTP 错误
            detail = ""
        raise AgentError(
            f"模型服务返回 HTTP {exc.code}：{detail or exc.reason}",
            exit_code=EXIT_MODEL,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AgentError(
            f"无法连接模型服务 {url}：{exc}（检查 --base-url、网络与代理；不伪造结果）",
            exit_code=EXIT_MODEL,
        ) from exc

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise AgentError("模型服务返回的不是合法 JSON（可能命中了错误的 URL）",
                         exit_code=EXIT_MODEL) from exc

    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise AgentError(f"模型响应缺少 choices：{_preview(str(data), 300)}", exit_code=EXIT_MODEL)
    message = data["choices"][0].get("message")
    if not isinstance(message, dict):
        raise AgentError("模型响应缺少 choices[0].message", exit_code=EXIT_MODEL)
    return message


def _tool_error(name: str, code: str, message: str) -> dict:
    """构造与 dispatch 失败同构的结果，让模型看到的错误形状始终一致。"""
    return {"ok": False, "tool": name, "error": {"code": code, "message": message,
                                                 "field": None, "detail": None}}


def run_once(*, base_url: str, api_key: str | None, model: str, task: str,
             max_turns: int, yes_write: bool, timeout: float, trace: TraceWriter) -> str:
    """跑完一次完整对话，返回模型最终文本；失败抛 AgentError。

    做什么：维护 messages，循环"模型 → 工具 → 模型"，直到没有 tool_calls。
    为什么用"回放 assistant 原消息"：OpenAI 协议要求 tool 消息必须对应上一条
            assistant 的 tool_call_id，任何字段改写都可能被服务端拒绝。
    不这样做：自己重组 assistant 消息，容易丢 tool_calls 或 id，导致 400。
    还有别的办法：Responses API（更复杂，且不是所有兼容网关都支持）。
    """
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    tools = tool_specs_openai()

    for turn in range(1, max_turns + 1):
        trace.log("request", turn=turn, url=_chat_url(base_url), model=model,
                  n_messages=len(messages))
        print(f"[run {trace.run_index}][turn {turn}] -> 请求模型（messages={len(messages)}）", flush=True)

        message = call_model(base_url=base_url, api_key=api_key, model=model,
                             messages=messages, tools=tools, timeout=timeout)

        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        trace.log("response", turn=turn, content=content,
                  tool_calls=[{"id": tc.get("id"), "name": tc.get("function", {}).get("name"),
                               "arguments": tc.get("function", {}).get("arguments")}
                              for tc in tool_calls])
        print(f"[run {trace.run_index}][turn {turn}] <- 模型"
              f"（content={_preview(content, 120)!r}, tool_calls={len(tool_calls)}）", flush=True)

        # 必须原样回放：tool 消息要用这里的 id 对应；不要重建字段。
        messages.append(message)

        if not tool_calls:
            if not content.strip():
                raise AgentError("模型既没有调用工具也没有给出答复（空响应）", exit_code=EXIT_MODEL)
            return content

        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            call_id = call.get("id") or f"call_{turn}_{len(messages)}"
            raw_args = function.get("arguments")

            try:
                if isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = json.loads(raw_args or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments 不是 JSON 对象")
            except (ValueError, TypeError) as exc:
                result = _tool_error(name, "invalid-arguments", f"工具参数不是合法 JSON 对象：{exc}")
                trace.log("tool_result", turn=turn, tool=name, ok=False, code="invalid-arguments")
                print(f"[run {trace.run_index}][turn {turn}][tool] {name} 参数解析失败：{exc}", flush=True)
            else:
                if TOOLS.get(name, {}).get("mutating") and not yes_write:
                    # 这是智能体层的写盘闸：即使模型自己写 confirm=true 也不能写。
                    result = _tool_error(
                        name, "write-not-confirmed",
                        "该工具会写文件，但本次运行没有加 --yes-write；请先向用户展示校验结果并等待确认。")
                else:
                    result = dispatch(name, args)
                ok = bool(result.get("ok"))
                trace.log("tool_result", turn=turn, tool=name, ok=ok,
                          mutating=bool(TOOLS.get(name, {}).get("mutating")),
                          error=result.get("error"),
                          result=result.get("result"))
                print(f"[run {trace.run_index}][turn {turn}][tool] {name} -> "
                      f"{'ok' if ok else 'rejected:' + str((result.get('error') or {}).get('code'))}",
                      flush=True)

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    raise AgentError(f"超过 --max-turns={max_turns} 仍未得到最终答复（已停止，避免无限循环）")


def build_parser() -> argparse.ArgumentParser:
    """命令行参数。默认值尽量从环境变量取，便于在 CI/本地复用。"""
    parser = argparse.ArgumentParser(
        prog="vasp_core.agent",
        description="用 OpenAI 兼容接口驱动 VASP 前处理工具（只读工具 + 确认后写盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python -m vasp_core.agent \"把 POSCAR 扩成 2x2x2，ENCUT 600\" \\\n"
            "      --base-url https://api.deepseek.com/v1 --model deepseek-chat\n"
            "  python -m vasp_core.agent --task-file task.txt --base-url ... --model ... --yes-write\n"
        ),
    )
    parser.add_argument("task", nargs="?", default=None, help="自然语言任务（也可用 --task-file）")
    parser.add_argument("--task-file", default=None, help="从文件读取任务文本")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""),
                        help="OpenAI 兼容 base URL（默认取 OPENAI_BASE_URL）")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", ""),
                        help="模型名（默认取 OPENAI_MODEL）")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY",
                        help="保存 API key 的环境变量名；传空字符串表示不鉴权（默认 OPENAI_API_KEY）")
    parser.add_argument("--max-turns", type=int, default=12,
                        help="最大模型轮数，1–50（默认 12）")
    parser.add_argument("--yes-write", action="store_true",
                        help="允许 mutating 工具（write_inputs）；不加时写盘工具一律被拒")
    parser.add_argument("--runs", type=int, default=1,
                        help="连续跑几次完整任务（1–10，默认 1；用于观察稳定性，首次失败即停）")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单次 HTTP 请求超时秒数（默认 60）")
    parser.add_argument("--trace", default="agent_trace.jsonl",
                        help="trace JSONL 输出路径（默认当前目录 agent_trace.jsonl）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：校验配置 → 连跑 --runs 次 → 返回退出码。

    做什么：把参数/环境/任务文本三类错误在联网前挡住，再进入循环。
    为什么先挡：没有 base_url/model/key 时发请求只会得到晦涩的 401/404，
            用户误以为是工具坏了；明确提示"缺什么"更省时间。
    不这样做：用户看到 HTTP 错误后反复重试，浪费轮次且无信息量。
    还有别的办法：配置文件；当前项目坚持环境变量 + 命令行，够用且可审计。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    # ---- 任务文本 ----
    task = args.task
    if args.task_file:
        if task:
            print("[错误] 不能同时给位置参数 task 和 --task-file", file=sys.stderr)
            return EXIT_USAGE
        task_path = Path(args.task_file).expanduser()
        if not task_path.exists():
            print(f"[错误] --task-file 不存在：{task_path}", file=sys.stderr)
            return EXIT_USAGE
        task = task_path.read_text(encoding="utf-8-sig", errors="replace")
    if not task or not task.strip():
        print("[错误] 缺少任务：给一个位置参数，或用 --task-file 指定文件", file=sys.stderr)
        return EXIT_USAGE
    task = task.strip()

    # ---- 连接配置 ----
    base_url = (args.base_url or "").strip()
    model = (args.model or "").strip()
    if not base_url:
        print("[错误] 缺少 --base-url（或环境变量 OPENAI_BASE_URL）；不会尝试任何网络请求。",
              file=sys.stderr)
        return EXIT_USAGE
    if not model:
        print("[错误] 缺少 --model（或环境变量 OPENAI_MODEL）；不会伪造模型名。", file=sys.stderr)
        return EXIT_USAGE

    api_key: str | None = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            print(f"[错误] 环境变量 {args.api_key_env} 未设置；"
                  f"若本地服务不需要鉴权，请传 --api-key-env ''。", file=sys.stderr)
            return EXIT_USAGE

    if not 1 <= args.max_turns <= 50:
        print("[错误] --max-turns 必须在 1–50 之间", file=sys.stderr)
        return EXIT_USAGE
    if not 1 <= args.runs <= 10:
        print("[错误] --runs 必须在 1–10 之间", file=sys.stderr)
        return EXIT_USAGE
    if args.timeout <= 0:
        print("[错误] --timeout 必须为正数", file=sys.stderr)
        return EXIT_USAGE

    trace_path = Path(args.trace).expanduser()
    trace = TraceWriter(trace_path)
    print(f"[配置] model={model} base_url={_chat_url(base_url)} "
          f"max_turns={args.max_turns} runs={args.runs} yes_write={args.yes_write} "
          f"tools={len(TOOLS)} version={__version__}", flush=True)
    print(f"[配置] trace -> {trace_path}", flush=True)

    try:
        for run_index in range(1, args.runs + 1):
            trace.run_index = run_index
            trace.log("run_start", task=task, yes_write=args.yes_write,
                      max_turns=args.max_turns, model=model)
            print(f"\n===== run {run_index}/{args.runs} =====", flush=True)
            try:
                final = run_once(base_url=base_url, api_key=api_key, model=model,
                                 task=task, max_turns=args.max_turns,
                                 yes_write=args.yes_write, timeout=args.timeout, trace=trace)
            except AgentError as exc:
                trace.log("run_failed", run=run_index, error=exc.message,
                          exit_code=exc.exit_code)
                print(f"\n[失败] {exc.message}", file=sys.stderr)
                return exc.exit_code
            trace.log("run_final", run=run_index, final=final)
            print(f"\n[最终答复]\n{final}", flush=True)
    finally:
        trace.close()

    print(f"\n[完成] trace 已写入 {trace_path}", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
