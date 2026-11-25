"""服务层：网页和终端共用的一套"预览 / 写入"逻辑。

为什么要有这一层（先讲清楚，不然三个入口各写一套，早晚不一致）：
    网页（webui.py）、终端（chat.py）、命令行（prep.py）要做的事其实一样：
      1) 读源目录里的 POSCAR/INCAR/KPOINTS
      2) 把一句中文翻译成操作清单（模型或规则）
      3) 在"临时舞台"里真算一遍，得到新结构 + INCAR
      4) 把结果和源文件做 diff，给人看
      5) 人点"写入"之后，才把文件落进输出目录
    第 3~5 步最容易写错（写一半失败、覆盖原文件、目录没建好），
    所以只写一遍，放在这里；两个入口都只是"遥控器"。

一次预览会经历什么（和网页上看到的时间线一一对应）：
    [读文件]   解析 POSCAR：Cu，1 个原子，a=3.61 Å
    [理解]     模型在线 → 回 2 个操作；或 规则模式 → 匹配 2 条
    [试算]     扩胞 2×2×1 → 4 原子；替换第 3 个 → Co
    [校验]     INCAR 白名单 / 元素顺序 / 体积
    [diff]     POSCAR 行数变化、INCAR 新旧对照
    [等待确认] 点"写入"才落盘

注意：预览会把文件写到项目下的 .tmp_service/<job>/（临时舞台），
      这样才有"真实的产物"可以 diff；点取消/写完会清掉。
"""

from __future__ import annotations

import difflib
import getpass
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from . import llm as llm_mod
from . import nl as nl_mod
from .config import Config, ROOT
from .errors import PrepError
from .prep import build_parser, run as prep_run
from .poscar import read_poscar
from .incar import parse_incar

# 临时舞台与 job 表都放在项目内：显式、可清理，不往系统临时目录写
STAGE_ROOT = ROOT / ".tmp_service"
_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()

MAX_DIFF_LINES = 400  # 网页上不可能看 5000 行 diff，超了就截断并提示


def _log(logs: list[dict], level: str, msg: str) -> None:
    """时间线里加一行。level: info / ok / warn / err / step"""
    logs.append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg})


@dataclass
class Job:
    """一次预览的完整现场。write 阶段只认 job_id，避免网页传一堆路径被篡改。"""

    id: str
    source: Path
    output: Path
    request: str
    recipe: dict
    recipe_path: Path
    stage: Path
    summary: dict = field(default_factory=dict)
    diffs: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    written_to: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": str(self.source),
            "output": str(self.output),
            "request": self.request,
            "summary": self.summary,
            "diffs": self.diffs,
            "files": self.files,
            "logs": self.logs,
            "written_to": self.written_to,
        }


# ---------------------------------------------------------------------------
# 1) 读源目录
# ---------------------------------------------------------------------------
def summarize_source(source: str | Path) -> dict:
    """读 POSCAR/INCAR/KPOINTS，产出一个"给人看、也给模型看"的摘要。

    【为什么只读不写】预览阶段绝不能碰源目录；读坏了最坏是报错，不会毁数据。
    【不这么干会怎样】界面只能显示"路径"，用户根本不知道自己选对没有。
    """
    src = Path(source).expanduser().resolve()
    if not src.is_dir():
        raise PrepError(f"源目录不存在：{src}")
    poscar = src / "POSCAR"
    if not poscar.exists():
        raise PrepError(f"源目录里没有 POSCAR：{src}")

    st = read_poscar(poscar)
    lat = st.lattice
    abc = [sum(lat[i][j] ** 2 for j in range(3)) ** 0.5 for i in range(3)]
    species = list(dict.fromkeys(st.atom_species()))

    incar_keys: list[str] = []
    incar_path = src / "INCAR"
    if incar_path.exists():
        try:
            lines = parse_incar(incar_path.read_text(encoding="utf-8-sig", errors="replace"))
            incar_keys = [ln.key for ln in lines if ln.key]
        except Exception:  # INCAR 坏了不该阻止"看结构"，但要在时间线里说
            incar_keys = []

    calc = "relax"
    for key, val in ((k, v) for k, v in _incar_pairs(incar_path)):
        if key == "NSW" and str(val).strip() in ("0", "0.0"):
            calc = "static"
        if key == "ICHARG" and str(val).strip() in ("11",):
            calc = "band"
    return {
        "path": str(src),
        "natoms": st.natoms,
        "species": species,
        "counts": list(st.counts),
        "lattice_abc": abc,
        "volume": round(st.volume(), 4),
        "has_incar": incar_path.exists(),
        "has_kpoints": (src / "KPOINTS").exists(),
        "incar_keys": incar_keys,
        "calc_guess": calc,
    }


