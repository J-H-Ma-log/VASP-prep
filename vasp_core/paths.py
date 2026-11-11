"""统一的目录与命名规则。

【做什么】把"结果放哪、文件夹叫什么"集中到一处：
          runs（单文件）、batches（批量）、materials（数据库下载）、
          imports（手动导入）、cache（检索缓存）。
【为什么单独一个模块】这些路径被 service/batch/db/webui 四个地方用到；
        各写一份就会出现"网页显示一个路径、实际写到另一个路径"的鬼故事。
【不这么干会怎样】换电脑部署时目录散落在各处，用户找不到产物；
        或者命名规则不一致，同一个材料出现两种文件夹名。
【别的办法】也可以把路径写进 config.toml 到处读；这里做的是"默认值 + 配置覆盖"
        + 统一的 safe_name() 命名，配置项见 vasp-prep.toml 的 [paths]。
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config, ROOT

# 名称里不允许出现的字符（Windows 文件系统 + 跨平台考虑）
_BAD_CHARS = set('\\/:*?"<>|')
# 控制字符（\x00-\x1f）一律删掉：它们能生成"看起来正常、实际打不开"的目录
_CTRL_RE = re.compile(r"[\x00-\x1f]")
# 连续空白压成一个空格，避免"a    b"与"a b"变成两个目录
_SPACE_RE = re.compile(r"\s+")


def safe_name(raw: str, fallback: str = "run", max_len: int = 60) -> str:
    """把用户输入/源目录名变成安全的文件夹名。

    规则（和网页上提示的一致）：
      · 中英文、数字、空格、- _ . 保留；
      · \\ / : * ? " < > | 以及控制字符替换成 _；
      · 去掉首尾空格与首尾的点（Windows 不允许目录以点结尾）；
      · 空字符串、"."、".." 回退到 fallback；
      · 超过 max_len 截断。
    【为什么保留中文】组里材料文件夹常用中文名（"东方明珠"），强行改英文反而认不出。
    【不这么干会怎样】名字里带 / 会把文件写到别的目录；带 : 在 Windows 直接建不出来。
    """
    s = _CTRL_RE.sub("", str(raw or "")).strip()
    s = "".join("_" if (c in _BAD_CHARS) else c for c in s)
    s = _SPACE_RE.sub(" ", s).strip(" .")
    if not s or s in (".", ".."):
        s = fallback
    return s[:max_len].rstrip(" .") or fallback


def unique_dir(parent: Path, name: str, *, max_tries: int = 999) -> Path:
    """在 parent 下取一个还不存在的目录路径：name、name_2、name_3……

    【为什么在这里做】批量写入时多个材料可能同名（都在不同根下的 MoS2），
        或者用户连着跑两次同一批次；统一"不覆盖，自动加序号"，
        比"写到一半报错"或"悄悄覆盖上次结果"都好。
    【注意】只返回路径，不创建目录——真正 mkdir 由调用方在写盘时做。
    """
    candidate = parent / name
    if not candidate.exists():
        return candidate
    for i in range(2, max_tries + 1):
        candidate = parent / f"{name}_{i}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"{parent} 下 {name} 及 _2.._{max_tries} 都已存在")


# ---------------------------------------------------------------------------
# 各根目录：默认值来自 config，配置里没写就用这里的字符串
# ---------------------------------------------------------------------------
def runs_dir(cfg: Config | None = None) -> Path:
    """单文件前处理的输出根（保持历史行为：默认 runs/）。"""
    name = cfg.paths.default_output if cfg else "runs"
    return _as_root(name, "runs")


def batches_dir(cfg: Config | None = None) -> Path:
    """批量前处理的输出根。"""
    name = cfg.paths.batches_dir if cfg else "batches"
    return _as_root(name, "batches")


def materials_dir(cfg: Config | None = None) -> Path:
    """数据库下载/导入后的材料根。"""
    name = cfg.paths.materials_dir if cfg else "materials"
    return _as_root(name, "materials")


def imports_dir(cfg: Config | None = None) -> Path:
    """手动导入的默认落点（可以完全不用，导入时允许选任意目录）。"""
    name = cfg.paths.imports_dir if cfg else "imports"
    return _as_root(name, "imports")


def cache_dir(cfg: Config | None = None) -> Path:
    """数据库检索缓存（给人看没意义，程序自己用）。"""
    name = cfg.paths.cache_dir if cfg else "cache"
    return _as_root(name, "cache")


def _as_root(value: str, default: str) -> Path:
    """把配置里的目录值解析成绝对路径。

    【规则】相对路径一律相对项目根（vasp-prep-v2/），不相对"当前工作目录"——
        否则从桌面双击启动和从命令行启动会落到两个地方。
    【允许】绝对路径（比如 D:\\vasp_runs）原样使用。
    """
    p = Path(str(value or default)).expanduser()
    return p if p.is_absolute() else (ROOT / p)


# 给网页按钮用：kind -> (目录函数, 给人的标题)
KINDS = {
    "runs": (runs_dir, "单文件输出"),
    "batches": (batches_dir, "批量输出"),
    "materials": (materials_dir, "数据库材料"),
    "imports": (imports_dir, "手动导入"),
    "cache": (cache_dir, "检索缓存"),
}


def resolve_kind(kind: str, cfg: Config | None = None) -> Path:
    """按 kind 取目录；未知 kind 抛 KeyError（由 webui 转成 400）。"""
    fn, _ = KINDS[kind]
    return fn(cfg)
