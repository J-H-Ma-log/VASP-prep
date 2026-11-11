"""统一入口 `python -m vasp_core` 的转发逻辑（不启动服务、不联网）。"""
from __future__ import annotations

import unittest

from vasp_core import __main__ as entry


class TestEntry(unittest.TestCase):
    def test_no_args_prints_usage(self):
        # 没给子命令：打印用法，返回 2（命令行习惯：没说明要做什么算用法错误）
        self.assertEqual(entry.main([]), 2)

    def test_help_returns_zero(self):
        self.assertEqual(entry.main(["-h"]), 0)
        self.assertEqual(entry.main(["--help"]), 0)

    def test_unknown_command(self):
        self.assertEqual(entry.main(["nope"]), 2)

    def test_dispatch_forwards_args(self):
        seen = {}

        def fake(argv):
            seen["argv"] = argv
            return 7

        orig = entry.COMMANDS["batch"]
        entry.COMMANDS["batch"] = fake
        try:
            code = entry.main(["batch", "--root", "x", "--text", "y"])
        finally:
            entry.COMMANDS["batch"] = orig
        self.assertEqual(code, 7)
        self.assertEqual(seen["argv"], ["--root", "x", "--text", "y"])


if __name__ == "__main__":
    unittest.main()
