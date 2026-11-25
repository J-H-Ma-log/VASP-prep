"""配置读取：项目根目录下的 vasp-prep.toml（没有就用默认值）。

【做什么】把"模型地址、默认目录、网页端口"这些可调项从代码里拿出来，放进一个
         人可读的文件；换电脑部署时只改这个文件，不动代码。
【为什么这样干】配置写死在代码里，换一台机器就得改源码——那是维护灾难，
         而且用户（你）没法自己调，只能找写代码的人。
【不这么干会怎样】每次换端口/换模型名都要改 .py，改错一处整晚跑不起来。
【别的办法】也可以用环境变量，但 TOML 能写注释、能分层（[llm]/[ui]），
         更适合"看得懂、改得动"的场景；环境变量只作为临时覆盖。

配置文件长这样（vasp-prep.toml）：

    [paths]
    default_source = ""
    default_output = "runs"

    [llm]
    enabled = true
    base_url = "http://127.0.0.1:11434/v1"
    model = "qwen2.5-coder:7b"
    api_key = "ollama"
    timeout = 120
    privacy = "strict"

    [ui]
    host = "127.0.0.1"
    port = 8765
    auto_open = true
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 项目根 = vasp_core/ 的上一级。所有相对路径都相对于它解析，
# 这样无论从哪个目录启动 python，行为都一致。
ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "vasp-prep.toml"


@dataclass
class PathsConfig:
    """默认目录。空字符串表示"每次由界面选"。

    【为什么这些目录能改】每个人盘符/目录习惯不同（有人放 D 盘、有人放网盘同步目录），
        写死了就得改代码。默认值只是"开箱即用"，改成绝对路径也行。
    【相对 vs 绝对】相对路径相对项目根（vasp-prep/），绝对路径原样使用。
    """

    default_source: str = ""
    default_output: str = "runs"      # 单文件前处理
    batches_dir: str = "batches"      # 批量前处理
    materials_dir: str = "materials"  # 数据库下载
    imports_dir: str = "imports"      # 手动导入（可完全不用）
    cache_dir: str = "cache"          # 检索缓存


@dataclass
class BatchConfig:
    """批量处理的上限。设上限是为了防止"手一抖选了 2000 个文件夹"。"""

    max_materials: int = 200     # 一次最多处理多少个材料
    max_dirs: int = 500          # 材料数 × 参数数 的上限（产物目录总数）
    recursive_depth: int = 0     # 0=只看根下第一层；1=再进一层；最多 3


@dataclass
class DbConfig:
    """数据库检索/下载。

    api_key 一律不写进配置文件：走环境变量（C2DB_API_KEY），避免把 key 提交进仓库。
    """

    timeout: int = 30
    c2db_api_key_env: str = "C2DB_API_KEY"
    mp_optimade: str = "https://optimade.materialsproject.org/v1"
    oqmd_optimade: str = "https://oqmd.org/optimade/v1"
    aflow_aflux: str = "https://aflow.org/API/aflux/"
    twodmatpedia_db: str = "http://www.2dmatpedia.org/static/db.json.gz"


@dataclass
class LlmConfig:
    """本地模型（或任何 OpenAI 兼容端点）的连接参数。

    privacy:
      - "strict"：只把中文指令 + 极简摘要（原子数、元素、晶格取整）发给模型，
                  不发坐标、不发文件内容。默认值，适合云端模型。
      - "normal"：允许发结构摘要（元素、晶格、原子数），仍不发坐标。
      - "off"   ：不调用模型，只用规则模式。
    """

    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen2.5-coder:7b"
    api_key: str = "ollama"
    timeout: int = 120
    privacy: str = "strict"


@dataclass
class UiConfig:
    host: str = "127.0.0.1"  # 只监听本机，局域网里别人访问不到
    port: int = 8765
    auto_open: bool = True  # 启动后自动打开浏览器


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    db: DbConfig = field(default_factory=DbConfig)
    root: Path = ROOT
    source_file: Path | None = None  # 实际读到的配置文件（None=全用默认）


def _load_toml(path: Path) -> dict:
    """读 TOML；文件不存在或 Python 太老（<3.11 没 tomllib）就返回空 dict。

    【坑】不要在这里抛异常：配置文件坏了应该是"用默认值 + 提示"，
         而不是"程序根本起不来"。所以只有语法错误才抛（由 load() 转成提示）。
    """
    if not path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+ 自带；3.12/3.14 都有
    except ModuleNotFoundError:  # pragma: no cover - 兼容 3.9/3.10
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _env_override(cfg: Config) -> Config:
    """用环境变量做一次性覆盖，方便临时切模型/端口，不污染配置文件。

    支持：VASP_PREP_LLM_BASE / VASP_PREP_LLM_MODEL / VASP_PREP_PORT
    优先级：环境变量 > vasp-prep.toml > 默认值
    """
    if v := os.environ.get("VASP_PREP_LLM_BASE"):
        cfg.llm.base_url = v
    if v := os.environ.get("VASP_PREP_LLM_MODEL"):
        cfg.llm.model = v
    if v := os.environ.get("VASP_PREP_PORT"):
        try:
            cfg.ui.port = int(v)
        except ValueError:
            pass
    # 无浏览器环境（服务器/自动化测试）用 VASP_PREP_NO_BROWSER=1 关掉自动打开
    if os.environ.get("VASP_PREP_NO_BROWSER"):
        cfg.ui.auto_open = False
    return cfg


def load(path: str | Path | None = None) -> Config:
    """读配置。传 path 可指定文件；默认读项目根目录的 vasp-prep.toml。"""
    p = Path(path).expanduser().resolve() if path else CONFIG_FILE
    data = _load_toml(p)
    cfg = Config(source_file=p if p.exists() else None)

    pa = data.get("paths", {})
    cfg.paths.default_source = str(pa.get("default_source", cfg.paths.default_source))
    cfg.paths.default_output = str(pa.get("default_output", cfg.paths.default_output))
    cfg.paths.batches_dir = str(pa.get("batches_dir", cfg.paths.batches_dir))
    cfg.paths.materials_dir = str(pa.get("materials_dir", cfg.paths.materials_dir))
    cfg.paths.imports_dir = str(pa.get("imports_dir", cfg.paths.imports_dir))
    cfg.paths.cache_dir = str(pa.get("cache_dir", cfg.paths.cache_dir))

    bt = data.get("batch", {})
    cfg.batch.max_materials = int(bt.get("max_materials", cfg.batch.max_materials))
    cfg.batch.max_dirs = int(bt.get("max_dirs", cfg.batch.max_dirs))
    cfg.batch.recursive_depth = int(bt.get("recursive_depth", cfg.batch.recursive_depth))

    db = data.get("db", {})
    cfg.db.timeout = int(db.get("timeout", cfg.db.timeout))
    cfg.db.mp_optimade = str(db.get("mp_optimade", cfg.db.mp_optimade))
    cfg.db.oqmd_optimade = str(db.get("oqmd_optimade", cfg.db.oqmd_optimade))
    cfg.db.aflow_aflux = str(db.get("aflow_aflux", cfg.db.aflow_aflux))
    cfg.db.twodmatpedia_db = str(db.get("twodmatpedia_db", cfg.db.twodmatpedia_db))

    ll = data.get("llm", {})
    cfg.llm.enabled = bool(ll.get("enabled", cfg.llm.enabled))
    cfg.llm.base_url = str(ll.get("base_url", cfg.llm.base_url)).rstrip("/")
    cfg.llm.model = str(ll.get("model", cfg.llm.model))
    cfg.llm.api_key = str(ll.get("api_key", cfg.llm.api_key))
    cfg.llm.timeout = int(ll.get("timeout", cfg.llm.timeout))
    cfg.llm.privacy = str(ll.get("privacy", cfg.llm.privacy))

    ui = data.get("ui", {})
    cfg.ui.host = str(ui.get("host", cfg.ui.host))
    cfg.ui.port = int(ui.get("port", cfg.ui.port))
    cfg.ui.auto_open = bool(ui.get("auto_open", cfg.ui.auto_open))

    return _env_override(cfg)


def as_dict(cfg: Config) -> dict:
    """给网页/命令行看的精简配置（不含 api_key，避免日志泄露）。"""
    return {
        "root": str(cfg.root),
        "source_file": str(cfg.source_file) if cfg.source_file else None,
        "paths": {"default_source": cfg.paths.default_source,
                  "default_output": cfg.paths.default_output,
                  "batches_dir": cfg.paths.batches_dir,
                  "materials_dir": cfg.paths.materials_dir,
                  "imports_dir": cfg.paths.imports_dir,
                  "cache_dir": cfg.paths.cache_dir},
        "batch": {"max_materials": cfg.batch.max_materials,
                  "max_dirs": cfg.batch.max_dirs,
                  "recursive_depth": cfg.batch.recursive_depth},
        "llm": {"enabled": cfg.llm.enabled, "base_url": cfg.llm.base_url,
                "model": cfg.llm.model, "privacy": cfg.llm.privacy,
                "timeout": cfg.llm.timeout},
        "ui": {"host": cfg.ui.host, "port": cfg.ui.port, "auto_open": cfg.ui.auto_open},
    }
