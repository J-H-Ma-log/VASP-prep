#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prep.py —— 命令行入口：配方 JSON → 一个可以直接 qsub 的算例目录。

一条命令做完全部前处理，但**默认不写任何东西**（--dry-run 是默认行为之一）：
    python -m vasp_core.prep recipes/cu_co_relax.json              # 预览（dry-run）
    python -m vasp_core.prep recipes/cu_co_relax.json --write      # 真写新目录
    python -m vasp_core.prep recipes/cu_co_relax.json --write --out runs/manual

设计上的四条硬规则（都是为了"出错也不会毁掉你的算例"）：
  1. 只写新目录：目标已存在就退出（除非显式 --force），绝不覆盖原 POSCAR/INCAR；
  2. 先校验后写盘：任何 ERROR 都不落文件，退出码 6；
  3. 每一步都留证据：change_report.md + INCAR.diff + 全局 audit.jsonl；
  4. POSCAR 的 scale 统一归一成 1.0（Å 单位），避免 scale 与操作叠加出错。

退出码（给脚本/智能体判断用）：
  0 成功；2 输入错误（配方/POSCAR 读不了）；3 策略拒绝（危险键、需要 pymatgen 的操作）；
  4 结构错误（元素/索引/矩阵不合法）；5 文件冲突（输出目录已存在）；6 校验未通过。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import tempfile
from pathlib import Path

from .errors import FileConflict, PrepError, ValidationError
from .incar import build_incar, check_consistency, parse_incar
from .kpoints import check_kpoints, render_kpoints
from .backends import describe as backend_describe
from .ml import magmom_file_text, predict_magmom, pre_relax, suggest_incar
from .verify import render_report as render_verify_report, verify_run
from .potcar import (
    check_order,
    choose,
    concat,
    scan_library,
    spec_text,
    total_enmax,
)
from .poscar import read_poscar, write_poscar_text
from .recipe import Recipe, apply_ops, load_recipe
from .report import make_change_report, make_diff, write_audit

DEFAULT_AUDIT = Path.home() / ".vasp-prep" / "audit.jsonl"


def _issue_dicts(issues) -> list[dict]:
    """把 incar.Issue 对象或 dict 统一成 dict。"""
    out = []
    for i in issues:
        if isinstance(i, dict):
            out.append(i)
        else:
            out.append({"level": getattr(i, "level", "WARN"),
                        "code": getattr(i, "code", "issue"),
                        "message": getattr(i, "message", str(i))})
    return out


def _incar_dict(lines) -> dict[str, str]:
    """把最终 INCAR 行转成 {键: 值文本}，供一致性检查用（跳过注释）。"""
    d: dict[str, str] = {}
    for line in lines:
        if line.key and not line.key.startswith("#"):
            d[line.key.upper()] = str(line.value)
    return d


