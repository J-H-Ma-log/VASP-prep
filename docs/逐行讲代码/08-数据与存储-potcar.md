# 08 数据与存储：potcar.py 逐行讲解

本文件只讲 `vasp_core\potcar.py`（202 行），即「扫描本地赝势库、按 POSCAR 元素顺序挑选 POTCAR、拼接、检查顺序」这一层。INCAR 与 KPOINTS 见 `04-INCAR与KPOINTS.md`。

## 逻辑树（先看这张）

> POTCAR 这条支线怎么被 `prep.run` 调用。向下 = 调用/数据流。

```text
prep.run（potcar_mode != skip，且解析到了赝势库目录）
   ▼
scan_library(pp_dir)
   └─ 递归找 POTCAR ──► parse_potcar_header
                         ├─ TITEL 只看前 60 行
                         └─ ENMAX 在全文 20KB 内搜
                         → Entry（元素 / 变体 / TITEL / ENMAX / 路径）
   ▼
choose(元素顺序, 库)
   └─ 每个元素按 DEFAULT_PRIORITY 选一个变体；缺元素 → StructureError（退出码 4）
   ▼
check_order(POTCAR 顺序, POSCAR 元素顺序)   ← 顺序错是 VASP 的"沉默错误"
   ▼
concat(选中项) ──► POTCAR 文本（调用方负责写文件）
total_enmax(选中项) ──► 给 ENCUT 一致性检查用
   ▼
prep.run 写 POTCAR + POTCAR.spec；--potcar-only 模式只做上面这四步
```

## 0. potcar.py 在项目里的位置

**谁调用它**：`vasp_core\prep.py`。主流程在 `prep.py:245-253` 依次调 `scan_library`、`choose`、`check_order`、`concat`；`prep.py:269-270` 用 `total_enmax` 给一致性检查提供 ENMAX；`prep.py:402-421` 的 `--potcar-only` 模式又走一遍同样的四步。`__init__.py:32` 把 `check_order, choose, concat, scan_library, spec_text` 再导出；`tests\test_core.py:398-419` 直接测这些函数。

**它读什么、写什么**：只读——递归扫描赝势库目录，读每个 `POTCAR` 文件头部的 `TITEL`/`ENMAX`，拼出 POTCAR 文本返回给调用方。**本模块自己不写任何文件**；写 `POTCAR` 和 `POTCAR.spec` 是 `prep.py:331-334` 的事。

**出错怎么表现**：所有可预期的错误都抛 `StructureError`（`errors.py` 中退出码 4）。这些错误发生在 `prep.py` 写文件之前，所以一旦报错，输出目录不会被写入半成品。消息都是中文、可操作，例如「赝势库里缺少这些元素的 POTCAR：Cu, Co」并给出三种解决办法。

| 行号 | 名称 | 类型 | 一句话 | 被谁调用 |
|---|---|---|---|---|
| 1-16 | 模块 docstring | 文档 | 强调「POTCAR 顺序必须与 POSCAR 元素行一致」这个沉默错误，以及本模块的三件事 | — |
| 18-25 | import | 导入 | `os`/`re`/`dataclass`/`Path`、`StructureError` | — |
| 28 | `DEFAULT_PRIORITY` | 常量 | 变体后缀的挑选优先级 | `choose:129` |
| 30-31 | `_TITEL_RE` / `_ENMAX_RE` | 常量 | 解析 POTCAR 头部的两个正则 | `parse_potcar_header:64,75` |
| 34-44 | `Entry` | dataclass | 一个 POTCAR 文件的身份：元素、变体、TITEL、路径、ENMAX | `parse_potcar_header` 返回，`choose`/`check_order` 用 |
| 47-87 | `parse_potcar_header` | 函数 | 读单个 POTCAR 头部 → `Entry` 或 `None` | `scan_library:108` |
| 90-121 | `scan_library` | 函数 | 递归扫描库目录，按元素归类 | `prep.py:245,269,402`、`tests:399` |
| 124-165 | `choose` | 函数 | 按元素顺序选出每个元素用哪个 POTCAR | `prep.py:246,269,417`、`tests:401,408,419` |
| 168-174 | `concat` | 函数 | 把选中的 POTCAR 拼成一个文本 | `prep.py:250,418` |
| 177-179 | `spec_text` | 函数 | 生成 POTCAR.spec 清单文本 | `tests:403`；`prep.py:43` 导入但主流程未调用（`prep.py:334` 自己拼） |
| 182-184 | `total_enmax` | 函数 | 取所有选中 POTCAR 的最大 ENMAX | `prep.py:269` |
| 187-202 | `check_order` | 函数 | 核对 POTCAR 顺序与 POSCAR 元素顺序 | `prep.py:248,421`、`tests:413` |

