# 04 INCAR 与 KPOINTS 逐行讲解

> 本文只讲 `vasp_core/incar.py`（562 行）与 `vasp_core/kpoints.py`（189 行）。行号以当前工作区文件为准；代码列去掉了行首缩进，标识符、运算符、字符串内容未改动。

## 0. 这两个文件在项目里的位置

- `incar.py` 负责 INCAR：解析已有文件 → 套预设 → 应用用户覆盖 → 规范化/白名单/危险键检查 → 写回文本，最后做跨键一致性检查。它读的是 INCAR 文本，产出的是 INCAR 文本 + `Change` 变更列表 + `Issue` 问题列表。
- `kpoints.py` 负责 KPOINTS：把配方 JSON 里的 `kpoints` 段翻译成 `Kpoints` 对象（gamma / mp / line / explicit 四种），再渲染成 VASP 能读的文本；另带一个轻量检查函数。
- 调用链：`recipe.py:238` 在装配配方时调用 `kpoints.from_recipe`；`prep.py:216-221` 调 `build_incar`，`prep.py:258` 调 `render_kpoints`，`prep.py:266-267` 调 `check_kpoints`，`prep.py:271-279` 调 `check_consistency`。
- 只读用途：`tools.py:561-603` 的 `read_incar` 用 `parse_incar` + `INCAR_TYPES`/`DANGEROUS_KEYS` 标出未知键、重复键、危险键；`service.py:118` 只用 `parse_incar` 取键名列表；`nl.py:245,255` 用 `IVDW_NAMES` 把 IVDW 数字翻成人话。
- 出错表现：两个文件的输入类错误抛 `PolicyError`（退出码 3，incar）或 `StructureError`（退出码 4，kpoints），都是 `PrepError` 子类，由 `prep.py:465` 捕获、打印 `[错误] …` 并返回异常自带的 `code`。跨键问题**不抛异常**，`check_consistency` 返回 `Issue` 列表，`prep.py:319-323` 汇总所有 ERROR 后抛 `ValidationError`（退出码 6），此时一个文件都不写。

| 调用方 | 行 | 调用 | 用途 |
|---|---|---|---|
| `prep.py` | 216-221 | `build_incar` | 生成最终 INCAR 文本、变更列表、问题列表 |
| `prep.py` | 258 | `render_kpoints` | 生成 KPOINTS 文本 |
| `prep.py` | 266-267 | `check_kpoints` | KPOINTS 层面的 WARN/ERROR |
| `prep.py` | 271-279 | `check_consistency` | 跨键 / 跨文件一致性检查 |
| `recipe.py` | 238 | `kpoints.from_recipe` | 配方校验阶段把 JSON 段变成 `Kpoints` |
| `tools.py` | 369-375 | `build_incar` | 给网页复算 INCAR.diff |
| `tools.py` | 573 | `parse_incar` | `read_incar` 工具解析用户已有 INCAR |
| `service.py` | 118 | `parse_incar` | 源目录摘要里的 `incar_keys` |
| `nl.py` | 245,255 | `IVDW_NAMES` | 中文解释里的 IVDW 名称 |
| `__init__.py` | 30-31 | 再导出 | `build_incar` / `check_consistency` / `Kpoints` / `kpoints_from_recipe` |

## 1. incar.py（562 行）

### 1.0 文件结构总览

| 行号 | 名称 | 类型 | 一句话 | 被谁调用 |
|---|---|---|---|---|
| 3-15 | 模块 docstring | 文档 | 说明 v1 的坑（MAGMOM 写成 Python 列表、不查 ISMEAR=-5+NSW>0）与四条设计原则 | — |
| 17-22 | import | 导入 | `dataclass`、`Any/Iterable`、`PolicyError` | — |
| 24-27 | 分节注释 | 注释 | 「键的类型定义」 | — |
| 30-79 | `INCAR_TYPES` | 常量表 | 42 个允许自动写的键，每个键带 type/range/enum/unit/note | `build_incar`、`validate_overrides`、`tools.py:581` |
| 81-86 | `DANGEROUS_KEYS` | 常量集合 | 16 个永不自动写的键 | `build_incar`、`validate_overrides`、`tools.py:582` |
| 88-90 | `EXPLICIT_KEYS` | 常量集合 | 必须在用户原话里点名的键（当前只有 IVDW） | 仅被注释/文档引用，代码未读取 |
| 92-93 | `ALLOWED_OVERRIDE_KEYS` | 派生集合 | `INCAR_TYPES - DANGEROUS_KEYS` = 41 个键 | 代码未读取；`skill/vasp-prep/references/recipe-schema.md:87` 当对外契约 |
| 95-117 | `PRESETS` | 常量表 | 四套预设：relax / static / band / none | `build_incar`、`recipe.py:230`（校验 preset 名） |
| 119 | `DEFAULT_INCAR` | 常量 | `dict(PRESETS["relax"])`，无底稿时的默认值 | `build_incar:364` |
| 122-124 | 分节注释 | 注释 | 「行模型与解析」 | — |
| 126-138 | `Line` | dataclass | 一行 INCAR：注释行（key=None）或 key/value 行 | `parse_incar` 返回、`render_incar`/`upsert` 输入输出、`prep.py:216` 接收 |
| 141-149 | `Change` | dataclass | 一处修改：key / old / new / reason，可转 dict | `build_incar` 返回、`prep.py:306`、`report.py` |
| 152-177 | `parse_incar` | 函数 | INCAR 文本 → `list[Line]`，保留注释、缩进、行内注释 | `build_incar:360`、`tools.py:573`、`service.py:118`、测试 |
| 180-200 | `render_incar` | 函数 | `list[Line]` → 文本；没改的行逐字节写回 | `build_incar:397` |
| 203-213 | `upsert` | 函数 | 有该键就改值（保留位置/大小写/注释），没有就追加 | `build_incar:374,393` |
| 216-221 | 分节注释 + `_TRUE`/`_FALSE` | 常量集合 | 布尔值的可接受写法 | 仅 `_fmt_value` 使用 |
| 224-274 | `_fmt_value` | 函数 | 按类型格式化并校验一个值，返回 VASP 能读的字符串 | `build_incar:363,373,389`、`validate_overrides:336` |
| 277-307 | `_fmt_magmom` | 函数 | MAGMOM 的三种写法（标量/列表/字符串）统一成空格分隔 | `_fmt_value:272` |
| 310-314 | `_to_float` | 函数 | 能转 float 就转，失败返回 None | `_fmt_magmom:286,300` |
| 317-321 | `Issue` | dataclass | 一条检查结果：level（PASS/WARN/ERROR）/code/message | `validate_overrides`、`build_incar`、`check_consistency` 返回 |
| 324-339 | `validate_overrides` | 函数 | 只校验用户覆盖键，返回 Issue 列表、不抛异常 | 当前仓库无调用方（只定义未使用） |
| 342-344 | 分节注释 | 注释 | 「构建最终 INCAR」 | — |
| 346-397 | `build_incar` | 函数 | 默认值/底稿 → 预设 → 覆盖 → 文本 + 变更 + 问题 + 行列表 | `prep.py:216`、`tools.py:369`、测试 |
| 400-407 | `_same_value` | 函数 | 判断两个字符串值是否等价（先数值后文本） | `_merge_changes:428` |
| 410-431 | `_merge_changes` | 函数 | 同一个键改两次时合并成一行 old → new，并丢掉没变化的 | `build_incar:397` |
| 434-436 | 分节注释 | 注释 | 「跨键 / 跨文件校验」 | — |
| 438-439 | `incar_dict` | 函数 | `list[Line]` → `{KEY: value}` | 当前仓库无调用方（`prep.py:66` 的 `_incar_dict` 是另一个实现） |
| 442-549 | `check_consistency` | 函数 | 写盘前的确定性跨键检查，返回 Issue 列表 | `prep.py:271`、测试 |
| 552-558 | `IVDW_NAMES` | 常量表 | IVDW 取值 → 人话 | `check_consistency:545`、`nl.py:245,255` |
| 561-562 | `_count_values` | 函数 | 数空格/逗号分隔的值的个数（用于 MAGMOM 长度检查） | `check_consistency:471` |

### 1.1 数据结构

#### `INCAR_TYPES`（行 30-79）

**是什么**：一张「键 → 类型规格」的字典，同时也是允许自动写入的白名单。每个值又是一个 dict，键为 `type`（`float` / `int` / `bool` / `enum` / `float_list`）、`range`、`enum`、`unit`、`note`。

**为什么需要**：VASP 对拼错的键名、类型不对的值大多**静默忽略或用默认值继续算**，不会报错。所以工具必须自己拿着一张表判断「这个键存不存在、值合不合法」。这张表也是 `tools.py` 的 `read_incar` 判断「未知键」的依据。

| 键 | type | 允许值 / 范围 | 备注（源码 note 或说明） |
|---|---|---|---|
| ENCUT | float | 50.0 – 3000.0 | eV，平面波截断能 |
| EDIFF | float | 1e-9 – 1.0 | eV，电子步收敛判据 |
| EDIFFG | float | -10.0 – 1.0 | eV/Å，离子步判据；负值表示力 |
| PREC | enum | Low / Normal / Accurate / Single | — |
| ALGO | enum | Normal / Fast / VeryFast / All / Damped / Subrot / Eigenval / None / CHI / BSE | — |
| ISPIN | int | 1 / 2 | — |
| MAGMOM | float_list | 无 range/enum | 逐原子或按元素展开的初始磁矩 |
| LREAL | enum | Auto / TRUE / FALSE / .TRUE. / .FALSE. | — |
| NELM | int | 1 – 10000 | — |
| KSPACING | float | 0.01 – 2.0 | Å^-1 |
| LWAVE | bool | True / False 及其别名 | — |
| LCHARG | bool | 同上 | — |
| ISMEAR | int | -5 / -4 / -1 / 0 / 1 / 2 | — |
| SIGMA | float | 0.001 – 2.0 | eV |
| ISIF | int | 0 – 7 | — |
| IBRION | int | -1 / 0 / 1 / 2 / 3 / 5 / 6 / 7 / 8 / 44 | — |
| NSW | int | 0 – 100000 | — |
| LORBIT | int | 0 / 1 / 2 / 5 / 10 / 11 / 12 | — |
| ICHARG | int | 0 / 1 / 2 / 3 / 4 / 5 / 10 / 11 / 12 | — |
| LMAXMIX | int | 0 / 2 / 4 / 6 | — |
| LASPH | bool | True / False | — |
| NCORE | int | 1 – 256 | — |
| NPAR | int | 1 – 256 | — |
| KPAR | int | 1 – 256 | — |
| GGA | enum | PE / PS / CA / RP / AM05 / BO / OR | **同时在 `DANGEROUS_KEYS` 里，所以实际不可自动写** |
| AMIX | float | 0.01 – 1.0 | — |
| BMIX | float | 0.001 – 5.0 | — |
| ISYM | int | 0 / 1 / 2 / 3 | — |
| SYMPREC | float | 1e-8 – 1e-2 | — |
| ADDGRID | bool | True / False | — |
| LVTOT | bool | True / False | — |
| LVHAR | bool | True / False | — |
| LAECHG | bool | True / False | — |
| EMIN | float | -50.0 – 20.0 | eV |
| EMAX | float | -20.0 – 50.0 | eV |
| NEDOS | int | 1 – 100000 | — |
| POTIM | float | 1e-4 – 10.0 | — |
| SMASS | float | -3.0 – 3.0 | — |
| TEBEG | float | 0.0 – 10000.0 | K |
| TEEND | float | 0.0 – 10000.0 | K |
| MDALGO | int | 0 / 1 / 2 / 3 / 11 / 21 / 13 | — |
| IVDW | int | 0 / 1 / 2 / 3 / 4 / 10 / 11 / 12 / 13 / 14 / 15 / 20 / 21 / 202 / 263 | vdW 色散校正；11=D3 零阻尼，12=D3(BJ)，20=TS，202=MBD |