def _incar_pairs(path: Path):
    """INCAR 的 (键, 值) 迭代器；文件不存在就空。"""
    if not path.exists():
        return []
    pairs = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.split("#", 1)[0].split("!", 1)[0].strip()
        if not s or "=" not in s:
            continue
        k, v = s.split("=", 1)
        pairs.append((k.strip().upper(), v.strip()))
    return pairs


# ---------------------------------------------------------------------------
# 2) 把中文翻译成"操作清单"
# ---------------------------------------------------------------------------
def understand(text: str, summary: dict, cfg: Config, logs: list[dict]) -> dict:
    """优先模型，失败/离线退回规则。返回 nl 形状的 dict（含 recipe/matched/...）。

    【为什么先探活再调】直接调要等 120 秒超时；探活 3 秒就知道能不能用。
    【不这么干会怎样】没开 Ollama 时，网页要卡两分钟才告诉你"模型不可用"。
    """
    if cfg.llm.enabled and cfg.llm.privacy != "off":
        pr = llm_mod.probe(cfg)
        if pr.available:
            _log(logs, "step", f"模型在线：{cfg.llm.model}（探测 {pr.elapsed_ms} ms）")
            try:
                return llm_mod.text_to_recipe(cfg, text, summary)
            except llm_mod.LlmError as exc:
                _log(logs, "warn", f"模型没给出可用结果（{exc}），改用规则模式")
        else:
            _log(logs, "warn", f"模型不可用（{pr.reason}），改用规则模式")
    else:
        _log(logs, "step", "未启用模型，使用规则模式")

    res = nl_mod.text_to_recipe(text)
    res.setdefault("meta", {})["source"] = "rules"
    return res


def _detect_calc(text: str) -> str:
    """从中文里判断用户到底要不要"改计算设置"。

    【为什么要判断】"扩成 2x2x1"只是结构操作，不该顺手把 INCAR 换成 relax 预设
        （那会写入 ISIF/IBRION/NSW/EDIFFG 等 10 个键）。
        只有用户明确说"弛豫/优化/静态/能带"时，才套对应预设。
    【不这么干会怎样】用户只想换个原子，结果 INCAR 被整篇重写，
        还得逐行确认哪些是他要的、哪些是工具加的。
    """
    t = text.lower()
    if any(k in t for k in ("能带", "band", "nscf")):
        return "band"
    if any(k in t for k in ("静态", "单点", "static", "scf")):
        return "static"
    if any(k in t for k in ("弛豫", "优化", "relax", "几何优化")):
        return "relax"
    return "none"


def _finalize_recipe(res: dict, source: Path, name: str) -> dict:
    """把翻译结果补成一份完整、可校验的配方。

    【为什么要补 base_dir】配方里的相对路径（POSCAR、INCAR 底稿）都按 base_dir 解析；
        网页传来的源目录是绝对的，补上它，配方文件放哪都能复跑。
    """
    recipe = res.get("recipe") or {}
    recipe["version"] = 2
    recipe.setdefault("name", name)
    recipe["name"] = name
    recipe["base_dir"] = str(source)
    recipe.setdefault("structure", {}).setdefault("file", "POSCAR")
    recipe["structure"].setdefault("ops", [])
    recipe.setdefault("incar", {}).setdefault("overrides", {})
    return recipe


