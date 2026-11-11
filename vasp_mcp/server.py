#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""server.py —— 零依赖的 stdio MCP 服务（JSON-RPC 2.0，newline-delimited JSON）。

它做什么
--------
把 `vasp_core.tools` 里的 8 个工具按 MCP 2024-11-05 协议暴露出去：
  initialize → capabilities.tools
  tools/list → 工具 schema（来自同一个注册表，和 OpenAI 视图同源）
  tools/call → 校验并执行工具，结果用 text content 回传
  ping       → {}

为什么用 stdio + 换行分隔
------------------------
MCP 的 stdio transport 规定：每条消息是一个 JSON-RPC 对象，UTF-8 编码，
以换行分隔，消息内不能有裸换行。这样任何 MCP 客户端（Claude Desktop 等）
不用装额外依赖就能拉起本服务：`python -m vasp_mcp.server`。

错误码规范（JSON-RPC 2.0）
-------------------------
-32700 解析错误（收到非 JSON）
-32600 非法请求（不是对象 / jsonrpc 不是 2.0 / method 不是字符串 / batch）
-32601 方法不存在（本服务只实现 tools 能力）
-32602 参数非法（tools/call 的 name/arguments 结构不对）
-32603 服务内部错误（兜底，绝不把 Python traceback 写给客户端）

注意：**工具执行失败不是协议错误**。MCP 约定 tools/call 用 `isError: true`
在结果里报告业务失败（例如参数校验拒绝、文件不存在），协议层仍是成功响应，
这样客户端能把失败原因原样展示给模型，而不是中断整个会话。

安全边界
--------
本服务没有执行 ssh/scp/qsub 的代码路径；write_inputs 仍要求 confirm=true
且目标目录必须不存在。真正的作业提交永远由人完成。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 允许 `python vasp_mcp/server.py` 直接运行（没有包上下文）：
# 此时需要把仓库根加入 sys.path，才能 import vasp_core。用 -m 运行时不会走到这里。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vasp_core import __version__  # noqa: E402
from vasp_core.tools import dispatch, tool_specs_mcp  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "vasp-prep"
SERVER_VERSION = __version__

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "RpcError",
    "handle_message",
    "serve",
    "selftest",
    "main",
]