行 72-76 的注释解释了 IVDW 的双重身份：它是常用设置，所以没进危险键；它又是科学选择（用哪种色散），所以规则是「必须用户明确点名才写」，见 `EXPLICIT_KEYS`。

#### `DANGEROUS_KEYS`（行 82-86）

**是什么**：16 个「永不自动写」的键。`build_incar` 遇到它们直接抛 `PolicyError`，`validate_overrides` 则记为一条 ERROR 的 Issue。

**为什么需要**：这些键写错不会让 VASP 报错，但会悄悄改变物理模型（结果「看起来正常，物理上全错」），必须人工确认。

| 类别 | 键 | 为什么危险 |
|---|---|---|
| DFT+U | LDAU、LDAUTYPE、LDAUL、LDAUU、LDAUJ、LDAUPRINT | 必须与 POSCAR 元素顺序、每个元素的 d/f 电子数一一对应，写错不会报错 |
| 电荷 / 占据 | NELECT、NELMIN | 带电体系必须同时处理补偿背景，否则总能量无意义 |
| 杂化泛函 | LHFCALC、AEXX、HFSCREEN | 依赖 POTCAR 是否为含 HF 的版本，且计算量/收敛行为大变 |
| metaGGA | METAGGA | 需要匹配的 POTCAR 与额外设置 |
| vdW-DF | VDW_RADIUS、VDW_SCALING、LUSE_VDW | 与 IVDW 是两条不同路线，混用通常不是用户本意 |
| 泛函 | GGA | 改泛函会让 POTCAR 与 INCAR 不匹配 |

#### `EXPLICIT_KEYS` / `ALLOWED_OVERRIDE_KEYS`（行 88-93）

- `EXPLICIT_KEYS = {"IVDW"}`：只有用户原话明确提到（例如「加 D3 修正」「IVDW=12」）才允许写。注释说「模型自己顺手加一个会被服务层拦掉」——**该拦截逻辑不在 incar.py 里**，本文件只提供这个集合；实际执行拦截的是上层对原话的检查。
- `ALLOWED_OVERRIDE_KEYS = set(INCAR_TYPES) - DANGEROUS_KEYS`：42 个键减去 `GGA`（唯一同时出现在两张表里的键），得到 41 个。**注意**：`build_incar` 和 `validate_overrides` 判断的是「在 `INCAR_TYPES` 里且不在 `DANGEROUS_KEYS` 里」，并没有读取这个常量；它被 `skill/vasp-prep/references/recipe-schema.md:87` 当作对外契约描述。

#### `PRESETS`（行 96-117）

**是什么**：四套预设参数。`relax` 用于结构弛豫，`static` 用于静态单点，`band` 用于能带（读 CHGCAR 非自洽），`none` 表示不套任何预设。

**为什么需要 `none`**（注释 112-115 写明）：像「扩成 2x2x1」这种纯结构需求不应该顺手往 INCAR 里塞 ISIF/IBRION/NSW 等十个键——那是改计算设置，不是改结构。不这么干，用户只想换原子，结果 INCAR 被整篇改写。

| 键 | relax | static | band |
|---|---|---|---|
| ENCUT | 520 | 520 | 520 |
| EDIFF | 1e-5 | 1e-6 | 1e-6 |
| EDIFFG | -0.02 | — | — |
| PREC | Accurate | Accurate | Accurate |
| ALGO | Normal | Normal | Normal |
| ISMEAR | 0 | -5 | 0 |
| SIGMA | 0.05 | 0.05 | 0.05 |
| ISIF | 3 | — | — |
| IBRION | 2 | -1 | -1 |
| NSW | 100 | 0 | 0 |
| LREAL | Auto | Auto | — |
| LWAVE | False | False | False |
| LCHARG | True | True | False |
| ICHARG | — | — | 11 |
| LORBIT | — | — | 11 |

`none` 的内容是空字典 `{}`（行 116）。

#### `DEFAULT_INCAR`（行 119）

`DEFAULT_INCAR = dict(PRESETS["relax"])`：没有底稿 INCAR 时，用 relax 预设的副本作为初始行。`dict(...)` 是浅拷贝——内层没有容器，值都是数字/字符串/布尔，所以改动 `DEFAULT_INCAR` 的顶层键不会污染 `PRESETS["relax"]`。

#### `Line`（行 126-138）

**是什么**：一行 INCAR 的完整信息。注释行/空行用 `key=None` 表示；参数行保留原始行、缩进、行内注释。

**为什么这么设计**：写回时，**没改过的行必须逐字节保留**（`raw`），否则整份 INCAR 会因去缩进/重排版而整篇 diff 飘红，真正改动的行反而被淹没。`indent` 和 `note` 让「只改数字」成为可能（`   ENCUT = 520  # 说明` 改完仍是这个形状）。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| key | `str \| None` | 无（必填） | 参数名；注释/空行为 None |
| value | `str \| None` | 无（必填） | 参数值的字符串形式 |
| raw | `str` | 无（必填） | 原始整行（含缩进、行内注释、行尾空白） |
| changed | `bool` | False | 本行是否被本次修改过 |
| indent | `str` | `""` | 原行前导空白 |
| note | `str` | `""` | 行内注释（含 `#` / `!` 符号本身，去掉了行尾空白） |

#### `Change`（行 141-149）

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| key | str | 无 | 键名（统一大写） |
| old | str | 无 | 原值；键原本不存在时是字符串 `"(不存在)"` |
| new | str | 无 | 新值（已格式化为 VASP 写法） |
| reason | str | `"override"` | 修改来源：`override` 或 `preset:relax` 这类 |

`as_dict()`（148-149）把它转成 `{"key","old","new","reason"}`，`prep.py:306-307` 直接拿这个结构进 JSON 摘要。

#### `Issue`（行 317-321）

| 字段 | 类型 | 含义 |
|---|---|---|
| level | str | `PASS` / `WARN` / `ERROR` |
| code | str | 机器可读的短代码，如 `ispin2-no-magmom` |
| message | str | 中文说明，直接进报告 |

#### `IVDW_NAMES`（行 553-558）

**是什么**：IVDW 数值 → 中文/英文名称的对照表，只用于日志和报告。

**为什么放在 545 行使用它的函数之后**：模块加载时先定义 `check_consistency`、后定义 `IVDW_NAMES`，但函数体里的名字是在**调用时**才解析的，所以合法、无行为差别；放在文件尾部是为了把这张表集中在一处。

| IVDW | 名称 | IVDW | 名称 |
|---|---|---|---|
| 0 | 不校正 | 11 | DFT-D3（零阻尼） |
| 1 | DFT-D2 | 12 | DFT-D3(BJ) |
| 2 | TS | 13 | DFT-D4 |
| 3 | DFT-ulg | 14 | libMBD |
| 4 | dDsC | 15 | simple-DFT-D3 |
| 10 | DFT-D2 | 20 | TS |
| 21 | TS/HI | 202 | MBD@rsSCS |
| 263 | MBD@rSC/FI |  |  |

### 1.2 逐行讲解

#### `parse_incar(text: str) -> list[Line]`（行 152-177）

**干什么**：把 INCAR 文本拆成 `Line` 列表；注释行、空行保留为 `key=None`，参数行拆出 key/value，并把前导空白与行内注释单独存起来。

**参数**：`text` — 整个 INCAR 文件的内容。调用方读文件时统一用 `encoding="utf-8-sig"`（`prep.py:210,214`），所以 BOM 已被去掉；本函数自己不做解码。

**返回**：`list[Line]`，行数与 `text.splitlines()` 一致（空文本得到空列表）。

**异常**：不抛异常。任何一行都能落成 `Line`，哪怕格式很奇怪——这是有意的：解析阶段不该因为一行怪格式就拒绝整份 INCAR。

**被谁调用**：`build_incar:360`（有底稿时）、`tools.py:573`（`read_incar`）、`service.py:118`（只取键名）、`tests/test_core.py`。

| 行 | 代码 | 说明 |
|---|---|---|
| 152 | def parse_incar(text: str) -> list[Line]: | 入口 |
| 153-158 | （docstring） | 规则：含 `=` 的是参数，`#`/`!` 开头是注释，其余原样保留；并说明「续行不合并」的取舍：续行只出现在长列表，本工具写 MAGMOM 时自己控制格式，不依赖用户原文件的续行。 |
| 159 | out: list[Line] = [] | 结果容器 |
| 160 | for raw in text.splitlines(): | 逐行遍历；`splitlines()` 不保留行尾 `\n`，最后一行没有换行也能处理 |
| 161 | line = raw.strip() | 只用于判断「是否为空 / 是否注释」，不用于保存原文 |
| 162 | if not line or line.startswith("#") or line.startswith("!"): | 空行、`#` 注释、`!` 注释都走这里；用 `line` 是为了带前导空格的注释也能认出来 |
| 163 | out.append(Line(None, None, raw)) | 原文 `raw` 原样进 `raw` 字段，写回时逐字节保留 |
| 164 | continue | 跳到下一行 |
| 165 | if "=" not in raw: | 不含等号又不是注释（例如垃圾行、漏了 `=` 的行）：同样当注释行保留，不报错 |
| 166 | out.append(Line(None, None, raw)) | 同上，保留原文 |
| 167 | continue | — |
| 168 | k, v = raw.split("=", 1) | 只按第一个 `=` 切分；值里再出现 `=`（很少见）不会被切坏 |
| 169 | key = k.strip() | 键名去掉两侧空白 |
| 170 | indent = raw[: len(raw) - len(raw.lstrip())] | 前导空白 = 原行长度 − 去左侧空白后的长度；用切片取出，制表符/空格都保留 |
| 171 | （注释） | 说明行内注释单独存、改值时原样带回 |
| 172 | pos = [p for p in (v.find("#"), v.find("!")) if p >= 0] | 在「等号右边」找 `#` 和 `!` 的位置；只找第一次出现；找不到的返回 -1 被过滤掉 |
| 173 | cut = min(pos) if pos else -1 | 取最靠前的注释符号；没有注释就是 -1 |
| 174 | note = v[cut:].rstrip() if cut >= 0 else "" | 注释部分从符号本身开始保存（含 `#`/`!`），只去行尾空白；前导空格保留（写回时统一加两个空格） |
| 175 | val = (v if cut < 0 else v[:cut]).strip() | 值 = 等号右边去掉注释后的部分，两端空白去掉 |
| 176 | out.append(Line(key, val, raw, False, indent, note)) | `changed=False`：新解析的行都算「没改过」 |
| 177 | return out | 返回行列表 |

