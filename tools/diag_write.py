"""tools/diag_write.py — 双击 诊断权限.bat 时运行：查清楚"到底哪个目录写不了、为什么"。

为什么需要它：
    网页里报 WinError 5 时，可能是①进程被限制、②目录权限、③杀软/受控文件夹访问。
    只看一句报错分不清。这个脚本对每个候选目录真写一次，并把系统原话记下来。

输出：
    屏幕打印 + 同目录生成 权限诊断.txt（UTF-8），可以直接把这个文件发给我。
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "权限诊断.txt"


def _run(cmd: list[str]) -> str:
    """跑一条系统命令，拿文本输出；失败不抛异常，把错误当内容返回。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要尽量把信息带出来
        return f"<命令失败: {exc}>"


def probe(path: Path) -> tuple[bool, str]:
    """在 path 下真建一个临时目录、写一个文件，再删掉。返回 (能否写, 原因)。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        d = path / f".diag_{os.getpid()}_{datetime.now():%H%M%S%f}"
        d.mkdir()
        f = d / "a.txt"
        f.write_text("ok", encoding="utf-8")
        f.unlink()
        d.rmdir()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    lines: list[str] = []

    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out("=" * 64)
    out("VASP 前处理 · 写入权限诊断")
    out(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    out("=" * 64)
    out(f"Python：{sys.executable}")
    out(f"版本：{sys.version.split()[0]}")
    out(f"工作目录：{Path.cwd()}")
    out(f"程序目录：{ROOT}")
    out("")
    out("【环境变量】")
    for k in ("USERNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERDOMAIN", "SESSIONNAME"):
        out(f"  {k}={os.environ.get(k, '<未设置>')}")
    out(f"  Path.home()={Path.home()}")
    out("")

    out("【逐目录真实写入测试】")
    candidates: list[Path] = [
        ROOT / "runs",
        ROOT,
        Path.home(),
        Path.home() / "vasp_runs",
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop",
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents",
    ]
    for drive in ("D:\\", "E:\\"):
        if Path(drive).exists():
            candidates.append(Path(drive) / "vasp_runs")

    results: dict[str, bool] = {}
    for p in candidates:
        ok, why = probe(p)
        results[str(p)] = ok
        out(f"  [{'可写' if ok else '不可写'}] {p}")
        if why:
            out(f"          原因：{why}")
    out("")

    out("【当前进程身份 / 完整性级别】")
    who = _run(["whoami"])
    out("  " + who.strip().replace("\n", "\n  "))
    groups = _run(["whoami", "/groups"])
    for line in groups.splitlines():
        low = line.lower()
        if "mandatory" in low or "s-1-16" in low or "完整性" in line or "appcontainer" in low:
            out("  " + line.strip())
    out("")

    out("【用户目录 ACL（只看含用户名的行）】")
    acl = _run(["icacls", str(Path.home())])
    user = os.environ.get("USERNAME", "")
    for line in acl.splitlines():
        if user and user.lower() in line.lower():
            out("  " + line.strip())
    out("")

    out("【结论】")
    ws_ok = results.get(str(ROOT / "runs"), False) or results.get(str(ROOT), False)
    home_ok = results.get(str(Path.home()), False)
    if ws_ok and home_ok:
        out("  · 程序目录和用户目录都能写 → 正常。网页里应显示「普通模式」。")
    elif ws_ok and not home_ok:
        out("  · 程序目录可写、用户目录不可写 → 当前进程被限制在工作区内（受限环境）。")
        out("    处理：把网页关掉，从「资源管理器」双击 start_webui.bat 或桌面快捷方式重启；")
        out("    如果从资源管理器启动仍然如此，说明是系统策略/安全软件限制，见下面 ACL。")
    elif not ws_ok:
        out("  · 连程序目录都写不了 → 程序所在磁盘被设成只读，或整个进程被禁止写盘。")
        out("    处理：把 vasp-prep-v2 整个文件夹复制到 D 盘/用户目录下再试。")
    else:
        out("  · 未能判断，请把本文件发给我。")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"诊断结果已保存：{REPORT}")
    print("请把这个文件（或上面最后几行）发给我。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        input("\n按回车关闭…")
        raise