def _resolve_potcar_dir(recipe: Recipe, cli_dir: str | None) -> Path | None:
    """确定赝势库目录：命令行 > 配方 > 环境变量。

    相对路径的语义：命令行给的相对当前工作目录（和 shell 习惯一致）；
    配方里给的相对配方所在目录（配方放哪都能跑）。
    """
    if cli_dir:
        return Path(cli_dir).expanduser()
    cand = recipe.potcar.get("dir")
    if cand:
        p = Path(str(cand)).expanduser()
        return p if p.is_absolute() else (recipe.base_dir / p)
    for env in ("PMG_VASP_PSP_DIR", "VASP_PP_PATH"):
        v = os.environ.get(env)
        if v:
            return Path(v).expanduser()
    return None


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再改名：避免中途出错留下半个 INCAR。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vasp-prep",
        description="把配方 JSON 变成 VASP 算例目录（默认只预览，--write 才落盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python -m vasp_core.prep recipes/cu_co_relax.json\n"
            "  python -m vasp_core.prep recipes/cu_co_relax.json --write\n"
            "  python -m vasp_core.prep r.json --write --pp-dir ~/Optpaw_PBE.54\n"
        ),
    )
    p.add_argument("recipe", nargs="?", default=None,
                   help="配方 JSON 路径（用 --potcar-only 时可省略）")
    p.add_argument("--potcar-only", metavar="DIR", default=None,
                   help="只给已生成好的算例目录补 POTCAR（本地只写 spec、到超算再拼时用）")
    p.add_argument("--write", action="store_true", help="真正写文件（不加只做 dry-run）")
    p.add_argument("--out", default=None, help="输出目录（默认 runs/<name>_<时间戳>）")
    p.add_argument("--force", action="store_true", help="输出目录已存在时仍写入（会覆盖同名文件）")
    p.add_argument("--pp-dir", default=None, help="赝势库目录（覆盖配方与环境变量）")
    p.add_argument("--potcar-mode", choices=["auto", "spec", "skip"], default=None,
                   help="auto=拼 POTCAR；spec=只写 POTCAR.spec；skip=不碰赝势")
    p.add_argument("--json", action="store_true", help="以 JSON 输出摘要（给智能体读）")
    p.add_argument("--quiet", action="store_true", help="只输出最终路径与错误")
    p.add_argument("--no-audit", action="store_true", help="不写全局审计日志")
    p.add_argument("--audit", default=str(DEFAULT_AUDIT), help=f"审计日志路径（默认 {DEFAULT_AUDIT}）")
    # ---- v2.1：能力自检 / 机器学习势 / 对称性 / 能带 / 结果核验 ----
    p.add_argument("--doctor", action="store_true",
                   help="自检：Python、后端（pymatgen/ase/chgnet）、赝势库、配置，然后退出")
    p.add_argument("--ml", choices=["off", "relax", "magmom", "both"], default="off",
                   help="机器学习势：relax=CHGNet 预弛豫；magmom=预测磁矩建议；both=都做")
    p.add_argument("--ml-model", default="chgnet", choices=["chgnet", "emt"],
                   help="预弛豫用的势（emt 仅用于验证流程，不能作为科研结论）")
    p.add_argument("--ml-device", default="cpu", help="chgnet 设备：cpu / cuda")
    p.add_argument("--ml-fmax", type=float, default=0.05, help="预弛豫的力收敛阈值（eV/Å）")
    p.add_argument("--ml-steps", type=int, default=100, help="预弛豫最大步数")
    p.add_argument("--symmetry", action="store_true", help="用 pymatgen/spglib 算空间群与点群")
    p.add_argument("--kpath", action="store_true",
                   help="生成能带用 line-mode KPOINTS（写 KPOINTS_line.txt，供 band 计算用）")
    p.add_argument("--verify", metavar="DIR", default=None,
                   help="生成完顺手核验该算例目录的结果（等价于 python -m vasp_core.verify DIR）")
    p.add_argument("--no-strict-ionic", action="store_true",
                   help="核验时允许离子步未收敛（静态计算/单点算例用）")
    return p