_VDW_WORDS = ("ivdw", "d2", "d3", "d4", "ts", "mbd", "色散", "范德华", "vdw", "tkatchenko")


def _strip_unrequested_ivdw(recipe: dict, text: str, logs: list[dict]) -> None:
    """若用户原话里没提色散，就把模型擅自加的 IVDW 删掉。

    【为什么】IVDW 是"科学上的方法选择"：D3 还是 MBD、加不加，会改变结论，
        必须由人点名。模型看别人配方里有 IVDW，容易顺手补一条。
    【不这么干会怎样】用户只想扩胞，结果 INCAR 多出 IVDW=12，
        算出来的吸附能带色散修正，跟组里其他人的数据不可比。
    """
    ov = (recipe.get("incar") or {}).get("overrides") or {}
    if "IVDW" not in ov:
        return
    low = text.lower()
    if any(w in low for w in _VDW_WORDS):
        return
    ov.pop("IVDW", None)
    _log(logs, "warn", "模型自行加了 IVDW，但你没提色散修正，已丢弃（需要就明说 D3/MBD 等）")


def _apply_calc_policy(recipe: dict, text: str, logs: list[dict]) -> None:
    """按"用户是否明确要改计算类型"决定 calc/preset（纯结构编辑不套预设）。"""
    calc = _detect_calc(text)
    recipe["calc"] = calc
    recipe.setdefault("incar", {})
    if calc == "none":
        # 模型/规则可能默认给了 relax，这里去掉，避免 INCAR 被顺手改写
        if recipe["incar"].pop("preset", None):
            _log(logs, "info", "没有提到弛豫/静态/能带，不套 INCAR 预设（只改你点名的键）")
    else:
        recipe["incar"]["preset"] = calc
        _log(logs, "info", f"检测到计算类型：{calc}（会套用对应 INCAR 预设）")

    _strip_unrequested_ivdw(recipe, text, logs)


# ---------------------------------------------------------------------------
# 3) 预览：在临时舞台里真跑一遍，产出文件与 diff
# ---------------------------------------------------------------------------
def _prep_args(recipe_path: Path, out_dir: Path) -> "argparse.Namespace":
    """构造 prep 的 argparse 参数。

    【为什么复用 build_parser 而不是手写 Namespace】prep.run 依赖十几个字段，
        手写漏一个就是 AttributeError；用同一个 parser 解析，永远对齐。
    """
    p = build_parser()
    args = p.parse_args([str(recipe_path),
                         "--potcar-mode", "skip",   # 前处理不碰 POTCAR
                         "--no-audit",              # 审计由 service 自己写
                         "--force"])
    args.write = True
    args.out = str(out_dir)
    args.quiet = True
    args.json = True
    return args


def _diff_text(old: str, new: str, label: str) -> str:
    """unified diff，带行数上限。"""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{label}（源）", tofile=f"{label}（新）", lineterm="", n=2,
    ))
    if not lines:
        return ""
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"...（diff 太长，只显示前 {MAX_DIFF_LINES} 行）"]
    return "\n".join(lines)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _safe_name(text: str) -> str:
    """从中文需求里取一个短名字做目录名；取不到就用 edit_时间。"""
    import re

    m = re.search(r"[A-Za-z0-9_\-]{2,20}", text)
    if m:
        return m.group(0)
    return "edit_" + datetime.now().strftime("%H%M%S")


