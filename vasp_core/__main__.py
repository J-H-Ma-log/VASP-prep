"""统一入口：`python -m vasp_core <子命令>`。

【为什么要这个】
以前有五个入口（prep / webui / chat / batch / db），新手要先记住模块名再拼参数，
容易在"我该用哪个"上卡住。这里只做**转发**，不放任何业务逻辑——
`prep.main` 仍然是原来那个函数，行为、退出码、帮助都一模一样。

【常用】
    python -m vasp_core webui      # 网页版（推荐）
    python -m vasp_core chat       # 终端版
    python -m vasp_core prep 配方.json
    python -m vasp_core batch --root D:\\models --text "扩成 2x2x1"
    python -m vasp_core db search --provider mp --formula MoS2
"""
from __future__ import annotations

import sys

from . import batch, chat, db, prep, webui

USAGE = """VASP 前处理 · 统一入口

用法：
  python -m vasp_core <子命令> [参数...]

子命令：
  webui        启动本地网页（127.0.0.1:8765，推荐日常使用）
  chat         终端交互版（一步一步问）
  prep         执行一个配方 JSON（默认只试算，加 --write 才落盘）
  batch        批量：扫描材料、参数扫描、全部写入
  db           数据库检索 / 下载 / 导入（MP、OQMD、AFLOW、2Dmatpedia）

例子：
  python -m vasp_core webui
  python -m vasp_core prep my_recipe.json --json
  python -m vasp_core batch --root D:\\models --text "扩成 2x2x1" --ivdw 0,11 --write
  python -m vasp_core db search --provider mp --formula MoS2 --limit 5

每个子命令都支持 `-h`，例如：python -m vasp_core batch -h
"""

# 只做转发；键就是网页/文档里出现的名字，避免两套叫法
COMMANDS = {
    "webui": webui.main,
    "chat": chat.main,
    "prep": prep.main,
    "batch": batch.main,
    "db": db.main,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        # 不带子命令时打印用法并返回 2：命令行习惯上"没给要做的事"算用法错误
        print(USAGE)
        return 0 if args else 2

    cmd, rest = args[0], args[1:]
    func = COMMANDS.get(cmd)
    if func is None:
        print(f"[错误] 不认识的子命令：{cmd}")
        print("可用：" + "、".join(COMMANDS))
        return 2
    return func(rest)


if __name__ == "__main__":
    raise SystemExit(main())