**为什么这么写 / 不这么写会怎样**：没有把「续行」合并进来。VASP 的续行是「下一行以空格开头」表示接上一行的值；这里不做，因为工具自己写长列表（MAGMOM）时会控制成一行，用户原文件里的续行被当成独立注释行也会原样写回，不破坏内容。若强行合并，遇到「以空格开头的参数行」就会误判。

#### `render_incar(lines: Iterable[Line]) -> str`（行 180-200）

**干什么**：把 `Line` 列表写回文本。改过的参数行统一成 `"KEY = VALUE"`（保留缩进和行内注释），没改过的行逐字节写回原文。

**参数**：`lines` — 任意可迭代的 `Line`；`build_incar` 传的是列表。

**返回**：`str`。空列表返回 `""`；非空时保证以 `\n` 结尾。

**异常**：不抛异常。`parts[-1].strip()` 要求 `parts[-1]` 是字符串，`Line.raw/key` 由构造方保证。

**被谁调用**：`build_incar:397`（唯一生产调用方）。

| 行 | 代码 | 说明 |
|---|---|---|
| 180 | def render_incar(lines: Iterable[Line]) -> str: | 入口 |
| 181-186 | （docstring） | 说明为什么只重排改过的行：diff 是给人看的；若全篇重排，整份 INCAR 飘红，真正改动被淹没。行尾空格、制表符、末尾空行都算原文的一部分。 |
| 187 | parts = [] | 每行渲染后的字符串 |
| 188 | for ln in lines: | 逐行 |
| 189 | if ln.key is None or not ln.changed: | 注释行/空行，或本次没改过的参数行 |
| 190 | parts.append(ln.raw) | 原样写回（含缩进、行尾空白） |
| 191 | else: | 改过的参数行 |
| 192 | note = f"  {ln.note}" if ln.note else "" | 有行内注释就前置两个空格；没有就是空串 |
| 193 | parts.append(f"{ln.indent}{ln.key} = {ln.value}{note}") | 保留原缩进和原键名大小写，等号两边各一个空格 |
| 194 | if not parts: | 一个 `Line` 都没有 |
| 195 | return "" | 返回空文本（不返回 `"\n"`，否则会凭空多一个空行） |
| 196 | text = "\n".join(parts) | 用换行连接 |
| 197 | if parts[-1].strip() == "": | 最后一行是空行/纯空白行：原文末尾本来就有一个空行 |
| 198 | （注释） | `join` 只在行之间补了一个 `\n`，需要在末尾再补一个，才等于原来的两个 `\n` |
| 199 | return text + "\n" | 补成 `…\n\n` |
| 200 | return text if text.endswith("\n") else text + "\n" | 一般情况保证末尾恰好一个 `\n`；若最后一行原文自带换行（极少）就不重复加 |

**为什么这么写 / 不这么写会怎样**：`ln.raw` 已经含行尾内容，但 `splitlines()` 去掉了换行符，所以统一在拼接时补。若不特殊处理末尾空行，`["A=1", ""]` 只会得到 `"A=1\n"`，原文件末尾的空行会丢——`prep.py:225-226` 还有一层「纯复制原文件」的兜底，但底稿 + 覆盖同时存在时就必须靠渲染器本身不丢行。

#### `upsert(lines: list[Line], key: str, value: str, reason: str = "override") -> tuple[list[Line], Change]`（行 203-213）

**干什么**：按键名查行：已存在就就地替换值（保留位置、原键名大小写、缩进、行内注释，标记 `changed=True`）；不存在就追加到末尾。

**参数**：
- `lines` — 当前行列表（会被就地修改并返回）。
- `key` — 键名；比较时统一转大写，所以 `encut` 能找到 `ENCUT`。
- `value` — **已经格式化好的**字符串（调用方先过 `_fmt_value`），本函数不再校验。
- `reason` — 写进 `Change.reason` 的来源标记。

**返回**：`(lines, Change)`。`Change.old` 是原值；键原本不存在时是字符串 `"(不存在)"`。`Change.key` 统一大写。

**异常**：不抛异常。

**被谁调用**：`build_incar:374`（套预设）、`build_incar:393`（用户覆盖）。

| 行 | 代码 | 说明 |
|---|---|---|
| 203 | def upsert(lines: list[Line], key: str, value: str, reason: str = "override") -> tuple[list[Line], Change]: | 入口；返回二元组 |
| 204 | （docstring） | 「存在则替换值（保留位置与大小写），不存在则追加到末尾」 |
| 205 | ku = key.upper() | 用于比较和 Change 的规范化键名 |
| 206 | for i, ln in enumerate(lines): | 线性查找；INCAR 一般几十行，不值得建索引 |
| 207 | if ln.key is not None and ln.key.upper() == ku: | 跳过注释行；键名大小写不敏感 |
| 208 | old = ln.value or "" | 记录原值；`None` 归一成空串（参数行理论上不会是 None） |
| 209 | note = f"  {ln.note}" if ln.note else "" | 保留行内注释 |
| 210 | lines[i] = Line(ln.key, value, f"{ln.indent}{ln.key} = {value}{note}", True, ln.indent, ln.note) | 生成新行：用**原** `ln.key` 保留用户写法（如 `encut` 不会被改成 `ENCUT`），`changed=True`，`raw` 同步成新内容 |
| 211 | return lines, Change(ku, old, value, reason) | 命中即返回；同一键在文件里出现多次时只改**第一处** |
| 212 | lines.append(Line(ku, value, f"{ku} = {value}", True)) | 没找到：追加到末尾，用大写键名、无缩进 |
| 213 | return lines, Change(ku, "(不存在)", value, reason) | 用固定文案标记「新增键」 |

**为什么这么写 / 不这么写会怎样**：只改第一处而不改所有重复键，是因为重复键本身已经是异常输入（`tools.py:595-596` 会提示「VASP 通常取最后一次」）。如果要改所有重复项，diff 里会出现多条同名变更，反而掩盖问题；第一处改掉后，`check_consistency` 读到的最终字典仍是「最后一行获胜」，所以后续检查看到的是**未被改动的最后一行**——这是本实现的一个已知边界，文档在此如实记录，不掩饰。

#### `_fmt_value(v: Any, spec: dict, natoms: int | None) -> str`（行 224-274）

**干什么**：按 `spec["type"]` 把 JSON 里的值格式化成 VASP 能读的字符串，同时做类型/范围/枚举检查。所有键的最终写法都由它决定（`bool` → `.TRUE.`，浮点 → `:g` 格式，整数 → 十进制）。

**参数**：
- `v` — 用户给的值，可能来自 JSON（`int/float/bool/str/list`）。
- `spec` — `INCAR_TYPES[键名]` 里那条规格（含 `type`，可能含 `range`/`enum`）。
- `natoms` — 原子数，只有 `MAGMOM` 用标量（一个数展开给所有原子）时才需要。

**返回**：格式化后的字符串。

**异常**：`PolicyError`。类型不符、超出 `range`、不在 `enum` 里、`MAGMOM` 非法，都抛这个（退出码 3）。注意：这里抛出的消息不一定带键名，`build_incar:388-392` 会在外层补上键名后再抛。

**被谁调用**：`build_incar:363,373,389`（默认值、预设、用户覆盖各一次）、`validate_overrides:336`。

| 行 | 代码 | 说明 |
|---|---|---|
| 224 | def _fmt_value(v: Any, spec: dict, natoms: int \| None) -> str: | 入口；函数名以下划线开头表示模块内私有 |
| 225 | （docstring） | 「格式化 + 类型/范围检查」二合一，避免调用方忘了校验 |
| 226 | t = spec["type"] | 取类型标签 |
| 227 | if t == "float": | 浮点键 |
| 228 | if isinstance(v, bool) or not isinstance(v, (int, float)): | 布尔值或非数值（例如字符串 `"520"`）先尝试转 float |
| 229 | try: | 转换可能失败 |
| 230 | v = float(str(v).strip()) | 用字符串中转，`" 520 "` 这类带空白的写法也能接受 |
| 231 | except ValueError as exc: | 转换失败 |
| 232 | raise PolicyError(f"该键需要数值，收到 {v!r}") from exc | 保留原异常链，报错时能看到原始值 |
| 233 | lo, hi = spec.get("range", (float("-inf"), float("inf"))) | 没写 range 的 float 键按「无穷范围」处理（注释 28 行说 range 可选） |
| 234 | if not (lo <= float(v) <= hi): | 闭区间判断；写成 `not (lo<=v<=hi)` 既能拦过小也能拦过大 |
| 235 | raise PolicyError(f"数值 {v} 超出允许范围 [{lo}, {hi}]") | 报出错值和允许区间 |
| 236 | （注释） | ENCUT/NCORE 这类整数语义的键，用 int 更自然（但类型仍是 float，允许 520.0 这类输入） |
| 237 | return f"{float(v):g}" | `:g` 最多 6 位有效数字，自动去掉多余的 0；所以 1e-5 会写成 `1e-05`，520.0 写成 `520` |
| 238 | （空行） | — |
| 239 | if t == "int": | 整数键 |
| 240 | if isinstance(v, bool): | **必须先拦 bool**：`bool` 是 `int` 的子类，不拦的话 `True` 会被当成 1 静默写进 INCAR |
| 241 | raise PolicyError("该键需要整数，收到布尔值") | — |
| 242 | try: | — |
| 243 | iv = int(str(v).strip()) | 用字符串中转；`3.0` 这种字符串会失败（`int("3.0")` 抛 ValueError），不会悄悄截断 |
| 244 | except (TypeError, ValueError) as exc: | JSON 的 null 会走 TypeError |
| 245 | raise PolicyError(f"该键需要整数，收到 {v!r}") from exc | — |
| 246 | if "enum" in spec and iv not in spec["enum"]: | 有枚举就先查枚举 |
| 247 | raise PolicyError(f"取值 {iv} 不在允许集合 {spec['enum']} 中") | — |
| 248 | if "range" in spec: | 有范围再查范围 |
| 249 | lo, hi = spec["range"] | — |
| 250 | if not (lo <= iv <= hi): | — |
| 251 | raise PolicyError(f"整数 {iv} 超出允许范围 [{lo}, {hi}]") | — |
| 252 | return str(iv) | 十进制字符串；`ISPIN=2` 就写 `2` |
| 253 | （空行） | — |
| 254 | if t == "bool": | 布尔键 |
| 255 | if isinstance(v, bool): | JSON 里的 true/false 直接处理 |
| 256 | return ".TRUE." if v else ".FALSE." | VASP 的布尔写法 |
| 257 | s = str(v).strip().upper() | 字符串/数字写法先归一成大写 |
| 258 | if s in _TRUE: | `_TRUE = {"TRUE", ".TRUE.", "T", "YES", "Y", "1"}`（行 220） |
| 259 | return ".TRUE." | — |
| 260 | if s in _FALSE: | `_FALSE = {"FALSE", ".FALSE.", "F", "NO", "N", "0"}`（行 221） |
| 261 | return ".FALSE." | — |
| 262 | raise PolicyError(f"该键需要布尔值（True/False/.TRUE./.FALSE.），收到 {v!r}") | 注意：`"0"`/`"1"` 也接受，因为是 VASP 的常见写法 |
| 263 | （空行） | — |
| 264 | if t == "enum": | 字符串枚举键 |
| 265 | sv = str(v).strip() | 用户输入 |
| 266 | for cand in spec["enum"]: | 逐个比 |
| 267 | if str(cand).lower() == sv.lower(): | **大小写不敏感**，且把枚举项先 `str()`（兼容 int 枚举） |
| 268 | return str(cand) | 返回表里的「标准写法」，不是用户原样：`accurate` → `Accurate`，`true` → `TRUE` |
| 269 | raise PolicyError(f"取值 {v!r} 不在允许集合 {spec['enum']} 中") | 循环跑完都没匹配 |
| 270 | （空行） | — |
| 271 | if t == "float_list": | 列表型键（当前只有 MAGMOM） |
| 272 | return _fmt_magmom(v, natoms) | 交给专门函数 |
| 273 | （空行） | — |
| 274 | return str(v) | 兜底：表里没有的类型直接转字符串。当前 `INCAR_TYPES` 只有 5 种 type，这行实际走不到，但保证函数对所有输入都有返回值 |