## 1. potcar.py（202 行）

### 1.0 文件结构总览

见上表。整体是「一个 dataclass + 两个正则 + 七个函数」：解析（`parse_potcar_header`）→ 扫描（`scan_library`）→ 选择（`choose`）→ 输出（`concat`/`spec_text`/`total_enmax`）→ 校验（`check_order`）。

### 1.1 数据结构

#### `DEFAULT_PRIORITY`（行 28）

**是什么**：字符串列表，`choose` 在同一个元素有多个候选 POTCAR 时，按这个顺序找后缀完全匹配的变体。

**为什么需要**：同一个元素在赝势库里可能有多份（如 `Cu` 和 `Cu_pv`），不指定就会「随便选一个」。VASP 里变体选错不一定报错，但价电子数、截断能都变了。这份列表把默认选择变成确定的、可复现的；要显式指定时用配方里的 `potcar.map`。

| 优先级 | 后缀 | 命中条件 / 说明 |
|---|---|---|
| 1 | `_pv` | 源码注释：过渡金属常用；`e.variant == "_pv"` |
| 2 | `""` | 与元素同名的无后缀版本（如 `Cu`） |
| 3 | `_sv` | `e.variant == "_sv"` |
| 4 | `_d` | `e.variant == "_d"` |
| 5 | `_h` | `e.variant == "_h"` |
| 6 | `_3` | `e.variant == "_3"` |
| 7 | `_2` | `e.variant == "_2"` |
| 8 | `_s` | `e.variant == "_s"` |

**为什么这么写 / 不这么写会怎样**：这里只做字符串匹配，不解释每个后缀的物理含义（那是赝势库文档的事）。匹配不到任何后缀时 `choose:157` 退回 `cands[0]`——因为 `scan_library:120` 已按 `(variant, path)` 排序，所以「第一个候选」是确定的，不会随文件系统遍历顺序变化。

#### `_TITEL_RE`（行 30）

**是什么**：识别 POTCAR 头部元素行的正则：

```
^\s*(?:TITEL\s*=\s*)?([A-Za-z_]+)\s+([A-Za-z0-9_]+)\s+(\S+)?\s*$
```

**为什么需要**：POTCAR 头部有两种历史写法，旧版是一行裸的 `  PAW_PBE Cu_pv 06Sep2000`，新版是 `  TITEL  = PAW_PBE Cu_pv 06Sep2000`。用同一个正则兼容两者，`(?:TITEL\s*=\s*)?` 是可选的。

| 捕获组 | 模式 | 含义 | 示例 |
|---|---|---|---|
| 1 | `[A-Za-z_]+` | 赝势函数族（后面还要过滤） | `PAW_PBE`、`PAW_GGA`、`US`、`NC` |
| 2 | `[A-Za-z0-9_]+` | 元素 + 变体符号 | `Cu`、`Cu_pv`、`H` |
| 3 | `(\S+)?` | 可选的日期/版本 token | `06Sep2000` |

#### `_ENMAX_RE`（行 31）

**是什么**：`ENMAX\s*=\s*([0-9.]+)`，抓 `ENMAX = 293.0` 里的数字。

