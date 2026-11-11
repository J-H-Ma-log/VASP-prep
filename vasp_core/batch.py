"""批量前处理（v3）。

【一句话】选一个根目录 → 自动找出里面所有含 POSCAR 的材料文件夹 →
        用同一句中文给每个材料做前处理，结果写到 batches\\<批次>\\<材料>\\。

【两种批量语义】
  A. 多材料 × 同一需求：每个材料都做"扩成 2x2x1"；
  B. 同/多材料 × 参数扫描：每个材料各生成 IVDW=0/11/12 三份。
  两者可叠加：N 个材料 × M 个参数值 = N×M 份产物。

【为什么"理解"只做一次】批量里如果每个材料都问一遍模型，同一句话可能在 MoS2 上
  理解成扩胞、在 WS2 上理解成替换——结果不可解释。所以这里只理解一次，
  得到一份 recipe 模板，再套到每个材料上；参数扫描只是往模板里改一个键。
【为什么不直接调 prep 而是先预览】用户在点"写入"之前必须能看到每个材料会生成什么、
  哪些材料会失败；直接写盘等于"拿真实目录试错"。

【命名规则（写死，所见即所得）】
  batches\\<批次名>\\<材料名>\\<参数子目录>\\
    · 批次名：用户填；留空 = batch_<YYYYMMDD-HHMM>_<关键词>
    · 材料名：源文件夹名（非法字符换 _，同名加 _2）
    · 参数子目录：ivdw_<值>（以后加别的扫描参数就用 <参数名>_<值>）
    · 批次根再写 batch_summary.csv 与 batch_report.md
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .config import Config
from .errors import PrepError
from .paths import batches_dir, safe_name, unique_dir
from .prep import build_parser as prep_build_parser, run as prep_run
from .service import (
    STAGE_ROOT,
    _prep_args,
    compose_recipe,
    stage_recipe,
    summarize_source,
)

# 参数扫描：目前只开放 IVDW（其余键走单文件流程或后续再开）
SWEEP_KEYS = {"ivdw": "IVDW"}

# 源目录里认哪些文件名（大小写都认，尽量宽容）
_POSCAR_NAMES = ("POSCAR", "poscar", "CONTCAR")


# ---------------------------------------------------------------------------
# 1) 找材料
# ---------------------------------------------------------------------------
def find_materials(root: str | Path, depth: int = 0) -> list[Path]:
    """在 root 下找"含 POSCAR 的文件夹"。

    depth=0 只看 root 的第一层子目录（最常见的"一个材料一个文件夹"）；
    depth=1 再往下找一层（适合 materials\\<子文件夹>\\<材料>\\ 这种结构）。
    【为什么不用 os.walk 全递归】有人会把整个 home 选进来，全递归会扫到几万个
        无关目录；限制层数 + 只认 POSCAR 是安全与速度的折中。
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise PrepError(f"根目录不存在或不是文件夹：{root}")
    found: list[Path] = []
    level = [root]
    for _ in range(max(0, depth) + 1):
        nxt: list[Path] = []
        for d in level:
            try:
                children = sorted(p for p in d.iterdir() if p.is_dir())
            except OSError:
                continue
            for child in children:
                if any((child / name).is_file() for name in _POSCAR_NAMES):
                    found.append(child)
                nxt.append(child)
        level = nxt
    return found


def scan_materials(root: str | Path, depth: int = 0, cfg: Config | None = None) -> dict:
    """扫描并给每个材料一个摘要（原子数/元素/体积），失败的材料单独标出。

    【为什么失败也要列出来】用户勾选的是"文件夹"，不是"一定能解析的结构"；
        把坏的悄悄跳过，用户会以为处理了、其实没处理。
    """
    cfg = cfg or config_mod.load()
    mats = find_materials(root, depth=depth)
    out: list[dict] = []
    for m in mats:
        try:
            s = summarize_source(m)
            out.append({
                "name": safe_name(m.name, fallback="material"),
                "path": str(m),
                "ok": True,
                "natoms": s["natoms"],
                "species": s["species"],
                "volume": s["volume"],
                "has_incar": s["has_incar"],
                "has_kpoints": s["has_kpoints"],
            })
        except Exception as exc:  # 读不了 POSCAR 也要列出来
            out.append({"name": safe_name(m.name, fallback="material"),
                        "path": str(m), "ok": False, "error": str(exc)})
    return {
        "root": str(Path(root).expanduser().resolve()),
        "depth": depth,
        "materials": out,
        "n_found": len(out),
        "n_ok": sum(1 for x in out if x["ok"]),
        "max_materials": cfg.batch.max_materials,
    }