def run(args: argparse.Namespace) -> dict:
    """主流程；返回摘要 dict（同时用于 --json 输出与退出码判断）。"""
    recipe = load_recipe(args.recipe)
    if not recipe.structure_file.exists():
        raise PrepError(f"源结构不存在：{recipe.structure_file}", detail="检查配方 structure.file 与 base_dir")

    # 1) 结构
    before = read_poscar(recipe.structure_file)
    after, ops_log = apply_ops(before, recipe.ops)

    # 1.5) 机器学习势预弛豫（可选）：把"手搭的初始构型"先松到力 < fmax
    #     放在 INCAR 之前，是因为后面的一致性检查（如 ENCUT vs ENMAX）只看原子数，
    #     而预弛豫不改变元素/原子数，只改坐标与晶胞。
    ml_info: dict | None = None
    ml_issues: list[dict] = []
    if args.ml in ("relax", "both"):
        after, ml_info = pre_relax(
            after, fmax=args.ml_fmax, steps=args.ml_steps,
            model=args.ml_model, device=args.ml_device,
        )
        detail = (f"{args.ml_model} 预弛豫：{ml_info['n_steps']} 步，"
                  f"E={ml_info['energy_per_atom_eV']} eV/atom，fmax={ml_info['fmax_actual']} eV/Å")
        ops_log.append({"step": len(ops_log) + 1, "op": "ml_relax", "detail": detail})
        if not ml_info["converged"]:
            ml_issues.append({
                "level": "WARN", "code": "ml-not-converged",
                "message": f"ML 预弛豫未在 {args.ml_steps} 步内收敛（fmax={ml_info['fmax_actual']}）；"
                           f"建议加大 --ml-steps 或检查初始结构。",
            })
        if args.ml_model == "emt":
            ml_issues.append({
                "level": "WARN", "code": "ml-emt-smoke-only",
                "message": "用的是 EMT（仅用于验证流程），能量不能作为科研结论。",
            })

    # 1.6) 磁矩建议（可选）：只产建议，不擅自改写用户的 INCAR
    magmom: dict | None = None
    if args.ml in ("magmom", "both"):
        magmom = predict_magmom(after, device=args.ml_device)
        magmom["suggestion"] = suggest_incar(magmom)

    # 1.7) 对称性（可选）
    sym_info: dict | None = None
    if args.symmetry:
        from .backends.pmg_backend import symmetry as _symmetry

        sym_info = _symmetry(after)

    # 2) INCAR：底稿 = 配方 incar.base（相对 base_dir）或同目录 INCAR
    base_text = None
    if recipe.incar_base:
        bp = (recipe.base_dir / recipe.incar_base).resolve()
        if not bp.exists():
            raise PrepError(f"配方指定了 incar.base={recipe.incar_base}，但文件不存在：{bp}")
        base_text = bp.read_text(encoding="utf-8-sig")
    else:
        default_base = recipe.base_dir / "INCAR"
        if default_base.exists():
            base_text = default_base.read_text(encoding="utf-8-sig")

    incar_text, changes, incar_issues, lines = build_incar(
        base_text=base_text,
        preset=recipe.incar_preset,
        overrides=recipe.incar_overrides,
        natoms=after.natoms,
    )
    # 纯结构编辑（用户没要求改参数、也没给覆盖）时，INCAR 原样复制。
    # 【因】build_incar 会统一写成 "KEY = VALUE"，行内空格/注释位置会变；
    #      虽然数值一样，但网页上会出现一整片无意义的 INCAR diff，让人以为改了什么。
    if base_text is not None and not changes and not recipe.incar_overrides:
        incar_text = base_text

    # 3) POTCAR
    potcar_mode = args.potcar_mode or recipe.potcar.get("mode") or "auto"
    titles: list[str] = []
    potcar_text: str | None = None
    potcar_issues: list[dict] = []
    potcar_note = ""
    pp_dir = _resolve_potcar_dir(recipe, args.pp_dir)
    species_order = list(dict.fromkeys(after.atom_species()))
    if potcar_mode == "skip":
        potcar_note = "按配方要求跳过了赝势拼接；请自行确认 POTCAR。"
    elif pp_dir is None:
        if potcar_mode == "auto":
            potcar_issues.append({"level": "WARN", "code": "potcar-nodir",
                                  "message": "没找到赝势库目录（--pp-dir / potcar.dir / PMG_VASP_PSP_DIR 都为空），"
                                             "改为只写 POTCAR.spec，请到超算上按 spec 拼接。"})
        potcar_mode = "spec"
    else:
        lib = scan_library(pp_dir)
        entries = choose(lib, species_order, mapping=recipe.potcar.get("map"))
        titles = [e.title for e in entries]
        potcar_issues.extend(check_order(entries, species_order))
        if potcar_mode == "auto":
            potcar_text = concat(entries)
            potcar_note = f"POTCAR 由 {pp_dir} 拼接（{' '.join(titles)}）"
        else:
            potcar_note = f"只写了 POTCAR.spec（{' '.join(titles)}），POTCAR 请在超算上拼接"

    # 4) KPOINTS / 一致性
    #    【为什么"没要求改"也要复制一份】输出目录要能直接传到超算跑，
    #    四件套缺一个就得回源目录翻文件；复制不改内容，报告里会写明是原样复制。
    kpoints_text = render_kpoints(recipe.kpoints) if recipe.kpoints else None
    kpoints_copied = False
    if kpoints_text is None:
        src_kp = recipe.base_dir / "KPOINTS"
        if src_kp.exists():
            kpoints_text = src_kp.read_text(encoding="utf-8-sig", errors="replace")
            kpoints_copied = True
    is_slab = any(o["op"] == "vacuum" for o in recipe.ops)
    kp_issues = _issue_dicts(check_kpoints(
        recipe.kpoints, lattice=after.lattice, is_slab=is_slab))

    enmax = total_enmax(choose(scan_library(pp_dir), species_order,
                               mapping=recipe.potcar.get("map"))) if potcar_text else None
    cons_issues = _issue_dicts(check_consistency(
        incar=_incar_dict(lines),
        natoms=after.natoms,
        counts=after.counts,
        species=species_order,
        calc_type=recipe.calc,
        potcar_enmax=enmax,
        has_kpoints_file=kpoints_text is not None,
    ))
    incar_issues_d = _issue_dicts(incar_issues)
    all_issues = incar_issues_d + cons_issues + potcar_issues + kp_issues
    errors = [i for i in all_issues if i["level"] == "ERROR"]
    warns = [i for i in all_issues if i["level"] == "WARN"]

    # 5) 输出目录
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out).expanduser().resolve() if args.out else \
        (recipe.path.parent / "runs" / f"{recipe.name}_{stamp}")
    if out_dir.exists() and not args.force:
        raise FileConflict(
            f"输出目录已存在：{out_dir}\n"
            f"（不会覆盖已有算例。换一个 --out，或确认要覆盖时加 --force）"
        )

    summary = {
        "ok": not errors,
        "dry_run": not args.write,
        "recipe": str(recipe.path),
        "out_dir": str(out_dir),
        "structure": {
            "natoms": [before.natoms, after.natoms],
            "species": species_order,
            "volume": [round(before.volume(), 4), round(after.volume(), 4)],
        },
        "ops": ops_log,
        "incar_changes": [{"key": c.key, "old": str(c.old), "new": str(c.new), "reason": c.reason}
                          for c in changes],
        "kpoints": recipe.kpoints.as_dict() if recipe.kpoints else None,
        "potcar": {"mode": potcar_mode, "titles": titles, "note": potcar_note},
        "issues": all_issues,
        "errors": [i["code"] for i in errors],
        "warnings": [i["code"] for i in warns],
    }

    if not args.write:
        summary["message"] = "dry-run：没有写任何文件；确认无误后加 --write"
        return summary

    if errors:
        raise ValidationError(
            "校验未通过，已中止，没有写任何文件。\n" +
            "\n".join(f"  [{i['level']}] {i['code']}: {i['message']}" for i in all_issues if i["level"] == "ERROR")
        )

    # 6) 真正写盘
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(out_dir / "POSCAR", write_poscar_text(after, comment=None))
    _atomic_write(out_dir / "INCAR", incar_text)
    if kpoints_text is not None:
        _atomic_write(out_dir / "KPOINTS", kpoints_text)
    if potcar_text is not None:
        _atomic_write(out_dir / "POTCAR", potcar_text)
    if potcar_mode != "skip" and titles:
        _atomic_write(out_dir / "POTCAR.spec", "".join(f"{t}\n" for t in titles))

    diff_text = make_diff(base_text or "", incar_text, "INCAR")
    if diff_text:
        _atomic_write(out_dir / "INCAR.diff", diff_text)
    report = make_change_report(
        recipe_name=recipe.name,
        recipe_path=recipe.path,
        source_structure=recipe.structure_file,
        before=before,
        after=after,
        ops_log=ops_log,
        incar_changes=changes,
        incar_issues=incar_issues_d + cons_issues,
        potcar_issues=potcar_issues,
        kpoints_text=kpoints_text,
        potcar_titles=titles,
        out_dir=out_dir,
        potcar_note=potcar_note,
    )
    _atomic_write(out_dir / "change_report.md", report)

    if not args.no_audit:
        try:
            write_audit(Path(args.audit).expanduser(), {
                "ts": stamp,
                "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
                "host": socket.gethostname(),
                "platform": platform.platform(),
                "recipe": str(recipe.path),
                "out_dir": str(out_dir),
                "incar_changes": summary["incar_changes"],
                "warnings": summary["warnings"],
            })
        except OSError as exc:  # 审计失败不影响主流程，但要说出来
            summary.setdefault("notes", []).append(f"审计日志写入失败：{exc}")

    summary["files"] = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    summary["message"] = f"已写入 {out_dir}"
    return summary


