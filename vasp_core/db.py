"""数据库检索与下载（v4）：把 ICSD/AFLOW/OQMD/MP/C2DB/2Dmatpedia… 的结构变成 POSCAR。

【它做什么】
    1) 按化学式（或元素组合）去公开数据库检索候选结构；
    2) 选中一个候选，下载成 `materials\\<provider>_<id>\\POSCAR`；
    3) 记一份 `SOURCE.json`（来自哪个库、哪个 id、URL、下载时间、许可）。
    下载下来的目录可以直接当"批量处理"的根目录用。

【它不做什么】
    - 不做计算，不判断结构好坏；
    - 不替你接受许可协议：ICSD / Materials Cloud / 中科院二维铁电库需要你自己登录导出，
      再用 `import_folder()` 把导出的 CIF/POSCAR 转成统一格式；
    - 不缓存整库（2Dmatpedia 除外：它的 db.json.gz 一次下载后可反复查）。

【为什么用 OPTIMADE 直连 MP/OQMD】
    这两个库都提供公开的 OPTIMADE 端点，返回的字段里直接有
    晶格矩阵 + 笛卡尔坐标 + 每个原子的元素，足以写 POSCAR，不需要 API key。
    MP 的 `limit` 参数不被接受，必须用 `page_limit`（踩过坑，写在 _optimade_search 里）。

运行示例：
    python -m vasp_core.db search --provider mp --formula MoS2
    python -m vasp_core.db download --provider mp --id mp-2815
    python -m vasp_core.db import --path D:\\exported_cifs
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths as paths_mod
from .config import ROOT, Config, load
from .errors import PrepError

# 元素符号表：用来把 "MoS2" 拆成 ["Mo","S"]，不做化学式合法性校验（那是 pymatgen 的事）
_ELEMENTS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si",
    "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni",
    "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb",
    "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho",
    "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np",
    "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr",
}

PROVIDERS = ("mp", "oqmd", "aflow", "2dmatpedia", "c2db", "import")


@dataclass
class Candidate:
    """一条检索结果。字段保持"人看的 + 程序要的"混合，序列化时只输出这些。"""

    provider: str
    id: str
    formula: str = ""
    nsites: int = 0
    url: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "id": self.id,
            "formula": self.formula,
            "nsites": self.nsites,
            "url": self.url,
            "extra": self.extra,
        }


# ---------------------------------------------------------------- 基础工具

def elements_of(formula: str) -> list[str]:
    """把 "MoS2" / "Mo S2" 拆成元素列表（按出现顺序、去重）。

    【为什么不用正则一次到位】"C" 和 "Ca" 的歧义只有化学式解析器能判；
    这里只服务于"检索时按元素过滤"，所以按最长匹配 + 元素表校验即可。
    """
    s = re.sub(r"[^A-Za-z]", "", formula or "")
    out: list[str] = []
    i = 0
    while i < len(s):
        two = s[i:i + 2]
        one = s[i:i + 1]
        nxt = s[i + 2:i + 3]
        # 双字母元素优先，但不能把 "CO"（C+O）误判成 "Co"
        if len(two) == 2 and two in _ELEMENTS and not nxt.islower():
            sym = two
            i += 2
        elif one in _ELEMENTS:
            sym = one
            i += 1
        else:
            i += 1
            continue
        if sym not in out:
            out.append(sym)
    if not out:
        raise PrepError(f"看不出化学式里的元素：{formula!r}（示例：MoS2、Bi2O3）")
    return out


def _http_get(url: str, timeout: int = 30, *, gzip_ok: bool = True) -> bytes:
    """GET 一个 URL；把 HTTP 错误翻译成中文，避免网页上只看到 403/404。"""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "vasp-prep/2.2 (+https://github.com/)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if gzip_ok and resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as exc:
        raise PrepError(f"数据库返回 HTTP {exc.code}：{url}") from exc
    except urllib.error.URLError as exc:
        raise PrepError(f"连不上数据库（网络/代理/超时）：{exc.reason}") from exc


def _http_json(url: str, timeout: int = 30):
    raw = _http_get(url, timeout)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise PrepError(f"数据库返回的不是 JSON：{url}（{exc}）") from exc


def _inv3(m: list[list[float]]) -> list[list[float]]:
    """3×3 逆矩阵。写 POSCAR 要用它把笛卡尔坐标换成分数坐标。"""
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise PrepError("晶格矩阵奇异（体积为 0），无法换算分数坐标")
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


def _fmt(x: float) -> str:
    return f"{x:.10f}"


def poscar_from_optimade(entry: dict) -> str:
    """把 OPTIMADE 的 structure 对象写成 POSCAR 文本。

    【字段来源】attributes.lattice_vectors（3×3，行是晶格矢量）、
    cartesian_site_positions、species_at_sites，都是 OPTIMADE 规范字段。
    【坐标换算】POSCAR 用分数坐标：cart = frac @ L ⇒ frac = cart @ L⁻¹。
    """
    attrs = entry.get("attributes") or {}
    lat = attrs.get("lattice_vectors")
    cart = attrs.get("cartesian_site_positions")
    species = attrs.get("species_at_sites")
    if not lat or not cart or not species:
        raise PrepError("这条 OPTIMADE 记录缺晶格/坐标/元素，无法转 POSCAR")
    inv = _inv3([[float(x) for x in row] for row in lat])
    fracs = []
    for pos in cart:
        x, y, z = (float(v) for v in pos)
        fracs.append([
            sum(x * inv[0][k] for k in range(3)),
            sum(y * inv[1][k] for k in range(3)),
            sum(z * inv[2][k] for k in range(3)),
        ])
    # 按元素分组（VASP 要求同元素连续），并保持"先出现的元素在前"
    order: list[str] = []
    for sym in species:
        if sym not in order:
            order.append(sym)
    counts = [sum(1 for s in species if s == sym) for sym in order]
    ordered_fracs: list[list[float]] = []
    for target in order:
        ordered_fracs.extend(frac for sym_i, frac in zip(species, fracs) if sym_i == target)

    lines = [str(entry.get("id") or "optimade"), "1.0"]
    lines += ["  ".join(_fmt(float(v)) for v in row) for row in lat]
    lines.append("  ".join(order))
    lines.append("  ".join(str(n) for n in counts))
    lines.append("Direct")
    lines += ["  ".join(_fmt(v) for v in frac) for frac in ordered_fracs]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 各库检索

def _norm_formula(s: str) -> str:
    """把化学式压成"只留字母+数字、小写"的串，用来宽松比较 MoS2 / S2Mo / mo s2。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _hill_formula(species: list[str]) -> str:
    """按 Hill 规则拼化学式：有 C 时 C、H 在前，其余按字母序；数量 1 不写。

    【为什么要自己算】OPTIMADE 的 chemical_formula_reduced 排序各家不一致，
    直接字符串比会漏掉 "S2Mo" 这类写法；按原子算一遍再比最稳。
    """
    counts: dict[str, int] = {}
    for s in species:
        counts[s] = counts.get(s, 0) + 1
    keys = list(counts)
    if "C" in counts:
        keys.sort(key=lambda s: (s != "C", s != "H", s))
    else:
        keys.sort()
    return "".join(k + (str(counts[k]) if counts[k] > 1 else "") for k in keys)


