#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""vasp_mcp —— 把 vasp_core 的 VASP 前处理工具暴露成 MCP 服务。

用法：
    python -m vasp_mcp.server            # 启动 stdio MCP 服务
    python -m vasp_mcp.server --selftest # 进程内自测，成功退出 0

为什么单独一个包：MCP 是"传输/协议"层，工具实现留在 vasp_core.tools；
这样同一份工具既能给 OpenAI tool-calling 用（vasp_core.agent），
也能给任意 MCP 客户端用，不需要维护两套实现。

为什么这里用惰性导出（__getattr__）而不是直接 `from .server import ...`：
`python -m vasp_mcp.server` 会先导入包再执行子模块，若包初始化时就导入
server，runpy 会报 "found in sys.modules ... prior to execution" 警告。
惰性导出保持 `from vasp_mcp import PROTOCOL_VERSION` 可用，同时让 -m 干净启动。
"""

from __future__ import annotations

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


def __getattr__(name: str):
    """按需加载 server 子模块的属性，避免包初始化与 -m 执行重复导入。"""
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