# ---------------------------------------------------------------------------
# 2) 批次名、参数、目标路径
# ---------------------------------------------------------------------------
def parse_values(raw: str) -> list[str]:
    """把 "0,11,12" 解析成 ["0","11","12"]；空串表示"不扫描，只跑一次"。"""
    if not raw or not str(raw).strip():
        return [""]
    vals: list[str] = []
    for part in re.split(r"[,，\s]+", str(raw).strip()):
        if not part:
            continue
        if part in vals:
            continue  # 去重：用户手滑写 11,11 时不要生成两个同名目录
        vals.append(part)
    return vals or [""]


def _keyword(text: str) -> str:
    """从需求里抠一个短关键词，用作默认批次名的一部分。

    例："扩成 2x2x1，加 DFT-D3(BJ)" → "2x2x1_d3bj"
    抠不到就用 "edit"（批次名仍然可用，只是不好认）。
    """
    low = (text or "").lower()
    keys: list[str] = []
    if m := re.search(r"\d+\s*[x×*]\s*\d+\s*[x×*]\s*\d+", low):
        keys.append(re.sub(r"\s*[x×*]\s*", "x", m.group(0)))
    if "d3" in low and ("bj" in low or "零阻尼" not in low and "zero" not in low):
        keys.append("d3bj")
    elif "d3" in low:
        keys.append("d3")
    elif "d2" in low:
        keys.append("d2")
    if "空位" in low or "vacancy" in low:
        keys.append("vacancy")
    if "替" in low or "substitut" in low:
        keys.append("subst")
    return "_".join(safe_name(k, fallback="", max_len=20) for k in keys if k)[:40] or "edit"


def make_batch_name(text: str, name: str = "") -> str:
    """批次目录名：用户填就用用户的；留空用 batch_时间_关键词。"""
    if name and name.strip():
        return safe_name(name, fallback="batch")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return safe_name(f"batch_{stamp}_{_keyword(text)}", fallback="batch")


def _dedupe(names: list[str]) -> list[str]:
    """同一批次内材料重名时依次加 _2、_3（不创建目录，纯算名字）。"""
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen[n] = 1
            out.append(n)
        else:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
    return out


def _param_dir(value: str) -> str:
    """参数子目录名：ivdw_11；空值表示不扫描。"""
    if not value:
        return ""
    return f"ivdw_{safe_name(str(value), fallback='value', max_len=12)}"


def plan_targets(batch_dir: Path, material_names: list[str],
                 values: list[str]) -> list[dict]:
    """算出每个 (材料, 参数) 将要写入的目录（此时不创建）。

    预览表最后一列直接显示这些路径，用户点"写入"前就能看到会生成什么。
    """
    names = _dedupe([safe_name(n, fallback="material") for n in material_names])
    rows: list[dict] = []
    for name in names:
        for v in values:
            pdir = _param_dir(v)
            rows.append({"material": name, "value": v, "param_dir": pdir,
                         "target": batch_dir / name / pdir if pdir else batch_dir / name})
    return rows


# ---------------------------------------------------------------------------
# 3) 预览
# ---------------------------------------------------------------------------
def _merge_sweep(recipe: dict, value: str) -> dict:
    """把参数值合进 recipe 的副本（不改模板）。

    IVDW 属于"必须用户点名才写"的键，这里是用户显式扫的值，所以直接写；
    但走的是 recipe 的 incar.overrides，仍然要过 prep 的枚举校验（99 会被拒）。
    """
    if not value:
        return copy.deepcopy(recipe)
    r = copy.deepcopy(recipe)
    r.setdefault("incar", {}).setdefault("overrides", {})
    r["incar"]["overrides"]["IVDW"] = int(value) if str(value).isdigit() else value
    r["name"] = f"{r.get('name', 'recipe')}_ivdw_{value}"
    return r