def _optimade_search(base: str, formula: str, limit: int, timeout: int) -> list[Candidate]:
    """OPTIMADE 通用检索：MP 与 OQMD 共用。

    【坑】MP 端点拒绝 `limit=`，只认 `page_limit=`；元素过滤用
    `filter=elements HAS ALL "Mo","S"`（OPTIMADE 语法）。
    【为什么要二次过滤】元素过滤只能保证"含有这些元素"，MgMo2S2… 也会进来；
    所以再按 Hill 化学式比一遍，有精确命中就只留精确的。
    """
    els = elements_of(formula)
    # 第一优先：按"还原化学式"精确查（MP/OQMD 都支持 chemical_formula_reduced）。
    # 精确查不到（用户写成 "Mo1S2"、或库的写法不同）再退回"含这些元素"的宽查。
    exact_flt = "chemical_formula_reduced=" + json.dumps(formula.replace(" ", ""))
    data: dict = {}
    try:
        data = _http_json(f"{base.rstrip('/')}/structures?"
                          + urllib.parse.urlencode({"filter": exact_flt, "page_limit": str(limit)}),
                          timeout)
    except PrepError:
        data = {}
    if not (data.get("data") or []):
        flt = "elements HAS ALL " + ", ".join(f'"{e}"' for e in els)
        # 先多取一些再按化学式精筛：只取 limit 条时，精确命中可能排在很后面
        fetch = min(max(limit * 5, 50), 100)
        q = urllib.parse.urlencode({"filter": flt, "page_limit": str(fetch)})
        data = _http_json(f"{base.rstrip('/')}/structures?{q}", timeout)
    want = _norm_formula(formula)
    exact: list[Candidate] = []
    loose: list[Candidate] = []
    for entry in (data.get("data") or []):
        attrs = entry.get("attributes") or {}
        species = attrs.get("species_at_sites") or []
        guess = _hill_formula(species) if species else str(attrs.get("chemical_formula_reduced") or "")
        cand = Candidate(
            provider="mp" if "materialsproject" in base else "oqmd",
            id=str(entry.get("id") or ""),
            formula=guess or formula,
            nsites=int(attrs.get("nsites") or 0),
            url=f"{base.rstrip('/')}/structures/{entry.get('id')}",
            extra={"space_group": (attrs.get("space_group_symbols") or "")},
        )
        (exact if _norm_formula(guess) == want else loose).append(cand)
    return (exact or loose)[:limit]


