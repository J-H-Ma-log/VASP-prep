"""弹出"系统原生"的选择文件夹对话框（Windows / macOS / Linux）。

【为什么需要它】
    浏览器出于安全限制，`<input type="file">` 拿不到文件夹的**绝对路径**，
    只能拿到文件名。用户就得自己去资源管理器复制地址栏再粘贴——
    一步多余的操作，而且 Windows 的地址栏复制的是带引号/反斜杠的路径，容易粘错。

【怎么做】
    页面点"选择…" → 前端 GET /api/browse → 后端在这里弹出**原生**对话框
    （Windows 用 WinForms 的 FolderBrowserDialog，macOS 用 osascript，
    Linux 用 zenity/kdialog）。用户选完，路径原样返回前端填进输入框。

【为什么不用 tkinter】
    本工具的网页跑在 chgnet_env（嵌入式 Python）里，那个解释器**没有 tkinter**；
    系统 Python 有，但我们不能为了一个对话框让用户换解释器。
    PowerShell 每个 Windows 都有，且不受解释器影响。

【不这么干会怎样】
    用户必须手打路径；打错一个字符就是"源目录不存在"，而错误信息到不了他眼前。

【另一个办法】
    纯前端可以做"拖拽文件夹"，但拖进浏览器同样拿不到绝对路径，没用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# 对话框可能被用户晾着很久；给足时间，但别无限等
DEFAULT_TIMEOUT = 600

# PowerShell 脚本：弹 FolderBrowserDialog，把选中的路径写进 OutFile
# 说明：
#   - 用 FolderBrowserDialog 而不是 OpenFileDialog：后者只能选文件；
#   - ShowNewFolderButton=$true 让用户可以当场新建目录；
#   - 取消时写空字符串，Python 侧据此返回 None；
#   - 结果写文件而不是 stdout：PowerShell 5.1 的 stdout 编码随代码页变，路径含中文会乱码。
_PS_SCRIPT = r"""
param([string]$Initial, [string]$Title, [string]$OutFile, [switch]$SelfTest)
Add-Type -AssemblyName System.Windows.Forms | Out-Null
Add-Type -AssemblyName System.Drawing | Out-Null

$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $Title
$dialog.ShowNewFolderButton = $true
if ($Initial -and (Test-Path -LiteralPath $Initial)) {
    $dialog.SelectedPath = $Initial
}