def compose_recipe(text: str, summary: dict, src: Path, cfg: Config,
                   logs: list[dict]) -> dict:
    """把一句中文理解成 recipe.json 的内容（单文件与批量共用）。

    【为什么抽出来】批量处理是"同一句话 × N 个材料"，如果每个材料都重新问一遍模型，
        一是慢（每个 3–8 秒），二是可能出现"同一句话在 MoS2 上理解成扩胞、在 WS2 上
        理解成替换"——这就不可解释了。批量里只理解一次，再套到每个材料上。
    【不这么干会怎样】批量结果不一致，用户没法用"我明明说的是同一件事"来质疑。
    返回的 recipe 里已经过 _finalize_recipe（补 base_dir/version）与
    _apply_calc_policy（纯结构编辑不套 INCAR 预设）两道处理。
    """
    res = understand(text, summary, cfg, logs)
    matched = res.get("matched") or []
    needs = res.get("needs_human") or []
    if matched:
        # 规则模式给的是 dict（rule/text/effect），模型模式给的是字符串，两种都要能显示
        def _fmt(item) -> str:
            if isinstance(item, dict):
                return str(item.get("effect") or item.get("desc") or item.get("rule") or item)
            return str(item)

        _log(logs, "ok", f"理解结果：{len(matched)} 项 → " + "；".join(_fmt(x) for x in matched))
    for item in needs:
        _log(logs, "warn", f"需要你确认：{item}")
    if res.get("unmatched"):
        _log(logs, "warn", "没有听懂的片段：" + "、".join(str(x) for x in res["unmatched"]))

    name = _safe_name(text)
    recipe = _finalize_recipe(res, src, name)
    _apply_calc_policy(recipe, text, logs)
    return recipe


def stage_recipe(recipe: dict, src: Path, stage: Path,
                 logs: list[dict] | None = None) -> tuple[dict, dict[str, str], list[str]]:
    """在 stage 目录里真跑一遍 prep（写到 stage/out），返回 (summary, diffs, files)。

    【为什么真跑而不是"算一算"】用户要看的是"生成出来的文件长什么样"，
        只有真调写盘逻辑（写到临时舞台）才能拿到 INCAR 的真实排版、POSCAR 的
        元素分组、KPOINTS 的格式——这些自己另写一遍预览逻辑必然和实际不一致。
    【注意】舞台在 .tmp_service 下，写错了也只弄脏临时目录；写成功与否由调用方决定
        是否把 recipe.json 再跑一遍到最终目录。
    """
    logs = logs if logs is not None else []
    stage.mkdir(parents=True, exist_ok=False)
    recipe_path = stage / "recipe.json"
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")

    out_stage = stage / "out"
    _log(logs, "step", f"试算（临时舞台）：{out_stage}")
    summary2 = prep_run(_prep_args(recipe_path, out_stage))

    for op in summary2.get("ops", []):
        _log(logs, "ok", f"{op.get('op')}：{op.get('detail', '')}")
    for ch in summary2.get("incar_changes", []):
        _log(logs, "ok", f"INCAR {ch['key']}：{ch.get('old')} → {ch.get('new')}")
    for issue in summary2.get("issues", []):
        lvl = {"ERROR": "err", "WARN": "warn"}.get(str(issue.get("level")), "info")
        _log(logs, lvl, f"[{issue.get('level')}] {issue.get('code')}: {issue.get('message')}")

    # diff：只对实际产出的文件做
    diffs: dict[str, str] = {}
    produced = [p for p in out_stage.iterdir() if p.is_file()] if out_stage.exists() else []
    for p in sorted(produced, key=lambda x: x.name):
        if p.name in ("POSCAR", "INCAR", "KPOINTS"):
            old = _read_text(src / p.name)
            new = _read_text(p)
            d = _diff_text(old, new, p.name)
            if d:
                diffs[p.name] = d
    files = sorted(p.name for p in produced)
    _log(logs, "step", f"试算完成：将生成 {', '.join(files) or '（无文件）'}")
    return summary2, diffs, files