def _aflow_search(formula: str, limit: int, timeout: int) -> list[Candidate]:
    """AFLOW：用 AFLUX 语法检索；结构下载给 aurl，网页上人工取（$files 目前返回空）。"""
    els = elements_of(formula)
    def _cond(e: str) -> str:
        return f"species({e})"
    aflux = ",".join(_cond(e) for e in els) + f",$paging({limit})"
    url = "https://aflow.org/API/aflux/?aflux=" + urllib.parse.quote(aflux)
    data = _http_json(url, timeout)
    out: list[Candidate] = []
    for row in data if isinstance(data, list) else []:
        auid = str(row.get("auid") or "")
        out.append(Candidate(
            provider="aflow",
            id=auid,
            formula=str(row.get("compound") or formula),
            nsites=0,
            url=str(row.get("aurl") or ""),
            extra={"pearson": row.get("Pearson_symbol_relax") or ""},
        ))
    return out


def _twodmatpedia_search(formula: str, limit: int, timeout: int, cfg: Config) -> list[Candidate]:
    """2Dmatpedia：整库 db.json.gz 下载一次，缓存到 cache\\，之后本地过滤。"""
    cache = paths_mod.cache_dir(cfg) / "2dmatpedia_db.json"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        raw = _http_get(cfg.db.twodmatpedia_db, max(timeout, 120))
        cache.write_bytes(gzip.decompress(raw))
    try:
        rows = json.loads(cache.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise PrepError(f"2Dmatpedia 缓存文件坏了，删掉再试：{cache}") from exc
    key = re.sub(r"[^A-Za-z0-9]", "", formula).lower()
    out: list[Candidate] = []
    for row in rows:
        name = str(row.get("formula") or row.get("pretty_formula") or "")
        if key and key not in re.sub(r"[^A-Za-z0-9]", "", name).lower():
            continue
        out.append(Candidate(
            provider="2dmatpedia", id=str(row.get("material_id") or row.get("mp_id") or ""),
            formula=name, nsites=0, url=str(row.get("source") or ""),
            extra={"nlayers": row.get("nlayers"), "spacegroup": row.get("spacegroup")},
        ))
        if len(out) >= limit:
            break
    return out


def search(provider: str, formula: str, limit: int = 20, cfg: Config | None = None) -> list[Candidate]:
    """按 provider 分流检索。未知 provider 直接报错，列可用值。"""
    cfg = cfg or load()
    if provider == "mp":
        return _optimade_search(cfg.db.mp_optimade, formula, limit, cfg.db.timeout)
    if provider == "oqmd":
        return _optimade_search(cfg.db.oqmd_optimade, formula, limit, cfg.db.timeout)
    if provider == "aflow":
        return _aflow_search(formula, limit, cfg.db.timeout)
    if provider == "2dmatpedia":
        return _twodmatpedia_search(formula, limit, cfg.db.timeout, cfg)
    if provider == "c2db":
        raise PrepError(
            "C2DB 没有稳定的公开检索 API：请在 https://cmrdb.fysik.dtu.dk/c2db/ 导出 JSON/CIF，"
            "再用「导入」把它变成 POSCAR"
        )
    raise PrepError(f"不认识的数据源 {provider!r}；可用：{', '.join(PROVIDERS)}")


def download(cand: Candidate, cfg: Config | None = None, subdir: str = "",
             force: bool = False, timeout: int | None = None) -> Path:
    """下载一个候选结构到 materials\\[子文件夹]\\<provider>_<id>\\{POSCAR,SOURCE.json}。"""
    cfg = cfg or load()
    timeout = timeout or cfg.db.timeout
    root = paths_mod.materials_dir(cfg)
    if subdir:
        root = root / paths_mod.safe_name(subdir, fallback="db")
    sid = re.sub(r"[^A-Za-z0-9._-]", "_", cand.id) or "unknown"
    dest = root / f"{cand.provider}_{sid}"
    if dest.exists() and not force:
        if (dest / "POSCAR").exists():
            return dest
        raise PrepError(f"目录已存在但没有 POSCAR：{dest}（加 force 覆盖）")

    text = ""
    if cand.provider in ("mp", "oqmd"):
        url = cand.url
        if not url:  # 命令行只给了 provider+id 时，按端点规则拼出来
            base = cfg.db.mp_optimade if cand.provider == "mp" else cfg.db.oqmd_optimade
            url = f"{base.rstrip('/')}/structures/{cand.id}"
        entry = _http_json(url, timeout)
        # OPTIMADE 单条结构返回 {"data": {...}}；有的端点直接返回裸对象
        if isinstance(entry, dict) and "data" in entry:
            entry = entry["data"]
        if isinstance(entry, list):
            if not entry:
                raise PrepError(f"这个 id 查不到结构：{cand.id}")
            entry = entry[0]
        text = poscar_from_optimade(entry)
    elif cand.provider == "2dmatpedia":
        raise PrepError(
            "2Dmatpedia 的整库缓存不含坐标；请在网页上按 material_id 下载 CIF 后导入"
        )
    elif cand.provider == "aflow":
        raise PrepError(
            f"AFLOW 的 POSCAR 直链当前不可用；请打开 {cand.url or '其 aurl'} 手动下载后，"
            "用「导入」转成 POSCAR"
        )
    else:
        raise PrepError(f"{cand.provider} 不支持直接下载，请用「导入」")

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "POSCAR").write_text(text, encoding="utf-8")
    (dest / "SOURCE.json").write_text(json.dumps({
        "provider": cand.provider, "id": cand.id, "formula": cand.formula,
        "url": cand.url, "query": cand.extra.get("query", ""),
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "license": "见数据库页面（MP: CC BY 4.0；OQMD: 开放；AFLOW: 开放）",
        "note": "由 vasp_core.db 下载，坐标已转为 POSCAR 分数坐标",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


# ---------------------------------------------------------------- 导入

def _cif_to_poscar_text(cif_path: Path) -> str:
    """CIF → POSCAR。借 pymatgen（只在导入 CIF 时才需要它，没装就给明确提示）。"""
    try:
        from pymatgen.core import Structure
    except ImportError as exc:
        raise PrepError("导入 CIF 需要 pymatgen：在 chgnet_env 里安装后重试"
                        "（pip install pymatgen）") from exc
    st = Structure.from_file(str(cif_path))
    return st.to(fmt="poscar")


def import_file(path: Path, provider: str = "import", subdir: str = "",
                cfg: Config | None = None, force: bool = False) -> Path:
    """把一个文件（POSCAR/CIF）转成 materials\\<provider>_<文件名>\\POSCAR。"""
    cfg = cfg or load()
    if not path.exists():
        raise PrepError(f"找不到文件：{path}")
    sid = re.sub(r"[^A-Za-z0-9._-]", "_", path.stem) or "import"
    root = paths_mod.materials_dir(cfg)
    if subdir:
        root = root / paths_mod.safe_name(subdir, fallback="db")
    dest = root / f"{provider}_{sid}"
    if dest.exists() and not force:
        return dest
    suffix = path.suffix.lower()
    if suffix in (".cif", ".mcif"):
        text = _cif_to_poscar_text(path)
    elif suffix in (".vasp", ".poscar", "") or path.name.upper() in ("POSCAR", "CONTCAR"):
        text = path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".zip":
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            def _looks_like_structure(name: str) -> bool:
                base = Path(name).name.upper()
                return (base in ("POSCAR", "CONTCAR")
                        or name.lower().endswith((".cif", ".mcif", ".vasp", ".poscar")))

            names = [n for n in zf.namelist() if _looks_like_structure(n)]
            if not names:
                raise PrepError(f"zip 里没有 CIF/POSCAR：{path}")
            inner = names[0]
            data = zf.read(inner)
        tmp = dest / Path(inner).name
        tmp.write_bytes(data)
        return import_file(tmp, provider=provider, subdir=subdir, cfg=cfg, force=True)
    else:
        raise PrepError(f"不认识这个格式：{path.suffix}（支持 POSCAR/CIF/zip）")

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "POSCAR").write_text(text, encoding="utf-8")
    (dest / "SOURCE.json").write_text(json.dumps({
        "provider": provider, "id": sid, "source_file": str(path),
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "note": "由本地文件导入（ICSD/Materials Cloud/中科院二维铁电库导出）",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def import_folder(root: str | Path, provider: str = "import", subdir: str = "",
                  cfg: Config | None = None, force: bool = False) -> list[Path]:
    """把一个目录里的所有 CIF/POSCAR/zip 逐个导入；单个失败不影响其它。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        raise PrepError(f"不是文件夹：{root}")
    files = [p for p in sorted(root.rglob("*"))
             if p.is_file() and (p.suffix.lower() in (".cif", ".mcif", ".vasp", ".poscar", ".zip")
                                 or p.name.upper() in ("POSCAR", "CONTCAR"))]
    out: list[Path] = []
    errors: list[str] = []
    for p in files:
        try:
            out.append(import_file(p, provider=provider, subdir=subdir, cfg=cfg, force=force))
        except PrepError as exc:
            errors.append(f"{p.name}: {exc}")
    if not out and errors:
        raise PrepError("一个都没导入成功：\n  " + "\n  ".join(errors[:8]))
    return out


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m vasp_core db",
                                description="从公开数据库检索/下载结构，或导入本地 CIF/POSCAR")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="按化学式检索候选")
    s.add_argument("--provider", required=True, choices=PROVIDERS)
    s.add_argument("--formula", required=True)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("download", help="下载一个候选到 materials\\")
    d.add_argument("--provider", required=True, choices=("mp", "oqmd"))
    d.add_argument("--id", required=True)
    d.add_argument("--formula", default="")
    d.add_argument("--subdir", default="", help="放到 materials\\<子文件夹>\\ 下")
    d.add_argument("--force", action="store_true")
    d.add_argument("--json", action="store_true")

    i = sub.add_parser("import", help="导入本地 CIF/POSCAR/zip")
    i.add_argument("--path", required=True)
    i.add_argument("--provider", default="import")
    i.add_argument("--subdir", default="")
    i.add_argument("--force", action="store_true")
    i.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load()
    try:
        if args.cmd == "search":
            items = [c.to_dict() for c in search(args.provider, args.formula, args.limit, cfg)]
            if args.json:
                print(json.dumps({"ok": True, "items": items}, ensure_ascii=False, indent=2))
            else:
                if not items:
                    print("没有找到；换个化学式或数据源试试。")
                for it in items:
                    print(f"{it['provider']:10s} {it['id']:24s} {it['formula']:12s} "
                          f"nsites={it['nsites']:4d} {it['url']}")
            return 0
        if args.cmd == "download":
            cand = Candidate(provider=args.provider, id=args.id, formula=args.formula)
            dest = download(cand, cfg, subdir=args.subdir, force=args.force)
            if args.json:
                print(json.dumps({"ok": True, "path": str(dest)}, ensure_ascii=False))
            else:
                print(f"已下载：{dest}\\POSCAR")
            return 0
        if args.cmd == "import":
            p = Path(args.path).expanduser()
            dirs = import_folder(p, provider=args.provider, subdir=args.subdir,
                                 cfg=cfg, force=args.force) if p.is_dir() else [
                import_file(p, provider=args.provider, subdir=args.subdir, cfg=cfg, force=args.force)]
            if args.json:
                print(json.dumps({"ok": True, "paths": [str(d) for d in dirs]},
                                 ensure_ascii=False, indent=2))
            else:
                for d in dirs:
                    print(f"已导入：{d}\\POSCAR")
            return 0
    except PrepError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