def preview_batch(root: str, text: str, name: str = "", ivdw: str = "",
                  cfg: Config | None = None, depth: int | None = None) -> dict:
    """批量预览：扫描 → 理解一次 → 逐材料试算 → 返回"将要生成"的目录与状态。"""
    cfg = cfg or config_mod.load()
    depth = cfg.batch.recursive_depth if depth is None else depth
    logs: list[dict] = []

    def log(level: str, msg: str) -> None:
        logs.append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg})

    scan = scan_materials(root, depth=depth, cfg=cfg)
    mats = scan["materials"]
    good = [m for m in mats if m["ok"]]
    if not good:
        raise PrepError("这个根目录下没有找到可读的材料（子文件夹里要有 POSCAR）")
    if len(good) > cfg.batch.max_materials:
        raise PrepError(f"材料太多（{len(good)} 个），上限 {cfg.batch.max_materials} 个；"
                        "请选更具体的根目录，或改 [batch] max_materials 配置")
    if not text.strip():
        raise PrepError("需求是空的：请写一句要对每个材料做什么")

    values = parse_values(ivdw)
    batch_name = make_batch_name(text, name)
    parent = batches_dir(cfg)
    batch_dir = unique_dir(parent, batch_name)  # 只算路径，不创建
    # 材料名先去重，再生成"材料×参数"的全部目标目录。
    # 【坑】不能 zip(good, targets)：targets 是 N×M 行，zip 会在第 N 行就停，
    #       表现是"明明扫 3 个值，结果只处理第一个值"。
    names = _dedupe([safe_name(m["name"], fallback="material") for m in good])
    mat_by_name = dict(zip(names, good))
    targets = plan_targets(batch_dir, names, values)
    if len(targets) > cfg.batch.max_dirs:
        raise PrepError(f"将要生成 {len(targets)} 个目录，上限 {cfg.batch.max_dirs}；"
                        "请减少材料或参数值")
    log("info", f"找到 {len(good)} 个材料；参数扫描：{values if values != [''] else '不扫描'}")
    log("step", f"批次目录：{batch_dir}")

    # ---- 理解一次，得到模板 recipe（用第一个可读材料的摘要） ----
    first = good[0]
    summary0 = summarize_source(first["path"])
    log("step", f"用第一个材料 {first['name']} 做理解样本")
    recipe0 = compose_recipe(text, summary0, Path(first["path"]), cfg, logs)

    st_root = STAGE_ROOT / f"batch_{uuid.uuid4().hex[:8]}"
    items: list[dict] = []
    ok_n = 0
    try:
        for tgt in targets:
            m = mat_by_name[tgt["material"]]
            recipe = _merge_sweep(recipe0, tgt["value"])
            # recipe 的 base_dir 必须指向当前材料，否则会拿第一个材料的 POSCAR 去跑
            recipe["base_dir"] = str(Path(m["path"]))
            item_logs: list[dict] = []
            stage = st_root / f"{len(items):04d}"
            item = {
                "material": tgt["material"],
                "source": m["path"],
                "value": tgt["value"],
                "param_dir": tgt["param_dir"],
                "target": str(tgt["target"]),
                "recipe": recipe,
                "status": "ok",
                "issue": "",
                "natoms": m.get("natoms"),
                "ops": [],
                "incar_changes": [],
                "logs": item_logs,
            }
            try:
                summary2, diffs, files = stage_recipe(recipe, Path(m["path"]), stage, item_logs)
                item["natoms_after"] = (summary2.get("structure") or {}).get("natoms", [None, None])[-1]
                item["ops"] = summary2.get("ops", [])
                item["incar_changes"] = summary2.get("incar_changes", [])
                item["files"] = files
                item["diffs"] = diffs
                item["issues"] = summary2.get("issues", [])
                ok_n += 1
            except PrepError as exc:
                item["status"] = "error"
                item["issue"] = str(exc)
                shutil.rmtree(stage, ignore_errors=True)
            items.append(item)
    finally:
        shutil.rmtree(st_root, ignore_errors=True)

    log("ok", f"预览完成：{ok_n}/{len(items)} 个可写；{len(items) - ok_n} 个失败")
    return {
        "batch": batch_dir.name,
        "batch_dir": str(batch_dir),
        "parent": str(parent),
        "materials": good,
        "values": values,
        "items": items,
        "n_ok": ok_n,
        "n_total": len(items),
        "logs": logs,
        "text": text,
    }


# ---------------------------------------------------------------------------
# 4) 写入
# ---------------------------------------------------------------------------
def _write_item(item: dict, force: bool = False) -> None:
    """把一个预览项写到它的目标目录。"""
    target = Path(item["target"])
    if target.exists() and any(target.iterdir()) and not force:
        item["status"] = "skip"
        item["issue"] = "目标目录已存在且非空（勾“覆盖”才会重写）"
        return
    target.mkdir(parents=True, exist_ok=True)
    recipe_path = target / "recipe.json"
    recipe_path.write_text(json.dumps(item["recipe"], ensure_ascii=False, indent=2),
                           encoding="utf-8")
    p = prep_build_parser()
    args = p.parse_args([str(recipe_path), "--potcar-mode", "skip", "--no-audit"])
    args.write = True
    args.out = str(target)
    args.force = True  # 目录是刚建的/已确认要覆盖
    args.quiet = True
    args.json = True
    summary = prep_run(args)
    item["written"] = sorted(summary.get("files", []))
    item["status"] = "written"


def write_batch(preview: dict, force: bool = False) -> dict:
    """按预览结果写盘，并生成 batch_summary.csv 与 batch_report.md。

    【为什么失败不整体中止】批量里一个材料坏（POSCAR 读不了、INCAR 有非法值）
        不应该让另外 19 个都白跑；逐项记状态，最后报告里列清楚。
    """
    batch_dir = Path(preview["batch_dir"])
    batch_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = failed = 0
    for item in preview["items"]:
        if item["status"] != "ok":
            failed += 1
            continue
        try:
            _write_item(item, force=force)
            if item["status"] == "written":
                written += 1
            else:
                skipped += 1
        except PrepError as exc:
            item["status"] = "error"
            item["issue"] = str(exc)
            failed += 1
        except OSError as exc:
            item["status"] = "error"
            item["issue"] = f"{type(exc).__name__}: {exc}"
            failed += 1

    _write_summary_csv(batch_dir, preview["items"])
    _write_report_md(batch_dir, preview, written, skipped, failed)
    preview.update({"written": written, "skipped": skipped, "failed": failed})
    return preview