def preview(source: str, output: str, text: str, cfg: Config | None = None) -> Job:
    """读文件 → 理解 → 试算 → diff，返回 Job（此时还没写最终输出目录）。"""
    cfg = cfg or config_mod.load()
    logs: list[dict] = []
    src = Path(source).expanduser().resolve()
    if not output.strip():
        # 输出目录留空 → 写到程序自己的 runs 下，这个位置一定有写权限；
        # 用户桌面/文档常被 Windows 拦（WinError 5），留空是最稳的默认。
        output = str((ROOT / cfg.paths.default_output / f"{src.name or 'run'}_out").resolve())
        _log(logs, "info", f"输出目录留空，默认写到：{output}")

    _log(logs, "step", f"读取源目录：{src}")
    summary = summarize_source(src)
    _log(logs, "ok", f"POSCAR：{''.join(summary['species'])}，{summary['natoms']} 个原子；"
                     f"a/b/c = {' / '.join(f'{x:.3f}' for x in summary['lattice_abc'])} Å；"
                     f"体积 {summary['volume']} Å³")
    _log(logs, "info", f"INCAR：{'有' if summary['has_incar'] else '无'}；"
                       f"KPOINTS：{'有' if summary['has_kpoints'] else '无'}")

    if not text.strip():
        raise PrepError("需求是空的：请写一句要改什么，例如“扩成 2x2x1，第 3 个原子换成 Co”")

    recipe = compose_recipe(text, summary, src, cfg, logs)

    job_id = uuid.uuid4().hex[:10]
    stage = STAGE_ROOT / job_id
    try:
        summary2, diffs, files = stage_recipe(recipe, src, stage, logs)
    except PrepError as exc:
        _log(logs, "err", str(exc))
        shutil.rmtree(stage, ignore_errors=True)
        raise

    job = Job(id=job_id, source=src, output=Path(output).expanduser().resolve(),
              request=text, recipe=recipe, recipe_path=stage / "recipe.json", stage=stage,
              summary=summary2, diffs=diffs, files=files, logs=logs)
    with _LOCK:
        _JOBS[job_id] = job
        _cleanup_old_jobs()
    return job


def _cleanup_old_jobs(max_jobs: int = 10) -> None:
    """只保留最近 max_jobs 个预览，避免舞台目录越堆越大（单线程内调用）。"""
    if len(_JOBS) <= max_jobs:
        return
    for jid in sorted(_JOBS, key=lambda x: _JOBS[x].created)[:-max_jobs]:
        old = _JOBS.pop(jid, None)
        if old and not old.written_to:
            shutil.rmtree(old.stage, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4) 写入：只有这一步会碰最终输出目录
# ---------------------------------------------------------------------------
def _preflight_out(out: Path) -> None:
    """写入前先试一下输出目录能不能建、能不能写。

    【为什么】Windows 上"拒绝访问(WinError 5)"可能来自四种完全不同的原因
        （受控文件夹访问、杀毒、ACL 只读、程序跑在受限沙箱里），
        直接 mkdir 抛出来的 traceback 用户看不懂，也分不清该找谁。
        这里主动建一个临时目录再删掉，把系统原话连同 pid/用户/建议一起给出来。
    【不这么干会怎样】用户只看到 `PermissionError: [WinError 5] ... MoS2_out`，
        既不知道是自己选错了盘，还是被杀毒拦了，只能来问。
    """
    parent = out.parent
    ok, msg = _probe_dir(parent)
    if ok:
        return

    # 再探几个地方，好把"该换到哪"直接写进报错里：
    #   · 程序自己的 runs 目录（一定有写权限，最稳）
    #   · 用户主目录下的 vasp_runs（非桌面，通常不受受控文件夹访问影响）
    #   · 用户主目录本身（如果连它都写不了，基本就是进程跑在受限沙箱里）
    from .config import ROOT as _ROOT

    repo_runs = _ROOT / "runs"
    repo_ok, repo_msg = _probe_dir(repo_runs)
    home = Path.home()
    suggested = home / "vasp_runs"
    home_ok, home_msg = _probe_dir(home)
    sug_ok, sug_msg = _probe_dir(suggested)
    if repo_ok:
        advice = (f"     → 最省事：把“输出目录”留空，程序会写到\n"
                  f"       {repo_runs}（已实测可写），写完再从资源管理器挪走。\n")
    elif sug_ok:
        advice = f"     → 建议改用：{suggested}（已实测可写）\n"
    elif home_ok:
        advice = f"     → 用户目录可写，但 {suggested} 建不了：{sug_msg}\n"
    else:
        advice = ("     → 连用户目录都写不了（" + home_msg + "），几乎可以确定这个 Python 进程跑在\n"
                  "       受限沙箱里：只能写它自己的工作目录。请用 Win+R 输入 cmd，在普通命令行里\n"
                  "       进到 vasp-prep 再运行 启动网页.bat（不要从别的工具/终端里启动）。\n")

    raise PrepError(
        f"无法在下面这个位置建目录/写文件：\n  {parent}\n"
        f"系统原话：{msg}\n"
        f"当前状态：pid={os.getpid()}，用户={getpass.getuser()}，工作目录={Path.cwd()}\n"
        "常见原因与处理：\n"
        + advice +
        "  1) 目标在“桌面/文档”等受保护文件夹，被 Windows「受控文件夹访问」或杀毒软件拦截\n"
        "     → 换到非桌面位置；或在 设置→隐私和安全性→Windows 安全中心→病毒和威胁防护→\n"
        "       勒索软件防护→受控文件夹访问 里把 python.exe 加进允许列表；\n"
        "  2) 目标是 C:\\ 根目录或系统目录（普通账号没有建文件夹的权限）→ 换到用户目录下；\n"
        "  3) 该文件夹只读/权限受限/正被 OneDrive 同步占用 → 换目录或取消“只读”。"
    )

