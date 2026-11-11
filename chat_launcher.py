"""双击/命令行启动终端版（给"嵌入式 Python"用的垫片）。

和 webui_launcher.py 同一个原因：chgnet_env 的 sys.path 被 ._pth 钉死，
`python -m vasp_core.chat` 会 ModuleNotFoundError；运行 .py 文件时脚本目录
会自动进 sys.path[0]，所以用这个入口。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vasp_core.chat import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
