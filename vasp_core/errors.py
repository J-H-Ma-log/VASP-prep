#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""errors.py —— 工具包里所有"可预期错误"的异常类型。

为什么要单独定义异常，而不是到处 sys.exit()：
  【做】把"哪一类问题"编码进异常类 【因】上层要按类型决定退出码与提示
  【坑】直接 sys.exit(1)，调用方（比如评测脚本、MCP 工具）分不清是输入错还是代码错
  【替】也可以返回 (ok, msg) 元组，但异常更不容易被漏检查

退出码约定（prep.py 用）：
  2 = 输入/参数问题（配方不存在、JSON 坏了、字段缺失）
  3 = 策略拒绝（白名单外的键、危险键、不支持的 op）
  4 = 结构操作失败（索引越界、元素顺序冲突、POTCAR 缺失等）
  5 = 文件冲突（输出目录已存在、磁盘空间不足）
  6 = 校验失败（validate 发现 ERROR）
"""

from __future__ import annotations


class PrepError(Exception):
    """所有可预期错误的基类；code 就是进程退出码。"""

    code = 2

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 只影响显示
        if self.detail:
            return f"{self.message}\n{self.detail}"
        return self.message


class InputError(PrepError):
    """配方/参数/文件读不了。"""

    code = 2


class PolicyError(PrepError):
    """被策略拒绝：危险键、白名单、未支持的 op。"""

    code = 3


class StructureError(PrepError):
    """结构操作失败：索引越界、元素未知、POTCAR 缺失。"""

    code = 4


class FileConflict(PrepError):
    """输出目录已存在、磁盘空间不足等。"""

    code = 5


class ValidationError(PrepError):
    """校验器发现 ERROR。"""

    code = 6