**为什么这么写 / 不这么写会怎样**：`float` 和 `int` 都用「先转字符串再转数字」，是为了让 JSON 的 `520`、`520.0`、`"520"` 三种写法都能用；如果直接 `float(v)`，字符串会被接受但 `None` 会抛 TypeError，而这里统一抛 `PolicyError`，上层就能按「策略拒绝」处理（退出码 3）而不是崩溃。

#### `_fmt_magmom(v: Any, natoms: int | None) -> str`（行 277-307）

**干什么**：把 MAGMOM 的三种写法统一成「空格分隔的一串数」：标量 → 展开成 natoms 个；列表 → 逐个格式化；字符串 → 校验后原样（只把逗号换成空格）。这是为了修 v1 的 bug——v1 会把列表直接写成 `MAGMOM = [0.6, 0.6]`，VASP 不认。

**参数**：
- `v` — 数字、数字列表/元组、或字符串。
- `natoms` — 标量展开所需的原子数；`None` 时标量报错。

**返回**：空格分隔的字符串，例如 `"0.6 0.6"`。

**异常**：`PolicyError`（空列表、非数字、布尔值、不支持的未知类型）。

**被谁调用**：`_fmt_value:272`（即所有 `float_list` 类型的键）。

| 行 | 代码 | 说明 |
|---|---|---|
| 277 | def _fmt_magmom(v: Any, natoms: int \| None) -> str: | 入口 |
| 278-283 | （docstring） | 三种写法说明；输出统一空格分隔，因为「VASP 只认这种」 |
| 284 | if isinstance(v, str): | 字符串：高级用户直接给一整行 |
| 285 | toks = v.replace(",", " ").split() | 逗号、空格、制表符、换行都当分隔符 |
| 286 | if not toks or any(_to_float(t) is None for t in toks): | 空串或任意一个 token 转不成数字 → 非法 |
| 287 | raise PolicyError(f"MAGMOM 字符串里有非数字：{v!r}") | — |
| 288 | return " ".join(toks) | **保留用户原始 token 文本**（`0.60` 不会变成 `0.6`），只统一分隔符 |
| 289 | if isinstance(v, bool): | bool 必须在 int/float 之前拦（子类关系） |
| 290 | raise PolicyError("MAGMOM 不能是布尔值") | — |
| 291 | if isinstance(v, (int, float)): | 标量：所有原子同一个初值 |
| 292 | if natoms is None: | 没有原子数就展开不了 |
| 293 | raise PolicyError("MAGMOM 用标量展开时需要知道原子数") | — |
| 294 | return " ".join(f"{float(v):g}" for _ in range(natoms)) | 重复 natoms 次；`:g` 去多余 0 |
| 295 | if isinstance(v, (list, tuple)): | 列表/元组：逐原子（或按元素）给值 |
| 296 | vals = [] | 收集转好的 float |
| 297 | for x in v: | 逐项 |
| 298 | if isinstance(x, bool): | 列表里的 `true/false` 也拦掉 |
| 299 | raise PolicyError("MAGMOM 列表里不能有布尔值") | — |
| 300 | fx = _to_float(x) | 尝试转数字 |
| 301 | if fx is None: | 转不了 |
| 302 | raise PolicyError(f"MAGMOM 列表里有非数字：{x!r}") | — |
| 303 | vals.append(fx) | — |
| 304 | if not vals: | 空列表 |
| 305 | raise PolicyError("MAGMOM 列表是空的") | — |
| 306 | return " ".join(f"{x:g}" for x in vals) | 每个值 `:g` 格式化后空格连接 |
| 307 | raise PolicyError(f"MAGMOM 的类型不支持：{type(v).__name__}") | 例如 dict/None |

**为什么这么写 / 不这么写会怎样**：列表按「给定的个数」原样输出，**不**在这里展开成逐原子；按元素给值（长度=元素种数）是否合法由 `check_consistency:471-476` 判断——那里同时知道 `natoms` 和 `counts`，能判断是「逐原子」还是「按元素」。若在格式化阶段展开，就会把用户的按元素写法悄悄改成逐原子，反而看不出 MAGMOM 与结构对不上。

#### `_to_float(x: Any) -> float | None`（行 310-314）

**干什么**：一个不会抛异常的 `float()` 包装。

**参数**：`x` — 任意值。

**返回**：能转就返回 float，转不了返回 `None`（用于 `any(... is None)` 这种判定）。

**异常**：不抛。

**被谁调用**：`_fmt_magmom:286,300`。

| 行 | 代码 | 说明 |
|---|---|---|
| 310 | def _to_float(x: Any) -> float \| None: | 入口 |
| 311 | try: | — |
| 312 | return float(x) | `float("1e-3")`、`float(True)` 都能转（True→1.0）；布尔在调用方已被拦，这里不管 |
| 313 | except (TypeError, ValueError): | None / dict 抛 TypeError，非法字符串抛 ValueError |
| 314 | return None | 失败标记 |

#### `validate_overrides(overrides: dict[str, Any], natoms: int | None) -> list[Issue]`（行 324-339）

**干什么**：只校验「用户配方里显式给出的覆盖键」，返回问题列表；与 `build_incar` 不同，它**不抛异常**，供需要「一次收集全部问题」的调用方使用。

**参数**：
- `overrides` — 配方里的 `incar.overrides`（键 → 值）。
- `natoms` — 原子数，传给 `_fmt_value` 做 MAGMOM 标量展开检查。

**返回**：`list[Issue]`。每个问题一条；全部合法时返回空列表（**不会**追加 PASS）。

**异常**：不抛。内部 `_fmt_value` 抛的 `PolicyError` 会被捕获并转成 Issue。

**被谁调用**：当前仓库 grep 不到调用方（只有定义和测试文档里提到概念），属于保留的公共校验入口。实际写盘路径用的是 `build_incar`，它在同样检查之后**直接抛** `PolicyError`。

| 行 | 代码 | 说明 |
|---|---|---|
| 324 | def validate_overrides(overrides: dict[str, Any], natoms: int \| None) -> list[Issue]: | 入口 |
| 325 | （docstring） | 「只校验用户配方里显式给出的键，返回问题列表（不抛异常的用 Issue）」 |
| 326 | issues: list[Issue] = [] | 结果容器 |
| 327 | for k, v in overrides.items(): | 逐键 |
| 328 | ku = str(k).upper() | 键名归一；`str()` 防非字符串键 |
| 329 | if ku in DANGEROUS_KEYS: | 危险键：拒绝，且**不再检查值**（`continue`） |
| 330 | issues.append(Issue("ERROR", "dangerous-key", f"{ku} 属于危险键，禁止自动写入（DFT+U/泛函/电荷/vdW）")) | 一条 ERROR |
| 331 | continue | 跳过后续检查 |
| 332 | if ku not in INCAR_TYPES: | 白名单外 |
| 333 | issues.append(Issue("ERROR", "unknown-key", f"{ku} 不在允许的键表里")) | — |
| 334 | continue | — |
| 335 | try: | — |
| 336 | _fmt_value(v, INCAR_TYPES[ku], natoms) | 复用格式化校验；返回值不需要（这里只关心是否合法） |
| 337 | except PolicyError as exc: | 值不合法 |
| 338 | issues.append(Issue("ERROR", f"bad-value:{ku}", str(exc))) | code 里带键名，方便定位 |
| 339 | return issues | — |

**为什么这么写 / 不这么写会怎样**：它和 `build_incar` 的检查内容重复，但错误处理策略不同：一个「收集」，一个「立即中止」。保留两种是因为接口层（网页/工具）常需要一次展示所有问题，而 CLI 写盘则必须 fail-fast。当前代码里没有调用 `validate_overrides`，属于未被使用的 API。

#### `build_incar(*, base_text, preset, overrides, natoms) -> tuple[str, list[Change], list[Issue], list[Line]]`（行 346-397）

**干什么**：INCAR 的总装配函数。三步：① 取底稿（有 `base_text` 就解析它，没有就用内置 relax 默认值）；② 套预设；③ 应用用户覆盖。每一步都过 `INCAR_TYPES` 白名单与类型检查。最后返回最终文本 + 变更列表 + 问题列表 + 最终行列表。

**参数**（全部是 keyword-only，调用时必须写参数名）：
- `base_text` — 现有 INCAR 的全文；`None` 表示没有底稿。
- `preset` — 预设名（`relax`/`static`/`band`/`none`），`None` 表示不套预设。注意 `"none"` 同样会进 `PRESETS`，只是内容为空。
- `overrides` — 用户覆盖，键 → 值。
- `natoms` — 原子数，给 `MAGMOM` 标量展开用。

