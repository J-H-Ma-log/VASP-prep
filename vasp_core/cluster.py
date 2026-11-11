#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cluster.py —— 本机做前处理、超算只负责算：一条命令完成"传上去 + 拼 POTCAR + 提交"。

为什么要有这一层：
  · 前处理（改结构/改 INCAR/生成 KPOINTS）在本机做最舒服：有图形界面、能反复看 diff、
    本机磁盘不挤占超算（你的 home 在 Lustre 上已经用了 98%）；
  · 超算登录节点只该干三件事：拼 POTCAR、qsub、看状态 —— 不该在上面装模型、跑 Python 包；
  · 但"传文件 → 拼赝势 → 提交"这三步手工容易漏（尤其 POTCAR 顺序），所以固化成命令。

它不做的事（故意）：
  · 不替你输入密码（用 ssh key 或自己在终端里输）；
  · 不自动改超算上的文件（只跑 `--potcar-only` 和 `qsub` 这两个明确命令）；
  · 不下载结果（`pull` 只在你显式调用时执行，避免误覆盖本机文件）。

用法（在本机，仓库根目录）：
    python -m vasp_core.cluster push   runs/cu_relax --host 20249980004@219.231.162.66
    python -m vasp_core.cluster submit runs/cu_relax --host ... --pp-dir ~/Optpaw_PBE.54
    python -m vasp_core.cluster status --host ... --job 12345
    python -m vasp_core.cluster pull   runs/cu_relax --host ...
    python -m vasp_core.cluster up     runs/cu_relax --host ... --pp-dir ~/Optpaw_PBE.54

环境变量（省得每次敲）：
    VASP_HOST / VASP_REMOTE（默认 ~/vasp-work）/ VASP_TOOLS（默认 ~/vasp-prep-v2）
    VASP_PP_DIR（默认 ~/Optpaw_PBE.54）
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

DEFAULTS = {
    "host": os.environ.get("VASP_HOST", ""),
    "remote": os.environ.get("VASP_REMOTE", "~/vasp-work"),
    "tools": os.environ.get("VASP_TOOLS", "~/vasp-prep-v2"),
    "pp_dir": os.environ.get("VASP_PP_DIR", "~/Optpaw_PBE.54"),
}


def _say(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str], *, dry_run: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    """跑一条本地命令；dry_run 时只打印不执行。

    【为什么 capture 可选】scp/ssh 要继承终端才能让你输密码、看进度；
    只有 status 这种要读输出的才 capture。
    """
    printable = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        _say(f"[dry-run] {printable}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    _say(f"$ {printable}")
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd)


def _ssh(host: str, command: str, *, dry_run: bool = False, capture: bool = False):
    """在超算上执行一条命令（不改本机状态）。"""
    return _run(["ssh", host, command], dry_run=dry_run, capture=capture)


def _make_archive(run_dir: Path) -> Path:
    """把算例目录打包成 tar.gz（不含本机临时文件与大日志）。

    【为什么用 tar 而不是 rsync】你的超算 PATH 里没有 rsync；
    tar 用 Python 标准库生成，Windows/mac 都一致，且能一条命令解包。
    """
    tmp = Path(tempfile.gettempdir()) / f"vprep_{uuid.uuid4().hex[:8]}.tar.gz"
    skip_names = {".DS_Store", "Thumbs.db"}
    with tarfile.open(tmp, "w:gz") as tf:
        for p in sorted(run_dir.rglob("*")):
            if p.name in skip_names or p.suffix in {".log", ".out"}:
                continue
            tf.add(p, arcname=str(p.relative_to(run_dir)))
    return tmp


def _remote_dir(args) -> str:
    name = args.name or Path(args.run_dir).name
    return f"{args.remote.rstrip('/')}/{name}"