**为什么需要**：ENMAX 用在 `total_enmax` → `prep.py` 的 `check_consistency`，用来判断 INCAR 里的 `ENCUT` 是否低于赝势要求。正则只抓数字和点，不校验格式（`296.0` 和 `296.` 都能过，坏的如 `.` 会在 `float()` 处被兜住）。

#### `Entry`（行 34-44）

**是什么**：一个 POTCAR 文件被解析后的身份记录。

**为什么需要**：`scan_library` 要把「元素 → 候选列表」存起来，`choose` 要按 `variant` 挑、按 `title` 显式匹配，`concat` 要读 `path`，`check_order` 要比 `element`，`total_enmax` 要读 `enmax`。用一个 dataclass 而不是裸元组，是为了让这些字段有名字、方便传给报告层。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| element | `str` | 无（必填） | 元素符号，`Cu_pv` 里的 `Cu` |
| variant | `str` | 无（必填） | 变体后缀，`Cu_pv` → `_pv`，`Cu` → `""` |
| title | `str` | 无（必填） | TITEL 的完整符号，如 `Cu_pv`；`choose` 的 map 按它精确匹配 |
| path | `Path` | 无（必填） | 该 POTCAR 文件路径 |
| enmax | `float \| None` | `None` | 头部解析出的 ENMAX；没有就是 `None` |

`as_dict()`（42-44）把五个字段装进 dict（`path` 转成 `str`），供 JSON/报告序列化；没有调用方时也不报错，属于预留接口。

### 1.2 逐行讲解

#### `parse_potcar_header(path: str | Path) -> Entry | None`（行 47-87）

**干什么**：读一个文件的头部，判断它是不是 POTCAR，并提取元素、变体、TITEL、ENMAX。

**参数**：`path` — POTCAR 文件路径（str 或 Path）。

**返回**：`Entry`；文件读不了或头部 60 行里没有合法 TITEL 时返回 `None`。**返回 `None` 不是异常**，调用方 `scan_library` 据此跳过。

**异常**：不抛。`OSError`（不存在/无权限）被捕获后返回 `None`；ENMAX 的 `ValueError` 被捕获后置 `None`。

**被谁调用**：`scan_library:108`（本模块内部）；测试不直接调它。

| 行 | 代码 | 说明 |
|---|---|---|
| 47 | def parse_potcar_header(path: str \| Path) -> Entry \| None: | 入口 |
| 48-53 | （docstring） | 说明两种头部写法都要认，并给出旧/新各一例 |
| 54 | p = Path(path) | 统一成 Path，后面 `p.open`/`p` 字段都用它 |
| 55 | try: | 只包住「打开并读取」这一步 |
| 56-57 | （注释） | 解释为什么一次性 `read(20000)` 而不是按行迭代：按行迭代/`next(f)` 在文件比预期短时容易触发 `StopIteration`，反而把整段解析跳过；直接读文本再切行更稳 |
| 58 | with p.open("r", encoding="utf-8", errors="replace") as f: | `errors="replace"`：遇到非 UTF-8 字节用替换符代替，不让解码异常打断扫描 |
| 59 | text = f.read(20000) | 只读前 20000 个字符；POTCAR 通常几十 KB 到几 MB，头部信息足够 |
| 60 | except OSError: | 文件不存在、是目录、没权限等 |
| 61 | return None | 当作「不是可解析的 POTCAR」交给调用方跳过 |
| 62 | head = text.splitlines()[:60] | TITEL 只在头 60 行里找；`text` 可能因 20000 字符截断而结尾不完整，切行不影响 |
| 63 | enmax = None | 先给默认值 |
| 64 | m = _ENMAX_RE.search(text) | **注意**：ENMAX 在整个 20 KB 文本里搜，不受 60 行限制 |
| 65 | if m: | 找到 `ENMAX = ...` |
| 66 | try: | `float` 可能失败 |
| 67 | enmax = float(m.group(1)) | 取捕获组转 float |
| 68 | except ValueError: | 正则允许 `.` 这类畸形数字 |
| 69 | enmax = None | 解析失败就当没有 ENMAX（后续 `total_enmax` 会过滤掉 `None`） |
| 70 | （空行） | — |
| 71 | for line in head: | 逐行找合法的 TITEL 行 |
| 72 | s = line.strip() | 去掉首尾空白，用于判断空行/注释 |
| 73 | if not s or s.startswith("!"): | 空行，或以 `!` 开头的注释行 |
| 74 | continue | 跳过 |
| 75 | m = _TITEL_RE.match(line) | 用整行匹配（正则自带 `^\s*`，能容忍行首缩进）；变量 `m` 在此复用 |
| 76 | if not m: | 不是 TITEL 形态 |
| 77 | continue | 看下一行 |
| 78 | （注释） | 说明还要检查第一段像 `PAW_PBE`/`US`、第二段像 `Cu_pv`/`Cu` |
| 79 | func, sym = m.group(1), m.group(2) | 函数族、元素符号 |
| 80 | if not func.upper().startswith(("PAW", "US", "NC")): | 只认这几类赝势；`upper()` 让大小写不敏感 |
| 81 | continue | 不是就跳过该行 |
| 82 | if not re.match(r"^[A-Z][a-z]?", sym): | 元素符号形状：大写字母开头，最多跟一个小写字母（`Cu`、`H` 合法；`CU`、`PAW_PBE` 这类会被拒） |
| 83 | continue | 形状不对跳过 |
| 84 | element = sym.split("_")[0] | 去掉变体：`Cu_pv` → `Cu` |
| 85 | variant = sym[len(element):] | 切掉元素部分：`Cu_pv` → `_pv`；`Cu` → `""` |
| 86 | return Entry(element=element, variant=variant, title=sym, path=p, enmax=enmax) | 命中第一行合法 TITEL 就返回 |
| 87 | return None | 头 60 行都没有合法 TITEL |