def _probe_dir(parent: Path) -> tuple[bool, str]:
    """试建一个临时目录再删掉，返回 (是否可写, 失败原因)。"""
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".vasp_prep_probe_{uuid.uuid4().hex[:8]}"
        probe.mkdir()
        probe.rmdir()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def write_job_obj(job: Job, force: bool = False) -> Job:
    """把一个已预览的 Job 真正写进它自己的输出目录（批量也复用这个）。

    【为什么重新跑一遍而不是复制舞台文件】
        舞台里的文件是"已经算好的"，复制过去当然快；但重跑一遍能保证
        用户看到 diff 的那份配方与落盘的那份完全一致（同一份 recipe.json），
        而且顺便再校验一次——复制则跳过了校验，"预览通过了但写盘失败"更难查。
    【注意】输出目录已存在且非空时默认拒绝，除非 force=True。
    """
    out = job.output
    _preflight_out(Path(out))
    _log(job.logs, "step", f"写入输出目录：{out}" + ("（--force 覆盖）" if force else ""))
    p = build_parser()
    args = p.parse_args([str(job.recipe_path),
                         "--potcar-mode", "skip",
                         "--no-audit"])
    args.write = True
    args.out = str(out)
    args.force = bool(force)
    args.quiet = True
    args.json = True

    summary = prep_run(args)
    job.summary = summary
    job.files = summary.get("files", job.files)
    job.written_to = str(out)
    for issue in summary.get("issues", []):
        if issue.get("level") == "ERROR":
            _log(job.logs, "err", str(issue.get("message")))
    _log(job.logs, "ok", f"已写入 {len(job.files)} 个文件：{', '.join(job.files)}")
    # 写完把舞台删掉（配方已存进输出目录，舞台没用了）
    shutil.rmtree(job.stage, ignore_errors=True)
    return job


def write(job_id: str, force: bool = False) -> Job:
    """按 job_id 找到预览任务并写入（网页/终端用）。"""
    job = get_job(job_id)
    if job is None:
        raise PrepError(f"找不到预览任务 {job_id}（可能已过期，请重新预览）")
    return write_job_obj(job, force=force)


def get_job(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def cleanup_all() -> None:
    """退出时清理所有舞台目录（网页服务关闭时调用）。"""
    with _LOCK:
        for job in _JOBS.values():
            shutil.rmtree(job.stage, ignore_errors=True)
        _JOBS.clear()
    shutil.rmtree(STAGE_ROOT, ignore_errors=True)