def _unique(seq: list[str]) -> list[str]:
    """按首次出现顺序去重（POTCAR 需要元素顺序，不要逐原子重复）。"""
    seen: list[str] = []
    for x in seq:
        if x not in seen:
            seen.append(x)
    return seen


def run_potcar_only(target: str, pp_dir: str | None, *, quiet: bool = False) -> dict:
    """给一个已经生成好的算例目录补 POTCAR（本地写 spec、超算上拼真赝势时用）。

    为什么单独一个模式：本机没有赝势库、超算又没有网和 pymatgen。
    所以本地生成时只写 `POTCAR.spec`（一行一个元素变体），把目录传上去后，
    在超算跑这一条命令：读 POSCAR 元素顺序 → 扫赝势库 → 拼接 → 校验 TITEL 顺序。
    """
    d = Path(target).expanduser().resolve()
    poscar = d / "POSCAR"
    if not poscar.exists():
        raise PrepError(f"目录里没有 POSCAR：{d}", detail="先在本机生成算例目录，再整体传上来")
    st = read_poscar(poscar)
    # POTCAR 一个元素一段，不是一原子一段：用元素顺序（去重），不是逐原子列表
    species = list(st.species) if st.species else _unique(st.atom_species())
    if not pp_dir:
        raise PrepError("没有赝势库目录", detail="加 --pp-dir ~/Optpaw_PBE.54，或设 PMG_VASP_PSP_DIR")
    root = Path(pp_dir).expanduser()
    lib = scan_library(root)
    notes: list[str] = []
    if not lib:
        raise PrepError(f"在 {root} 下没找到任何 POTCAR 文件",
                        detail=f"检查 ls {root} 是否有 potpaw_PBE.54/ 之类的子目录")
    mapping: dict[str, str] = {}
    spec = d / "POTCAR.spec"
    if spec.exists():
        # spec 里是"元素变体"（如 Cu_pv），把它当显式映射，避免自动选到别的变体
        for line in spec.read_text(encoding="utf-8-sig").splitlines():
            tok = line.strip()
            if not tok:
                continue
            el = tok.split("_")[0][:2] if not tok[1:2].islower() else tok[:2]
            mapping[el] = tok
    entries = choose(lib, species, mapping=mapping)
    text = concat(entries)
    out = d / "POTCAR"
    out.write_text(text, encoding="utf-8", errors="replace")
    issues = _issue_dicts(check_order(entries, species))
    summary = {
        "ok": all(i["level"] != "ERROR" for i in issues),
        "dir": str(d),
        "species": species,
        "potcar": [e.title for e in entries],
        "enmax_min": min((e.enmax for e in entries if e.enmax), default=None),
        "issues": issues,
        "notes": notes,
        "message": f"已写入 {out}",
    }
    if not quiet:
        print(f"POTCAR：{' + '.join(e.title for e in entries)}")
        for i in issues:
            print(f"  [{i['level']}] {i['code']}: {i['message']}")
        print(summary["message"])
    return summary


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文/符号会崩；统一切 UTF-8 且不因编码失败中断
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    if args.potcar_only:
        try:
            summary = run_potcar_only(args.potcar_only, args.pp_dir, quiet=args.quiet)
        except PrepError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            if args.json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return getattr(exc, "code", 1)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary.get("ok", True) else 6
    if not args.recipe:
        print("[错误] 需要配方 JSON（或改用 --potcar-only DIR）", file=sys.stderr)
        return 2
    try:
        summary = run(args)
    except PrepError as exc:
        detail = getattr(exc, "detail", None)
        msg = f"[错误] {exc}"
        if detail:
            msg += f"\n  提示：{detail}"
        print(msg, file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc), "detail": detail},
                             ensure_ascii=False))
        return getattr(exc, "code", 1)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human(summary, quiet=args.quiet)
    return 0 if summary.get("ok", True) else 6