**为什么这么写 / 不这么写会怎样**：`head` 先限制 60 行，避免在一个几 MB 的 POTCAR 里逐行扫；ENMAX 又不在这 60 行里找，因为不同版本 POTCAR 的 ENMAX 行位置不固定，直接在 20 KB 窗口里搜更省事。两者范围不一致是刻意取舍，不是笔误。

#### `scan_library(root: str | Path, *, max_depth: int = 5) -> dict[str, list[Entry]]`（行 90-121）

**干什么**：递归遍历赝势库目录，把每个名为 `POTCAR` 的文件解析成 `Entry`，按元素收进字典。

**参数**：
- `root` — 赝势库根目录（如 `potpaw_PBE.54`）。
- `max_depth` — 递归上限，keyword-only，默认 5。

**返回**：`{元素符号: [Entry, ...]}`。同一元素多份（不同变体/日期/泛函）都在列表里，已排序。

**异常**：`StructureError`：目录不存在；或目录存在但一个 `POTCAR` 都没扫到。

**被谁调用**：`prep.py:245`、`prep.py:269`（`total_enmax(choose(scan_library(...)))`）、`prep.py:402`（`--potcar-only`）、`tests\test_core.py:399,406,411,417`。

| 行 | 代码 | 说明 |
|---|---|---|
| 90 | def scan_library(root: str \| Path, *, max_depth: int = 5) -> dict[str, list[Entry]]: | 入口 |
| 91-95 | （docstring） | 解释为什么限制深度：赝势库可能几万文件，全盘 `os.walk` 很慢；正常布局 `potpaw_PBE.54/Cu/POTCAR` 深度不超过 3 |
| 96 | base = Path(root) | 统一成 Path |
| 97 | if not base.exists(): | 目录不存在（或路径写错） |
| 98 | raise StructureError(f"赝势库目录不存在：{base}") | 直接报错，不返回空库——空库和「路径写错」对用户是两回事 |
| 99 | lib: dict[str, list[Entry]] = {} | 结果容器 |
| 100 | base_depth = len(base.parts) | 基准深度，用「目录路径段数」相对根目录算层级 |
| 101 | for dirpath, dirnames, filenames in os.walk(base): | 标准库递归遍历 |
| 102 | depth = len(Path(dirpath).parts) - base_depth | 当前目录相对根的深度；根目录为 0 |
| 103 | if depth >= max_depth: | 到达上限 |
| 104 | dirnames[:] = [] | 原地清空子目录列表，`os.walk` 就不会再往下走；**不能写 `dirnames = []`**，那只是重新绑定局部变量，遍历器仍会继续 |
| 105 | for fn in filenames: | 只看当前目录的文件 |
| 106 | if fn.upper() != "POTCAR": | 文件名大小写不敏感地精确匹配；`POTCAR.old`、`POTCAR.spec` 都会被排除 |
| 107 | continue | 不是 POTCAR 跳过 |
| 108 | e = parse_potcar_header(Path(dirpath) / fn) | 解析 |
| 109 | if e is None: | 读不了或头部不合法 |
| 110 | continue | 跳过该文件 |
| 111 | lib.setdefault(e.element, []).append(e) | 按元素追加；同一元素保留所有候选 |
| 112 | if not lib: | 一个都没扫到 |
| 113-117 | raise StructureError( f"在 {base} 下没找到任何 POTCAR 文件。\n" f"检查：ls {base} 看是否有 potpaw_PBE.54/ 之类的子目录；\n" f"或者用 --potcar spec 只生成清单，人工拼接。" ) | 消息给出「怎么排查 + 怎么绕过」两条路 |
| 118 | （注释） | 同一元素可能多个版本，按路径排序保证可复现 |
| 119 | for k in lib: | 对每个元素 |
| 120 | lib[k].sort(key=lambda e: (e.variant, str(e.path))) | 先按变体名、再按路径字符串排序；`choose` 的 `hit[0]`/`cands[0]` 因此是确定的 |
| 121 | return lib | — |