class RpcError(Exception):
    """协议层错误：携带 JSON-RPC code，由 serve 统一转成 error 响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def handle_message(message: Any) -> dict | None:
    """处理一条已解析的 JSON-RPC 消息；通知（无 id）返回 None。

    做什么：校验 envelope → 按 method 分发 → 返回 response。
    为什么把"协议错误"和"工具错误"分开：前者用 error 对象（客户端会当传输层故障），
            后者用 tools/call 的 isError（客户端会展示给模型），混用会让模型看不到原因。
    不这样做：工具参数错误也用 -32602，客户端可能直接重连/报协议错，模型拿不到提示。
    还有别的办法：用成熟 MCP SDK；项目要求零依赖，且这里只需要 tools 子集。
    """
    if isinstance(message, list):
        # MCP 客户端不使用 JSON-RPC batch；明确拒绝比静默拆包更安全。
        raise RpcError(INVALID_REQUEST, "不支持批量请求（batch）")
    if not isinstance(message, dict):
        raise RpcError(INVALID_REQUEST, "请求必须是 JSON 对象")
    if message.get("jsonrpc") != "2.0":
        raise RpcError(INVALID_REQUEST, "jsonrpc 字段必须是 '2.0'")

    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(INVALID_REQUEST, "method 必须是非空字符串")

    req_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params")
    if params is not None and not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params 必须是 JSON 对象")

    if method == "initialize":
        # 协议版本按服务端支持返回（MCP 要求：客户端不支持的版本由服务端给出自己的）。
        return None if is_notification else _response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        # 纯通知：没有响应；客户端可能在任何请求之间插入它们。
        return None

    if method == "ping":
        return None if is_notification else _response(req_id, {})

    if method == "tools/list":
        return None if is_notification else _response(req_id, {"tools": tool_specs_mcp()})

    if method == "tools/call":
        call_params = params or {}
        name = call_params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "tools/call 缺少非空字符串参数 name")
        arguments = call_params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "tools/call 的 arguments 必须是对象")

        result = dispatch(name, arguments)
        payload = {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not bool(result.get("ok")),
        }
        return None if is_notification else _response(req_id, payload)

    if method in ("resources/list", "prompts/list", "logging/setLevel"):
        # 本服务只声明 tools 能力；明确返回"方法不存在"，别假装支持。
        raise RpcError(METHOD_NOT_FOUND, f"本服务不实现 method：{method}")

    raise RpcError(METHOD_NOT_FOUND, f"未知 method：{method}")


def serve(stdin, stdout) -> None:
    """从 stdin 逐行读 JSON-RPC，把响应逐行写回 stdout（不缓冲、不丢行）。

    做什么：一行一条消息的主循环，含解析错误与内部错误的兜底。
    为什么逐行 flush：MCP 客户端按行等待响应，不 flush 会一直挂起。
    不这样做：stdout 被缓冲、客户端超时，表现为"服务启动后无响应"。
    还有别的办法：Content-Length 分帧（那是 LSP 风格，不是 MCP stdio 规范）。
    """
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(json.dumps(_error(None, PARSE_ERROR,
                                           f"JSON 解析失败：{exc.msg}（第 {exc.colno} 列）"),
                                    ensure_ascii=False) + "\n")
            stdout.flush()
            continue

        req_id = message.get("id") if isinstance(message, dict) else None
        try:
            response = handle_message(message)
        except RpcError as exc:
            response = _error(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - 协议服务不能因单条请求崩溃
            response = _error(req_id, INTERNAL_ERROR,
                              f"服务内部错误：{type(exc).__name__}: {exc}")

        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def selftest() -> int:
    """进程内自测：initialize → tools/list → 一次只读 tools/call → 错误码检查。

    做什么：不启动 stdio 循环，直接调用 handle_message 断言关键字段。
    为什么要有：部署到新机器/新 Python 时，一条命令就能确认"协议层 + 工具层"
            都活着，而不是等客户端连上来才发现。
    不这样做：MCP 客户端报"服务器无响应"时，还得手工构造 JSON 排查。
    还有别的办法：跑真实子进程客户端；tests/test_tools.py 里另有子进程测试覆盖。
    """
    checks: list[str] = []
    try:
        init = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": PROTOCOL_VERSION,
                                          "capabilities": {}, "clientInfo": {"name": "selftest",
                                                                             "version": "0"}}})
        assert isinstance(init, dict) and init["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert "tools" in init["result"]["capabilities"]
        checks.append("initialize: protocolVersion/capabilities 正常")

        listed = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = listed["result"]["tools"]
        assert len(tools) == 8, f"工具数量应为 8，实际 {len(tools)}"
        assert all({"name", "description", "inputSchema"} <= set(t) for t in tools)
        checks.append(f"tools/list: {len(tools)} 个工具，schema 字段齐全")

        called = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "workspace_status", "arguments": {}}})
        assert called["result"]["isError"] is False
        payload = json.loads(called["result"]["content"][0]["text"])
        assert payload.get("ok") is True and "workspace" in payload["result"]
        checks.append("tools/call: workspace_status 只读调用成功")

        ping = handle_message({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        assert ping["result"] == {}
        checks.append("ping: 正常")

        try:
            handle_message({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"})
        except RpcError as exc:
            assert exc.code == METHOD_NOT_FOUND
            checks.append("错误码: 未知 method → -32601")
        else:
            raise AssertionError("未知 method 没有报错")
    except AssertionError as exc:
        print(f"selftest FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"selftest FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for line in checks:
        print(f"selftest ok: {line}")
    print("selftest PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    """入口：--selftest 走进程内自测，否则进入 stdio 服务循环。

    做什么：解析参数、统一 UTF-8、把启动日志写到 stderr（stdout 只留给协议消息）。
    为什么日志必须走 stderr：stdout 混入任何非 JSON 文本都会让客户端解析失败。
    不这样做：客户端一连接就报"invalid JSON"，排查方向被误导。
    还有别的办法：logging 模块；这里只有一两行启动信息，print 到 stderr 足够。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(
        prog="vasp_mcp.server",
        description="VASP 前处理工具的 MCP stdio 服务（JSON-RPC 2.0）",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="进程内自测（initialize/tools/list/tools/call/ping），成功退出 0")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{SERVER_NAME} {SERVER_VERSION} (MCP {PROTOCOL_VERSION})")
        return 0
    if args.selftest:
        return selftest()

    print(f"[{SERVER_NAME}] MCP stdio 服务已启动：protocol={PROTOCOL_VERSION} "
          f"version={SERVER_VERSION}", file=sys.stderr, flush=True)
    try:
        serve(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
