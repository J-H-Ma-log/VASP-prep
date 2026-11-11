"""网页服务：`python -m vasp_core.webui`，然后浏览器打开 http://127.0.0.1:8765。

【为什么只用标准库 http.server】
    装 Flask/FastAPI 都要 pip；这台机器上系统 Python 是干净的、超算上更不能装。
    本工具就一个人在本机用，页面只有一屏，标准库完全够，还省掉"环境装不上"的问题。

【为什么只监听 127.0.0.1】
    127.0.0.1 是"只有本机能访问"的地址。就算你开着防火墙，别人也连不进来；
    反过来，如果绑 0.0.0.0，同办公室的人都能改你的 VASP 输入——没有登录功能，很危险。

【接口只有五个】
    GET  /                 返回网页（web/index.html）
    GET  /api/status       返回配置摘要 + 模型是否在线
    GET  /api/browse       弹系统原生"选择文件夹"对话框（kind=source|output）
    POST /api/preview      {source, output, text} → 读文件/理解/试算/diff
    POST /api/write        {job_id, confirm:true} → 真正写入输出目录
    错误统一返回 {"ok": false, "error": "..."}，网页上直接显示。

【不这么干会怎样】
    把"预览"和"写入"合成一个接口，用户点一下就直接改文件，
    没有机会看 diff —— 那正是之前反复强调要避免的。
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import batch as batch_mod
from . import browse
from . import config as config_mod
from . import db as db_mod
from . import llm as llm_mod
from . import paths as paths_mod
from . import service
from .config import ROOT
from .errors import PrepError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX = WEB_DIR / "index.html"
MAX_BODY = 1 << 20  # 1 MB，够放一段需求文本；防止有人塞大文件把内存打满
CFG = config_mod.load()
# 预览/写入串行执行：同一时刻只跑一个，避免两个请求同时写舞台目录
_PREP_LOCK = threading.Lock()
# 批量预览的结果（含每个材料的 recipe）要留到用户点"写入"时用；
# 只留最近 3 个，避免网页开一整天后内存里堆几百份配方。
_BATCH_JOBS: dict[str, dict] = {}
_BATCH_KEEP = 3


def _open_folder(p: Path) -> tuple[bool, str]:
    """在系统文件管理器里打开一个目录，返回 (是否成功, 说明)。

    【为什么失败不算错】进程被沙箱降成 Low 完整性时（本机从 AI 工具里启动就是），
    系统不允许它去启动 explorer.exe —— 这时"打不开"是环境限制，不是用户做错了。
    与其弹一串 WinError 5，不如告诉用户"路径已复制，手动粘贴到资源管理器"。
    """
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", str(p)])  # noqa: S603
        return True, "已用系统资源管理器打开"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _batch_public(pv: dict) -> dict:
    """把批量预览结果裁成网页需要的样子：去掉每个材料的完整 recipe（大且无用）。"""
    items = []
    for it in pv["items"]:
        items.append({
            "material": it["material"],
            "value": it["value"],
            "param_dir": it["param_dir"],
            "target": it["target"],
            "status": it["status"],
            "issue": it["issue"],
            "natoms": it.get("natoms"),
            "natoms_after": it.get("natoms_after"),
            "ops": [o.get("detail", "") for o in it.get("ops", [])],
            "incar_changes": [f"{c['key']} {c.get('old')}→{c.get('new')}"
                              for c in it.get("incar_changes", [])],
        })
    return {
        "batch": pv["batch"],
        "batch_dir": pv["batch_dir"],
        "n_total": pv["n_total"],
        "n_ok": pv["n_ok"],
        "values": pv["values"],
        "materials": [m["name"] for m in pv["materials"]],
        "items": items,
        "logs": pv.get("logs", []),
        "written": pv.get("written"),
        "skipped": pv.get("skipped"),
        "failed": pv.get("failed"),
    }


def _can_write(d: Path) -> bool:
    """试建一个临时目录再删掉，判断这个进程能不能写 d。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".probe_{uuid.uuid4().hex[:8]}"
        probe.mkdir()
        probe.rmdir()
        return True
    except OSError:
        return False


_BAD_NAME_CHARS = set('\\/:*?"<>|')


def _clean_run_name(name: str) -> str:
    """校验用户起的输出文件夹名：不允许路径分隔符/Windows 保留字符。

    【为什么必须校验】这个名字会被拼成 `runs\\<名字>`；如果允许 `..\\..`，
        程序就会把文件写到 runs 外面——沙箱里写不进去（报 WinError 5），
        普通环境下则会写到用户没想到的地方。
    """
    name = (name or "").strip().strip(".")
    if not name:
        return ""
    bad = [c for c in name if c in _BAD_NAME_CHARS or ord(c) < 32]
    if bad:
        raise PrepError(f"输出名称里不能有这些字符：{''.join(bad)}（只允许中英文、数字、空格、- _ .）")
    if len(name) > 60:
        raise PrepError("输出名称太长（最多 60 个字符）")
    if name in (".", ".."):
        raise PrepError("输出名称不能是 . 或 ..")
    return name