**为什么这么写 / 不这么写会怎样**：排序放在 `scan_library` 而不是 `choose`，是为了「扫描结果本身可复现」——测试断言、报告里的标题顺序都依赖它。`max_depth` 用 `>=` 而不是 `>`，所以默认 5 时最深会扫到第 5 层目录（含其中的文件），再往下不进入；这比「不限深度」安全，也比写死布局灵活。

#### `choose(lib, species, *, mapping=None, priority=None) -> list[Entry]`（行 124-165）

**干什么**：按 `species` 的顺序，为每个元素挑一个 `Entry`。三种情况：配方显式指定（`mapping`）、只有一个候选、多个候选时按优先级。

**参数**：
- `lib` — `scan_library` 的结果。
- `species` — 元素符号列表，**按 POSCAR 元素行顺序**，通常是去重后的原子种类顺序。
- `mapping` — 可选，`{元素: 完整 TITEL}`，来自配方 `potcar.map`。
- `priority` — 可选，变体后缀优先级；不传时用 `DEFAULT_PRIORITY`。

**返回**：`list[Entry]`，长度与 `species` 相同、顺序一一对应。

**异常**：`StructureError`：
1. `mapping` 指定了某个元素→某个 TITEL，但该元素的候选里没有这个 TITEL（消息列出可用标题）；
2. 有元素在库里一个候选都没有（所有缺失元素合并成一条消息，一次报全）。

**被谁调用**：`prep.py:246`（主流程）、`prep.py:269`（算 ENMAX）、`prep.py:417`（`--potcar-only`）、`tests\test_core.py:401,408,419`。