def _print_human(s: dict, *, quiet: bool = False) -> None:
    if quiet:
        print(s.get("message", ""))
        return
    head = "预览（dry-run）" if s.get("dry_run") else "已生成"
    print(f"== {head}：{s['recipe']} ==")
    st = s["structure"]
    print(f"结构：{st['natoms'][0]} → {st['natoms'][1]} 原子；"
          f"体积 {st['volume'][0]} → {st['volume'][1]} Å³；元素 {' '.join(st['species'])}")
    for item in s["ops"]:
        print(f"  {item['step']}. {item['op']} —— {item['detail']}")
    if s["incar_changes"]:
        print("INCAR 变更：")
        for c in s["incar_changes"]:
            print(f"  {c['key']}: {c['old']} → {c['new']}  （{c['reason']}）")
    else:
        print("INCAR：无覆盖（沿用预设）")
    kp = s["kpoints"]
    if kp:
        print(f"KPOINTS：mode={kp['mode']} mesh={kp.get('mesh')}")
    print(f"POTCAR：{s['potcar']['mode']} {(' '.join(s['potcar']['titles']) if s['potcar']['titles'] else '')}")
    for i in s["issues"]:
        print(f"  [{i['level']}] {i['code']}: {i['message']}")
    if s.get("files"):
        print(f"文件：{' '.join(s['files'])}")
    print(s.get("message", ""))


if __name__ == "__main__":
    sys.exit(main())