def _run_output_path(source: str, name: str) -> str:
    """把"输出名称"转成 runs 下的绝对路径；重名自动加 _2/_3，绝不覆盖已有结果。"""
    base = _clean_run_name(name) or f"{Path(source).name or 'run'}_out"
    runs = ROOT / CFG.paths.default_output
    candidate = runs / base
    i = 2
    while candidate.exists():
        candidate = runs / f"{base}_{i}"
        i += 1
        if i > 999:
            raise PrepError("runs 下同名文件夹太多，请换个名称")
    return str(candidate.resolve())


def runtime_mode() -> dict:
    """判断当前进程是"普通启动"还是"受限环境（只能写程序目录）"。

    【为什么要判断】在 AI 沙箱里启动时，进程只能写工作区，写桌面会报
        `PermissionError [WinError 5]`，用户会以为是自己选错目录。
        启动时探一次、状态接口再报一次，界面就能直说"换普通方式启动"。
    """
    home = Path.home()
    workspace_ok = _can_write(ROOT / "runs")
    home_ok = _can_write(home)
    mode = "normal" if home_ok else "restricted"
    label = "普通权限（可选任意输出目录）" if home_ok else "受限环境（只能写程序目录 runs/）"
    return {
        "mode": mode,
        "label": label,
        "workspace_writable": workspace_ok,
        "home_writable": home_ok,
        "home": str(home),
        "integrity": integrity_level(),
    }


def integrity_level() -> str:
    """读当前进程的 Windows 完整性级别（Low/Medium/High）。

    【为什么要看】沙箱不是靠"目录权限"限制，而是把子进程降到 Low 完整性级别；
        Low 进程写不了 Medium 的桌面/文档/用户目录，报错却是 WinError 5，很容易误判。
        显示这一行，就能立刻区分"启动方式不对"和"目录权限真的有问题"。

    【为什么用 whoami 而不是 ctypes】ctypes 调 OpenProcessToken/GetTokenInformation
        要手工声明一堆指针类型，一旦漏了就静默失败；whoami 的输出里直接有
        `Mandatory Label\\Low Mandatory Level Label S-1-16-4096`，稳定且好核对。
    """
    try:
        p = subprocess.run(["whoami", "/groups"], capture_output=True,
                           text=True, errors="replace", timeout=10)
        text = (p.stdout or "") + (p.stderr or "")
        if "S-1-16-4096" in text:
            return "Low"
        if "S-1-16-8192" in text:
            return "Medium"
        if "S-1-16-12288" in text:
            return "High"
        return "unknown"
    except Exception:  # noqa: BLE001 - 拿不到就显示 unknown，不影响主流程
        return "unknown"