| 行 | 代码 | 说明 |
|---|---|---|
| 124-126 | def choose(lib: dict[str, list[Entry]], species: list[str], *, mapping: dict[str, str] \| None = None, priority: list[str] \| None = None) -> list[Entry]: | 签名三行；`mapping`/`priority` 是 keyword-only |
| 127 | （docstring） | 「按 POSCAR 元素顺序选出每个元素对应的 POTCAR」 |
| 128 | mapping = {str(k): str(v) for k, v in (mapping or {}).items()} | `None` 归一成空 dict，键值都转 str，避免 JSON 里出现非字符串键 |
| 129 | prio = priority or DEFAULT_PRIORITY | 调用方不传就用默认优先级 |
| 130 | out: list[Entry] = [] | 结果 |
| 131 | missing: list[str] = [] | 先收集缺失元素，最后一次性报错 |
| 132 | for sym in species: | 严格按传入顺序，这就是 POSCAR 元素行顺序 |
| 133 | cands = lib.get(sym, []) | 该元素的所有候选；没有就是空列表（注意元素符号大小写必须与库里的 TITEL 一致） |
| 134 | if sym in mapping: | 配方显式指定了变体 |
| 135 | want = mapping[sym] | 期望的完整 TITEL |
| 136 | hit = [e for e in cands if e.title == want] | 精确匹配标题 |
| 137 | if not hit: | 指定了但库里没有 |
| 138-141 | raise StructureError( f"potcar.map 指定了 {sym} → {want}，但库里没有这个 POTCAR。\n" f"该元素可用：{[e.title for e in cands] or '（无）'}" ) | 列出该元素实际可用的标题；`or '（无）'` 处理该元素在库里完全不存在的情况 |
| 142 | out.append(hit[0]) | 取第一个匹配（同一标题理论上唯一） |
| 143 | continue | 该元素处理完，下一个 |
| 144 | if not cands: | 没指定映射，且库里没有该元素 |
| 145 | missing.append(sym) | 记下来，不立刻抛 |
| 146 | continue | 先继续处理其他元素 |
| 147 | if len(cands) == 1: | 唯一候选 |
| 148 | out.append(cands[0]) | 没得选，直接用 |
| 149 | continue | — |
| 150 | picked = None | 多候选时按优先级找 |
| 151 | for suffix in prio: | 依次尝试 `_pv`、`""`、`_sv`… |
| 152 | hit = [e for e in cands if e.variant == suffix] | 后缀完全相等才命中 |
| 153 | if hit: | 命中 |
| 154 | picked = hit[0] | 取第一个（`scan_library:120` 已排序，确定） |
| 155 | break | 找到就停，不再看后面的优先级 |
| 156 | if picked is None: | 所有优先级后缀都没命中 |
| 157 | picked = cands[0] | 兜底选排序后的第一个，保证「不会因为不认识的后缀而失败」 |
| 158 | out.append(picked) | — |
| 159 | if missing: | 有缺失元素 |
| 160-164 | raise StructureError( "赝势库里缺少这些元素的 POTCAR：" + ", ".join(missing) + "\n" "解决：① 用 --potcar spec 只生成清单；② 用 potcar.map 指定已有变体；" "③ 检查 --pp-dir 是否指到了 potpaw_PBE.54 这一层。" ) | 一次列出全部缺失元素 + 三条排查路径 |
| 165 | return out | 顺序与 `species` 一致 |

**为什么这么写 / 不这么写会怎样**：`missing` 采用「先收集、最后统一抛」而不是遇到第一个缺失就抛，否则用户要反复跑、每次只看到一个缺失元素。`mapping` 命中失败则立刻抛，因为那是用户明确写下的错误映射，越早指出越好。

#### `concat(entries: list[Entry]) -> str`（行 168-174）

**干什么**：按给定顺序读取每个 POTCAR 的全文，拼成一个 POTCAR 文本。

**参数**：`entries` — 已按 POSCAR 元素顺序排好的 `Entry` 列表。

**返回**：拼接后的文本，相邻两段之间恰好一个空行，末尾有换行。

**异常**：不主动抛。文件在扫描后被删除/无权限时，`read_text` 的 `OSError` 会原样抛给调用方（`prep.py` 的上层会以退出码处理）；`errors="replace"` 只兜解码问题。

**被谁调用**：`prep.py:250`（`auto` 模式真拼 POTCAR）、`prep.py:418`（`--potcar-only`）。

