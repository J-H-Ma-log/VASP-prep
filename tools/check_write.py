#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_write.py —— 写入权限自检（不用改任何 VASP 文件，只建临时目录再删）。

什么时候用：
    网页上点“写入”报 `PermissionError: [WinError 5] 拒绝访问` 时，双击
    仓库根目录的 `检查写入权限.bat`，把它打印的结果发我。

它检查三件事：
    1) 当前 python 进程是谁（pid / 用户 / 解释器路径 / 工作目录）；
    2) 几个候选父目录能不能“建目录 + 写文件 + 删除”；
    3) 给出建议：能写的位置列表、不能写的位置及系统原话。

为什么单独写一个脚本：
    网页进程跑在后台，报错只有一行；这里把探测过程逐条打印出来，
    才能分清是“桌面被受控文件夹访问拦了”还是“沙箱只允许写工作区”。
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 候选位置：仓库、桌面测试目录、C:\vasp_runs、用户临时目录
CANDIDATES = [
    ROOT,
    Path.home() / "Desktop" / "test",
    Path.home() / "Desktop" / "test" / "MoS2test",
    Path("C:/vasp_runs"),
    Path(tempfile.gettempdir()),
]


def probe(parent: Path) -> tuple[bool, str]:
    """在 parent 下建一个临时目录、写一个文件、再删掉。"""
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe_dir = parent / f".vasp_prep_probe_{uuid.uuid4().hex[:8]}"
        probe_dir.mkdir()
        (probe_dir / "probe.txt").write_text("ok", encoding="utf-8")
        (probe_dir / "probe.txt").unlink()
        probe_dir.rmdir()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=" * 64)
    print("VASP 前处理 · 写入权限自检")
    print("=" * 64)
    print(f"python      : {sys.executable}")
    print(f"version     : {sys.version.split()[0]}")
    print(f"pid         : {os.getpid()}")
    print(f"用户        : {getpass.getuser()}")
    print(f"工作目录    : {Path.cwd()}")
    print(f"仓库目录    : {ROOT}")
    print("-" * 64)

    ok_dirs: list[Path] = []
    bad: list[tuple[Path, str]] = []
    for parent in CANDIDATES:
        ok, msg = probe(parent)
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {parent}")
        if ok:
            ok_dirs.append(parent)
        else:
            print(f"        {msg}")
            bad.append((parent, msg))

    print("-" * 64)
    if ok_dirs:
        print("可以写入的位置（把网页里的“输出目录”换到这些下面即可）：")
        for d in ok_dirs:
            print(f"  - {d}")
    if bad:
        print()
        print("不能写的位置与系统原话（上面已逐条列出）。")
        print("常见原因：")
        print("  1) 桌面/文档受「受控文件夹访问」或杀毒保护 → 换到 C:\\vasp_runs，")
        print("     或在 Windows 安全中心→勒索软件防护→受控文件夹访问 里允许 python.exe；")
        print("  2) 文件夹只读 / OneDrive 同步占用 / 权限受限 → 换目录；")
        print("  3) 程序跑在受限沙箱（只允许写工作区）→ 用普通双击方式启动网页。")
    print("=" * 64)
    return 0 if ok_dirs else 1


if __name__ == "__main__":
    sys.exit(main())