# -SelfTest：不弹窗，只验证"对话框对象能不能创建"，用来在安装后自检
if ($SelfTest) {
    [System.IO.File]::WriteAllText($OutFile, "SELFTEST_OK", (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}

# 让对话框尽量出现在最前面（不抢焦点也不会藏到浏览器后面）
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.StartPosition = 'Manual'
$owner.Location = New-Object System.Drawing.Point(-2000, -2000)

$result = $dialog.ShowDialog($owner)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [System.IO.File]::WriteAllText($OutFile, $dialog.SelectedPath, (New-Object System.Text.UTF8Encoding($false)))
} else {
    [System.IO.File]::WriteAllText($OutFile, "", (New-Object System.Text.UTF8Encoding($false)))
}
$owner.Dispose()
"""


class BrowserUnavailable(RuntimeError):
    """系统对话框弹不出来（不是用户取消）。

    区分这两件事很重要：
      - 用户点"取消" → 正常，返回 None，网页提示"已取消"；
      - PowerShell 起不来/没有桌面会话 → 必须报错，否则网页会假装"你取消了"，
        用户反复点也没用，还不知道为什么。
    """


def _temp_dir() -> Path:
    """放临时脚本/结果文件的地方。

    不用 tempfile.mkdtemp()：本机 Python 3.14 上它按 0o700 建目录，
    建出来的目录自己都不可写（见 tests 里的说明）。这里用项目内的 .tmp_dialog。
    """
    d = Path(__file__).resolve().parent.parent / ".tmp_dialog"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pick_windows(initial: str | None, title: str, timeout: int,
                  self_test: bool = False) -> str | None:
    tmp = _temp_dir()
    tag = uuid.uuid4().hex[:8]
    script = tmp / f"pick_{tag}.ps1"
    result = tmp / f"pick_{tag}.txt"
    # utf-8-sig：PowerShell 5.1 默认按 ANSI 读 .ps1，带 BOM 才会正确识别 UTF-8 中文
    script.write_text(_PS_SCRIPT, encoding="utf-8-sig")
    try:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Initial", initial or "",
            "-Title", title,
            "-OutFile", str(result),
        ]
        if self_test:
            cmd.append("-SelfTest")
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                capture_output=True,
                text=True,
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BrowserUnavailable(f"无法启动 Windows 选择对话框（{type(exc).__name__}: {exc}）") from exc

        if self_test:
            if result.exists() and result.read_text(encoding="utf-8", errors="replace").strip() == "SELFTEST_OK":
                return "SELFTEST_OK"
            raise BrowserUnavailable(
                "自检失败：PowerShell 无法创建文件夹对话框。\n"
                f"退出码 {proc.returncode}\n{proc.stderr.strip()[:800]}"
            )

        if not result.exists():
            # 用户取消时脚本一定会写空文件；连文件都没有 = 脚本没跑完/报错
            raise BrowserUnavailable(
                "选择窗口没有正常返回（可能是 PowerShell 报错或无桌面会话）。\n"
                f"退出码 {proc.returncode}\n{proc.stderr.strip()[:800]}"
            )
        text = result.read_text(encoding="utf-8", errors="replace").strip()
        return text or None
    finally:
        for p in (script, result):
            try:
                p.unlink()
            except OSError:
                pass


def _pick_macos(initial: str | None, title: str, timeout: int) -> str | None:
    # AppleScript 的 choose folder 返回 alias，POSIX path of 转成 /Users/...
    default = f' default location POSIX file "{initial}"' if initial else ""
    script = (
        f'POSIX path of (choose folder with prompt "{title}"{default})'
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = (out.stdout or "").strip()
    return path or None


def _pick_linux(initial: str | None, title: str, timeout: int) -> str | None:
    # 优先 zenity（GNOME），其次 kdialog（KDE）；都没有就没法弹，返回 None 让用户手输
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if initial:
            cmd.append(f"--filename={initial}/")
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", initial or str(Path.home()), "--title", title]
    else:
        return None
    try:
        out = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = (out.stdout or "").strip()
    return path or None


def pick_directory(
    initial: str | None = None,
    title: str = "选择文件夹",
    timeout: int = DEFAULT_TIMEOUT,
) -> str | None:
    """弹原生文件夹选择框。

    返回：选中的绝对路径；用户取消、或系统不支持时返回 None。
    【为什么不抛异常】取消是正常操作，不该让网页显示红色错误框。
    """
    if os.name == "nt":
        return _pick_windows(initial, title, timeout)
    if sys.platform == "darwin":
        return _pick_macos(initial, title, timeout)
    return _pick_linux(initial, title, timeout)


def self_test() -> str:
    """不弹窗，只验证"这个系统能不能弹选择框"。安装后可用它自检。"""
    if os.name == "nt":
        return "windows: " + str(_pick_windows(None, "自检", timeout=60, self_test=True))
    if sys.platform == "darwin":
        return "darwin: osascript " + ("ok" if shutil.which("osascript") else "missing")
    for tool in ("zenity", "kdialog"):
        if shutil.which(tool):
            return f"linux: {tool} ok"
    return "linux: 没有 zenity/kdialog，无法弹图形选择框"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="弹系统文件夹选择框（自检用）")
    ap.add_argument("--selftest", action="store_true", help="只验证环境，不弹窗")
    ap.add_argument("--initial", default=None)
    ap.add_argument("--title", default="选择文件夹")
    args = ap.parse_args()
    if args.selftest:
        print(self_test())
    else:
        print(pick_directory(args.initial, args.title) or "(cancelled)")