**返回**：四元组 `(最终文本, 变更列表, 问题列表, 最终行列表)`。`prep.py:216` 拿前三项；第四项给 `prep.py:272` 做一致性检查（那里用的是 `prep.py:66` 自己的 `_incar_dict`，不是本文件的 `incar_dict`）。

**异常**：`PolicyError`（退出码 3）四种情况：未知 preset、覆盖里有危险键、覆盖里键不在白名单、覆盖值不合法。前三种消息各不相同。

**被谁调用**：`prep.py:216`（真正写盘路径）、`tools.py:369`（给网页生成 diff）、`tests/test_core.py`。

| 行 | 代码 | 说明 |
|---|---|---|
| 346-352 | def build_incar( … ) -> tuple[str, list[Change], list[Issue], list[Line]]: | 签名跨 7 行；`*` 表示后面全部是 keyword-only |
| 353-357 | （docstring） | 说明返回四件套的用途：调用方要拿变更列表写 diff/报告、拿问题列表决定是否中止 |
| 358 | issues: list[Issue] = [] | 问题列表；本函数只往里放 PASS（一致性检查在别处） |
| 359 | if base_text is not None: | 有底稿 |
| 360 | lines = parse_incar(base_text) | 解析并保留注释/顺序 |
| 361 | base_note = "以现有 INCAR 为底稿（未知键/注释/顺序保留）" | 后面会作为 PASS 的消息 |
| 362 | else: | 没有底稿 |
| 363-364 | lines = [Line(k, _fmt_value(v, INCAR_TYPES[k], natoms), f"{k} = {_fmt_value(v, INCAR_TYPES[k], natoms)}") for k, v in DEFAULT_INCAR.items()] | 用 relax 默认值建行；`_fmt_value` 调用两次（一次做 value，一次拼 raw），保证两者一致；这些行的 `changed` 用默认值 False |
| 365 | base_note = "没有底稿 INCAR，使用内置保守默认值（relax 预设）" | — |
| 366 | （空行） | — |
| 367 | changes: list[Change] = [] | 变更列表 |
| 368 | （空行） | — |
| 369 | if preset is not None: | 指定了预设才套 |
| 370 | if preset not in PRESETS: | 校验预设名；`"none"` 在 `PRESETS` 里所以合法 |
| 371 | raise PolicyError(f"未知 preset：{preset}（可用：{sorted(PRESETS)}）") | `sorted` 让报错内容稳定可复现 |
| 372 | for k, v in PRESETS[preset].items(): | 按预设字典顺序逐键（`"none"` 是空字典，直接跳过） |
| 373 | sval = _fmt_value(v, INCAR_TYPES[k], natoms) | 预设值也要过类型检查（防止有人改表改错） |
| 374 | lines, ch = upsert(lines, k, sval, reason=f"preset:{preset}") | 有底稿时改原行，没底稿时改默认行；reason 记录来源 |
| 375 | changes.append(ch) | 每个预设键都记一条（后面 `_merge_changes` 会过滤/合并） |
| 376 | （空行） | — |
| 377 | for k, v in overrides.items(): | 用户覆盖，最后应用，所以优先级最高 |
| 378 | ku = str(k).upper() | 键名归一 |
| 379 | if ku in DANGEROUS_KEYS: | 危险键直接拒绝，不进入后面的值检查 |
| 380-383 | raise PolicyError( f"拒绝自动写入危险键 {ku}：…\n（这类参数写错不会报错，但结果物理上不可信）" ) | 消息分两行：第一行说拒绝，第二行解释后果。**这里抛异常**，不是返回 Issue |
| 384 | if ku not in INCAR_TYPES: | 白名单外 |
| 385-387 | raise PolicyError( f"键 {ku} 不在白名单里。允许的键：{sorted(INCAR_TYPES)}" ) | 把全部合法键名报给用户，避免反复试错 |
| 388 | try: | — |
| 389 | sval = _fmt_value(v, INCAR_TYPES[ku], natoms) | 值格式化 + 校验 |
| 390 | except PolicyError as exc: | 值不合法 |
| 391 | （注释） | 把键名带上：只说「取值 99 不在允许集合」用户不知道是哪个键 |
| 392 | raise PolicyError(f"{ku}：{exc}") from exc | 重新抛，保留原异常链 |
| 393 | lines, ch = upsert(lines, ku, sval, reason="override") | 覆盖写入 |
| 394 | changes.append(ch) | — |
| 395 | if base_text is not None: | 只有「有底稿」才记这条 PASS |
| 396 | issues.append(Issue("PASS", "base-incar", base_note)) | 没有底稿时 issues 保持为空 |
| 397 | return render_incar(lines), _merge_changes(changes), issues, lines | 渲染文本、合并变更、返回问题与最终行 |

**为什么这么写 / 不这么写会怎样**：预设先执行、覆盖后执行，所以「用户显式值 > 预设值」；两者对同一个键都改时，`upsert` 会改两次，`_merge_changes` 再把它们合成「原值 → 用户值」一条。如果把顺序倒过来，用户值会被预设覆盖，这正是最容易让人困惑的「我明明写了 600，结果 INCR 里还是 520」。

#### `_same_value(a: str, b: str) -> bool`（行 400-407）

**干什么**：判断两个值文本是否「等价」，用于 `_merge_changes` 丢弃没实际变化的条目。

**参数**：`a`、`b` — 两个值字符串（来自 `Change.old/new`）。

**返回**：`True` 表示等价。

**异常**：不抛。所有转换失败都被 `except` 接住。

**被谁调用**：`_merge_changes:428`。

| 行 | 代码 | 说明 |
|---|---|---|
| 400 | def _same_value(a: str, b: str) -> bool: | 入口 |
| 401 | （docstring） | 先按数值比，再按大小写不敏感文本比 |
| 402 | if a.strip() == b.strip(): | 文本完全相同（去两端空白） |
| 403 | return True | — |
| 404 | return abs(float(a.split()[0]) - float(b.split()[0])) < 1e-12 | 只取第一个空格分隔的 token 转 float；差小于 1e-12 视为相同 |
| 405 | except (ValueError, IndexError): | 不是数字（如 `(不存在)`、空串） |
| 406 | return a.strip().lower() == b.strip().lower() | 退回大小写不敏感的文本比较（`Auto` vs `auto`） |

**为什么这么写 / 不这么写会怎样**：用 1e-12 的绝对容差而不是字符串相等，是为了让 `520` 和 `520.0` 这类「数值一样、写法不同」不被算成改动。**已知边界**：对 `MAGMOM` 这种空格分隔的多值键，只比较第一个数——如果用户把 `"0.6 0.6"` 改成 `"0.6 -0.6"`，`_same_value` 会判成相同，`_merge_changes` 于是不把这处改动写进变更列表（INCAR 文本本身仍已更新，因为 `lines` 是单独返回的）。这个函数最初是为单值数值键写的，多值键的变更报告因此可能漏记。

#### `_merge_changes(changes: list["Change"]) -> list[Change]`（行 410-431）

**干什么**：同一个键被预设和覆盖连续改两次时，合并成一条「最初 old → 最终 new」，并丢掉 old 与 new 等价的条目。

**参数**：`changes` — `build_incar` 里按「预设 → 覆盖」顺序累积的变更列表。

**返回**：合并后的列表，顺序 = 键**第一次出现**的顺序。

**异常**：不抛。

**被谁调用**：`build_incar:397`。

| 行 | 代码 | 说明 |
|---|---|---|
| 410 | def _merge_changes(changes: list["Change"]) -> list[Change]: | 入口；`"Change"` 加引号是前向引用写法（这里 `Change` 已定义，引号只是风格） |
| 411-415 | （docstring） | 报告里出现两行 ENCUT 会让人误以为改了两次；真正要告诉人的是「原值 → 最终值」 |
| 416 | merged: dict[str, Change] = {} | 键 → 合并后的 Change |
| 417 | order: list[str] = [] | 记录键首次出现的顺序，保证输出稳定 |
| 418 | for ch in changes: | 按发生顺序 |
| 419 | if ch.key not in merged: | 第一次见到这个键 |
| 420 | merged[ch.key] = Change(ch.key, ch.old, ch.new, ch.reason) | 存下最初的 old（关键：old 取第一次） |
| 421 | order.append(ch.key) | 记顺序 |
| 422 | else: | 这个键之前改过 |
| 423 | prev = merged[ch.key] | 取出上一条 |
| 424 | merged[ch.key] = Change(prev.key, prev.old, ch.new, ch.reason) | old 保留第一次的，new 和 reason 用最后一次的 |
| 425 | out: list[Change] = [] | 最终结果 |
| 426 | for k in order: | 按首次出现顺序输出 |
| 427 | ch = merged[k] | — |
| 428 | if _same_value(ch.old, ch.new): | 值没变（例如预设 relax 的值恰好等于底稿原值） |
| 429 | continue | 不写进报告 |
| 430 | out.append(ch) | — |
| 431 | return out | — |

**为什么这么写 / 不这么写会怎样**：默认值 + relax 预设里每个键都会产生一条 Change，但绝大多数 old==new；如果不过滤，change_report 会把「没改任何东西」写成一大片变更。反过来，覆盖与预设对同一键都改时，不合并就会出现同一个键两行，读者无法判断 INCAR 最终是什么。

#### `incar_dict(lines: list[Line]) -> dict[str, str]`（行 438-439）

**干什么**：把行列表转成 `{大写键名: 值文本}`，跳过注释行。

**参数**：`lines` — `parse_incar` 或 `build_incar` 产出的行。

**返回**：字典；同一个键出现多次时**后出现的覆盖先出现的**（字典赋值语义）。

**异常**：不抛。

**被谁调用**：当前仓库 grep 不到调用方。`prep.py:66` 有一个功能相同的私有实现 `_incar_dict`，实际写盘路径用的是它。

| 行 | 代码 | 说明 |
|---|---|---|
| 438 | def incar_dict(lines: list[Line]) -> dict[str, str]: | 入口 |
| 439 | return {ln.key.upper(): (ln.value or "") for ln in lines if ln.key is not None} | 注释行 key 为 None 被过滤；value 为 None 时归一成空串；重复键由后写者获胜 |

#### `check_consistency(...) -> list[Issue]`（行 442-549）

**干什么**：写盘前的确定性跨键检查。检查的都是「VASP 不会报错、但结果不可信」的组合：ISPIN=2 没 MAGMOM、MAGMOM 个数不对、ISMEAR=-5 配弛豫、ENCUT 低于 POTCAR 的 ENMAX、KSPACING 与 KPOINTS 并存、DFT+U 参数不全、IVDW 取值非法/与 LUSE_VDW 叠加等。