def _write_summary_csv(batch_dir: Path, items: list[dict]) -> None:
    """机器可读的汇总表（Excel 直接打开；utf-8-sig 防止中文乱码）。"""
    path = batch_dir / "batch_summary.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["材料", "参数", "输出目录", "原子数(前)", "原子数(后)",
                    "操作", "INCAR 改动", "状态", "说明"])
        for it in items:
            w.writerow([
                it["material"], it["value"] or "-", it["target"],
                it.get("natoms", ""), it.get("natoms_after", ""),
                "; ".join(o.get("detail", "") for o in it.get("ops", [])) or "-",
                "; ".join(f"{c['key']} {c.get('old')}→{c.get('new')}"
                          for c in it.get("incar_changes", [])) or "-",
                it["status"], it.get("issue", ""),
            ])


def _write_report_md(batch_dir: Path, preview: dict,
                     written: int, skipped: int, failed: int) -> None:
    """给人看的报告：怎么跑出来的、每项结果、失败原因、下一步。"""
    lines = [
        f"# 批量前处理报告：{preview['batch']}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 源根目录：`{preview.get('parent', '')}`",
        f"- 需求：{preview.get('text', '')}",
        f"- 参数扫描：{preview.get('values')}",
        f"- 结果：写入 {written}，跳过 {skipped}，失败 {failed}，共 {preview.get('n_total')}",
        "",
        "## 逐项结果",
        "",
        "| 材料 | 参数 | 状态 | 原子数 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for it in preview["items"]:
        lines.append(f"| {it['material']} | {it['value'] or '-'} | {it['status']} | "
                     f"{it.get('natoms', '')}→{it.get('natoms_after', '')} | {it.get('issue', '')} |")
    lines += ["", "## 下一步", "", "1. 打开各材料目录，检查 `INCAR.diff` 与 `change_report.md`；",
              "2. 确认无误后把整个材料目录上传超算；",
              "3. POTCAR 在超算上按 POSCAR 第 6 行元素顺序拼（本程序不生成 POTCAR）。"]
    (batch_dir / "batch_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 5) 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m vasp_core batch",
                                description="批量前处理：一句话 × 多个材料（可带参数扫描）")
    p.add_argument("--root", required=True, help="根目录（其子文件夹里含 POSCAR）")
    p.add_argument("--text", required=True, help="中文需求，例如“扩成 2x2x1，加 DFT-D3(BJ)”")
    p.add_argument("--name", default="", help="批次目录名；留空自动命名")
    p.add_argument("--ivdw", default="", help="IVDW 扫描值，逗号分隔，例如 0,11,12")
    p.add_argument("--depth", type=int, default=None, help="向下找材料几层（默认读配置）")
    p.add_argument("--write", action="store_true", help="真正写入（默认只预览）")
    p.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pv = preview_batch(args.root, args.text, name=args.name,
                           ivdw=args.ivdw, depth=args.depth)
        if args.write:
            pv = write_batch(pv, force=args.force)
    except PrepError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "batch": pv["batch"], "dir": pv["batch_dir"],
            "n_total": pv["n_total"], "n_ok": pv["n_ok"],
            "written": pv.get("written"), "skipped": pv.get("skipped"),
            "failed": pv.get("failed"),
            "items": [{"material": it["material"], "value": it["value"],
                       "target": it["target"], "status": it["status"],
                       "issue": it["issue"]} for it in pv["items"]],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"批次：{pv['batch']}")
    print(f"目录：{pv['batch_dir']}")
    print(f"材料 {len(pv['materials'])} 个，参数 {pv['values']}，共 {pv['n_total']} 项")
    for it in pv["items"]:
        mark = {"ok": "✓", "written": "✓", "skip": "-", "error": "✗"}.get(it["status"], "?")
        extra = f"  {it['issue']}" if it.get("issue") else ""
        print(f"  {mark} {it['material']}{'/' + it['param_dir'] if it['param_dir'] else ''}"
              f"  → {it['target']}{extra}")
    if not args.write:
        print("\n（这是预览，没有写文件；加 --write 才写）")
    else:
        print(f"\n写入 {pv.get('written')}，跳过 {pv.get('skipped')}，失败 {pv.get('failed')}")
        print(f"报告：{Path(pv['batch_dir']) / 'batch_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
