"""verify.py —— 判定一次 VASP 计算"到底成没成"。

【为什么需要单独一层】
    "任务跑完了" 和 "物理上收敛了" 是两件事：
      · qstat 显示 C（完成）、OUTCAR 最后有 "General timing and accounting"
        → 只说明进程正常退出；
      · 电子步没收敛（OUTCAR 里 "aborting loop because EDIFF is reached"
        是**好**消息；如果出现 "EDIFF is reached" 之外的自洽失败才是坏消息）；
      · 离子步没收敛（没有 "reached required accuracy"）→ 结构还没到极小点，
        这时候拿能量去比较，结论不可信。
    用户看到的"算完了"经常只是第一层。这一层把三层拆开、逐条给证据。

【读什么、按什么优先级】
    1. vasprun.xml（最准）：converged（电子+离子）、final_energy、离子步数、末步力；
    2. OUTCAR 文本（没有 pymatgen 时的退路）：扫尾部标记；
    3. OSZICAR：只看离子步能量序列，判断"最后几步能量是否还在降"。
    读不到任何输出时，明确说"没有找到结果文件"，绝不猜。

【为什么不直接 grep 一个词就下结论】
    单个词会误判：比如 OUTCAR 里有 "aborting loop because EDIFF is reached"
    其实代表电子步收敛；而 "ZBRENT: fatal error" 才是真空洞。所以这里把
    "进程完成 / 电子收敛 / 离子收敛" 分成三个布尔量，再给综合结论。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .backends import has_pymatgen
from .errors import InputError

# OUTCAR / OSZICAR 里认的标记（都是 VASP 自己打印的固定文本）
_RE_TOTEN = re.compile(r"free\s+energy\s+TOTEN\s*=\s*(-?\d+\.\d+)")
_RE_EDIFF_OK = re.compile(r"aborting loop because EDIFF is reached")
_RE_IONIC_OK = re.compile(r"reached required accuracy")
_RE_ZBRENT = re.compile(r"ZBRENT: fatal error")
_RE_TIMING = re.compile(r"General timing and accounting")
_RE_NIONS = re.compile(r"ions per type\s*=\s*(.+)")


def find_outputs(run_dir: str | Path) -> dict[str, Path | None]:
    """在一个算例目录里找结果文件（大小写都认，兼容 CONTCAR/vasprun.xml.gz）。"""
    d = Path(run_dir)
    if not d.exists():
        raise InputError(f"算例目录不存在：{d}")
    names = {
        "vasprun": ["vasprun.xml", "vasprun.xml.gz"],
        "outcar": ["OUTCAR", "OUTCAR.gz"],
        "oszicar": ["OSZICAR"],
        "contcar": ["CONTCAR"],
        "incar": ["INCAR"],
    }
    found: dict[str, Path | None] = {}
    for key, cands in names.items():
        found[key] = next((d / c for c in cands if (d / c).exists()), None)
    return found


def _tail_text(path: Path, nbytes: int = 400_000) -> str:
    """读文件末尾若干字节。

    【为什么只读尾部】OUTCAR 动辄几百 MB，全读进内存既慢又可能撑爆；
        要判断的标记（timing/EDIFF/accuracy/TOTEN）都在结尾附近。
    """
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > nbytes:
            f.seek(size - nbytes)
        raw = f.read()
    return raw.decode("utf-8", errors="replace")


def parse_outcar(path: Path) -> dict[str, Any]:
    """从 OUTCAR 尾部提取：进程是否完成、电子/离子是否收敛、最后能量。"""
    text = _tail_text(path)
    totens = [float(x) for x in _RE_TOTEN.findall(text)]
    m = _RE_NIONS.search(text)
    return {
        "completed": bool(_RE_TIMING.search(text)),
        "electronic_converged": bool(_RE_EDIFF_OK.search(text)),
        "ionic_converged": bool(_RE_IONIC_OK.search(text)),
        "zbrend_error": bool(_RE_ZBRENT.search(text)),
        "last_toten": totens[-1] if totens else None,
        "n_toten_lines": len(totens),
        "ions_line": m.group(1).strip() if m else None,
    }


def parse_oszicar(path: Path) -> dict[str, Any]:
    """从 OSZICAR 提取离子步能量序列，判断最后几步是否还在明显下降。

    【判据】最后 3 步能量差 < 1 meV 视为"能量已平"，只是辅助信号：
        能量平不等于力收敛（结构可能还在缓慢移动），所以只作提示。
    """
    energies: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        # 离子步行形如：  1 F= -.12345678E+02 E0= -.12345678E+02  d E =-.123E-03
        if "F=" in line and "E0=" in line:
            m = re.search(r"E0=\s*(-?\d+\.\d+E[+-]\d+)", line)
            if m:
                energies.append(float(m.group(1)))
    tail = energies[-3:]
    flat = None
    if len(tail) >= 2:
        flat = (max(tail) - min(tail)) < 1e-3
    return {"n_ionic_steps": len(energies), "last_energy": energies[-1] if energies else None,
            "energy_flat_last3": flat}


def verify_run(run_dir: str | Path, *, strict_ionic: bool = True) -> dict[str, Any]:
    """综合判定一次计算，返回结构化结论（不写文件）。

    【converged 的定义】strict_ionic=True 时要求：进程完成 + 电子收敛 + 离子收敛；
        如果只做静态计算（NSW=0，本来就没有离子步），调用方应传 strict_ionic=False，
        或由调用方根据 INCAR 判断——这里留参数，是因为"静态算例没有离子步"
        不该被判成失败。
    """
    files = find_outputs(run_dir)
    info: dict[str, Any] = {
        "run_dir": str(Path(run_dir).resolve()),
        "files": {k: (str(v) if v else None) for k, v in files.items()},
        "source": None,
        "converged": False,
        "notes": [],
    }

    if files["vasprun"] and has_pymatgen():
        from .backends.pmg_backend import read_vasprun

        vr = read_vasprun(files["vasprun"])
        info.update({
            "source": "vasprun",
            "converged_electronic": vr["converged_electronic"],
            "converged_ionic": vr["converged_ionic"],
            "final_energy": vr["final_energy"],
            "n_ionic_steps": vr["n_ionic_steps"],
            "max_force": vr.get("max_force"),
            "final_volume": vr.get("final_volume"),
            "final_natoms": vr.get("final_natoms"),
        })
        info["converged"] = bool(vr["converged"]) if strict_ionic else bool(vr["converged_electronic"])
        if not vr["converged_ionic"]:
            info["notes"].append("离子步未收敛：结构还没到极小点，能量不能直接用于比较。")
    elif files["outcar"]:
        oc = parse_outcar(files["outcar"])
        info.update({"source": "outcar", **oc})
        info["final_energy"] = oc["last_toten"]
        info["converged"] = bool(
            oc["completed"]
            and oc["electronic_converged"]
            and (oc["ionic_converged"] or not strict_ionic)
        )
        if not oc["completed"]:
            info["notes"].append("OUTCAR 没有 'General timing and accounting'：进程可能被杀或还没算完。")
        if oc["zbrend_error"]:
            info["notes"].append("OUTCAR 里有 'ZBRENT: fatal error'：离子步线搜索失败，结构需要人工调整。")
    elif files["oszicar"]:
        osz = parse_oszicar(files["oszicar"])
        info.update({"source": "oszicar", **osz})
        info["final_energy"] = osz["last_energy"]
        info["notes"].append("只有 OSZICAR，无法判断电子/离子收敛；请提供 OUTCAR 或 vasprun.xml。")
    else:
        info["notes"].append("目录里没有 vasprun.xml / OUTCAR / OSZICAR，无法判定。")

    if info["final_energy"] is not None and info.get("final_natoms"):
        info["energy_per_atom"] = round(info["final_energy"] / info["final_natoms"], 6)

    # 静态算例的提示：没有离子步时，strict_ionic=True 会误判成失败，明确说出来
    if strict_ionic and info.get("n_ionic_steps") == 0 and info["source"] == "vasprun":
        info["notes"].append(
            "该算例没有离子步（NSW=0？）。如果是静态计算，请加 --no-strict-ionic 重新判定。"
        )
    return info


def render_report(info: dict[str, Any]) -> str:
    """把 verify 结果渲染成中文 markdown 报告。"""
    def yn(x: Any) -> str:
        if x is None:
            return "未知"
        return "是" if x else "否"

    lines = [
        "# 结果核验报告",
        "",
        f"- 算例目录：`{info['run_dir']}`",
        f"- 数据来源：{info['source'] or '无'}",
        f"- **综合结论：{'✅ 已收敛' if info['converged'] else '❌ 未收敛/无法判定'}**",
        "",
        "## 三层判据",
        "",
        "| 判据 | 结果 | 为什么重要 |",
        "|---|---|---|",
        f"| 进程正常结束 | {yn(info.get('completed'))} | 只说明没被杀，不代表物理收敛 |",
        f"| 电子步收敛 | {yn(info.get('converged_electronic'))} | 自洽没收敛时能量没有意义 |",
        f"| 离子步收敛 | {yn(info.get('converged_ionic'))} | 力没到阈值时结构不是极小点 |",
        "",
        "## 数值",
        "",
    ]
    if info.get("final_energy") is not None:
        lines.append(f"- 最后总能：{info['final_energy']} eV")
    if info.get("energy_per_atom") is not None:
        lines.append(f"- 每原子能量：{info['energy_per_atom']} eV/atom")
    if info.get("n_ionic_steps") is not None:
        lines.append(f"- 离子步数：{info['n_ionic_steps']}")
    if info.get("max_force") is not None:
        lines.append(f"- 末步最大残余力：{info['max_force']} eV/Å")
    if info.get("final_volume") is not None:
        lines.append(f"- 末态体积：{info['final_volume']} Å³")
    lines += ["", "## 提示", ""]
    if info["notes"]:
        lines += [f"- {n}" for n in info["notes"]]
    else:
        lines.append("- 没有发现异常。")
    lines += ["", "> 收敛 ≠ 结论正确：还要检查 ENCUT/κ 点/真空/磁序是否合理。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI：`python -m vasp_core.verify DIR [--json] [--no-strict-ionic] [--write]`。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(prog="vasp_core.verify", description="核验 VASP 计算结果是否真的收敛")
    ap.add_argument("run_dir", help="算例目录（含 vasprun.xml / OUTCAR / OSZICAR）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给工具层/智能体用）")
    ap.add_argument("--no-strict-ionic", action="store_true", help="静态计算：不要求离子步收敛")
    ap.add_argument("--write", action="store_true", help="把 verify_report.md 写进算例目录")
    args = ap.parse_args(argv)

    info = verify_run(args.run_dir, strict_ionic=not args.no_strict_ionic)
    if args.json:
        out = {k: v for k, v in info.items() if k not in ("final_structure",)}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_report(info))
    if args.write:
        p = Path(args.run_dir) / "verify_report.md"
        p.write_text(render_report(info), encoding="utf-8")
        print(f"已写入 {p}")
    return 0 if info["converged"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