| 行 | 代码 | 说明 |
|---|---|---|
| 168 | def concat(entries: list[Entry]) -> str: | 入口 |
| 169 | （docstring） | 「每段之间保证有换行」 |
| 170 | parts: list[str] = [] | 每段先规范成「恰好一个结尾换行」 |
| 171 | for e in entries: | 按顺序读 |
| 172 | text = e.path.read_text(encoding="utf-8", errors="replace") | 整文件读入；POTCAR 是纯文本，替换非法字节不会影响结构 |
| 173 | parts.append(text.rstrip("\n") + "\n") | 先去掉原有全部结尾换行，再补一个：无论源文件末尾有没有空行，结果都恰好一个 |
| 174 | return "\n".join(parts) | 段之间用 `\n` 连接；因为每段已带一个 `\n`，实际效果是段间一个空行；最后一段保留结尾换行 |

**为什么这么写 / 不这么写会怎样**：如果直接 `"".join(texts)`，源文件末尾少换行时两段会粘在同一行（`...06Sep2000  PAW_PBE...`），VASP 读 POTCAR 时按行解析，可能把第二段的 TITEL 行错位。`rstrip("\n") + "\n"` 是幂等规范化：不管输入怎样，输出每段恰好一个换行分隔。

#### `spec_text(entries: list[Entry]) -> str`（行 177-179）

**干什么**：生成 `POTCAR.spec` 的内容——每行一个完整 TITEL。超算上没有本地赝势库时，用户拿这份清单人工拼接。

**参数**：`entries` — 选中的 `Entry` 列表。

**返回**：文本，每行 `title` + `\n`，末行也有换行。

**异常**：不抛。

**被谁调用**：`tests\test_core.py:403` 断言 `"Cu_pv\nCo\n"`。`prep.py:43` 导入了它，但主流程写 spec 时用的是 `prep.py:334` 的内联 `"".join(f"{t}\n" for t in titles)`，**没有调用本函数**——两处逻辑相同，是重复实现。

| 行 | 代码 | 说明 |
|---|---|---|
| 177 | def spec_text(entries: list[Entry]) -> str: | 入口 |
| 178 | （docstring） | 「每行一个 TITEL 符号，人工拼 POTCAR 时的核对清单」 |
| 179 | return "".join(f"{e.title}\n" for e in entries) | 生成器表达式逐个拼；空列表返回 `""` |

#### `total_enmax(entries: list[Entry]) -> float | None`（行 182-184）

**干什么**：取所有选中 POTCAR 里最大的 ENMAX，作为 `check_consistency` 判断 `ENCUT` 是否够用的依据。

**参数**：`entries` — 选中的 `Entry` 列表。

**返回**：最大 ENMAX；一个可用值都没有时返回 `None`。

**异常**：不抛。

**被谁调用**：`prep.py:269-270`，且只在真正拼了 POTCAR（`potcar_text` 非空）时才算。

| 行 | 代码 | 说明 |
|---|---|---|
| 182 | def total_enmax(entries: list[Entry]) -> float \| None: | 入口 |
| 183 | vals = [e.enmax for e in entries if e.enmax] | 过滤掉 `None`；`if e.enmax` 是「真值判断」，理论上 `0.0` 也会被滤掉（真实 ENMAX 不会为 0） |
| 184 | return max(vals) if vals else None | 空列表返回 `None`，调用方据此跳过 ENMAX 相关检查 |

**为什么这么写 / 不这么写会怎样**：取 `max` 而不是逐元素比较，是因为 VASP 的 `ENCUT` 是全局值，必须不低于所有赝势中最高的那个 ENMAX。返回 `None` 而不是 `0`，是为了让「不知道 ENMAX」和「ENMAX 很小」在检查逻辑里可区分。

#### `check_order(entries: list[Entry], species: list[str]) -> list[dict]`（行 187-202）

**干什么**：核对「拼接出来的 POTCAR 顺序」与「POSCAR 元素行顺序」是否一致。这是本模块存在的核心理由：顺序错了 VASP 不报错，会算出一个看起来正常、物理上错误的结果。

**参数**：
- `entries` — 实际选中的 `Entry` 列表（即将拼进 POTCAR 的顺序）。
- `species` — POSCAR 元素行顺序。