class Handler(BaseHTTPRequestHandler):
    server_version = "vasp-prep/2.1"

    # ---- 小工具 ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise PrepError("请求体太大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PrepError(f"请求不是合法 JSON：{exc}") from exc

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志（太吵）
        pass

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802 (http.server 的命名约定)
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                if not INDEX.exists():
                    return self._send(500, "缺少 web/index.html".encode("utf-8"),
                                      "text/plain; charset=utf-8")
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                if CFG.llm.enabled:
                    model = llm_mod.probe(CFG).to_dict()
                else:
                    model = {"available": False, "base_url": CFG.llm.base_url,
                             "models": [], "elapsed_ms": 0,
                             "reason": "已在 vasp-prep.toml 里禁用模型（llm.enabled=false）"}
                return self._json(200, {
                    "ok": True,
                    "config": config_mod.as_dict(CFG),
                    "model": model,
                    # 运行信息：排查"写不进去/连错服务"时，先看这里是谁在跑
                    "runtime": {
                        "pid": os.getpid(),
                        "cwd": str(Path.cwd()),
                        "user": getpass.getuser(),
                        "python": sys.executable,
                        "run_mode": runtime_mode(),
                    },
                })
            if path == "/api/browse":
                # 弹"原生"文件夹对话框；kind=source 选源目录，kind=output 选输出父目录
                qs = parse_qs(urlparse(self.path).query)
                kind = (qs.get("kind") or ["source"])[0]
                initial = (qs.get("initial") or [""])[0]
                title = "选择源目录（含 POSCAR 的文件夹）" if kind == "source" \
                    else "选择输出位置（选一个父文件夹，程序会在里面新建子目录）"
                picked = browse.pick_directory(initial or None, title)
                return self._json(200, {"ok": True, "path": picked, "cancelled": picked is None})
            if path == "/api/open":
                # 打开"人该看的目录"：runs / batches / materials / imports。
                # 也允许带 path= 直接打开任意已存在的目录（批量根、下载子文件夹）。
                qs = parse_qs(urlparse(self.path).query)
                kind = (qs.get("kind") or ["runs"])[0]
                raw = (qs.get("path") or [""])[0]
                target = Path(raw).expanduser() if raw else paths_mod.resolve_kind(kind, CFG)
                target.mkdir(parents=True, exist_ok=True)
                opened, message = _open_folder(target)
                return self._json(200, {"ok": True, "path": str(target),
                                        "opened": opened, "message": message})
            if path == "/api/batch/scan":
                qs = parse_qs(urlparse(self.path).query)
                root = (qs.get("root") or [""])[0].strip()
                if not root:
                    raise PrepError("请先填批量根目录")
                depth = int((qs.get("depth") or [str(CFG.batch.recursive_depth)])[0])
                with _PREP_LOCK:
                    scan = batch_mod.scan_materials(root, depth=depth, cfg=CFG)
                return self._json(200, {"ok": True, "scan": scan})
            if path == "/api/ls":
                # 受限进程打不开资源管理器时的替代：把目录内容列在网页里。
                # 只读、只列一层、最多 300 条，避免误点到大盘把页面卡死。
                qs = parse_qs(urlparse(self.path).query)
                kind = (qs.get("kind") or ["runs"])[0]
                raw = (qs.get("path") or [""])[0]
                target = Path(raw).expanduser() if raw else paths_mod.resolve_kind(kind, CFG)
                if not target.exists():
                    raise PrepError(f"目录还不存在：{target}")
                if not target.is_dir():
                    raise PrepError(f"不是目录：{target}")
                items = []
                for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:300]:
                    try:
                        stt = p.stat()
                        items.append({
                            "name": p.name,
                            "dir": p.is_dir(),
                            "size": 0 if p.is_dir() else stt.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(stt.st_mtime)),
                        })
                    except OSError:
                        continue
                return self._json(200, {"ok": True, "path": str(target), "items": items,
                                        "parent": str(target.parent)})
            if path == "/api/db/providers":
                # 数据源能力表：网页据此显示"哪些能直接下、哪些要导入"
                return self._json(200, {"ok": True, "providers": [
                    {"id": "mp", "label": "Materials Project（OPTIMADE，免 key）", "mode": "search+download"},
                    {"id": "oqmd", "label": "OQMD（OPTIMADE，免 key）", "mode": "search+download"},
                    {"id": "aflow", "label": "AFLOW（AFLUX 检索）", "mode": "search"},
                    {"id": "2dmatpedia", "label": "2Dmatpedia（整库缓存）", "mode": "search"},
                    {"id": "c2db", "label": "C2DB（需官网导出）", "mode": "import"},
                    {"id": "icsd", "label": "ICSD（订阅，导出后导入）", "mode": "import"},
                ]})
            if path == "/api/db/search":
                qs = parse_qs(urlparse(self.path).query)
                provider = (qs.get("provider") or ["mp"])[0]
                formula = (qs.get("formula") or [""])[0].strip()
                limit = int((qs.get("limit") or ["20"])[0])
                if not formula:
                    raise PrepError("请先填化学式，例如 MoS2")
                with _PREP_LOCK:
                    items = [c.to_dict() for c in db_mod.search(provider, formula, limit, CFG)]
                return self._json(200, {"ok": True, "items": items})
            return self._json(404, {"ok": False, "error": "没有这个路径"})
        except PrepError as exc:
            # 用户输入/路径问题：400，并保持 JSON 形状；别让它变成 500 吓人一跳
            return self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # 任何未预期错误都变成 JSON，别让网页看到空白
            return self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/preview":
                source = str(data.get("source") or "").strip()
                name = str(data.get("name") or "").strip()
                text = str(data.get("text") or "").strip()
                if not source or not text:
                    raise PrepError("请先选源目录并写一句需求")
                output = _run_output_path(source, name)
                with _PREP_LOCK:
                    job = service.preview(source, output, text, CFG)
                return self._json(200, {"ok": True, "job": job.to_dict()})
            if path == "/api/batch/preview":
                root = str(data.get("root") or "").strip()
                text = str(data.get("text") or "").strip()
                if not root or not text:
                    raise PrepError("请先填批量根目录并写一句需求")
                name = str(data.get("name") or "").strip()
                ivdw = str(data.get("ivdw") or "").strip()
                depth = data.get("depth")
                with _PREP_LOCK:
                    pv = batch_mod.preview_batch(
                        root, text, name=name, ivdw=ivdw,
                        cfg=CFG, depth=int(depth) if depth not in (None, "") else None)
                bid = uuid.uuid4().hex[:10]
                _BATCH_JOBS[bid] = pv
                while len(_BATCH_JOBS) > _BATCH_KEEP:
                    _BATCH_JOBS.pop(next(iter(_BATCH_JOBS)))
                out = _batch_public(pv)
                out["id"] = bid
                return self._json(200, {"ok": True, "batch": out})
            if path == "/api/batch/write":
                bid = str(data.get("id") or "")
                if not data.get("confirm"):
                    raise PrepError("写入需要 confirm=true（网页上就是那次确认点击）")
                pv = _BATCH_JOBS.get(bid)
                if pv is None:
                    raise PrepError("找不到这个批量预览（可能已过期，请重新预览）")
                with _PREP_LOCK:
                    pv = batch_mod.write_batch(pv, force=bool(data.get("force")))
                out = _batch_public(pv)
                out["id"] = bid
                return self._json(200, {"ok": True, "batch": out})
            if path == "/api/db/download":
                provider = str(data.get("provider") or "").strip()
                cid = str(data.get("id") or "").strip()
                if not provider or not cid:
                    raise PrepError("缺少 provider 或 id")
                cand = db_mod.Candidate(provider=provider, id=cid,
                                        formula=str(data.get("formula") or ""),
                                        url=str(data.get("url") or ""))
                with _PREP_LOCK:
                    dest = db_mod.download(cand, CFG, subdir=str(data.get("subdir") or ""),
                                           force=bool(data.get("force")))
                return self._json(200, {"ok": True, "path": str(dest)})
            if path == "/api/db/import":
                raw = str(data.get("path") or "").strip()
                if not raw:
                    raise PrepError("缺少要导入的路径")
                p = Path(raw).expanduser()
                provider = str(data.get("provider") or "import")
                subdir = str(data.get("subdir") or "")
                with _PREP_LOCK:
                    if p.is_dir():
                        dirs = db_mod.import_folder(p, provider=provider, subdir=subdir,
                                                    cfg=CFG, force=bool(data.get("force")))
                    else:
                        dirs = [db_mod.import_file(p, provider=provider, subdir=subdir,
                                                   cfg=CFG, force=bool(data.get("force")))]
                return self._json(200, {"ok": True, "paths": [str(d) for d in dirs]})
            if path == "/api/write":
                jid = str(data.get("job_id") or "")
                if not data.get("confirm"):
                    raise PrepError("写入需要 confirm=true（网页上就是那次确认点击）")
                with _PREP_LOCK:
                    job = service.write(jid, force=bool(data.get("force")))
                out = job.to_dict()
                out["written"] = True
                return self._json(200, {"ok": True, "job": out})
            return self._json(404, {"ok": False, "error": "没有这个接口"})
        except PrepError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main(argv: list[str] | None = None) -> int:
    host, port = CFG.ui.host, CFG.ui.port
    # 端口被占就往后找 5 个，别让"上次没关干净"卡住今天的工作
    srv = None
    for p in range(port, port + 6):
        try:
            srv = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"[错误] {host}:{CFG.ui.port}~{CFG.ui.port + 5} 都被占用，请关掉占用程序或改 vasp-prep.toml 的 port")
        return 2

    url = f"http://{host}:{port}/"
    mode = runtime_mode()
    print("=" * 60)
    print("VASP 前处理 · 网页版")
    print(f"地址：{url}    （只监听本机，别人访问不到）")
    print(f"模型：{CFG.llm.base_url}  →  {CFG.llm.model}")
    print(f"运行模式：{mode['label']}")
    print(f"进程完整性级别：{mode['integrity']}（Low = 被沙箱降权，Medium = 正常）")
    if mode["mode"] == "restricted":
        print("")
        print("!! 检测到受限环境：这个进程只能写程序自己的目录（runs/）。")
        print("!! 想自由选择输出目录（桌面/文档/D 盘），请关掉本窗口，")
        print("!! 从「资源管理器」里双击 启动网页.bat（不要从 AI 工具/受限终端里启动）。")
    print("按 Ctrl+C 或关闭这个窗口即停止。")
    print("=" * 60)

    if CFG.ui.auto_open:
        # 延迟 0.6 秒再开浏览器：先让服务进入监听状态，避免"连接被拒绝"
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        service.cleanup_all()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
