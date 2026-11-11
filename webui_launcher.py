"""双击/命令行启动网页的入口（给"嵌入式 Python"用的垫片）。

【为什么需要这个文件】
    项目自带的 chgnet_env 是 Windows 嵌入式 Python，它的 sys.path 被 ._pth 钉死了，
    既不认当前目录、也不认 PYTHONPATH——直接 `python -m vasp_core.webui` 会报
    ModuleNotFoundError: No module named 'vasp_core'。
    但"直接运行一个 .py 文件"时，Python 会把该文件所在目录放进 sys.path[0]，
    所以用一个 5 行的脚本把项目根塞进去，就能正常运行。

【不这么干会怎样】双击 .bat 会闪退，并留下一行看不懂的 ModuleNotFoundError。

【别的办法】用系统 Python（3.14）跑网页也行，但那样切表面/吸附质需要 pymatgen，
    系统 Python 里没有；用 chgnet_env 这个解释器才是"全功能"的。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把项目根（本文件所在目录）放到模块搜索路径最前面
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vasp_core.webui import main  # noqa: E402  （必须在 sys.path 改完之后导入）

if __name__ == "__main__":
    raise SystemExit(main())