**返回**：`list[dict]`，每条形如 `{"level","code","message"}`，`level` 为 `ERROR` 或 `PASS`。没有问题时返回一条 `PASS`，不是空列表。

**异常**：不抛。

**被谁调用**：`prep.py:248`（主流程，`extend` 进 `potcar_issues`）、`prep.py:421`（`--potcar-only`）、`tests\test_core.py:413`。

| 行 | 代码 | 说明 |
|---|---|---|
| 187 | def check_order(entries: list[Entry], species: list[str]) -> list[dict]: | 入口 |
| 188 | （docstring） | 「核对 POTCAR 顺序与 POSCAR 元素顺序。返回 issue 列表」 |
| 189 | out: list[dict] = [] | 结果 |
| 190 | titles = [e.title for e in entries] | 提前算好标题，PASS 消息和长度检查都用得上 |
| 191 | if len(entries) != len(species): | 段数与元素种数不等 |
| 192-193 | out.append({"level": "ERROR", "code": "potcar-count", "message": f"POTCAR 段数 {len(entries)} ≠ 元素种数 {len(species)}"}) | 数量都不对，逐位比较没有意义 |
| 194 | return out | 提前返回，不再做逐位比较 |
| 195 | for i, (e, sym) in enumerate(zip(entries, species), 1): | 逐位比较；`i` 从 1 开始，对应「第几个元素」 |
| 196 | if e.element != sym: | 元素符号不一致（注意只比 `element`，不比 `variant`） |
| 197-198 | out.append({"level": "ERROR", "code": "potcar-order", "message": f"第 {i} 段 POTCAR 是 {e.title}，POSCAR 第 6 行第 {i} 个元素是 {sym}"}) | 消息直接指出位置和双方取值，用户能立刻定位 |
| 199 | if not out: | 没有任何 ERROR |
| 200-201 | out.append({"level": "PASS", "code": "potcar-order-ok", "message": f"POTCAR 顺序与 POSCAR 一致：{titles}"}) | 显式返回 PASS，报告里能看出「检查跑过且通过」，与「没检查」区分开 |
| 202 | return out | — |

**为什么这么写 / 不这么写会怎样**：`check_order` 只比 `element`（`Cu` vs `Cu`），不比 `variant`（`Cu` vs `Cu_pv`）——变体是科学选择，由 `choose` 的 `mapping`/优先级决定，顺序检查不该把它误报成错误。数量不等时直接返回，是因为逐位 zip 会比较到较短的那个，后面元素会被静默忽略，先报数量错误更准确。

### 1.3 本模块已知边界（可观察到的事实）

- `parse_potcar_header` 的 TITEL 搜索范围是「20 KB 文本的前 60 行」，而 ENMAX 搜索范围是「整个 20 KB 文本」；两者不一致，但都不影响正常 POTCAR（头部信息都在很靠前的位置）。
- `scan_library` 会把同一元素的所有 POTCAR 都收进列表；`choose` 在没有 map、没有优先级命中时退回「排序后第一个」，这是确定的兜底，不是随机。
- `choose` 对元素符号大小写敏感：`species` 里的 `cu` 不会匹配库里 `Cu` 的条目，会走「缺少元素」错误。POSCAR 的物种行通常就是标准写法（`Cu`），所以实际很少触发。
- `spec_text` 在主流程中没有被调用（`prep.py:334` 用内联写法生成同样的内容），只有测试直接调它。
- `prep.py:404-406` 的「`scan_library` 返回空库」检查实际走不到：`scan_library` 在扫不到任何 POTCAR 时已经先抛 `StructureError` 了。
- `check_order` 在 `entries` 与 `species` 都为空时，会返回一条 `PASS`（`titles` 为 `[]`）；正常流程不会传空列表。
- 本模块不做「元素是否在赝势库里」之外的物理校验（如价电子数、推荐 ENCUT 之外的参数），这些由 `prep.py` 的一致性检查和用户配方负责。