**参数**（全部 keyword-only）：
- `incar` — `{大写键: 值文本}`（`prep.py:66` 生成）。
- `natoms` — 原子数。
- `counts` — 每种元素的原子数列表；MAGMOM 按元素写时用来判断长度。
- `species` — 去重后的元素顺序列表。
- `calc_type` — `relax`/`static`/`band`，来自配方 `calc`。
- `potcar_enmax` — 拼好的 POTCAR 里最大的 ENMAX；`None` 表示没拼 POTCAR。
- `has_kpoints_file` — 输出目录是否会有 KPOINTS 文件。

**返回**：`list[Issue]`。可能包含 PASS（`base` 之外的 `ivdw-on`、`incar-consistency`）、WARN、ERROR；调用方 `prep.py:282` 只按 `level=="ERROR"` 决定中止。

**异常**：不抛。所有解析失败（`float()` 失败）都退化成 `None`，然后走「缺失」分支。

**被谁调用**：`prep.py:271-279`（写盘前）、`tests/test_core.py:302-343`。

| 行 | 代码 | 说明 |
|---|---|---|
| 442-451 | def check_consistency( *, … ) -> list[Issue]: | 签名跨 10 行，全 keyword-only |
| 452 | （docstring） | 「这些组合错了不会报错，但结果不可信」 |
| 453 | issues: list[Issue] = [] | 结果容器 |
| 454 | （空行） | — |
| 455 | def num(key: str) -> float \| None: | 内部小工具：取某个键的第一个 token 当数字 |
| 456 | v = incar.get(key) | 键不存在 → None |
| 457 | if v is None: | — |
| 458 | return None | — |
| 459 | try: | — |
| 460 | return float(v.split()[0]) | 只取第一个 token；所以 `"520 eV"` 也能读成 520 |
| 461 | except (ValueError, IndexError): | 值不是数字，或值为空串（`split()` 后取下标 0 抛 IndexError） |
| 462 | return None | 当成「没设置」 |
| 463 | （空行） | — |
| 464 | ispin = num("ISPIN") | — |
| 465 | magmom = incar.get("MAGMOM") | 原始文本，不是数字 |
| 466 | if ispin == 2: | 自旋极化 |
| 467 | if not magmom: | 没给 MAGMOM（空串也算） |
| 468-469 | issues.append(Issue("ERROR", "ispin2-no-magmom", "ISPIN=2 但没有 MAGMOM：VASP 会用默认初值，磁性结果可能完全错")) | 这是 ERROR：结果可能物理上错 |
| 470 | else: | 有 MAGMOM |
| 471 | n = _count_values(magmom) | 数 token 个数 |
| 472 | if n not in (natoms, len(counts) if counts else -1): | 合法长度只有两种：逐原子（=natoms）或按元素（=元素种数）；`counts` 为空时用 -1 表示「这个选项不存在」 |
| 473-476 | issues.append(Issue("ERROR", "magmom-length", f"MAGMOM 个数 {n} ≠ 原子数 {natoms}（也不等于元素种数）。替位/空位/扩胞后必须重算 MAGMOM")) | 常见的沉默错误：改结构后忘了改 MAGMOM |
| 477 | elif ispin is None: | 没设置 ISPIN |
| 478 | issues.append(Issue("WARN", "ispin-missing", "没有设置 ISPIN（默认 1，非自旋极化）")) | WARN：不是错，但可能不是用户本意 |
| 479 | （空行） | — |
| 480 | ismear = num("ISMEAR") | — |
| 481 | nsw = num("NSW") or 0 | 没设置就当 0（静态/单点） |
| 482 | if ismear == -5 and nsw > 0: | 四面体法 + 离子步 |
| 483-484 | issues.append(Issue("ERROR", "ismear-tetra-relax", "ISMEAR=-5（四面体法）不能用于弛豫（NSW>0），力不准；静态计算才用它")) | 这是 v1 不检查的经典致命组合 |
| 485 | （空行） | — |
| 486 | isif = num("ISIF") | — |
| 487 | ibrion = num("IBRION") | — |
| 488 | if isif is not None and isif > 0 and ibrion is not None and ibrion <= 0: | ISIF>0 会动晶胞，但 IBRION≤0 不动离子 |
| 489-490 | issues.append(Issue("WARN", "isif-without-ibrion", f"ISIF={isif:g}（会动晶胞）但 IBRION={ibrion:g}（不动离子），设置互相矛盾")) | WARN 而不是 ERROR：有些特殊流程确实这么用 |
| 491 | if nsw > 0 and ibrion == -1: | 有离子步数但 IBRION=-1（不做弛豫） |
| 492-493 | issues.append(Issue("WARN", "nsw-with-ibrion-minus1", "NSW>0 但 IBRION=-1：不会做离子弛豫，NSW 没有意义")) | — |
| 494 | （空行） | — |
| 495 | encut = num("ENCUT") | — |
| 496 | if encut is None: | 没设 ENCUT |
| 497 | issues.append(Issue("WARN", "encut-missing", "没有设置 ENCUT：VASP 会用 POTCAR 的最大 ENMAX")) | — |
| 498 | elif encut < 200 or encut > 1000: | 有值但偏离常见区间 |
| 499 | issues.append(Issue("WARN", "encut-range", f"ENCUT={encut:g} eV 偏离常见区间 [200, 1000]")) | — |
| 500 | if encut is not None and potcar_enmax is not None and encut < potcar_enmax: | 截断能低于赝势推荐值 |
| 501-502 | issues.append(Issue("ERROR", "encut-below-enmax", f"ENCUT={encut:g} < POTCAR 的 ENMAX={potcar_enmax:g}：会严重欠收敛")) | ERROR：不是「可能不好」，而是明确欠收敛 |
| 503 | （空行） | — |
| 504 | if incar.get("KSPACING") and has_kpoints_file: | 两个 K 点来源同时存在 |
| 505-506 | issues.append(Issue("ERROR", "kspacing-vs-kpoints", "INCAR 有 KSPACING，同时目录里有 KPOINTS：两者冲突，VASP 优先用 KPOINTS")) | VASP 不警告、直接忽略 KSPACING，所以工具层必须报 |
| 507 | （空行） | — |
| 508 | icharg = num("ICHARG") | — |
| 509 | if icharg == 11 and calc_type != "band": | ICHARG=11 是非自洽能带专用 |
| 510-512 | issues.append(Issue("WARN", "icharg11-not-band", "ICHARG=11（读 CHGCAR 做非自洽）通常用于能带；当前 calc_type=" f"{calc_type}")) | WARN：配方 `calc` 类型与参数不一致 |
| 513 | （空行） | — |
| 514 | ldau_keys = [k for k in ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ") if k in incar] | 底稿里可能本来就带 DFT+U 键（`build_incar` 不改它们，只保留） |
| 515 | if ldau_keys and len(ldau_keys) < 4: | 出现了 DFT+U 键，但不足 4 个 |
| 516-517 | issues.append(Issue("ERROR", "ldau-incomplete", f"DFT+U 参数不全：只给了 {ldau_keys}，LDAU 必须同时给 LDAUTYPE/LDAUL/LDAUU/LDAUJ")) | **已知边界**：5 个键里给 4 个（例如缺 LDAUJ）不会触发，因为条件是 `< 4` 而不是 `< 5`；与消息文案「必须同时给」不完全一致，这里按源码实际行为记录 |
| 518 | （空行） | — |
| 519 | if "NELECT" in incar: | 手动设置电子数 |
| 520-521 | issues.append(Issue("WARN", "nelect-manual", "手动设置 NELECT（带电体系）：确认电荷量与补偿背景设置正确")) | 工具不自动处理带电背景，只提示 |
| 522 | （空行） | — |
| 523 | if species and incar.get("MAGMOM") and len(species) > 1: | 多元素体系且有 MAGMOM |
| 524-526 | issues.append(Issue("WARN", "magmom-check-by-species", f"MAGMOM 需要按 POSCAR 元素顺序（{species}）逐原子对齐；若是按元素写的，确认元素个数与 counts 一致")) | 只提示，不自动改：工具不知道用户是按原子还是按元素写的 |
| 527 | （空行） | — |
| 528 | （注释） | 「vdW 色散校正（IVDW）相关」分节 |
| 529 | ivdw = num("IVDW") | — |
| 530 | if ivdw is not None: | 设置了才检查 |
| 531 | if ivdw not in (0, 1, 2, 3, 4, 10, 11, 12, 13, 14, 15, 20, 21, 202, 263): | 与 `INCAR_TYPES["IVDW"]["enum"]` 相同的 15 个取值，这里又硬编码了一遍 |
| 532-533 | issues.append(Issue("ERROR", "ivdw-invalid", f"IVDW={ivdw:g} 不是 VASP 认识的取值（见 vasp.at/wiki/index.php/IVDW）")) | — |
| 534 | luse_vdw = str(incar.get("LUSE_VDW", "")).strip().upper().strip(".") | 把 `.TRUE.` 归一成 `TRUE`：先去空白、转大写、再去掉两端的点 |
| 535 | if ivdw > 0 and luse_vdw in ("TRUE", "T"): | 同时开了两条色散路线 |
| 536-538 | issues.append(Issue("WARN", "ivdw-vs-luse-vdw", "同时开了 IVDW（原子对/多体色散校正）和 LUSE_VDW（非局域 vdW-DF 泛函）：两者是不同路线，通常只选一种，确认不是叠加误设")) | — |
| 539 | if ivdw in (13, 14, 15): | 依赖外部编译包的方法 |
| 540-542 | issues.append(Issue("WARN", "ivdw-external-package", f"IVDW={ivdw:g} 依赖 VASP 编译时链接的外部包（DFT-D4 / libMBD / simple-D3）；先确认集群上的 VASP 编了它，否则运行时会停")) | 运行前能发现，比作业跑挂好 |
| 543 | if ivdw > 0: | 启用了任意一种校正 |
| 544-545 | issues.append(Issue("PASS", "ivdw-on", f"已启用色散校正 IVDW={ivdw:g}（{IVDW_NAMES.get(int(ivdw), '')}）")) | 即使取值非法（前面已报 ERROR），只要 >0 这里仍会加一条 PASS；`int(ivdw)` 用于查表，查不到给空串 |
| 546 | （空行） | — |
| 547 | if not [i for i in issues if i.level == "ERROR"]: | 前面没有任何 ERROR |
| 548 | issues.append(Issue("PASS", "incar-consistency", "INCAR 跨键检查通过")) | 注意：只要没有 ERROR 就加，不管有多少 WARN |
| 549 | return issues | — |

**为什么这么写 / 不这么写会怎样**：所有检查都做成 Issue 而不是异常，是为了「一次把问题说全」。工具的使用场景是「人审阅后去超算跑」，如果第一条就抛异常，用户得反复改、反复跑；收集全部 WARN/ERROR 才能一次看清。真正决定中止的地方只有 `prep.py:282`。

#### `_count_values(text: str) -> int`（行 561-562）

**干什么**：数一个空格/逗号分隔字符串里有多少个值，用于 MAGMOM 长度检查。

**参数**：`text` — 例如 `"0.6 0.6 -0.6"` 或 `"0.6,0.6"`。

**返回**：值个数（`int`）。空串返回 0。

**异常**：不抛。

**被谁调用**：`check_consistency:471`。

| 行 | 代码 | 说明 |
|---|---|---|
| 561 | def _count_values(text: str) -> int: | 入口 |
| 562 | return len(text.replace(",", " ").split()) | 逗号先换空格再 `split()`；连续空白不会产生空 token |

## 2. kpoints.py（189 行）

### 2.0 文件结构总览

| 行号 | 名称 | 类型 | 一句话 | 被谁调用 |
|---|---|---|---|---|
| 3-11 | 模块 docstring | 文档 | 说明为什么不用 pymatgen 的 Kpoints；提醒 KPOINTS 与 KSPACING 并存时 VASP 优先 KPOINTS 且不警告 | — |
| 13-17 | import | 导入 | `dataclass`、`field`（`field` 实际未使用）、`StructureError` | — |
| 20-37 | `Kpoints` | dataclass | 一个 KPOINTS 配置：mode + 各模式的参数 + 坐标系 + 注释 | `from_recipe` 返回、`prep.py:308`、`recipe.py:113` |
| 40-42 | `_fmt` | 函数 | 浮点格式化：8 位小数后去尾零、去尾点 | `render_kpoints:150,151,161` |
| 45-51 | `_vec` | 函数 | 校验「长度为 3 的列表/元组」并转 float | `from_recipe:89,90,114` |
| 54-125 | `from_recipe` | 函数 | 配方 `kpoints` 段（dict 或 None）→ `Kpoints` 或 `None` | `recipe.py:238`、`__init__.py:31` 再导出、测试 |
| 128-166 | `render_kpoints` | 函数 | `Kpoints` → VASP 格式的 KPOINTS 文本 | `prep.py:258`、测试 |
| 169-189 | `check_kpoints` | 函数 | KPOINTS 层面的轻量检查，返回 `[{level, code, message}]` | `prep.py:266-267`、测试 |

### 2.1 数据结构

#### `Kpoints`（行 20-37）

**是什么**：KPOINTS 的唯一内存表示。四种模式共用一个 dataclass，用 `mode` 区分该读哪组字段。

**为什么这么设计**：gamma/mp 只需要 `mesh`，line 需要 `segments`，explicit 需要 `points`；用可空字段而不是四个子类，是为了让 `render_kpoints` 在一处分支处理，且 `as_dict()` 能原样把配方结构返回给网页/JSON（`prep.py:308`）。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| mode | `str` | 无（必填） | `gamma` / `mp` / `line` / `explicit` |
| mesh | `list[int] \| None` | None | gamma/mp 的网格，如 `[4, 4, 1]` |
| segments | `list[dict] \| None` | None | line 模式的分段，元素形如 `{"from":[…],"to":[…],"points":N,"label_from":…,"label_to":…}` |
| points | `list[dict] \| None` | None | explicit 模式的点，元素形如 `{"coords":[x,y,z],"weight":1.0,"label":…}` |
| coord | `str` | `"Reciprocal"` | 倒空间/笛卡尔坐标；只在 line、explicit 渲染时写进文件 |
| comment | `str` | `"KPOINTS generated by vasp-prep"` | KPOINTS 第一行注释 |

`as_dict()`（29-37）把六个字段原样装进 dict，不复制列表（浅层返回），供 JSON 序列化。

### 2.2 逐行讲解

#### `_fmt(x: float) -> str`（行 40-42）

**干什么**：把 K 点坐标/权重格式化成小数文本，去掉末尾多余的 0。

**参数**：`x` — 一个能转 float 的值。

**返回**：字符串，如 `0.5`、`0.33333333`、`0`。

**异常**：`float(x)` 失败时抛 `ValueError`/`TypeError`，本函数不接。调用方传的是 `_vec` 转好的 float 或 weight float，所以实际不会触发。

**被谁调用**：`render_kpoints:150,151,161`。

| 行 | 代码 | 说明 |
|---|---|---|
| 40 | def _fmt(x: float) -> str: | 入口 |
| 41 | s = f"{float(x):.8f}".rstrip("0").rstrip(".") | 先固定 8 位小数（`0.5` → `0.50000000`），去掉末尾所有 0，再去掉可能留下的末尾小数点 |
| 42 | return s if s not in ("", "-0") else "0" | 极端情况兜底：`0` 会被前一步裁成 `""`，`-0.00000000` 裁成 `"-0"`，都归一成 `"0"` |

**为什么这么写 / 不这么写会怎样**：不用 `:g`（在 incar.py 里用的那种），因为 `:g` 只用 6 位有效数字，K 点坐标需要更高精度；也不用 `str(float)`，因为它会输出 `0.30000000000000004` 这类二进制浮点噪声。固定 8 位小数再裁零，是「足够精度 + 稳定文本」的折中。

#### `_vec(v, what: str) -> list[float]`（行 45-51）

**干什么**：校验一个「三维向量」：必须是长度为 3 的 list/tuple，且每个元素能转 float。

**参数**：
- `v` — 用户给的值。
- `what` — 出错信息里的定位文本，如 `"第 2 段 from"`。

**返回**：`[float, float, float]`。

**异常**：`StructureError`（退出码 4）：长度不是 3，或元素不是数字。

**被谁调用**：`from_recipe:89`（段起点）、`from_recipe:90`（段终点）、`from_recipe:114`（显式点坐标）。

| 行 | 代码 | 说明 |
|---|---|---|
| 45 | def _vec(v, what: str) -> list[float]: | 入口；`v` 没写类型，因为要先判断它是不是容器 |
| 46 | if not (isinstance(v, (list, tuple)) and len(v) == 3): | 必须是 list/tuple 且长度 3；字符串 `"0 0 0"` 也会被拒 |
| 47 | raise StructureError(f"{what} 需要 3 个数，收到 {v!r}") | 消息带定位，用户能知道是第几段/第几个点 |
| 48 | try: | — |
| 49 | return [float(x) for x in v] | 逐个转 float；`None`、字符串数字等都可转或在这里失败 |
| 50 | except (TypeError, ValueError) as exc: | — |
| 51 | raise StructureError(f"{what} 里有非数字：{v!r}") from exc | 保留异常链 |

#### `from_recipe(cfg: dict | None) -> Kpoints | None`（行 54-125）

**干什么**：把配方 JSON 里的 `kpoints` 段翻译成 `Kpoints`。这是整个文件的入口函数，负责所有输入校验：模式名、mesh、line 的分段、explicit 的点与权重、坐标系。

**参数**：`cfg` — 配方里的 `kpoints` 对象；`None` 表示不写 KPOINTS（由 INCAR 的 KSPACING 控制）。

**返回**：`Kpoints` 或 `None`。

**异常**：`StructureError`（退出码 4）。绝大多数消息都带「第几段/第几个点」的定位。

**被谁调用**：`recipe.py:238`（配方装配时）、`__init__.py:31` 以 `kpoints_from_recipe` 名字再导出、`tests/test_core.py:349-381`。

| 行 | 代码 | 说明 |
|---|---|---|
| 54 | def from_recipe(cfg: dict \| None) -> Kpoints \| None: | 入口 |
| 55 | （docstring） | 「None 表示不写 KPOINTS（用 KSPACING）」 |
| 56 | if cfg is None: | 配方没写 kpoints |
| 57 | return None | 调用方据此跳过渲染 |
| 58 | if not isinstance(cfg, dict): | 类型不对（列表/字符串等） |
| 59 | raise StructureError(f"kpoints 必须是对象或 null，收到 {type(cfg).__name__}") | — |
| 60 | mode = str(cfg.get("mode", "gamma")).lower() | 默认 gamma，统一小写；非字符串值会被 `str()` 后再判断，最终走「不支持的模式」 |
| 61 | coord = str(cfg.get("coord", "Reciprocal")) | 默认倒空间 |
| 62 | if coord.lower().startswith("c"): | 以 c 开头就认成笛卡尔（`Cartesian`、`cart`、`CART` 都行） |
| 63 | coord = "Cartesian" | 归一成标准写法 |
| 64 | else: | 其他所有写法 |
| 65 | coord = "Reciprocal" | 都当倒空间；注意这里**不会**报错，写错的 coord 会被静默归一 |
| 66 | comment = str(cfg.get("comment") or "KPOINTS generated by vasp-prep") | `or` 让空字符串也回退到默认注释 |
| 67 | （空行） | — |
| 68 | if mode in ("gamma", "mp"): | 均匀网格的两种模式 |
| 69 | mesh = cfg.get("mesh") or [1, 1, 1] | 没给或给空列表都用 `[1,1,1]`（Γ 点单点） |
| 70 | if not (isinstance(mesh, (list, tuple)) and len(mesh) == 3): | 必须是 3 个分量 |
| 71 | raise StructureError(f"{mode} 的 mesh 需要 3 个整数，收到 {mesh!r}") | — |
| 72 | m = [] | 校验后的网格 |
| 73 | for x in mesh: | 逐分量 |
| 74 | if isinstance(x, bool) or not isinstance(x, int) or x < 1: | 先拦 bool（它是 int 子类，`True` 会被当成 1），再要求整数且 ≥1；JSON 里写 `4.0` 会因不是 int 被拒 |
| 75 | raise StructureError(f"mesh 必须是正整数，收到 {x!r}") | — |
| 76 | if x > 64: | 上限保护 |
| 77 | raise StructureError(f"mesh 分量 {x} 太大（>64），检查是不是写错了") | 64 是经验上限；真正计算量提醒在 `check_kpoints:177-179` |
| 78 | m.append(x) | — |
| 79 | return Kpoints(mode=mode, mesh=m, coord=coord, comment=comment) | gamma/mp 到此结束 |
| 80 | （空行） | — |
| 81 | if mode == "line": | 能带路径模式 |
| 82 | segs = cfg.get("segments") or [] | 缺省为空列表，后面统一报错 |
| 83 | if not isinstance(segs, list) or not segs: | 必须是列表且至少 1 段 |
| 84 | raise StructureError("line 模式需要 segments 列表（至少 1 段）") | — |
| 85 | norm = [] | 归一后的分段 |
| 86 | for i, s in enumerate(segs, 1): | 从 1 开始编号，方便报错 |
| 87 | if not isinstance(s, dict): | 段必须是对象 |
| 88 | raise StructureError(f"第 {i} 段不是对象：{s!r}") | — |
| 89 | a = _vec(s.get("from"), f"第 {i} 段 from") | 起点坐标 |
| 90 | b = _vec(s.get("to"), f"第 {i} 段 to") | 终点坐标 |
| 91 | n = s.get("points", cfg.get("points_per_segment", 20)) | 段内点数：段自己的 `points` > 顶层 `points_per_segment` > 默认 20 |
| 92 | if isinstance(n, bool) or not isinstance(n, int) or n < 2: | 必须是不小于 2 的整数（VASP 的 Line-mode 至少 2 个点） |
| 93 | raise StructureError(f"第 {i} 段 points 必须是 ≥2 的整数，收到 {n!r}") | — |
| 94-98 | norm.append({ "from": a, "to": b, "points": n, "label_from": s.get("label_from") or "", "label_to": s.get("label_to") or "" }) | 标签缺失/空串都归一成 `""` |
| 99 | pts = sorted({s["points"] for s in norm}) | 所有段点数去重后排序 |
| 100 | if len(pts) > 1: | 各段点数不一致 |
| 101-103 | raise StructureError( f"line 模式要求所有段的 points 相同（VASP 只认一个值），当前有 {pts}" ) | **为什么**：VASP 的 Line-mode 只在第 2 行写一个总点数/段点数，无法为每段单独指定；不提前拦，写出来的文件会与实际意图不符 |
| 104 | return Kpoints(mode="line", segments=norm, coord=coord, comment=comment) | — |
| 105 | （空行） | — |
| 106 | if mode == "explicit": | 显式点列模式 |
| 107 | pts = cfg.get("points") or [] | 缺省为空列表，后面报错 |
| 108 | if not isinstance(pts, list) or not pts: | 必须是非空列表 |
| 109 | raise StructureError("explicit 模式需要 points 列表") | — |
| 110 | norm = [] | — |
| 111 | for i, p in enumerate(pts, 1): | 从 1 编号 |
| 112 | if not isinstance(p, dict): | 每个点必须是对象 |
| 113 | raise StructureError(f"第 {i} 个 K 点不是对象：{p!r}") | — |
| 114 | c = _vec(p.get("coords"), f"第 {i} 个 K 点 coords") | 坐标 |
| 115 | w = p.get("weight", 1.0) | 默认权重 1.0 |
| 116 | try: | — |
| 117 | w = float(w) | 接受数字或数字字符串；`True` 也会变成 1.0（这里没单独拦 bool） |
| 118 | except (TypeError, ValueError) as exc: | — |
| 119 | raise StructureError(f"第 {i} 个 K 点 weight 不是数字") from exc | — |
| 120 | if w < 0: | 权重为负没有物理意义 |
| 121 | raise StructureError("K 点权重不能为负") | 0 允许 |
| 122 | norm.append({"coords": c, "weight": w, "label": p.get("label") or ""}) | 标签归一 |
| 123 | return Kpoints(mode="explicit", points=norm, coord=coord, comment=comment) | — |
| 124 | （空行） | — |
| 125 | raise StructureError(f"不支持的 kpoints.mode={mode!r}（可用 gamma / mp / line / explicit）") | 兜底：模式名拼错或没实现 |

**为什么这么写 / 不这么写会怎样**：输入校验全部集中在 `from_recipe`，`render_kpoints` 因此可以假设数据结构已经合法、只做格式化。这样「配方阶段报错」和「写文件阶段报错」被清晰分开：前者给用户改配方，后者只可能是内部 bug（最后一行 `raise StructureError("内部错误…")`）。

#### `render_kpoints(kp: Kpoints) -> str`（行 128-166）

**干什么**：按 VASP 官方 KPOINTS 格式把 `Kpoints` 写成文本。

**参数**：`kp` — `from_recipe` 产出的对象（或调用方手工构造、但字段完整）。

**返回**：完整文本，末尾一个 `\n`。第一行始终是 `kp.comment`。

**异常**：`StructureError`（只在 `mode` 未知识别时抛，属于内部错误）；`assert` 失败会抛 `AssertionError`（`mesh`/`segments`/`points` 与 `mode` 不匹配时，属于调用方 bug）。

**被谁调用**：`prep.py:258`、`tests/test_core.py:350-366`。

| 行 | 代码 | 说明 |
|---|---|---|
| 128 | def render_kpoints(kp: Kpoints) -> str: | 入口 |
| 129 | （docstring） | 「按 VASP 官方格式写 KPOINTS 文本」 |
| 130 | lines: list[str] = [kp.comment] | 第 1 行：注释 |
| 131 | （空行） | — |
| 132 | if kp.mode in ("gamma", "mp"): | 网格模式 |
| 133 | assert kp.mesh is not None | 内部不变量：走到这里就一定有 mesh |
| 134 | lines.append("0") | 第 2 行：0 = 由 VASP 按网格自动生成 K 点 |
| 135 | lines.append("Gamma" if kp.mode == "gamma" else "Monkhorst-Pack") | 第 3 行：生成方式 |
| 136 | lines.append("  " + "  ".join(str(x) for x in kp.mesh)) | 第 4 行：网格，如 `  4  4  1`（行首两个空格） |
| 137 | lines.append("  0  0  0") | 第 5 行：整体平移，0 表示不平移 |
| 138 | return "\n".join(lines) + "\n" | 拼成文本 |
| 139 | （空行） | — |
| 140 | if kp.mode == "line": | 能带路径模式 |
| 141 | assert kp.segments is not None | — |
| 142 | points = kp.segments[0]["points"] | 取第一段的点数；`from_recipe:99-103` 已保证各段相同，所以这里只取一个 |
| 143 | lines.append(str(points)) | 第 2 行：每段点数 |
| 144 | lines.append("Line-mode") | 第 3 行：固定写法 |
| 145 | lines.append(kp.coord) | 第 4 行：坐标系（Reciprocal/Cartesian） |
| 146 | for seg in kp.segments: | 每段写两行（起点、终点） |
| 147 | （注释） | 相邻段共享的端点重复出现，VASP 会按顺序连起来 |
| 148 | a, b = seg["from"], seg["to"] | — |
| 149 | la, lb = seg["label_from"], seg["label_to"] | — |
| 150 | lines.append("  " + "  ".join(_fmt(x) for x in a) + (f"   ! {la}" if la else "")) | 起点；有标签时加 `   ! 标签`（VASP 的注释写法） |
| 151 | lines.append("  " + "  ".join(_fmt(x) for x in b) + (f"   ! {lb}" if lb else "")) | 终点，同上 |
| 152 | return "\n".join(lines) + "\n" | — |
| 153 | （空行） | — |
| 154 | if kp.mode == "explicit": | 显式点列 |
| 155 | assert kp.points is not None | — |
| 156 | lines.append(str(len(kp.points))) | 第 2 行：点的个数 |
| 157 | lines.append(kp.coord) | 第 3 行：坐标系 |
| 158 | for p in kp.points: | 每个点一行 |
| 159 | c = p["coords"] | — |
| 160-163 | lines.append( "  " + "  ".join(_fmt(x) for x in c) + f"   {_fmt(p['weight'])}" + (f"   ! {p['label']}" if p["label"] else "") ) | 坐标 + 权重 + 可选标签 |
| 164 | return "\n".join(lines) + "\n" | — |
| 165 | （空行） | — |
| 166 | raise StructureError(f"内部错误：未知 mode {kp.mode}") | 只有手工构造出非法 `mode` 才会到这里 |

**为什么这么写 / 不这么写会怎样**：`assert` 只用于「调用方保证的不变量」，不用于校验用户输入——用户输入的问题要在 `from_recipe` 里以 `StructureError` 报出来，带可读消息；`assert` 失败是程序员错误。注意 `python -O` 会移除 assert，所以这些行不能承担业务校验职责。

#### `check_kpoints(kp: Kpoints | None, *, lattice=None, is_slab=None) -> list[dict]`（行 169-189）

**干什么**：KPOINTS 的轻量检查，返回问题列表。只做「能一眼看出的可疑设置」：网格太大、网格 <1、line 段点数 <2、表面体系 z 方向 K 点不是 1。

**参数**：
- `kp` — `Kpoints` 或 `None`（None 表示不写 KPOINTS，直接返回空列表）。
- `lattice` — 晶格矩阵；**当前函数体没有使用这个参数**，签名里保留它是给未来「按晶格长度估算 K 点密度」预留的位置。
- `is_slab` — 是否表面体系（`prep.py:265` 用「配方里有 `vacuum` 操作」判断）。

**返回**：`list[dict]`，每条形如 `{"level": ..., "code": ..., "message": ...}`。与 incar 的 `Issue` 不同，这里是普通 dict，`prep.py:266` 用 `_issue_dicts` 统一两种形式。

**异常**：不抛。`assert` 失败是内部不变量问题。

**被谁调用**：`prep.py:266-267`、`tests/test_core.py:380-381`。

| 行 | 代码 | 说明 |
|---|---|---|
| 169-170 | def check_kpoints(kp: Kpoints \| None, *, lattice: list[list[float]] \| None = None, is_slab: bool \| None = None) -> list[dict]: | 签名两行；`lattice`/`is_slab` 是 keyword-only |
| 171 | （docstring） | 「KPOINTS 层面的轻量检查」 |
| 172 | out: list[dict] = [] | 结果容器 |
| 173 | if kp is None: | 不写 KPOINTS |
| 174 | return out | 返回空列表（用 KSPACING 的情况不检查） |
| 175 | if kp.mode in ("gamma", "mp"): | 网格模式 |
| 176 | assert kp.mesh is not None | — |
| 177 | if max(kp.mesh) >= 30: | 任一方向 ≥30 |
| 178-179 | out.append({"level": "WARN", "code": "kmesh-large", "message": f"K 网格 {kp.mesh} 很大，计算量可能爆炸；确认是否必要"}) | WARN 不是 ERROR：大网格有时确实需要 |
| 180 | if min(kp.mesh) < 1: | 有分量 <1 |
| 181 | out.append({"level": "ERROR", "code": "kmesh-invalid", "message": "K 网格必须 ≥1"}) | 正常路径下 `from_recipe:74` 已拦住，只有手工构造 `Kpoints` 才会到这里 |
| 182 | if kp.mode == "line": | line 模式 |
| 183 | assert kp.segments is not None | — |
| 184 | if any(s["points"] < 2 for s in kp.segments): | 任一段点数不足 2 |
| 185 | out.append({"level": "ERROR", "code": "line-points", "message": "line 模式每段至少 2 个点"}) | 同样的「正常路径已拦」检查，作为防御 |
| 186 | if is_slab and kp.mode in ("gamma", "mp") and kp.mesh and kp.mesh[2] != 1: | 表面体系、网格模式、且 z 方向不是 1 |
| 187-188 | out.append({"level": "WARN", "code": "slab-kz", "message": f"表面体系通常 z 方向只取 1 个 K 点，当前 mesh={kp.mesh}"}) | 真空层方向不需要 K 点采样，多了纯浪费 |
| 189 | return out | — |

**为什么这么写 / 不这么写会怎样**：这里只做 WARN/ERROR 提示，不自动改网格——K 点密度是科学选择，工具不应替用户决定。`lattice` 参数未使用是可以观察到的事实：如果将来要按晶格常数自动建议网格，会在这里接入，但现在没有。