def cmd_push(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        _say(f"[错误] 不是目录：{run_dir}")
        return 2
    if not (run_dir / "POSCAR").exists():
        _say(f"[错误] {run_dir} 里没有 POSCAR，先在本机生成算例（prep --write）")
        return 2
    arc = _make_archive(run_dir)
    try:
        _run(["scp", "-q", str(arc), f"{args.host}:{args.remote.rstrip('/')}/"], dry_run=args.dry_run)
        remote_arc = f"{args.remote.rstrip('/')}/{arc.name}"
        cmd = (f"mkdir -p {_remote_dir(args)} && "
               f"tar xzf {remote_arc} -C {_remote_dir(args)} && rm -f {remote_arc}")
        code = _ssh(args.host, cmd, dry_run=args.dry_run).returncode
    finally:
        arc.unlink(missing_ok=True)
    if code != 0:
        _say(f"[失败] 上传返回码 {code}")
        return code
    _say(f"已上传到 {args.host}:{_remote_dir(args)}")
    return 0


def cmd_submit(args) -> int:
    """在超算上：补 POTCAR（如果本地只写了 spec）→ qsub。"""
    rdir = _remote_dir(args)
    parts = [f"cd {rdir}"]
    if args.pp_dir:
        # 只有本地生成时写了 POTCAR.spec（没有真 POTCAR），才需要这一步
        parts.append(
            f"if [ -f POTCAR.spec ] && [ ! -s POTCAR ]; then "
            f"python3 -m vasp_core.prep --potcar-only . --pp-dir {args.pp_dir}; fi"
        )
    parts.append(f"qsub {args.script}")
    code = _ssh(args.host, " && ".join(f"({p})" for p in parts), dry_run=args.dry_run).returncode
    return code


def cmd_status(args) -> int:
    cmd = f"qstat -u $USER" if not args.job else f"qstat {args.job}"
    proc = _ssh(args.host, cmd, dry_run=args.dry_run, capture=True)
    if proc.stdout:
        print(proc.stdout,end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return proc.returncode


def cmd_pull(args) -> int:
    """把算例目录从超算拉回本机（默认拉到 runs/<name>/remote）。"""
    dest = Path(args.dest) if args.dest else Path(args.run_dir) / "remote"
    dest.mkdir(parents=True, exist_ok=True)
    code = _run(["scp", "-r", "-q", f"{args.host}:{_remote_dir(args)}/.", str(dest)],
                dry_run=args.dry_run).returncode
    if code == 0:
        _say(f"已拉回到 {dest}")
    return code


def cmd_up(args) -> int:
    rc = cmd_push(args)
    if rc != 0:
        return rc
    return cmd_submit(args)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=DEFAULTS["host"], help="user@host（默认取 VASP_HOST）")
    common.add_argument("--remote", default=DEFAULTS["remote"], help="超算上的工作根目录（默认 ~/vasp-work）")
    common.add_argument("--name", default=None, help="超算上的子目录名（默认取本地目录名）")
    common.add_argument("--dry-run", action="store_true", help="只打印要执行的命令，不真跑")

    p = argparse.ArgumentParser(
        prog="vasp_core.cluster",
        description="本机前处理 → 超算计算：推、跑、看、拉",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("例子：\n"
                "  python -m vasp_core.cluster up runs/cu --host me@ln01 --pp-dir ~/Optpaw_PBE.54\n"),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("push", "打包上传"), ("submit", "拼 POTCAR + qsub"),
                            ("pull", "把结果拉回本机"), ("up", "push + submit")):
        sp = sub.add_parser(name, help=help_text, parents=[common])
        sp.add_argument("run_dir", help="本机算例目录")
        if name in ("submit", "up"):
            sp.add_argument("--script", default="vasp.sh", help="超算上的提交脚本（默认 vasp.sh）")
            sp.add_argument("--pp-dir", default=DEFAULTS["pp_dir"],
                            help="超算赝势库（默认 ~/Optpaw_PBE.54；传空字符串可跳过 POTCAR 步骤）")
        if name == "push":
            sp.set_defaults(func=cmd_push)
        elif name == "submit":
            sp.set_defaults(func=cmd_submit)
        elif name == "pull":
            sp.add_argument("--dest", default=None, help="拉回位置（默认 <run_dir>/remote）")
            sp.set_defaults(func=cmd_pull)
        else:
            sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("status", help="看作业状态", parents=[common])
    sp.add_argument("--job", default=None, help="作业号；不填则列出自己的全部作业")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not args.host:
        _say("[错误] 没有超算地址：加 --host user@host，或设环境变量 VASP_HOST")
        return 2
    # 子命令自己的 --pp-dir 优先；顶层没有该参数时用默认值
    if not hasattr(args, "pp_dir"):
        args.pp_dir = DEFAULTS["pp_dir"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
