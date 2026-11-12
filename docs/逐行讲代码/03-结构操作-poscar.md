# 03 POSCAR 读写与结构操作逐行讲解

> 阅读约定：
> 1. 行号就是 `vasp_core/poscar.py` 的真实行号；表格里的代码是源码去掉行首缩进后的原文。表格中的 `\|` 是 Markdown 的转义写法，源码本身是 `|`（例如类型注解 `str \| Path`）。
> 2. 空行没有行为，不单独列表；连续多行注释、多行 docstring 会合并成一行，行号写成 `起–止`。
> 3. "异常"一律指本函数主动 `raise` 的异常；参数形状不对导致的 `IndexError` 只在有调用价值时提一句。

## 0. 这个文件为什么存在

- 不用 ASE/pymatgen：目标机器是 RHEL6.5 + glibc2.12 且无外网（`poscar.py:4-10` 的模块注释），装 pymatgen/ASE 要几十个包，不少轮子还要求 glibc≥2.17；本文件只用标准库，保证"拷过去就能读/写 POSCAR"。
- 坐标约定全文件只有一套：`lattice` 是 3 行晶格矢量（a、b、c 各一行），分数坐标 f 与笛卡尔坐标 r 满足 `r = f @ lattice`、`f = r @ inv(lattice)`（`poscar.py:12-15`）。
- 读要"无损"：`scale_raw`/`lattice_raw`/`coord_mode`/`flags`/`velocities`/`tail` 都按原样保留，避免"读进来再写出去，选择性弛豫和速度就没了"（`poscar.py:126-144`）。
- 结构操作全部是纯函数：返回新 `Structure`，不修改入参（`poscar.py:503-505`）；配方多步操作中途报错时，原结构仍然完好（`recipe.py:268-275`）。

## 1. 文件结构总览

| 行号 | 名称 | 类型 | 一句话 | 被谁调用 |
| --- | --- | --- | --- | --- |
| 1–20 | 模块 docstring | 注释 | 记录"为什么不用 ASE/pymatgen"和坐标约定 | 人不调用 |
| 22–29 | import | 导入 | math、re、dataclass、Path、StructureError | 全文件 |
| 31–34 | `_ELEMENT_RE` | 模块常量 | 元素符号粗校验 `^[A-Z][a-z]?$` | read_poscar_text:286、op_substitute:634、op_substitute_all:658、op_add_atom:698 |
| 41–47 | `det3` | 函数 | 3x3 行列式 | inv3:52、scale_factor:163、volume:176 |
| 50–71 | `inv3` | 函数 | 3x3 求逆（伴随矩阵法） | frac:191、op_add_atom:709、op_translate:872 |
| 74–76 | `mat_mul` | 函数 | 3x3 乘 3x3 | 本文件及全项目未发现调用（截至本次核对） |
| 79–80 | `mat_T` | 函数 | 3x3 转置 | 同上，未发现调用 |
| 83–85 | `vec_mat` | 函数 | 行向量右乘 3x3 | frac:192、cart:197、set_frac:213、write_poscar_text:475、op_add_atom:709、op_translate:872 |
| 88–89 | `vec_add` | 函数 | 3 维向量相加 | op_vacuum:800、op_translate:885 |
| 92–93 | `vec_scale` | 函数 | 3 维向量乘标量 | op_vacuum:774、op_vacuum:790、op_vacuum:793、op_vacuum:800 |
| 96–97 | `vec_len` | 函数 | 3 维向量长度 | op_vacuum:771、op_vacuum:793、op_vacuum:812 |
| 100–109 | `_fmt` | 函数 | 统一数字格式（10 位小数、去尾零、去 -0） | write_poscar_text:460、write_poscar_text:476、write_poscar_text:489、op_add_atom:718、op_strain:750 |
| 112–117 | `_is_int_token` | 函数 | 判断 token 能否转成 int | read_poscar_text:279 |
| 124–242 | `Structure` | 类 | POSCAR 的内存表示 | read_poscar_text、全部 op_*、pmg_backend、prep/report/service/tools |
| 249–414 | `read_poscar_text` | 函数 | POSCAR 文本 → Structure（核心解析） | read_poscar:439；测试与 tools/fuzz_poscar.py 直接调 |
| 417–422 | `_is_float_token` | 函数 | 判断 token 能否转成 float | read_poscar_text:367、read_poscar_text:372、read_poscar_text:386 |
| 425–439 | `read_poscar` | 函数 | POSCAR 文件 → Structure（处理 BOM/GBK/CRLF） | prep.py:163、prep.py:396、service.py:109、tools.py:514 |
| 446–494 | `write_poscar_text` | 函数 | Structure → POSCAR 文本 | prep.py:327、tests/test_core.py、tools/fuzz_poscar.py:56 |
| 497–500 | `write_poscar` | 函数 | POSCAR 文本 → 文件 | vasp_core/__init__.py:33 导出；本仓库内未发现其他调用 |
| 507–529 | `_check_indices` | 函数 | 1 基索引 → 0 基并做范围检查 | op_substitute:636、op_vacancy:679、op_fix_atoms:829 |
| 532–564 | `_regroup` | 函数 | 按首次出现顺序重排元素、坐标、flags、速度 | read_poscar_text:413、各 op_* 内部、backends/pmg_backend.py:88 |
| 567–629 | `op_supercell` | 函数 | 扩胞 | recipe.py:280、tools/fuzz_poscar.py:97、tests/test_core.py |
| 632–645 | `op_substitute` | 函数 | 替换单个原子 | recipe.py:282 |
| 648–674 | `op_substitute_all` | 函数 | 按元素批量替换 | recipe.py:284 |
| 677–691 | `op_vacancy` | 函数 | 删除原子造空位 | recipe.py:289 |
| 694–719 | `op_add_atom` | 函数 | 加一个原子 | recipe.py:291 |
| 722–750 | `op_strain` | 函数 | 加应变/形变 | recipe.py:296 |
| 753–812 | `op_vacuum` | 函数 | 沿某个晶格矢量加真空层 | recipe.py:298 |
| 815–845 | `op_fix_atoms` | 函数 | 固定/放开原子的某些方向 | recipe.py:304 |
| 848–862 | `op_reorder_species` | 函数 | 按指定元素顺序重排原子 | recipe.py:309 |
| 865–886 | `op_translate` | 函数 | 整体平移 | recipe.py:311 |
| 889–900 | `op_wrap` | 函数 | 分数坐标折回 [0,1) | recipe.py:313 |
| 903–906 | 解析流程注释 | 注释 | 文件末尾的 4 行速查表 | 人读 |

> 数量核对：本文件实际定义了 **11 个 `op_*` 函数**（AST 与 `grep '^def op_'` 均为 11）。`ops_ext.py:4` 的注释和任务说明里写的是"12 个 op"，与当前源码不一致；本文按源码实际数量讲，不虚构第 12 个函数。`recipe.py:81-91` 注册的内置 op 也正好是这 11 个（另有 `EXT_OPS` 里的高阶操作，不在本文件）。

## 2. 小工具（det3/inv3/mat_mul/mat_T/vec_mat/vec_add/vec_scale/vec_len/_fmt/_is_int_token）

这些函数都只做 3x3 矩阵 / 3 维向量的算术，**不检查输入形状**：形状不对会在下标处抛 `IndexError`。调用它们的都是本文件内部代码，形状由调用方保证。

### 2.1 det3（41–47）

- 干什么：算 3x3 矩阵的行列式。
- 参数：`m: list[list[float]]`，3 行 3 列。
- 返回：`float`。
- 异常：不主动抛；`m` 的行/列不足 3 时会在下标处抛 `IndexError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 41 | def det3(m: list[list[float]]) -> float: | 定义函数，接收 3x3 嵌套列表，返回浮点数 |
| 42 | """3x3 行列式。用于负 scale（按体积定义晶格）与体积检查。""" | docstring：点明两个调用场景 |
| 43 | return ( | 返回值是一条跨 4 行写的表达式，左括号让 44–46 行继续 |
| 44 | m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) | 按第一行展开的第一项：a00 乘其 2x2 余子式 |
| 45 | - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) | 第二项，代数余子式带负号 |
| 46 | + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]) | 第三项，带正号 |
| 47 | ) | 结束跨行表达式（det3 的返回值到此闭合） |

### 2.2 inv3（50–71）

- 干什么：用伴随矩阵法求 3x3 矩阵的逆。
- 参数：`m: list[list[float]]`，3 行 3 列。
- 返回：`list[list[float]]`，3 行 3 列，满足 `m @ inv3(m) = I`（在浮点误差内）。
- 异常：`StructureError` —— 当 `abs(det) < 1e-12`，即三个晶格矢量共面、矩阵不可逆。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 50 | def inv3(m: list[list[float]]) -> list[list[float]]: | 定义函数 |
| 51 | """3x3 求逆（伴随矩阵法）。体积为 0 说明三个矢量共面，POSCAR 本身就坏了。""" | docstring：说明算法和失败含义 |
| 52 | d = det3(m) | 先算行列式；后面每个元素都要除以它 |
| 53 | if abs(d) < 1e-12: | 浮点比较用绝对容差；为什么不用 `d == 0`：浮点矩阵的行列式很少恰好是 0 |
| 54 | raise StructureError("晶格矩阵行列式为 0（三个晶格矢量共面），无法求逆") | 走这里说明三个晶格矢量线性相关，结构没有体积 |
| 55 | return [ | 返回 3x3 逆矩阵，外层列表的每一项是一行 |
| 56 | [ | 逆矩阵第 0 行的开始 |
| 57 | (m[1][1] * m[2][2] - m[1][2] * m[2][1]) / d, | 逆矩阵 [0][0]：去掉第 0 行第 0 列的余子式除以 d |
| 58 | (m[0][2] * m[2][1] - m[0][1] * m[2][2]) / d, | 逆矩阵 [0][1]：对应余子式，注意下标顺序是转置过的 |
| 59 | (m[0][1] * m[1][2] - m[0][2] * m[1][1]) / d, | 逆矩阵 [0][2] |
| 60 | ], | 第 0 行结束 |
| 61 | [ | 逆矩阵第 1 行的开始 |
| 62 | (m[1][2] * m[2][0] - m[1][0] * m[2][2]) / d, | 逆矩阵 [1][0] |
| 63 | (m[0][0] * m[2][2] - m[0][2] * m[2][0]) / d, | 逆矩阵 [1][1] |
| 64 | (m[0][2] * m[1][0] - m[0][0] * m[1][2]) / d, | 逆矩阵 [1][2] |
| 65 | ], | 第 1 行结束 |
| 66 | [ | 逆矩阵第 2 行的开始 |
| 67 | (m[1][0] * m[2][1] - m[1][1] * m[2][0]) / d, | 逆矩阵 [2][0] |
| 68 | (m[0][1] * m[2][0] - m[0][0] * m[2][1]) / d, | 逆矩阵 [2][1] |
| 69 | (m[0][0] * m[1][1] - m[0][1] * m[1][0]) / d, | 逆矩阵 [2][2] |
| 70 | ], | 第 2 行结束 |
| 71 | ] | 整个返回值结束 |

### 2.3 mat_mul（74–76）

- 干什么：两个 3x3 矩阵相乘。
- 参数：`a`、`b` 都是 3x3。
- 返回：3x3，`c[i][j] = Σ_k a[i][k] * b[k][j]`。
- 异常：无主动异常；形状不对会 `IndexError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 74 | def mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]: | 定义函数 |
| 75 | """3x3 乘 3x3。行向量约定下，c[i][j] = sum_k a[i][k] * b[k][j]。""" | docstring：明确乘的方向 |
| 76 | return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)] | 两层列表推导 + 内层 `sum` 完成逐元素点积；本文件内没有被调用 |

### 2.4 mat_T（79–80）

- 干什么：3x3 转置。
- 参数：`a`，3x3。
- 返回：3x3，`out[i][j] = a[j][i]`。
- 异常：无主动异常；形状不对会 `IndexError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 79 | def mat_T(a: list[list[float]]) -> list[list[float]]: | 定义函数 |
| 80 | return [[a[j][i] for j in range(3)] for i in range(3)] | 行列下标对调即转置；本文件内没有被调用 |

### 2.5 vec_mat（83–85）

- 干什么：**行向量** `v` 右乘矩阵 `m`，算 `v @ m`。
- 参数：`v` 是长度 3 的向量；`m` 是 3x3。
- 返回：长度 3 的向量，`out[j] = Σ_i v[i] * m[i][j]`。
- 异常：无主动异常；长度不对会 `IndexError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 83 | def vec_mat(v: list[float], m: list[list[float]]) -> list[float]: | 定义函数 |
| 84 | """行向量 v 右乘矩阵 m：结果[j] = sum_i v[i] * m[i][j]。""" | docstring：固定"行向量在左"的约定 |
| 85 | return [sum(v[i] * m[i][j] for i in range(3)) for j in range(3)] | 外层循环列 j、内层循环行 i，正好是 `r = f @ lattice` 的算法 |

### 2.6 vec_add（88–89）

- 干什么：两个 3 维向量逐分量相加。
- 参数：`a`、`b` 长度 3。
- 返回：长度 3。
- 异常：无主动异常；长度不对会 `IndexError`（按下标 0..2 取）。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 88 | def vec_add(a: list[float], b: list[float]) -> list[float]: | 定义函数 |
| 89 | return [a[i] + b[i] for i in range(3)] | 逐分量相加 |

### 2.7 vec_scale（92–93）

- 干什么：3 维向量乘一个标量。
- 参数：`a` 长度 3；`s` 标量。
- 返回：长度 3。
- 异常：无主动异常。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 92 | def vec_scale(a: list[float], s: float) -> list[float]: | 定义函数 |
| 93 | return [x * s for x in a] | 逐分量乘标量；不要求 len(a)==3，实际按输入长度返回 |

### 2.8 vec_len（96–97）

- 干什么：3 维向量的欧氏长度（模）。
- 参数：`a` 长度 3。
- 返回：`float`，`sqrt(Σ x²)`。
- 异常：无主动异常。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 96 | def vec_len(a: list[float]) -> float: | 定义函数 |
| 97 | return math.sqrt(sum(x * x for x in a)) | 平方和开根号；不要求 len(a)==3 |

### 2.9 _fmt（100–109）

- 干什么：把浮点数转成稳定的 POSCAR 文本格式：10 位小数、去掉尾零、去掉 `-0`。
- 参数：`x: float`。
- 返回：`str`，例如 `0.25`、`0`、`1.2345678901`。
- 异常：无主动异常；传入非数字类型时格式化会抛 `TypeError`/`ValueError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 100 | def _fmt(x: float) -> str: | 定义函数 |
| 101–105 | """统一数字格式：10 位小数，去掉 -0.0000000000。…""" | docstring 解释为什么不用 `str(float)`（会写出 `0.30000000000000004` 这类丑数，diff 不稳定） |
| 106 | if abs(x) < 5e-11: | 绝对值小于半个末位（10 位小数的 0.5e-10）时按 0 处理 |
| 107 | x = 0.0 | 把极小值归零，避免写出 `-0.0000000000` 或 `1e-11` 这类噪声 |
| 108 | s = f"{x:.10f}".rstrip("0").rstrip(".") | 先固定 10 位小数，再去掉尾部 0，再去掉可能剩下的小数点 |
| 109 | return s if s not in ("", "-0") else "0" | 兜底：格式化后为空串或 `-0` 时统一返回 `0` |

### 2.10 _is_int_token（112–117）

- 干什么：判断一个字符串 token 能否被 `int()` 解析（用于识别 VASP4 的第 6 行）。
- 参数：`tok: str`（调用方保证是 `split()` 出来的字符串）。
- 返回：`bool`。
- 异常：不主动抛；`int()` 抛 `ValueError` 时返回 `False`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 112 | def _is_int_token(tok: str) -> bool: | 定义函数 |
| 113 | try: | 用"试着解析"代替正则匹配，写法短且接受 `+3`、`-3` 这类写法 |
| 114 | int(tok) | 只尝试解析，不保存结果；能解析就说明是整数字符串 |
| 115 | return True | 解析成功返回 True |
| 116 | except ValueError: | 只有 `ValueError` 被捕获；`int("3.0")` 也走这里返回 False |
| 117 | return False | 解析失败返回 False |

## 3. Structure 类

`Structure` 是"POSCAR 文件的内存镜像"：字段尽量贴文件格式，而不是先归一化成物理量，这样读进来再写出去时能保持原样（`poscar.py:126-132`）。

### 3.0 字段（134–144）

| 行 | 字段 | 类型 / 默认值 | 含义 |
| --- | --- | --- | --- |
| 134 | `comment` | `str = ""` | POSCAR 第 1 行注释 |
| 135 | `scale_raw` | `float = 1.0` | 第 2 行原始 scale：>0 是长度因子，<0 是按体积定义 |
| 136 | `lattice_raw` | `list[list[float]] = field(default_factory=list)` | 3x3 晶格矢量，**未乘 scale** |
| 137 | `species` | `list[str] \| None = None` | VASP5 的元素行；VASP4 为 `None` |
| 138 | `counts` | `list[int]` | 每个元素的原子数，与 `species` 一一对应 |
| 139 | `coords` | `list[list[float]]` | 坐标，保持原文件模式（direct 或 cartesian）的数值 |
| 140 | `coord_mode` | `str = "direct"` | `"direct"` 或 `"cartesian"` |
| 141 | `selective` | `bool = False` | 文件里是否有 Selective dynamics |
| 142 | `flags` | `list[list[bool]] \| None = None` | 每原子 3 个 T/F（仅 selective 时）；`True`=T=可动 |
| 143 | `velocities` | `list[list[float]] \| None = None` | 速度块（Å/fs），原样保留 |
| 144 | `tail` | `list[str]` | 速度块之后"不像数字"的尾部行，原样保留 |

`field(default_factory=list)` 是 `@dataclass` 的写法：每个实例各自拿一个新列表，避免多个实例共享同一个列表对象。

### 3.1 natoms（148–150）

- 干什么：所有元素原子数之和。
- 参数：无（property，写成 `st.natoms`，不加括号）。
- 返回：`int`。
- 异常：无主动异常。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 148 | @property | 装饰器：把下面的方法暴露成属性，调用方写 `st.natoms` |
| 149 | def natoms(self) -> int: | 方法定义；因为挂了 `@property`，调用时不带括号 |
| 150 | return sum(self.counts) | 每次现算，不缓存——结构对象的 `counts` 很少改，缓存反而要维护失效 |

### 3.2 scale_factor（152–167）

- 干什么：把 `lattice_raw` 变成 Å 的缩放因子。
- 参数：无（property）。
- 返回：`float`，正数。
- 异常：`StructureError` —— `scale_raw == 0`；或 `scale_raw < 0` 且原始晶格体积为 0。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 152 | @property | 属性装饰器 |
| 153 | def scale_factor(self) -> float: | 方法定义 |
| 154–159 | """把 lattice_raw 变成 Å 的因子。…""" | docstring：说明 VASP 规则：scale>0 直接乘；scale<0 时 abs(scale) 是目标体积，因子为 (目标体积/当前体积)^(1/3)；并提示直接拿负 scale 乘会得到负晶格 |
| 160 | if self.scale_raw > 0: | 分支一：正 scale——最常见的情况 |
| 161 | return self.scale_raw | 正 scale 本身就是因子 |
| 162 | if self.scale_raw < 0: | 分支二：负 scale——第二行给的是"想要的体积"而不是长度因子 |
| 163 | v0 = abs(det3(self.lattice_raw)) | 原始晶格体积（未乘 scale） |
| 164 | if v0 <= 0: | 原始晶格本身没有体积（三矢量共面） |
| 165 | raise StructureError("负 scale（按体积定义）但原始晶格体积为 0") | 这时无法按体积反推比例尺，只能报错 |
| 166 | return (abs(self.scale_raw) / v0) ** (1.0 / 3.0) | 因子 s 满足 det(s·raw) = s³·det(raw) = 目标体积，所以 s 取目标体积与原始体积之比的 1/3 次方 |
| 167 | raise StructureError("POSCAR 第二行的 scale 不能为 0") | 走到这里只剩 scale==0：既不是长度因子也不是体积，POSCAR 非 VASP 合法输入 |

### 3.3 lattice（169–173）

- 干什么：返回乘过 scale 的物理晶格（Å）。
- 参数：无（property）。
- 返回：3x3 的新列表。
- 异常：经由 `scale_factor` 抛出（见 3.2）。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 169 | @property | 属性装饰器 |
| 170 | def lattice(self) -> list[list[float]]: | 方法定义 |
| 171 | """物理晶格（Å），行向量。""" | docstring：强调行向量约定 |
| 172 | s = self.scale_factor | 先按 scale 的符号/大小算出因子 |
| 173 | return [[s * x for x in row] for row in self.lattice_raw] | 逐元素乘因子；返回新列表，不修改 `lattice_raw` |

### 3.4 volume（175–176）

- 干什么：晶胞体积（Å³）。
- 参数：无。
- 返回：`float`，非负。
- 异常：无主动异常（`det3` 形状不对会 `IndexError`）。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 175 | def volume(self) -> float: | 方法定义（这是普通方法，调用要写 `st.volume()`） |
| 176 | return abs(det3(self.lattice)) | 取绝对值：左手晶格行列式为负，但体积仍是正的 |

### 3.5 frac（180–192）

- 干什么：返回所有原子的分数坐标。
- 参数：无。
- 返回：`list[list[float]]`，每个原子 3 个数。
- 异常：Cartesian 模式下经 `inv3` 抛 `StructureError`（`lattice_raw` 行列式为 0）。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 180 | def frac(self) -> list[list[float]]: | 普通方法（不是 property）：调用方都写 `st.frac()` |
| 181–188 | """所有原子的分数坐标。…""" | docstring：Direct 模式原值就是分数坐标；Cartesian 模式下 r = scale·coord（Å），f = r @ inv(lattice)，而 inv(scale·raw) = inv(raw)/scale，scale 会约掉，所以直接用 `coord @ inv(raw)`；并提示 v1 写法 `coord @ inv(scale*raw)` 会让坐标整体缩小 scale 倍 |
| 189 | if self.coord_mode == "direct": | 分支一：Direct 模式 |
| 190 | return [list(c) for c in self.coords] | 直接复制坐标返回；`list(c)` 防止调用方改到内部列表 |
| 191 | invr = inv3(self.lattice_raw) | 分支二：Cartesian 模式。注意用的是 **lattice_raw**（未乘 scale），由 184–187 行的推导可知 scale 会约掉 |
| 192 | return [vec_mat(c, invr) for c in self.coords] | 对每个坐标做 `coord @ inv(raw)`，得到分数坐标 |

**为什么用 raw 而不是 lattice**：设文件里 Cartesian 坐标值为 `c`，则物理坐标 `r = scale·c`；`f = r @ inv(scale·raw) = (scale·c) @ (inv(raw)/scale) = c @ inv(raw)`。两条 scale 一约，用 `lattice_raw` 反而是对的，而且对任何 scale 都成立。

### 3.6 cart（194–197）

- 干什么：返回所有原子的笛卡尔坐标（Å）。
- 参数：无。
- 返回：`list[list[float]]`。
- 异常：经由 `frac()` / `inv3` 抛出。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 194 | def cart(self) -> list[list[float]]: | 普通方法 |
| 195 | """所有原子的笛卡尔坐标（Å）。""" | docstring |
| 196 | L = self.lattice | 先取物理晶格（已乘 scale） |
| 197 | return [vec_mat(f, L) for f in self.frac()] | r = f @ lattice；先转分数坐标再乘，任何 coord_mode 都统一 |

### 3.7 set_frac（199–213）

- 干什么：用分数坐标整体替换所有原子位置，并按当前 `coord_mode` 写回 `coords`。
- 参数：`fracs: list[list[float]]`，长度必须等于 `sum(counts)`。
- 返回：`None`（原地修改 `self.coords`）。
- 异常：`StructureError` —— 长度与 `counts` 之和不一致；Cartesian 模式下 `inv3` 失败也会抛。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 199 | def set_frac(self, fracs: list[list[float]]) -> None: | 普通方法，原地改对象 |
| 200–204 | """用分数坐标替换全部原子位置；按当前 coord_mode 转成文件里的数值。…""" | docstring：强调长度校验的原因——VASP 对"坐标数与元素行对不上"**不报错**，只会算错体系 |
| 205 | if len(fracs) != sum(self.counts): | 先校验，再改数据；这是本类最重要的不变量 |
| 206–208 | raise StructureError(… f"坐标数量 {len(fracs)} 与元素计数 {self.counts}（共 {sum(self.counts)} 个原子）不一致" ) | 长度不一致直接拒绝，错误信息带上两个数量，方便定位是哪个 op 写岔了 |
| 209 | if self.coord_mode == "direct": | Direct 模式：分数坐标就是文件里的数值 |
| 210 | self.coords = [list(f) for f in fracs] | 复制后保存，避免外部列表和内部状态共享 |
| 211 | else: | Cartesian 模式 |
| 212 | L = self.lattice | 取物理晶格（已乘 scale） |
| 213 | self.coords = [vec_mat(f, L) for f in fracs] | 存 `f @ lattice`（物理 Å）。本文件所有 op_* 新建的 Structure 都是 `scale_raw=1.0`，所以"物理 Å"与"文件里该写的数"相同；对 `scale_raw≠1` 的对象直接调本方法不在本文件的使用范围内 |

### 3.8 atom_species（217–227）

- 干什么：把"元素 + 计数"展开成逐原子元素列表，如 `["Mo","S"]`、`[2,4]` → `["Mo","Mo","S","S","S","S"]`。
- 参数：无。
- 返回：`list[str]`，长度等于 `natoms`。
- 异常：`StructureError` —— VASP4 格式（`species is None`）时无法知道每个原子是什么元素。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 217 | def atom_species(self) -> list[str]: | 普通方法 |
| 218 | """展开成逐原子元素列表；VASP4（无元素行）时抛错，让调用方给 --elements。""" | docstring：说明报错不是 bug，而是要求调用方补元素信息 |
| 219 | if self.species is None: | VASP4 的 Structure 没有元素行 |
| 220–223 | raise StructureError( "这是 VASP4 格式的 POSCAR（没有元素行），无法知道每个原子是什么元素。\n" "请先用 VASP5 格式重写，或在配方里用 elements 指定元素顺序。" ) | 错误信息给出两条出路：转 VASP5，或在配方里显式给 elements |
| 224 | out: list[str] = [] | 结果累加器，显式标类型方便阅读 |
| 225 | for sym, n in zip(self.species, self.counts): | 元素行与计数行一一配对；`read_poscar_text:294` 已校验两者等长 |
| 226 | out.extend([sym] * n) | 把 `sym` 重复 `n` 次追加进去 |
| 227 | return out | 返回逐原子元素列表 |

### 3.9 has_species（229–230）

- 干什么：判断是否 VASP5（有元素行）。
- 参数：无。
- 返回：`bool`。
- 异常：无主动异常。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 229 | def has_species(self) -> bool: | 普通方法 |
| 230 | return self.species is not None | 有元素行就是 VASP5；本仓库内未发现对本方法的调用（导出给外部/后续用） |

### 3.10 summary（232–242）

- 干什么：生成一份给工具层/日志用的摘要字典。
- 参数：无。
- 返回：`dict`，8 个键。
- 异常：无主动异常（`volume()` 可能经 `scale_factor` 抛）。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 232 | def summary(self) -> dict: | 普通方法；tools.py:515 用它给 `read_structure` 工具返回摘要 |
| 233 | return { | 直接构造并返回字典 |
| 234 | "natoms": self.natoms, | 原子总数 |
| 235 | "species": self.species, | 元素列表（VASP4 时是 None） |
| 236 | "counts": self.counts, | 各元素计数 |
| 237 | "volume": round(self.volume(), 4), | 体积保留 4 位小数，避免摘要里出现长浮点尾巴 |
| 238 | "coord_mode": self.coord_mode, | "direct" / "cartesian" |
| 239 | "selective": self.selective, | 是否带选择性弛豫 |
| 240 | "has_velocities": self.velocities is not None, | 是否带速度块；注意这里只判断"有没有"，不校验行数 |
| 241 | "scale_raw": self.scale_raw, | 原始第二行 scale，便于复查负 scale 输入 |
| 242 | } | 字典结束 |

## 4. 读取（read_poscar_text / _is_float_token / read_poscar）

### 4.1 解析流程鸟瞰

`read_poscar_text` 按文件顺序推进一个下标 `idx`，每一步只认"当前位置该有的东西"：

1. 第 1 行 → `comment`；
2. 第 2 行 → `scale_raw`（只取第一个 token，允许后面写注释）；
3. 第 3–5 行 → `lattice_raw`（每行只取前 3 个数）；
4. 第 6 行 → 判断 VASP4/VASP5；
5. 可选 `Selective dynamics`；
6. 坐标模式行（Direct / Cartesian）；
7. `natoms` 行坐标（selective 时每行再带 3 个 T/F）；
8. 可选速度块（前面可能夹空行和 `Velocities` 标题）；
9. 剩余"非数字"行 → `tail`；
10. 体积自检；
11. 元素重复时 `_regroup` 合并。

#### 关键问题 1：第 6 行怎么判断 VASP4 / VASP5

- 依据是 `all(_is_int_token(t) for t in toks)`（`poscar.py:279`）：**这一行所有 token 都能被 `int()` 解析** → 它是原子数行，文件是 VASP4；否则就是元素符号行，文件是 VASP5。
- VASP4 分支：`species = None`、`counts = [int(t) ...]`、`idx = 6`（下一行是 Selective/坐标模式）。
- VASP5 分支：`species = toks`，逐个用 `_ELEMENT_RE` 校验；第 7 行读 `counts`，`idx = 7`（下一行是 Selective/坐标模式）。
- 随后统一校验：元素数与计数个数相等（294–297 行）、计数非负（298–299 行）、原子总数不为 0（300–302 行）。
- 局限：第 7 行的 `counts` 后面不能再跟注释，因为 289 行对整行 `split()` 的每个 token 都做 `int()`，遇到非数字会直接报错。

#### 关键问题 2：速度块怎么探测

速度块不是 POSCAR 的必需段，而且是"位置不固定、前面可能有空行/标题"的可选段，所以代码用了"先跳过噪声、再探测、最后逐行确认"的三段式（355–378 行）：

1. `rest_all = lines[idx:]` 把坐标之后的所有行都取出来（356 行）；
2. 跳过开头的空行（358–359 行）；
3. 如果第一个非空行是 `velocities`/`velocity`（大小写不敏感），再跳过它和它后面的空行（360–363 行）；
4. `probe = rest[0].split()`：要求至少 3 个 token、前 3 个都是浮点、且剩余行数 ≥ `natoms`（365–367 行）——这是"看起来像速度块"的初筛；
5. 再逐行确认前 `natoms` 行每行都有 ≥3 个浮点（370–375 行）；任何一行不满足就 `ok = False`，整个块不算速度；
6. 确认成功后 `velocities = vel`，并把 `idx` 推到 `k0 + natoms`（377–378 行），让后面的 tail 从速度块之后开始。

为什么先"初筛"再逐行确认：真实文件里坐标后面可能跟注释行、`Velocities` 标题、甚至别的数字块；只凭第一行不够，必须确认恰好有 `natoms` 行可解析的 3 个浮点。这样做也保证了"识别出来的速度条数 == 原子数"，不会把半截块当速度。

#### 关键问题 3：tail 为什么只保留非数字行

`tail` 的收集在 380–388 行：跳过空行；取每行第一个 token；**如果第一个 token 能转成浮点，就丢掉这一行**；否则原样保留。

原因是：如果速度块因为行数不对/格式不对而没被识别，这些数字行会留在 `lines[idx:]` 里。若把它们原样塞进 `tail`，写回时它们会被追加在坐标后面，变成一个"没有标题、条数不对"的数字块，ASE/VASP 会按速度块去读，轻则断言失败，重则读错结构。所以这里选择"丢数字、留文字"：注释、标记行保留，无法归类的数字行丢弃。

代价是：任何"非速度的纯数字尾部数据"也会被丢掉；但 POSCAR 的尾部本来就没有别的数字段（速度之后只有注释/标记），所以取舍是合理的。

### 4.2 read_poscar_text 逐行（249–414）

- 干什么：把一段 POSCAR 文本解析成 `Structure`。
- 参数：`text: str`；关键字参数 `source: str = "<text>"`，只用于错误信息里标明来源。
- 返回：`Structure`。
- 异常：`StructureError`，错误信息统一带 `f"{source}: ..."` 前缀，方便定位是哪个文件坏了。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 249 | def read_poscar_text(text: str, *, source: str = "<text>") -> Structure: | 函数入口；`source` 用 `*` 限定为关键字参数，避免调用方按位置误传 |
| 250 | """把 POSCAR 文本解析成 Structure。逐行说明见文件末尾的"解析流程"。""" | docstring；文件末尾 903–906 行有 4 行速查 |
| 251 | lines = text.splitlines() | 按通用换行拆行：CRLF/CR/LF 都能处理（`read_poscar` 还处理了 BOM 和 GBK） |
| 252 | while lines and not lines[-1].strip(): | 去掉文件末尾的空行；`lines and` 防止空文件时 `lines[-1]` 越界 |
| 253 | lines.pop() | 从尾部弹掉一个空行，循环直到最后一行非空 |
| 254 | if len(lines) < 7: | 最少 7 行：comment、scale、3 行晶格、第 6 行、坐标模式行 |
| 255 | raise StructureError(f"{source}: 只有 {len(lines)} 行，不像 POSCAR") | 行数不足直接拒绝，避免后面各种 IndexError |
| 257 | comment = lines[0].strip() | 第 1 行原样保存为注释（去掉首尾空白） |
| 259 | # 第二行：scale。VASP 只读第一个数（后面可以跟注释） | 说明第 2 行只取第一个 token，允许行尾写注释 |
| 260 | try: | 第 2 行 scale 的解析放进 try，统一把解析错误换成 StructureError |
| 261 | scale_raw = float(lines[1].split()[0]) | 只取第一个 token：VASP 允许第二行后面跟注释 |
| 262 | except (IndexError, ValueError) as exc: | 行不存在（IndexError）或不是数字（ValueError）都在这里接住 |
| 263 | raise StructureError(f"{source}: 第二行不是数字：{lines[1]!r}") from exc | `!r` 保留原始 repr，方便看出到底是哪个 token 坏了；`from exc` 保留原始异常链 |
| 265 | # 第三~五行：三个晶格矢量；每行只取前 3 个数 | 说明晶格行只取前 3 个数，多余 token 忽略 |
| 266 | try: | 晶格 3 行的解析同样包 try |
| 267 | lattice_raw = [[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)] | 逐个取第 3/4/5 行，`[:3]` 只取前 3 个数（多余 token 忽略） |
| 268 | except (IndexError, ValueError) as exc: | 行不存在或不是数字 |
| 269 | raise StructureError(f"{source}: 第 3~5 行不是 3x3 晶格矩阵") from exc | 报错信息按人读的行号描述（第 3~5 行） |
| 270 | if any(len(row) != 3 for row in lattice_raw): | `[:3]` 取不满 3 个时行长度不足，这里补一道检查 |
| 271 | raise StructureError(f"{source}: 晶格矩阵某一行不足 3 个数") | 三行必须各 3 个数，否则矩阵不成立 |
| 273–274 | # 第六行：VASP5 是元素符号，VASP4 是原子数。…# 判断依据：所有 token 都能转成整数 → 原子数行（VASP4） | 两行注释交代 VASP4/VASP5 的判断依据，对应 279 行的 `all(...)` |
| 275 | idx = 5 | 当前处理到第 6 行（0 基下标 5） |
| 276 | toks = lines[idx].split() | 第 6 行拆 token |
| 277 | if not toks: | 空行的情况 |
| 278 | raise StructureError(f"{source}: 第 6 行是空的") | 第 6 行不能为空 |
| 279 | if all(_is_int_token(t) for t in toks): | 关键判断：全整数 → VASP4 的原子数行 |
| 280 | species = None | VASP4 没有元素行，只能先记 None |
| 281 | counts = [int(t) for t in toks] | 这一行本身就是每个元素的原子数 |
| 282 | idx = 6 | 下一行（下标 6）开始找 Selective/坐标模式 |
| 283 | else: | 否则这一行是 VASP5 的元素符号行 |
| 284 | species = toks | 元素符号按原顺序保存；顺序决定 POTCAR 的拼装顺序 |
| 285 | for sym in species: | 逐个校验元素符号格式 |
| 286 | if not _ELEMENT_RE.match(sym): | 只接受"首字母大写 + 可选一个小写字母" |
| 287 | raise StructureError(f"{source}: 元素符号不合法：{sym!r}") | 例如全小写、含数字的 token 在这里被拦下 |
| 288 | try: | 第 7 行原子数的解析 |
| 289 | counts = [int(x) for x in lines[idx + 1].split()] | 注意这里是对整行所有 token 做 int，所以行尾不能跟注释 |
| 290 | except (IndexError, ValueError) as exc: | 缺行或非整数 |
| 291 | raise StructureError(f"{source}: 第 7 行不是原子数列表") from exc | 报错按人读的行号 |
| 292 | idx = 7 | 下一行（下标 7）开始找 Selective/坐标模式 |
| 294 | if species is not None and len(counts) != len(species): | VASP5 才能做这个配对校验；VASP4 时 species 是 None，跳过 |
| 295–297 | raise StructureError( f"{source}: 元素 {len(species)} 个，原子数 {len(counts)} 个，数量对不上" ) | 元素行与计数行必须一样长，否则后面逐原子元素列表会错位 |
| 298 | if any(n < 0 for n in counts): | 原子数不允许负数 |
| 299 | raise StructureError(f"{source}: 原子数不能为负：{counts}") | 负数通常意味着文件写坏了 |
| 300 | natoms = sum(counts) | 总原子数；后面读坐标行数用 |
| 301 | if natoms == 0: | 没有原子的结构没有意义 |
| 302 | raise StructureError(f"{source}: 一个原子都没有") | 拒绝空结构，避免后续体积/操作出现除零或空循环 |
| 304 | # Selective dynamics 是可选的，必须在坐标模式行之前 | 顺序是硬性的：有这个块时它一定在坐标模式行之前 |
| 305 | selective = False | 默认没有选择性弛豫 |
| 306 | if idx < len(lines) and lines[idx].strip().lower().startswith("s"): | 只要当前行首字母是 s 就当作 Selective dynamics（大小写不敏感） |
| 307 | selective = True | 记下"有这个块" |
| 308 | idx += 1 | 跳过这一行 |
| 310 | # 坐标模式行：Direct 或 Cartesian | 说明下一行该读什么 |
| 311 | if idx >= len(lines): | 坐标模式行必须存在 |
| 312 | raise StructureError(f"{source}: 缺少坐标模式行（Direct/Cartesian）") | 文件在这里就截断了 |
| 313 | mode_line = lines[idx].strip() | 取坐标模式行的原文（去空白） |
| 314 | first = mode_line[:1].lower() | 只看首字母并转小写 |
| 315 | if first == "d": | D 开头 → Direct |
| 316 | coord_mode = "direct" | 分数坐标模式 |
| 317 | elif first in ("c", "k"):          # Cartesian / Kartesian，VASP 两种拼写都认 | C 或 K 开头 → Cartesian；VASP 传统上两种拼写都出现过 |
| 318 | coord_mode = "cartesian" | 笛卡尔坐标模式 |
| 319 | else: | 首字母不是 d/c/k |
| 320 | raise StructureError(f"{source}: 不认识坐标模式 {mode_line!r}（应为 Direct/Cartesian）") | 报错并回显整行，方便用户看出是哪一行 |
| 321 | idx += 1 | 坐标模式行已经消费掉，下一行是第一个原子的坐标 |
| 323 | # 读 natoms 行坐标；selective 时每行后面还有 3 个 T/F | 说明坐标块的结构 |
| 324 | coords: list[list[float]] = [] | 坐标累加器 |
| 325 | flags: list[list[bool]] \| None = [] if selective else None | 有条件时才准备 flags 列表；这里 `[] if selective else None` 先算，再赋给 `flags` |
| 326 | for k in range(natoms): | 逐个原子读坐标，共 natoms 行 |
| 327 | if idx + k >= len(lines): | 文件提前结束 |
| 328–330 | raise StructureError( f"{source}: 声明 {natoms} 个原子，坐标只找到 {k} 个" ) | 错误信息同时给出"声明数"和"实际找到数"，方便定位截断 |
| 331 | parts = lines[idx + k].split() | 当前原子行拆 token |
| 332 | if len(parts) < 3: | 坐标至少 3 个数 |
| 333 | raise StructureError(f"{source}: 第 {idx + k + 1} 行坐标不足 3 个数") | 错误信息用 1 基行号（`+1`）对齐编辑器显示 |
| 334 | try: | 坐标转浮点的 try |
| 335 | coords.append([float(parts[0]), float(parts[1]), float(parts[2])]) | 固定取前 3 个 token；多余的 token（如 T/F）这里先忽略 |
| 336 | except ValueError as exc: | 有 token 转不成浮点 |
| 337 | raise StructureError(f"{source}: 第 {idx + k + 1} 行坐标不是数字") from exc | 带回原始异常链 |
| 338 | if selective: | 只有 selective 才继续解析每行后面的 T/F |
| 339 | toks_f = parts[3:6] | 取第 4–6 个 token 作为三个方向的约束 |
| 340 | if len(toks_f) < 3: | 约束不足 3 个 |
| 341 | # 某些工具只写一个 T/F 表示三个方向相同，这里兼容 | 解释 342 行复制分支的来由 |
| 342 | if len(toks_f) == 1: | 兼容分支：某些工具只写一个 T/F 表示三个方向相同 |
| 343 | toks_f = toks_f * 3 | 把单个约束复制成 3 个 |
| 344 | else: | 只写 0 个或 2 个约束，无法解释 |
| 345–347 | raise StructureError( f"{source}: Selective dynamics 行的约束不足 3 个：{lines[idx + k]!r}" ) | 报错回显整行，方便用户直接看到坏行 |
| 348 | flags.append([t[:1].upper() == "T" for t in toks_f]) | 只看首字母、转大写：T 开头的 token → True（可动），其余 → False（固定） |
| 349 | idx += natoms | 坐标全部读完后，下标推到坐标块之后 |
| 351–354 | # 可选的速度块：坐标之后、每原子 3 个数的块，行数 >= natoms …（共 4 行注释） | 解释速度块位置不固定：要先跳过空行/标题；否则速度块会被当成 tail 原样写回，ASE 读这种文件会断言失败 |
| 355 | velocities: list[list[float]] \| None = None | 默认没有速度块 |
| 356 | rest_all = lines[idx:] | 坐标之后的所有行（可能包括空行、标题、速度、尾部注释） |
| 357 | k0 = 0 | `k0` 是"跳过噪声后，速度候选块在 rest_all 里的偏移" |
| 358 | while k0 < len(rest_all) and not rest_all[k0].strip(): | 跳过开头的空行 |
| 359 | k0 += 1 | 每跳过一个空行，偏移加一 |
| 360 | if k0 < len(rest_all) and rest_all[k0].strip().lower() in ("velocities", "velocity"): | 识别可选的 "Velocities"/"Velocity" 标题行（大小写不敏感） |
| 361 | k0 += 1 | 跳过标题行本身 |
| 362 | while k0 < len(rest_all) and not rest_all[k0].strip(): | 标题行和速度数据之间还可能有空行 |
| 363 | k0 += 1 | 继续跳过空行 |
| 364 | rest = rest_all[k0:] | 真正的速度候选区 |
| 365 | if rest: | 没有剩余行就不可能有速度块 |
| 366 | probe = rest[0].split() | 初筛用第一行 |
| 367 | if len(probe) >= 3 and all(_is_float_token(t) for t in probe[:3]) and len(rest) >= natoms: | 初筛条件：第一行前 3 个是浮点，且剩余行数至少 natoms 行 |
| 368 | vel = [] | 速度累加器 |
| 369 | ok = True | 逐行确认的开关 |
| 370 | for k in range(natoms): | 只检查前 natoms 行 |
| 371 | parts = rest[k].split() | 当前速度行拆 token |
| 372 | if len(parts) < 3 or not all(_is_float_token(t) for t in parts[:3]): | 任何一行不足 3 个数或前 3 个不是浮点 |
| 373 | ok = False | 标记整个块不是速度块 |
| 374 | break | 已经失败，不再往下检查 |
| 375 | vel.append([float(parts[0]), float(parts[1]), float(parts[2])]) | 逐行收集速度（只取前 3 个数） |
| 376 | if ok: | 所有 natoms 行都确认通过 |
| 377 | velocities = vel | 认定为速度块 |
| 378 | idx += k0 + natoms | 把下标推到速度块之后；`k0` 是速度块相对 `lines[idx]` 的偏移，`natoms` 是块的长度 |
| 380 | # 尾部只保留"不像数字"的行（注释/标记）；数字行是没识别出来的速度块，不能原样带走 | 解释 tail 的取舍：留文字、丢数字，避免写回一个条数不对的假速度块 |
| 381 | tail = [] | 尾部行累加器 |
| 382 | for ln in lines[idx:]: | 从当前位置（坐标之后或速度块之后）一直看到文件尾 |
| 383 | if not ln.strip(): | 空行没有信息 |
| 384 | continue | 空行直接跳过 |
| 385 | first = ln.split()[0] | 取这一行第一个 token |
| 386 | if _is_float_token(first): | 第一个 token 是数字 → 这一行像"没被识别的速度数据" |
| 387 | continue | 丢数字行，理由见 4.1 的"关键问题 3" |
| 388 | tail.append(ln) | 非数字行原样保留（包括缩进），写回时也原样输出 |
| 390 | st = Structure( | 构造 Structure，下面逐个字段对应 |
| 391 | comment=comment, | 第 1 行注释 |
| 392 | scale_raw=scale_raw, | 原始 scale（不在这里乘进晶格） |
| 393 | lattice_raw=lattice_raw, | 未乘 scale 的 3x3 |
| 394 | species=species, | VASP5 元素列表；VASP4 为 None |
| 395 | counts=counts, | 每个元素的原子数 |
| 396 | coords=coords, | 原模式下的坐标 |
| 397 | coord_mode=coord_mode, | "direct" / "cartesian" |
| 398 | selective=selective, | 是否带 Selective dynamics |
| 399 | flags=flags, | 每原子 3 个 T/F；非 selective 时为 None |
| 400 | velocities=velocities, | 速度块；没有则为 None |
| 401 | tail=tail, | 尾部非数字行 |
| 402 | ) | 构造调用结束 |
| 403 | # 解析时就做一次自检：体积必须为正，否则后面所有操作都没有物理意义 | 解释 404 行为什么要在解析阶段就检查体积 |
| 404 | if st.volume() <= 0: | 解析后立刻自检体积；`volume()` 用乘过 scale 的晶格 |
| 405 | raise StructureError(f"{source}: 晶格体积不为正，POSCAR 可能已损坏") | 体积非正时后面所有几何操作都没有意义，尽早报错 |
| 407–411 | # 同元素在元素行里重复（如 Mo S Mo S）时，合并成“每个元素只出现一次”。…（共 5 行注释） | 解释合并原因（否则 POTCAR 段数与 POSCAR 段数对不上会报错）、以及合并必须连带重排 flags/速度 |
| 412 | if species is not None and len(set(species)) != len(species): | 元素行里有重复符号（如 Mo S Mo S）时触发合并 |
| 413 | st = _regroup(st, st.atom_species(), st.frac(), st.flags, st.velocities) | 合并元素段，同时重排坐标、flags、速度；`_regroup` 细节见 6.2 |
| 414 | return st | 返回解析结果 |

### 4.3 _is_float_token（417–422）

- 干什么：判断一个字符串 token 能否被 `float()` 解析。
- 参数：`tok: str`。
- 返回：`bool`。
- 异常：不主动抛；`float()` 抛 `ValueError` 时返回 `False`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 417 | def _is_float_token(tok: str) -> bool: | 定义函数 |
| 418 | try: | 试着解析 |
| 419 | float(tok) | 能解析就说明是数字 token |
| 420 | return True | 解析成功 |
| 421 | except ValueError: | 只有 ValueError 被捕获（`float(None)` 会抛 TypeError，但这里 token 一定来自 split，是字符串） |
| 422 | return False | 解析失败 |

### 4.4 read_poscar（425–439）

- 干什么：从磁盘文件读 POSCAR（处理编码/BOM），再交给 `read_poscar_text` 解析。
- 参数：`path: str | Path`。
- 返回：`Structure`。
- 异常：`StructureError` —— 文件不存在；或三种编码都解码失败（实际上 latin-1 不会失败，这是防御分支）。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 425 | def read_poscar(path: str \| Path) -> Structure: | 定义函数；str 和 Path 都接受 |
| 426 | """从文件读 POSCAR；兼容 UTF-8 BOM、CRLF、GBK。""" | docstring：CRLF 由 `splitlines()` 处理，BOM 由 `utf-8-sig` 处理 |
| 427 | p = Path(path) | 统一转成 Path，后面用 `.exists()`/`.read_bytes()` |
| 428 | if not p.exists(): | 先检查存在性 |
| 429 | raise StructureError(f"找不到 POSCAR：{p}") | 不让 `read_bytes()` 抛原始 FileNotFoundError，保持错误类型统一 |
| 430 | raw = p.read_bytes() | 按二进制读，先拿字节再决定编码；这样不会被系统默认编码坑到 |
| 431 | for enc in ("utf-8-sig", "gbk", "latin-1"): | 依次尝试三种编码：UTF-8（带 BOM 也能吃）、GBK（中文 Windows 常见）、latin-1（永远能解） |
| 432 | try: | 尝试用当前编码解码 |
| 433 | text = raw.decode(enc) | 解码成功就得到文本 |
| 434 | break | 成功即跳出循环 |
| 435 | except UnicodeDecodeError: | 当前编码解不了 |
| 436 | continue | 换下一种编码 |
| 437 | else:  # pragma: no cover - latin-1 永远能解 | for-else：循环正常走完（没有 break）才进这里；因为 latin-1 任何字节都能解，这个分支实际到不了 |
| 438 | raise StructureError(f"{p}: 无法解码文本") | 防御性报错 |
| 439 | return read_poscar_text(text, source=str(p)) | 把文件名传进 `source`，后续所有解析错误都会带文件名 |

## 5. 写入（write_poscar_text / write_poscar）

写入的总策略写在 `poscar.py:449-453`：统一 `scale = 1.0`、保留坐标模式、保留 Selective/速度、元素行与原子数行重新生成。下面两小节逐行讲，并在末尾解释两个"看起来多余、实际必须"的细节。

### 5.1 write_poscar_text 逐行（446–494）

- 干什么：把 `Structure` 序列化成 POSCAR 文本（返回字符串，不落盘）。
- 参数：`st: Structure`；`comment: str | None = None`，给了就覆盖 `st.comment`。
- 返回：`str`，以 `\n` 结尾。
- 异常：无主动异常；`_fmt` 遇到非数字类型会抛 `TypeError`/`ValueError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 446 | def write_poscar_text(st: Structure, comment: str \| None = None) -> str: | 函数入口；`comment` 允许覆盖结构自带的注释 |
| 447–454 | """把 Structure 写成 POSCAR 文本。…""" | docstring：交代四条写回策略（scale=1.0、保留坐标模式、保留 Selective/速度、头两行重新生成） |
| 455 | lines: list[str] = [] | 逐行累加，最后统一 join；比字符串拼接更清晰也更快 |
| 456 | lines.append((comment if comment is not None else st.comment) or "generated by vasp-prep") | 注释优先级：显式参数 > 结构自带 > 默认文案；注意 `or` 让空字符串也落到默认文案 |
| 457 | lines.append("1.0") | 第二行固定写 1.0（原因见 5.3） |
| 458 | L = st.lattice | 取乘过 scale 的物理晶格（Å）；后面坐标换算也用它 |
| 459 | for row in L: | 逐行输出 3 个晶格矢量 |
| 460 | lines.append("  " + "  ".join(_fmt(x) for x in row)) | 行首两个空格，数字之间两个空格；每个数走 `_fmt` 统一格式 |
| 462 | if st.species is not None: | VASP5 才写元素行；VASP4 的结构写回去仍然是 VASP4 |
| 463 | lines.append("  " + "  ".join(st.species)) | 元素顺序原样保留（它决定 POTCAR 拼装顺序） |
| 464 | lines.append("  " + "  ".join(str(n) for n in st.counts)) | 原子数行总是按当前 `counts` 重新生成，保证"结构变了、头也跟着变" |
| 466 | if st.selective: | 只有带选择性弛豫才写这一行 |
| 467 | lines.append("Selective dynamics") | 固定的关键字行 |
| 468 | lines.append("Direct" if st.coord_mode == "direct" else "Cartesian") | 坐标模式按内存里的 `coord_mode` 原样写；这是为了减少 diff 噪音 |
| 470 | fracs = st.frac() | 统一先转成分数坐标：Direct 模式就是原值，Cartesian 模式会做一次换算 |
| 471 | for i, f in enumerate(fracs): | 逐原子输出，`i` 同时用于取对应的 flags |
| 472 | if st.coord_mode == "direct": | Direct 模式：直接写分数坐标 |
| 473 | xyz = f | 不需要再换算 |
| 474 | else: | Cartesian 模式 |
| 475 | xyz = vec_mat(f, L) | 把分数坐标乘回物理晶格（Å）。因为输出 scale 固定 1.0，这里得到的 Å 就是文件里该写的数（相当于把原 scale 乘进了坐标） |
| 476 | line = "  " + "  ".join(_fmt(x) for x in xyz) | 拼坐标行：行首两个空格，数字间两个空格 |
| 477 | if st.selective: | selective 时每个原子后面还要写 3 个 T/F |
| 478 | flags = st.flags[i] if st.flags else [False, False, False] | 取第 i 个原子的 flags；`st.flags` 意外为空时兜底成全 F（固定），避免 IndexError |
| 479 | line += "   " + "   ".join("T" if b else "F" for b in flags) | 先加 3 个空格，再用 3 个空格分隔 T/F；`True` 写 T（可动），`False` 写 F（固定） |
| 480 | lines.append(line) | 收下这一行 |
| 482–485 | # 速度块：坐标后先留一个空行，再写每原子 3 个数。…（四行注释） | 解释空行的必要性：ASE 的 `read_vasp` 会先读一行判断是不是 "Lattice velocities"（详见 5.4） |
| 486 | if st.velocities is not None and len(st.velocities) == st.natoms: | 只有速度条数与原子数**恰好相等**才写；不等就整块丢掉（见 5.4） |
| 487 | lines.append("") | 关键的空行：给 ASE 的判断行留一个"空"的占位 |
| 488 | for v in st.velocities: | 逐原子写速度 |
| 489 | lines.append("  " + "  ".join(_fmt(x) for x in v)) | 与坐标行同样的缩进/分隔格式 |
| 491 | if st.tail: | 有尾部非数字行就原样追加 |
| 492 | lines.extend(st.tail) | 不重新格式化、不排序，保持读进来时的样子 |
| 494 | return "\n".join(lines) + "\n" | 用 `\n` 连接并在末尾补一个换行；POSCAR 末尾留空行，VASP 和各解析器都接受 |

### 5.2 write_poscar 逐行（497–500）

- 干什么：把 `write_poscar_text` 的结果写到磁盘。
- 参数：`st`、`path: str | Path`、`comment: str | None = None`。
- 返回：`None`。
- 异常：不主动抛；目录创建失败/磁盘错误由 `Path.mkdir`/`write_text` 抛 `OSError`。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 497 | def write_poscar(st: Structure, path: str \| Path, comment: str \| None = None) -> None: | 函数入口 |
| 498 | p = Path(path) | 统一转 Path |
| 499 | p.parent.mkdir(parents=True, exist_ok=True) | 父目录不存在就创建；`exist_ok=True` 让重复运行不报错。本仓库内 `write_poscar` 只被导出、未发现内部调用，真正落盘的是 `prep.py:327` 的 `_atomic_write` |
| 500 | p.write_text(write_poscar_text(st, comment), encoding="utf-8", newline="\n") | 显式 UTF-8（不写 BOM）、显式 `newline="\n"`（Windows 上也不转成 CRLF），保证集群读到的换行与本地一致 |

### 5.3 为什么输出要统一 scale=1.0

1. **避免二次浮点误差**：读入时 `lattice_raw` 是"原始数"，写回时若保留原 scale，就要先把 Å 除回 scale 再写，来回除法会引入舍入误差；直接写 Å 只做一次乘法。
2. **负 scale 写不回去**：负 scale 的语义是"目标体积"，写回时若原样保留 `-V`，而晶格已经改成新结构的 Å，二者不再匹配，VASP 会按旧体积重新缩放，结构就变了；统一写 1.0 可以彻底绕开。
3. **diff 稳定**：同一个结构反复读写，第二行永远是 `1.0`，晶格行永远是 Å，不会出现"只改了一个原子却整块晶格数字都变"的 diff。
4. **读入端兼容**：VASP 和 ASE 都把 `scale=1.0` 当成"不缩放"，不需要额外解释。

### 5.4 为什么速度块前必须写一个空行

ASE 的 `read_vasp` 读完坐标后，会**先读一行**判断是不是 `Lattice velocities` 之类的标题：

- 没有空行时：第一行正好是第 1 个原子的速度（3 个数，不是标题），ASE 把它当"判断行"吞掉；接着再读 `natoms` 行速度时只剩 `natoms-1` 行，读到 EOF 就 `assert` 失败。
- 有空行时：判断行读到的是空行（不算标题），随后正好还能读满 `natoms` 行速度。

VASP 本身也接受这个空行，所以这是"两边都兼容"的写法。`tests/test_core.py:550-571` 有专门的回归测试：写出去再读回来，速度值必须还在。

另一个细节：486 行的条件是 `len(st.velocities) == st.natoms`，条数对不上就整块不写。这样虽然会丢速度数据，但不会写出一个"条数不对、读的人必然误判"的坏文件；代码没有为此发警告，这是可以改进的地方（未确认是否有意为之）。

## 6. 结构操作（_check_indices / _regroup / 11 个 op_*）

本节每个 `op_*` 都遵守同一套约定（`poscar.py:503-505` 的注释）：

- **纯函数**：返回新的 `Structure`，不修改入参；`recipe.apply_ops` 每一步都把 `cur` 换成新对象，所以中途报错时原结构完好。
- **返回二元组**：`(新 Structure, 一句中文说明)`；说明给配方日志/工具层展示用。
- **索引从 1 开始**：用户说的"第 3 个原子"就是 `index=3`，内部由 `_check_indices` 转成 0 基。
- **元素连续性**：凡是可能改变原子顺序或元素种类的操作，最后都走 `_regroup`，保证同元素原子连续（VASP 的硬要求）。

> 再次说明数量：源码里是 11 个 `op_*`，不是 12 个。`ops_ext.py` 另有 `op_surface` 等高阶操作，但它们不在本文件。

### 6.1 _check_indices（507–529）

- 干什么：把 1 基索引（单个 int 或 int 列表）规范成 0 基列表，并检查范围。
- 参数：`indices: int | list[int]`、`natoms: int`（当前结构原子数）、`what: str = "索引"`（拼错误信息用的名字）。
- 返回：`list[int]`，0 基索引。
- 异常：`StructureError` —— 类型不是 int/list/tuple；元素不是 int（含 `bool`）；索引越界（合法范围 1~natoms）。

**为什么这么写**：用户和模型都习惯从 1 数原子，内部却必须用 0 基；先把"1 基 → 0 基 + 越界检查"集中到一个函数，所有 op 的索引语义就一致了。
**不这么写会怎样**：直接在 op 里写 `sp[index]`，用户传 1 时取到第 2 个原子（悄悄错位）；传 0 或超过 natoms 时抛原始 `IndexError`，错误信息对用户没有指导意义；`True` 在 Python 里是 `int` 的子类，不显式拦 `bool` 会把 `True` 当索引 1。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 507 | def _check_indices(indices: int \| list[int], natoms: int, what: str = "索引") -> list[int]: | 函数入口 |
| 508–512 | """把 1 基索引规范成 0 基列表，并检查范围。…""" | docstring：说明 1 基/0 基的来源与越界风险 |
| 513 | if isinstance(indices, int): | 分支一：单个整数 |
| 514 | idxs = [indices] | 包成列表，后面统一按列表遍历 |
| 515 | elif isinstance(indices, (list, tuple)): | 分支二：列表或元组 |
| 516 | idxs = list(indices) | 复制成新列表，不修改调用方传入的对象 |
| 517 | else: | 其他类型（None、字符串、float 等） |
| 518 | raise StructureError(f"{what} 必须是整数或整数列表，收到 {type(indices).__name__}") | 错误信息带 `what` 和实际类型名，方便定位是哪个参数 |
| 519 | out = [] | 0 基结果累加器 |
| 520 | for i in idxs: | 逐个检查 |
| 521 | if not isinstance(i, int) or isinstance(i, bool): | 必须 int，且显式排除 bool（`True`/`False` 是 int 子类） |
| 522 | raise StructureError(f"{what} 必须是整数，收到 {i!r}") | 回显坏值，例如 `'3'` 或 `2.0` |
| 523 | j = i - 1 | 1 基转 0 基 |
| 524 | if j < 0 or j >= natoms: | 越界检查：0 基合法范围是 0..natoms-1 |
| 525–527 | raise StructureError( f"{what} {i} 越界：当前结构只有 {natoms} 个原子（1~{natoms}）" ) | 错误信息用 1 基范围描述，和用户输入习惯一致 |
| 528 | out.append(j) | 收下转换后的索引 |
| 529 | return out | 返回 0 基列表；注意本函数不去重，去重由调用方决定（如 `op_vacancy` 用 `set`） |

### 6.2 _regroup（532–564）

- 干什么：按"首次出现顺序"把逐原子元素列表重新分组，保证同元素连续；同时重排 `flags`/`velocities`，并用重排后的坐标重建 `Structure`。
- 参数：`st`（提供注释/晶格/坐标模式/selective/tail）、`atom_species: list[str]`（逐原子元素）、`fracs: list[list[float]]`（与 `atom_species` 同序的分数坐标）、`flags`、`velocities`。
- 返回：新的 `Structure`。
- 异常：无主动异常；`fracs`/`flags` 等长度与 `atom_species` 不一致时会 `IndexError` 或触发 `set_frac` 的长度校验。

**为什么这么写**：VASP 按第 6 行元素顺序逐段读 POTCAR；元素不连续时它**不报错**，而是把后面的原子当成前一种元素，算出来的体系是错的。只有在"换元素/加原子/删原子"后才需要重排；纯扩胞/应变不改变元素顺序，所以不走这里。
**不这么写会怎样**：`Mo S Mo S` 这种顺序会让 VASP 认为有 4 段元素、需要 4 段 POTCAR；我们生成的 POTCAR 只有 2 段时直接报 "number of POTCAR files does not match"，化学上却明明等价。只重排元素不重排 flags/速度的话，选择性约束和速度会错位到别的原子上。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 532 | def _regroup(st: Structure, atom_species: list[str], fracs: list[list[float]], | 函数入口（签名跨两行） |
| 533 |              flags: list[list[bool]] \| None, velocities: list[list[float]] \| None) -> Structure: | 第二行参数：flags/速度可为 None |
| 534–539 | """按"首次出现顺序"重新分组，保证同元素原子连续（VASP 的硬要求）。…""" | docstring：说明原因（POTCAR 顺序）和适用场景（换/加/删原子之后） |
| 540 | order: list[str] = [] | 记录元素的首次出现顺序 |
| 541 | for s in atom_species: | 按原子顺序扫描 |
| 542 | if s not in order: | 第一次见到这个元素 |
| 543 | order.append(s) | 追加到顺序表尾部；这样得到的是"首次出现顺序"，不是字母序 |
| 544 | buckets: dict[str, list[int]] = {s: [] for s in order} | 每个元素一个桶，值是该元素的原子下标（保持原原子顺序） |
| 545 | for i, s in enumerate(atom_species): | 再扫一遍，把下标放进对应桶 |
| 546 | buckets[s].append(i) | 同一个元素内部仍按原顺序 |
| 547 | new_order = [i for s in order for i in buckets[s]] | 按元素顺序把桶拼起来，得到新的原子排列 |
| 548 | new_counts = [len(buckets[s]) for s in order] | 每个桶的大小就是新的元素计数 |
| 550 | st2 = Structure( | 构造新结构；注意下面几行：`st` 的 species/counts 不参与，完全由传入的 `atom_species` 推导 |
| 551 | comment=st.comment, | 注释原样保留 |
| 552 | scale_raw=1.0, | 统一归一化：下面的 `lattice_raw` 用 Å，所以 scale 固定 1.0 |
| 553 | lattice_raw=[list(r) for r in st.lattice], | 用 `st.lattice`（乘过 scale 的 Å）并复制，避免共享列表 |
| 554 | species=order, | 元素行 = 首次出现顺序 |
| 555 | counts=new_counts, | 计数 = 各桶大小 |
| 556 | coords=[], | 先占位，等 `set_frac` 填 |
| 557 | coord_mode=st.coord_mode, | 保持原坐标模式 |
| 558 | selective=st.selective, | 保持 selective 标志 |
| 559 | flags=[flags[i] for i in new_order] if flags is not None else None, | flags 跟着原子一起重排；None 时保持 None |
| 560 | velocities=[velocities[i] for i in new_order] if velocities is not None else None, | 速度同理；不重排会让速度跟错原子 |
| 561 | tail=list(st.tail), | 尾部行复制一份 |
| 562 | ) | 构造调用结束 |
| 563 | st2.set_frac([fracs[i] for i in new_order]) | 坐标按 `new_order` 重排后写进新结构；`set_frac` 会再校验长度与 counts 一致 |
| 564 | return st2 | 返回新结构（入参 `st` 未被修改） |

> 复用点：`backends/pmg_backend.py:88` 也调用 `_regroup`，传的是一个 `species=None`、`counts=[]` 的"空壳" Structure，只借用 `comment/lattice/coord_mode` 等字段；因为本函数不从 `st.species`/`st.counts` 推导任何东西，这种用法是成立的。

### 6.3 op_supercell（567–629）

- 干什么：按 `[nx, ny, nz]` 扩胞，返回新结构。
- 参数：`st`；`scaling`，长度为 3 的正整数列表/元组。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— `scaling` 不是长度 3 的列表/元组；元素不是正整数（含 bool）；VASP4 结构（`atom_species()` 抛错）。

**为什么这么写**：先对元素去重再铺，且每个元素内部按原原子顺序、每个原子按 (a,b,c) 三重循环铺开，这样天然满足"同元素连续"，也让扩胞后的原子顺序可预测。
**不这么写会怎样**：若直接 `for sym in st.species` 遍历（不去重），`Mo S Mo S` 会把 Mo、S 各铺两遍，坐标数变成应有的 2 倍，而 `counts` 只乘一次，`set_frac` 立刻抛"坐标数量与元素计数不一致"（对应 VASP 的 488 号错误）。另外若不去重也不报错地写出去，VASP 读到的原子数和元素段数都会错。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 567 | def op_supercell(st: Structure, scaling: list[int]) -> tuple[Structure, str]: | 函数入口 |
| 568 | """扩胞：scaling = [nx, ny, nz]，正整数。""" | docstring |
| 569 | if not (isinstance(scaling, (list, tuple)) and len(scaling) == 3): | 必须是长度 3 的列表或元组 |
| 570 | raise StructureError(f"supercell 需要 scaling=[nx,ny,nz]，收到 {scaling!r}") | 回显坏参数 |
| 571 | ns = [] | 校验后的三个正整数 |
| 572 | for x in scaling: | 逐个检查 |
| 573 | if not isinstance(x, int) or isinstance(x, bool) or x < 1: | 必须是 int、不是 bool、且 ≥1 |
| 574 | raise StructureError(f"supercell 的缩放必须是正整数，收到 {x!r}") | 0 或负数没有意义，浮点也不接受 |
| 575 | ns.append(x) | 收下合法的缩放 |
| 576 | if ns == [1, 1, 1]: | 1x1x1 等于没扩 |
| 577 | return st, "扩胞 1x1x1（无变化）" | 直接返回入参对象（内容没变，仍满足"不改入参"） |
| 579 | sp = st.atom_species() | 展开成逐原子元素列表；VASP4 会在这里抛错 |
| 580 | fracs = st.frac() | 统一用分数坐标做扩胞，Cartesian 模式也能处理 |
| 581 | flags = st.flags | 只是引用，后面按需复制 |
| 582 | vels = st.velocities | 同上 |
| 584 | new_lat_raw = [[ns[i] * x for x in st.lattice[i]] for i in range(3)] | 第 i 个晶格矢量乘 `ns[i]`；用 `st.lattice`（Å），新结构 scale 归一化 |
| 585 | new_frac: list[list[float]] = [] | 新分数坐标累加器 |
| 586 | new_sp: list[str] = [] | 逐原子元素累加器（注意：本函数后面并未使用它，属于冗余变量，见下方说明） |
| 587 | new_flags: list[list[bool]] \| None = [] if flags is not None else None | 有 flags 才准备新列表 |
| 588 | new_vels: list[list[float]] \| None = [] if vels is not None else None | 有速度才准备新列表 |
| 590–593 | # 先按元素分组，再在每个元素内部按 (i,j,k) 铺开，保证同元素连续。…（共 4 行注释） | 解释去重的必要性：不去重会把 Mo、S 各铺两遍，坐标数翻倍而 counts 只乘一次 → 488 号“坐标数量与计数不一致” |
| 594 | unique_species: list[str] = [] | 去重后的元素顺序 |
| 595 | for s in (st.species or []): | 按元素行顺序遍历；VASP4 时 `st.species` 是 None，`or []` 让循环空转（但前面 `atom_species()` 已经抛错了，走不到这里） |
| 596 | if s not in unique_species: | 第一次见到 |
| 597 | unique_species.append(s) | 只保留第一次出现 |
| 598 | for sym in unique_species: | **对去重后的元素**逐个铺，保证同元素连续 |
| 599 | base = [i for i, s in enumerate(sp) if s == sym] | 这个元素在原结构里的所有原子下标（保持原顺序） |
| 600 | for i in base: | 逐个原原子 |
| 601 | for a in range(ns[0]): | a 方向复制 |
| 602 | for b in range(ns[1]): | b 方向复制 |
| 603 | for c in range(ns[2]): | c 方向复制（最内层，所以 z 变化最快） |
| 604 | new_frac.append([ | 追加一个复制原子的分数坐标 |
| 605 | (fracs[i][0] + a) / ns[0], | 原分数坐标加平移量 a，再除以 nx：正好落在新晶胞的第 a 份里 |
| 606 | (fracs[i][1] + b) / ns[1], | b 方向同理 |
| 607 | (fracs[i][2] + c) / ns[2], | c 方向同理 |
| 608 | ]) | 一个坐标结束 |
| 609 | new_sp.append(sym) | 记录这个复制原子的元素（冗余，后面构造时没用） |
| 610 | if new_flags is not None: | 原结构有 flags 才复制 |
| 611 | new_flags.append(list(flags[i]))  # type: ignore[index] | 每个复制原子继承原原子的 3 个 T/F；`list()` 防止共享 |
| 612 | if new_vels is not None: | 原结构有速度才复制 |
| 613 | new_vels.append(list(vels[i]))   # type: ignore[index] | 每个复制原子继承原速度 |
| 615 | st2 = Structure( | 构造扩胞后的结构 |
| 616 | comment=st.comment, | 注释保留 |
| 617 | scale_raw=1.0, | 晶格已是 Å，scale 归一化 |
| 618 | lattice_raw=new_lat_raw, | 乘过倍数的晶格 |
| 619 | species=list(st.species or []), | 元素行沿用原顺序（读入时已保证不重复；这里不再去重） |
| 620 | counts=[n * (ns[0] * ns[1] * ns[2]) for n in st.counts], | 每个元素的计数乘以总复制倍数；与 605–607 行的坐标数必须一致，`set_frac` 会校验 |
| 621 | coords=[], | 占位 |
| 622 | coord_mode=st.coord_mode, | 保持原坐标模式 |
| 623 | selective=st.selective, | 保持 selective |
| 624 | flags=new_flags, | 复制后的 flags |
| 625 | velocities=new_vels, | 复制后的速度 |
| 626 | tail=list(st.tail), | 尾部行保留 |
| 627 | ) | 构造调用结束 |
| 628 | st2.set_frac(new_frac) | 写入 605–607 行算出的分数坐标；如果坐标数与 counts 不符，这里会抛错 |
| 629 | return st2, f"扩胞 {ns[0]}x{ns[1]}x{ns[2]}：{st.natoms} → {st2.natoms} 原子" | 返回新结构和一句说明 |

> 关于 `new_sp`（586、609 行）：它收集了每个复制原子的元素，但构造 `Structure` 时用的是 `st.species`（元素行）和 `st.counts`（计数），没有消费 `new_sp`。也就是说这是一个冗余变量，不影响结果；保留它可能只是写作时的中间产物。

### 6.4 op_substitute（632–645）

- 干什么：把第 `index` 个原子（1 基）换成新元素，并重新分组。
- 参数：`index: int`、`species: str`。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— 元素符号不合法；索引类型/范围不对；VASP4 结构。

**为什么这么写**：替换只改一个元素的标签，位置不变；改完必须 `_regroup`，否则新元素可能插在旧元素段中间。
**不这么写会怎样**：不重排就写文件，`Cu Cu Co Cu` 这种顺序会让 VASP 把最后一个 Cu 当成 Co 读（同元素不连续不报错），体系直接算错。另外，替换会改变原子编号（因为重新分组），所以函数说明里明确写了"已按首次出现顺序重新分组"。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 632 | def op_substitute(st: Structure, index: int, species: str) -> tuple[Structure, str]: | 函数入口 |
| 633 | """把第 index 个原子（1 基）换成新元素。""" | docstring |
| 634 | if not _ELEMENT_RE.match(species or ""): | 先校验新元素符号；`or ""` 防止 None 触发 TypeError |
| 635 | raise StructureError(f"元素符号不合法：{species!r}") | 回显坏符号 |
| 636 | idxs = _check_indices(index, st.natoms, "substitute 的 index") | 1 基 → 0 基并检查范围；返回的是列表 |
| 637 | sp = st.atom_species() | 展开逐原子元素；VASP4 在这里抛错 |
| 638 | fracs = st.frac() | 取当前分数坐标（位置不变，只是给 `_regroup` 用） |
| 639 | i = idxs[0] | 取第一个（也是唯一一个）索引；即使误传多元素列表，也只用第一个 |
| 640 | old = sp[i] | 记录旧元素，供说明文字使用 |
| 641 | if old == species: | 换成本来就是的元素 |
| 642 | return st, f"第 {index} 个原子本来就是 {species}（无变化）" | 返回入参对象，不产生新结构 |
| 643 | sp[i] = species | 只改逐原子元素列表里的这一项 |
| 644 | st2 = _regroup(st, sp, fracs, st.flags, st.velocities) | 重排元素段，同时把坐标/flags/速度一起带着走 |
| 645 | return st2, f"把第 {index} 个原子 {old} → {species}（已按首次出现顺序重新分组）" | 返回新结构与说明 |

### 6.5 op_substitute_all（648–674）

- 干什么：按元素批量替换，如把所有 Cu 换成 Ni。
- 参数：`mapping: dict[str, str]`，如 `{"Cu": "Ni"}`。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— `mapping` 不是非空 dict；键或值的元素符号不合法；键对应的元素在结构里不存在；VASP4 结构。

**为什么这么写**：逐个原子调用 `op_substitute` 容易漏、而且每次都要重排；这里一次映射、一次重排，并且先检查"要替换的元素是否存在"，避免拼错元素名却静默无事发生。
**不这么写会怎样**：如果允许替换不存在的元素，用户写错 `{"Cuu": "Ni"}` 会得到"成功但什么也没改"的结果，问题被掩盖；如果替换后不重排，同元素连续性又会被破坏。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 648 | def op_substitute_all(st: Structure, mapping: dict[str, str]) -> tuple[Structure, str]: | 函数入口 |
| 649–653 | """把某种元素**全部**换成另一种（掺杂/合金化最常见的说法："把所有 Cu 换成 Ni"）。…""" | docstring：说明与 `op_substitute` 的分工 |
| 654 | if not isinstance(mapping, dict) or not mapping: | 必须是非空 dict |
| 655 | raise StructureError("substitute_all 需要 mapping，如 {\"Cu\": \"Ni\"}") | 错误信息给示例 |
| 656 | norm: dict[str, str] = {} | 规范化后的映射（键值都转成 str） |
| 657 | for k, v in mapping.items(): | 逐个校验 |
| 658 | if not _ELEMENT_RE.match(str(k)) or not _ELEMENT_RE.match(str(v)): | 键和值都必须是合法元素符号 |
| 659 | raise StructureError(f"元素符号不合法：{k!r} → {v!r}") | 回显整个映射项 |
| 660 | norm[str(k)] = str(v) | 存进规范映射；保持原 dict 的插入顺序 |
| 661 | sp = st.atom_species() | 展开逐原子元素 |
| 662 | present = set(sp) | 当前结构里出现过的元素集合 |
| 663 | unknown = [k for k in norm if k not in present] | 找出"要替换但结构里没有"的键 |
| 664 | if unknown: | 有拼错/不存在的元素 |
| 665–667 | raise StructureError( f"结构里没有这些元素：{unknown}；当前结构元素：{sorted(present)}" ) | 同时列出缺失元素和当前元素，方便用户对照 |
| 668 | new_sp = [norm.get(s, s) for s in sp] | 逐原子替换；不在映射里的元素保持原样 |
| 669 | changed = sum(1 for a, b in zip(sp, new_sp) if a != b) | 统计真正变了的原子数 |
| 670 | if changed == 0: | 映射存在但没有任何原子变化（例如 Cu→Cu） |
| 671 | return st, "替换前后元素相同（无变化）" | 返回入参对象 |
| 672 | st2 = _regroup(st, new_sp, st.frac(), st.flags, st.velocities) | 一次重排完成全部替换 |
| 673 | pairs = "、".join(f"{k}→{v}" for k, v in norm.items()) | 拼说明文字，如 "Cu→Ni、Zn→Co" |
| 674 | return st2, f"批量替换 {pairs}，共 {changed} 个原子（已重新分组）" | 返回新结构与说明 |

### 6.6 op_vacancy（677–691）

- 干什么：删除一个或多个原子（造空位）。
- 参数：`indices: int | list[int]`，1 基。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— 索引类型/范围不对；删完一个原子都不剩；VASP4 结构。

**为什么这么写**：先用 `_check_indices` 统一成 0 基、再 `sorted(set(...))` 去重排序，保证"传 [3,1,1]"和"传 [1,3]"结果一致，说明文字里的编号也稳定升序。
**不这么写会怎样**：不去重时同一个原子会被删两次（第二次删的是错位的别人）；不排序时说明文字里的编号顺序随用户输入变化，日志不好对照；删空结构会让后续 `natoms=0`、体积/坐标操作失去意义，所以显式拒绝。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 677 | def op_vacancy(st: Structure, indices: int \| list[int]) -> tuple[Structure, str]: | 函数入口 |
| 678 | """删除一个或多个原子（造空位）。""" | docstring |
| 679 | idxs = _check_indices(indices, st.natoms, "vacancy/remove 的索引") | 1 基 → 0 基并检查范围；`what` 同时写了 vacancy/remove，方便用户从报错找到配方字段 |
| 680 | idxs = sorted(set(idxs)) | 去重 + 升序：删除操作对顺序不敏感，排序让结果确定 |
| 681 | sp = st.atom_species() | 展开逐原子元素 |
| 682 | fracs = st.frac() | 取分数坐标 |
| 683 | keep = [i for i in range(st.natoms) if i not in idxs] | 所有"保留"的原下标 |
| 684 | sp2 = [sp[i] for i in keep] | 保留原子的元素 |
| 685 | fr2 = [fracs[i] for i in keep] | 保留原子的坐标 |
| 686 | fl2 = [st.flags[i] for i in keep] if st.flags is not None else None | flags 跟着保留原子走 |
| 687 | vl2 = [st.velocities[i] for i in keep] if st.velocities is not None else None | 速度同理 |
| 688 | if not keep: | 一个都不剩 |
| 689 | raise StructureError("删除后没有原子了，拒绝生成空结构") | 显式拒绝，避免生成没有物理意义的 POSCAR |
| 690 | st2 = _regroup(st, sp2, fr2, fl2, vl2) | 重新分组：删除可能让某个元素计数变 0，`_regroup` 会按实际剩余的首次出现顺序生成元素行 |
| 691 | return st2, f"删除原子 {[i + 1 for i in idxs]}：{st.natoms} → {st2.natoms} 原子" | 说明文字用 1 基、升序的编号 |

### 6.7 op_add_atom（694–719）

- 干什么：往结构里加一个原子，位置给分数坐标或笛卡尔坐标（二选一）。
- 参数：`st`；`frac: list[float] | None`、`cart: list[float] | None`（二选一）；`species: str = "H"`；`fixed: bool = False`。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— 元素符号不合法；`frac`/`cart` 同时给或都不给；坐标不是 3 个数；VASP4 结构。

**为什么这么写**：先统一转成分数坐标 `f`，再走 `_regroup`，这样新原子会自动插到同元素段的末尾，保证元素连续；`cart` 转分数用 `inv3(st.lattice)`，和 `frac()` 的坐标约定一致。
**不这么写会怎样**：直接把笛卡尔坐标写进 `coords` 而不换算，在 Direct 模式下会被当成分数坐标读出，原子位置完全错；加完不重排，新元素会落在最后一个原子后面，同元素不再连续。

> **代码事实（参数名与实际效果相反，必须按代码讲）**：`fixed` 的值被直接写进 `flags`（715 行），而 `flags` 的约定是 `True`=T=**可动**。所以：
> - `fixed=True` → flags 为 `[True, True, True]` → 写出 `T T T`（**可动**）；
> - `fixed=False` → flags 为 `[False, False, False]` → 写出 `F F F`（**固定**）。
> 这与 `op_fix_atoms` 的 `value=True→flags=False` 正好相反，也与参数名 "fixed" 的直觉相反。另外，只有 `st.selective` 为 True 时才会写 flags；对没有 Selective dynamics 的结构，`fixed` 被完全忽略（不会自动启用选择性弛豫）。本仓库 `tests/` 下没有 `op_add_atom` 的测试，`fixed` 分支实际未被测试执行，作者本意**未确认**；本文只陈述代码行为。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 694 | def op_add_atom(st: Structure, frac: list[float] \| None = None, | 函数入口（签名跨 3 行） |
| 695 |                 cart: list[float] \| None = None, species: str = "H", | 第二行：笛卡尔坐标、元素（默认 H） |
| 696 |                 fixed: bool = False) -> tuple[Structure, str]: | 第三行：fixed 标志，返回二元组 |
| 697 | """加一个原子。位置给分数坐标或笛卡尔坐标二选一。""" | docstring |
| 698 | if not _ELEMENT_RE.match(species or ""): | 校验元素符号；`or ""` 防 None |
| 699 | raise StructureError(f"元素符号不合法：{species!r}") | 回显坏符号 |
| 700 | if (frac is None) == (cart is None): | 异或式检查：两者同为 None 或同不为 None 都不行 |
| 701 | raise StructureError("add_atom 必须给 frac 或 cart 之一（不能都给/都不给）") | 错误信息把两种非法情况都写出来 |
| 702 | if frac is not None: | 分支一：给的是分数坐标 |
| 703 | if len(frac) != 3: | 必须是 3 个数 |
| 704 | raise StructureError(f"frac 需要 3 个数，收到 {frac!r}") | 回显坏值 |
| 705 | f = [float(x) for x in frac] | 转成 float 并复制，避免外部列表影响内部 |
| 706 | else: | 分支二：给的是笛卡尔坐标 |
| 707 | if len(cart) != 3:  # type: ignore[arg-type] | 必须是 3 个数；`# type: ignore` 是给类型检查器看的（此处 `cart` 一定非 None） |
| 708 | raise StructureError(f"cart 需要 3 个数，收到 {cart!r}") | 回显坏值 |
| 709 | f = vec_mat([float(x) for x in cart], inv3(st.lattice))  # type: ignore[arg-type] | 笛卡尔（Å）→ 分数：`f = r @ inv(lattice)`；用物理晶格（已乘 scale），与 `frac()` 的约定一致 |
| 711 | sp = st.atom_species() + [species] | 逐原子元素列表末尾加新元素；VASP4 在这里抛错 |
| 712 | fracs = st.frac() + [f] | 原有分数坐标（可能由 Cartesian 转换而来）加新原子 |
| 713 | flags = None | 默认不加 flags |
| 714 | if st.selective: | 原结构有选择性弛豫时，新原子也得有 3 个 T/F，否则行数对不上 |
| 715 | flags = [list(x) for x in (st.flags or [])] + [[fixed, fixed, fixed]] | 复制旧 flags，再给新原子加一项；`fixed` 直接进 flags（语义反转见上方提示） |
| 716 | vels = [list(v) for v in st.velocities] + [[0.0, 0.0, 0.0]] if st.velocities is not None else None | 原结构有速度时，新原子速度补 0；条件表达式作用于整个拼接结果 |
| 717 | st2 = _regroup(st, sp, fracs, flags, vels) | 重排元素段，新原子落到同元素段末尾；坐标/flags/速度一起重排 |
| 718 | pos = "分数 " + " ".join(_fmt(x) for x in f) | 把新原子的分数坐标拼成说明文字 |
| 719 | return st2, f"加原子 {species}（{pos}）：{st.natoms} → {st2.natoms} 原子" | 返回新结构与说明 |

### 6.8 op_strain（722–750）

- 干什么：给晶格加均匀应变/形变。
- 参数：`matrix: list[list[float]]`（3x3）；`mode: str = "delta"`（`"delta"` 增量 / `"absolute"` 绝对形变梯度）。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— 矩阵不是 3x3（外层必须是 list，行可以是 list/tuple）；`mode` 不是 delta/absolute。

**为什么这么写**：`delta` 模式下 `F = I + matrix`，用户写 0.02 就是 +2%，符合"加 2% 应变"的日常说法；`absolute` 模式直接给 F，方便从外部工具拿形变梯度。新晶格按行向量约定算 `L' = L @ F^T`，与全文的 `r = f @ L` 保持一致。
**不这么写会怎样**：若把 `L' = F @ L`（列向量写法）直接搬过来，晶格会转置/错位，结构看起来"也能跑"但物理上被镜像或剪切方向反了；`delta` 模式若忘记加单位阵，0.02 会被当成 2% 的绝对形变，晶格缩小 50 倍。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 722 | def op_strain(st: Structure, matrix: list[list[float]], mode: str = "delta") -> tuple[Structure, str]: | 函数入口 |
| 723–728 | """加应变/形变。…新晶格 L' = L @ F^T（行向量约定下，等价于每个晶格矢量左乘 F）。""" | docstring：解释 delta/absolute 两种语义与矩阵方向 |
| 729 | if not (isinstance(matrix, list) and len(matrix) == 3 | 校验第一层：必须是长度 3 的 list（元组不行） |
| 730 |         and all(isinstance(r, (list, tuple)) and len(r) == 3 for r in matrix)): | 校验每一行：长度 3 的 list/tuple |
| 731 | raise StructureError(f"strain 需要 3x3 矩阵，收到 {matrix!r}") | 回显坏矩阵 |
| 732 | M = [[float(x) for x in row] for row in matrix] | 复制并转 float，避免后面修改调用方的矩阵 |
| 733 | if mode not in ("delta", "absolute"): | 只认两种模式 |
| 734 | raise StructureError(f"strain.mode 只能是 delta/absolute，收到 {mode!r}") | 回显坏模式 |
| 735 | F = [[M[i][j] + (1.0 if i == j else 0.0) for j in range(3)] for i in range(3)] \ | delta 模式：在 M 的对角线上加 1，得到 F = I + M；行尾 `\` 表示下一行是本语句的继续 |
| 736 |     if mode == "delta" else M | absolute 模式直接用 M；三元表达式跨两行写 |
| 737 | base = st.lattice  # 用 Å 基（scale 已乘进去），新结构统一 scale=1.0 | 取物理晶格，输出结构 scale 归一化 |
| 738 | # L' = L @ F^T，即 L'[i][j] = sum_k L[i][k] * F[j][k] | 给出 739 行实现的数学式 |
| 739 | new_raw = [[sum(base[i][k] * F[j][k] for k in range(3)) for j in range(3)] for i in range(3)] | `L'[i][j] = Σ_k L[i][k]·F[j][k]`，即 `L' = L @ F^T`；每个晶格矢量被 F 变换 |
| 740 | st2 = Structure( | 构造新结构 |
| 741 | comment=st.comment, scale_raw=1.0, lattice_raw=new_raw, | 注释保留、scale 归一化、晶格用新值 |
| 742 | species=st.species, counts=list(st.counts), | 元素行沿用；计数复制一份（数量不变） |
| 743 | coords=[], coord_mode=st.coord_mode, selective=st.selective, | 坐标占位、模式保持、selective 保持 |
| 744 | flags=[list(f) for f in st.flags] if st.flags is not None else None, | flags 深复制：新对象不应和旧对象共享列表 |
| 745 | velocities=[list(v) for v in st.velocities] if st.velocities is not None else None, | 速度同样深复制 |
| 746 | tail=list(st.tail), | 尾部行复制 |
| 747 | ) | 构造调用结束 |
| 748 | st2.set_frac(st.frac()) | 分数坐标保持不变：原子随晶格一起形变（仿射变换），这是"加均匀应变"的定义 |
| 749 | tag = "增量" if mode == "delta" else "绝对" | 说明文字用中文标签 |
| 750 | return st2, f"应变（{tag}，{_fmt(F[0][0])}/{_fmt(F[1][1])}/{_fmt(F[2][2])} 对角）：体积 {st.volume():.3f} → {st2.volume():.3f} Å³" | 说明里给出 F 的对角元（delta 模式下是 1.02 这种"含 1"的值）和体积变化 |

### 6.9 op_vacuum（753–812）

- 干什么：沿某个晶格矢量方向加真空层（slab 常用）。
- 参数：`axis: int = 2`（0/1/2）、`thickness: float = 15.0`（Å）、`place: str = "bottom"`（bottom/center）。
- 返回：`(Structure, str)`；新结构 `coord_mode` 固定为 `"cartesian"`。
- 异常：`StructureError` —— axis 不是 0/1/2；thickness ≤ 0；place 不是 bottom/center；该方向晶格矢量长度为 0。

**为什么这么写**：先算"原子在轴方向上的投影跨度 extent"，只有 `gap = clen - extent` 小于要求厚度时才拉长晶格并把原子平移；这样用户重复调用不会把已有的 20 Å 真空压成 15 Å。
**不这么写会怎样**：无条件按 `thickness` 重设长度，会把大盒子压小、原子整体平移，用户以为"只是加了真空"实际晶格变了；另外不区分 bottom/center 的平移公式会让原子最低点落在负数或真空不对称。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 753 | def op_vacuum(st: Structure, axis: int = 2, thickness: float = 15.0, | 函数入口（签名跨 2 行） |
| 754 |               place: str = "bottom") -> tuple[Structure, str]: | 第二行：放置方式，默认贴底 |
| 755–761 | """沿某个晶格矢量方向加真空层（适合 slab）。…""" | docstring：做法、斜晶胞的近似性、以及"严格做法不在本工具范围" |
| 762 | if axis not in (0, 1, 2): | 轴只能是 0/1/2（对应 a/b/c） |
| 763 | raise StructureError(f"vacuum 的 axis 只能是 0/1/2，收到 {axis!r}") | 回显坏轴 |
| 764 | if thickness <= 0: | 厚度必须为正 |
| 765 | raise StructureError("vacuum 的厚度必须 > 0") | 0 或负数没有意义 |
| 766 | if place not in ("bottom", "center"): | 只认两种放置方式 |
| 767 | raise StructureError("vacuum.place 只能是 bottom/center") | 错误信息只列出合法取值 |
| 769 | L = st.lattice | 物理晶格（Å） |
| 770 | c = L[axis] | 目标方向的晶格矢量 |
| 771 | clen = vec_len(c) | 该矢量长度（旧盒子在这个方向上的"总长"） |
| 772 | if clen < 1e-8: | 退化晶格（长度近似 0） |
| 773 | raise StructureError("该方向晶格矢量长度为 0") | 无法定义"沿这个方向加真空" |
| 774 | n = vec_scale(c, 1.0 / clen) | 单位方向向量 |
| 775 | cart = st.cart() | 所有原子的笛卡尔坐标（Å） |
| 776 | proj = [sum(p[i] * n[i] for i in range(3)) for p in cart] | 每个原子在 n 方向上的投影（标量） |
| 777 | lo, hi = min(proj), max(proj) | 投影的最小值/最大值 |
| 778 | extent = hi - lo | 原子在目标方向上的跨度 |
| 779 | gap = clen - extent | 现有"真空"大小 = 盒子长度 - 原子跨度 |
| 780–782 | # 已经是“至少这么厚”就保持原样，不要为了凑数把用户的大盒子压小。…（共 3 行注释） | 解释 783 行为什么先判 gap：否则会把原本 20 Å 的真空压到 15 Å，晶格变小、原子整体平移 |
| 783 | if gap >= thickness - 1e-9: | 已有真空已经够厚（1e-9 是浮点容差） |
| 784 | return st, f"沿第 {axis + 1} 个矢量已有真空层 {gap:.2f} Å ≥ 要求 {thickness:g} Å（保持不变）" | 原样返回入参对象（内容不变，仍满足"不改入参"）；说明用 1 基轴号 |
| 785 | new_len = extent + thickness | 新盒子在目标方向上的总长 |
| 786 | factor = new_len / clen | 只拉长目标矢量所需的倍数 |
| 788 | base = [list(r) for r in st.lattice]   # Å 基，新结构统一 scale=1.0 | 复制物理晶格作为基础 |
| 789 | new_raw = [row[:] for row in base] | 再浅复制每一行，准备只改一行 |
| 790 | new_raw[axis] = vec_scale(new_raw[axis], factor) | 只把目标方向的晶格矢量乘 factor；方向不变，所以斜胞的另两个矢量夹角也不变 |
| 791 | L2 = [row[:] for row in new_raw] | 再复制一份，后面算新方向单位向量 |
| 792 | # 新矢量方向不变；重算投影，平移使最低点落在 0（或居中） | 说明 793–799 行在重算投影和计算平移量 |
| 793 | n2 = vec_scale(L2[axis], 1.0 / vec_len(L2[axis])) | 新矢量方向的单位向量（方向其实与 n 相同，重新算一遍更直观） |
| 794 | proj2 = [sum(p[i] * n2[i] for i in range(3)) for p in cart] | 用新方向重算原子投影 |
| 795 | lo2, hi2 = min(proj2), max(proj2) | 新投影的最小/最大值 |
| 796 | if place == "bottom": | 贴底放：最低原子投影落到 0 |
| 797 | shift = -lo2 | 平移量 = 最低点的相反数 |
| 798 | else: | 居中放 |
| 799 | shift = (new_len - (hi2 - lo2)) / 2.0 - lo2 | 让最低点落在 (new_len - 原子跨度)/2 处，即上下真空各一半 |
| 800 | cart2 = [vec_add(p, vec_scale(n2, shift)) for p in cart] | 所有原子沿新方向整体平移 shift |
| 802 | st2 = Structure( | 构造新结构 |
| 803 | comment=st.comment, scale_raw=1.0, lattice_raw=new_raw, | 注释保留、scale 归一化、晶格用拉长后的值 |
| 804 | species=st.species, counts=list(st.counts), | 元素与计数不变 |
| 805 | coords=[], coord_mode=st.coord_mode, selective=st.selective, | 坐标占位、模式先沿用、selective 保持 |
| 806 | flags=[list(f) for f in st.flags] if st.flags is not None else None, | flags 深复制 |
| 807 | velocities=[list(v) for v in st.velocities] if st.velocities is not None else None, | 速度深复制（整体平移不改变速度，保留是正确的） |
| 808 | tail=list(st.tail), | 尾部行复制 |
| 809 | ) | 构造调用结束 |
| 810 | st2.coord_mode = "cartesian" | 结果统一用笛卡尔模式（平移后的位置直接以 Å 给出，避免再转分数） |
| 811 | st2.coords = cart2 | 直接塞入笛卡尔坐标；这里不用 `set_frac`，因为 `cart2` 已经是 Å、不是分数坐标 |
| 812 | return st2, f"沿第 {axis + 1} 个矢量加真空 {thickness:g} Å（{place}）：长度 {clen:.3f} → {vec_len(L2[axis]):.3f} Å" | 说明里给出拉长前后的长度 |

> 关于斜晶胞：代码沿"目标晶格矢量的单位方向"投影，若 a、b 在该方向上有分量，`extent` 里会混入这些分量，实际真空厚度只是近似值。docstring（759–760 行）已经声明这一点，六方/正交胞则是精确的。

### 6.10 op_fix_atoms（815–845）

- 干什么：用 Selective dynamics 固定/放开指定原子的某些方向。
- 参数：`indices: int | list[int]`（1 基）、`axes: str = "123"`（由 `1`/`2`/`3` 组成的字符串）、`value: bool = True`（True=固定，False=放开）。
- 返回：`(Structure, str)`；结果一定带 `selective=True`。
- 异常：`StructureError` —— `axes` 为空或含非 1/2/3 字符；索引类型/范围不对；VASP4 结构。

**为什么这么写**：VASP 的 T/F 语义是"T=允许动，F=固定"，而用户/配方说的是"固定这些原子"；所以参数 `value=True` 表示"固定"，内部必须写 `flags=False`（835 行 `not value`）。`axes` 兼容旧的 `"111"`：把"同一个字符重复多次"解释成"三个方向全固定"。
**不这么写会怎样**：直接把 `value` 写进 flags（`value=True→True`）会写出 `T T T`，表面原子照常乱跑，能量对不上；把 `"111"` 当成"x 方向三次"则会重复设置同一个方向、另两个方向没被固定。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 815 | def op_fix_atoms(st: Structure, indices: int \| list[int], axes: str = "123", | 函数入口（签名跨 2 行） |
| 816 |                  value: bool = True) -> tuple[Structure, str]: | 第二行：`value=True` 表示"固定" |
| 817–822 | """固定指定原子在某些方向上（Selective dynamics）。…""" | docstring：强调 VASP 的 T/F 与"固定"是反的，参数 value 的语义是"是否固定" |
| 823 | if not axes or any(ch not in "123" for ch in axes): | `axes` 不能为空，且每个字符只能是 1/2/3 |
| 824 | raise StructureError(f"axes 只能由 1/2/3 组成，例如 '123'（三个方向）或 '12'（前两个方向），收到 {axes!r}") | 错误信息给两个正例，降低用户试错成本 |
| 825–826 | # 兼容旧写法/模型习惯：'111' 这种“同一个方向重复三次”按“三个方向全固定”处理。…（共 2 行注释） | 解释 827–828 行规范化的来由 |
| 827 | if len(axes) > 1 and len(set(axes)) == 1: | 旧写法/模型习惯：所有字符相同（如 "111"、"222"） |
| 828 | axes = "123" | 规范成"三个方向全固定"；真正只固定 x 应该写 "1"，不会写成 "111" |
| 829 | idxs = _check_indices(indices, st.natoms, "fix_atoms 的索引") | 1 基 → 0 基并检查范围 |
| 830 | fracs = st.frac() | 取当前分数坐标，最后原样写回以保持位置 |
| 831 | flags = [list(f) for f in st.flags] if st.flags is not None else [[True, True, True] for _ in range(st.natoms)] | 已有 flags 就深复制；没有就默认全 True（可动），再按 axes 改 |
| 832 | for i in idxs: | 逐个目标原子 |
| 833 | for ax in axes: | 逐个方向字符 |
| 834 | # value=True 表示"固定"，POSCAR 里写 F（允许移动=T，固定=F） | 解释 835 行 `not value` 的语义反转 |
| 835 | flags[i][int(ax) - 1] = not value | 把字符 "1"/"2"/"3" 转成 0/1/2 下标；`not value` 是 T/F 反转的关键：固定→False(F)，放开→True(T) |
| 836 | st2 = Structure( | 构造新结构 |
| 837 | comment=st.comment, scale_raw=1.0, lattice_raw=[list(r) for r in st.lattice], | 注释保留、scale 归一化、晶格复制 |
| 838 | species=st.species, counts=list(st.counts), coords=[], | 元素沿用、计数复制、坐标占位 |
| 839 | coord_mode=st.coord_mode, selective=True, flags=flags, | 强制开启 selective；flags 用上面改好的 |
| 840 | velocities=[list(v) for v in st.velocities] if st.velocities is not None else None, | 速度深复制 |
| 841 | tail=list(st.tail), | 尾部行复制 |
| 842 | ) | 构造调用结束 |
| 843 | st2.set_frac(fracs) | 原分数坐标写回：本操作只改约束，不改位置；同时触发长度校验 |
| 844 | verb = "固定" if value else "放开" | 说明文字用词跟着 value 走 |
| 845 | return st2, f"{verb}原子 {[i + 1 for i in idxs]}（方向 {axes}），已启用 Selective dynamics" | 说明里用 1 基编号和规范化后的 axes |

### 6.11 op_reorder_species（848–862）

- 干什么：按给定元素顺序重排原子，通常是为了和 POTCAR 顺序一致。
- 参数：`order: list[str]`，必须是当前元素集合的一个排列（不多、不少、不重复）。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— `order` 与现有元素集合不一致；VASP4 结构（`atom_species()` 抛错）。

**为什么这么写**：直接构造"按 order 分组的新原子序列"再交给 `_regroup`，元素顺序和同元素连续性一步到位；先用 `sorted(order) == sorted(present)` 做集合相等检查，能同时挡住"漏元素"和"多元素"。
**不这么写会怎样**：不校验就让用户传 `["S"]`，`_regroup` 会按实际出现的元素重新生成元素行，用户以为"只留 S"实际上只是把 S 排到前面、Mo 仍在后面；不重排 flags/速度则约束会跟错原子。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 848 | def op_reorder_species(st: Structure, order: list[str]) -> tuple[Structure, str]: | 函数入口 |
| 849 | """按给定元素顺序重排原子（为了和 POTCAR 顺序一致）。""" | docstring |
| 850 | sp = st.atom_species() | 逐原子元素列表 |
| 851 | present = [s for s in dict.fromkeys(sp)] | 按首次出现顺序去重（`dict.fromkeys` 去重且保序，外层列表推导只是转成 list） |
| 852 | if sorted(order) != sorted(present): | 多重集相等：order 必须恰好覆盖现有元素，且不能重复 |
| 853–856 | raise StructureError( f"reorder_species 的 order 必须恰好覆盖现有元素。\n" f"现有：{present}\n收到：{order}" ) | 同时列出"现有"和"收到"，方便用户对照 |
| 857 | idxs = [i for sym in order for i, s in enumerate(sp) if s == sym] | 按 order 依次收集每个元素的全部原子下标；这决定了新原子顺序 |
| 858 | fracs = st.frac() | 取分数坐标 |
| 859 | st2 = _regroup(st, [sp[i] for i in idxs], [fracs[i] for i in idxs], | 用重排后的元素列表和坐标调 `_regroup`；它再按首次出现顺序确认 order |
| 860 |                [st.flags[i] for i in idxs] if st.flags is not None else None, | flags 跟着同一个 `idxs` 重排 |
| 861 |                [st.velocities[i] for i in idxs] if st.velocities is not None else None) | 速度同理 |
| 862 | return st2, f"按 {order} 重排元素（原子编号全部变化）" | 说明里提醒"原子编号全部变化"，用户按编号引用原子时要注意 |

### 6.12 op_translate（865–886）

- 干什么：整体平移所有原子。
- 参数：`vector: list[float]`（3 个数）；`mode: str = "direct"`（`"direct"` 分数坐标增量 / `"cart"` 笛卡尔增量 Å）。
- 返回：`(Structure, str)`。
- 异常：`StructureError` —— `vector` 不是长度 3 的 list/tuple；`mode` 不是 direct/cart；Cartesian 模式下 `inv3` 失败也会抛。

**为什么这么写**：先统一成"分数坐标增量" `dv`（cart 模式用 `v @ inv(lattice)` 换算），再统一加到分数坐标上，两种模式只差一步换算。
**不这么写会怎样**：在 Direct 模式里直接加 Å 值，平移量会被晶格长度放大/缩小成错误比例；换算时若用 `lattice_raw` 而不是乘过 scale 的 `lattice`，scale≠1 的文件会平移错。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 865 | def op_translate(st: Structure, vector: list[float], mode: str = "direct") -> tuple[Structure, str]: | 函数入口 |
| 866 | """整体平移。mode=direct 时 vector 是分数坐标增量，cart 时是 Å。""" | docstring |
| 867 | if not (isinstance(vector, (list, tuple)) and len(vector) == 3): | 必须是长度 3 的 list/tuple |
| 868 | raise StructureError(f"translate 需要 3 个数，收到 {vector!r}") | 回显坏值 |
| 869 | v = [float(x) for x in vector] | 转 float 并复制 |
| 870 | fracs = st.frac() | 取当前分数坐标 |
| 871 | if mode == "cart": | 分支一：笛卡尔增量 |
| 872 | dv = vec_mat(v, inv3(st.lattice)) | Å → 分数增量：`dv = v @ inv(lattice)`，用物理晶格 |
| 873 | elif mode == "direct": | 分支二：分数增量 |
| 874 | dv = v | 直接用 |
| 875 | else: | 未知模式 |
| 876 | raise StructureError("translate.mode 只能是 direct/cart") | 说明里没有回显 mode（可以改进，但不影响正确性） |
| 877 | st2 = Structure( | 构造新结构 |
| 878 | comment=st.comment, scale_raw=1.0, lattice_raw=[list(r) for r in st.lattice], | 注释/晶格复制，scale 归一化 |
| 879 | species=st.species, counts=list(st.counts), coords=[], | 元素/计数不变 |
| 880 | coord_mode=st.coord_mode, selective=st.selective, | 坐标模式、selective 保持 |
| 881 | flags=[list(f) for f in st.flags] if st.flags is not None else None, | flags 深复制 |
| 882 | velocities=[list(vv) for vv in st.velocities] if st.velocities is not None else None, | 速度深复制（整体平移不改变速度） |
| 883 | tail=list(st.tail), | 尾部行复制 |
| 884 | ) | 构造调用结束 |
| 885 | st2.set_frac([vec_add(f, dv) for f in fracs]) | 每个原子加同一个分数增量；结果可能超出 [0,1)，需要时再用 `op_wrap` 折回 |
| 886 | return st2, f"整体平移 {v}（{mode}）" | 说明文字里的 `{v}` 是 Python 列表的 repr，例如 `[0.1, 0, 0]` |

### 6.13 op_wrap（889–900）

- 干什么：把所有分数坐标折回 `[0, 1)`。
- 参数：`st`。
- 返回：`(Structure, str)`。
- 异常：无主动异常；VASP4 不受影响（本操作不需要元素信息）。

**为什么这么写**：`x % 1.0` 对正数取小数部分，对负数会得到正余数（Python 的 `%` 与 C 不同），所以 `-0.2` 会变成 `0.8`，正好落在 `[0,1)`。
**不这么写会怎样**：用 `x - floor(x)` 也能实现，但用 C 风格取余（如 `math.fmod`）会让负坐标仍是负的，写出的 POSCAR 坐标可能远离 `[0,1)`；不过 VASP 本身接受任意分数坐标，折回主要是为了让 diff 干净、方便人看。

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 889 | def op_wrap(st: Structure) -> tuple[Structure, str]: | 函数入口 |
| 890 | """把所有分数坐标折回 [0, 1)。""" | docstring |
| 891 | st2 = Structure( | 构造新结构 |
| 892 | comment=st.comment, scale_raw=1.0, lattice_raw=[list(r) for r in st.lattice], | 注释/晶格复制，scale 归一化 |
| 893 | species=st.species, counts=list(st.counts), coords=[], | 元素/计数不变 |
| 894 | coord_mode=st.coord_mode, selective=st.selective, | 坐标模式、selective 保持 |
| 895 | flags=[list(f) for f in st.flags] if st.flags is not None else None, | flags 深复制 |
| 896 | velocities=[list(v) for v in st.velocities] if st.velocities is not None else None, | 速度深复制（wrap 不改变速度） |
| 897 | tail=list(st.tail), | 尾部行复制 |
| 898 | ) | 构造调用结束 |
| 899 | st2.set_frac([[x % 1.0 for x in f] for f in st.frac()]) | 每个坐标分量对 1 取模；Python 负数取模结果非负，正好折回 [0,1) |
| 900 | return st2, "所有原子折回 [0,1) 分数坐标" | 返回新结构与说明 |

### 6.14 文件末尾的解析流程注释（903–906）

| 行 | 代码（原文，去掉行首缩进） | 说明 |
| --- | --- | --- |
| 903 | # 解析流程（给读代码的人）： | 注释标题 |
| 904 | #   第 1 行 comment → 第 2 行 scale → 3~5 行晶格 → 第 6 行元素（或原子数） | 前半段流程速查 |
| 905 | #   → （可选）Selective dynamics → 坐标模式 → natoms 行坐标(+T/F) | 中段流程速查 |
| 906 | #   → （可选）natoms 行速度 → 尾部原样保留 | 末段流程速查；与第 4 节的解析流程一致 |

## 7. 一次扩胞的真实数据流

用一个 2 个化学式单元的 MoS₂ 胞演示：6 个原子（2 Mo + 4 S），`Direct` 模式，晶格取 a = 3.16 Å、b = (−1.58, 2.7367, 0) Å、c = 12.3 Å（体积约 106.3701 Å³）。示例里的数值是用本文件代码实际跑出来的。

原始 POSCAR（节选）：

```text
MoS2 2 formula units
1.0
3.16 0.0 0.0
-1.58 2.7367 0.0
0.0 0.0 12.3
Mo S
2 4
Direct
0.000000 0.000000 0.250000
0.500000 0.500000 0.750000
0.333333 0.666667 0.250000
0.833333 0.166667 0.250000
0.166667 0.333333 0.250000
0.666667 0.833333 0.250000
```

`read_poscar_text` 读完后得到：

| 字段 | 值 |
| --- | --- |
| `species` | `["Mo", "S"]` |
| `counts` | `[2, 4]` |
| `natoms` | 6 |
| `coords` 长度 | 6 |
| `coord_mode` | `"direct"` |
| 体积 | 约 106.3701 Å³ |
| 是否触发 `_regroup` | 否（元素行没有重复符号） |

然后 `recipe.apply_ops` 调到 `op_supercell(st, [2, 2, 1])`，数据流如下：

| 步骤 | 发生在哪 | 数据变化 |
| --- | --- | --- |
| 1. 参数校验 | 569–575 行 | `[2,2,1]` 是长度 3 的正整数，通过；不是 `[1,1,1]`，继续 |
| 2. 取逐原子元素 | 579 行 `st.atom_species()` | `["Mo", "Mo", "S", "S", "S", "S"]` |
| 3. 取分数坐标 | 580 行 `st.frac()` | 6 条，就是原文件里的 6 行 |
| 4. 元素去重 | 594–597 行 | `["Mo", "S"]`（若不去重，下一步会把 Mo、S 各铺两遍） |
| 5. 新晶格 | 584 行 | a 行 ×2 → `[6.32, 0, 0]`；b 行 ×2 → `[-3.16, 5.4734, 0]`；c 行 ×1 → `[0, 0, 12.3]` |
| 6. 铺 Mo | 598–613 行 | Mo 有 2 个原子，每个复制 2×2×1 = 4 份，共 8 个原子；每个原子内部按 a、b、c 三重循环，z 方向变化最快 |
| 7. 铺 S | 598–613 行 | S 有 4 个原子，每个 4 份，共 16 个原子 |
| 8. 计数 | 620 行 | `counts = [2×4, 4×4] = [8, 16]` |
| 9. 元素行 | 619 行 | 仍为 `["Mo", "S"]`（去重后的顺序，与 counts 对齐） |
| 10. 坐标写入 | 628 行 `st2.set_frac(new_frac)` | 24 条分数坐标；`set_frac` 校验 24 == 8+16，通过 |
| 11. 返回说明 | 629 行 | `"扩胞 2x2x1：6 → 24 原子"` |

扩胞前后对照：

| 指标 | 扩胞前 | 扩胞后 |
| --- | --- | --- |
| `species` | `["Mo", "S"]` | `["Mo", "S"]`（顺序不变） |
| `counts` | `[2, 4]` | `[8, 16]` |
| `natoms` | 6 | 24 |
| `coords` / `frac()` 长度 | 6 | 24 |
| 晶格 | a、b、c | a×2、b×2、c×1 |
| 体积 | 约 106.3701 Å³ | 约 425.4804 Å³（正好 4 倍） |
| `flags` / `velocities` 长度（若原结构带） | 6 | 24（每个复制原子继承原原子的那一份） |
| 原子顺序 | Mo₁ Mo₂ S₁ S₂ S₃ S₄ | Mo₁ 的 4 份、Mo₂ 的 4 份、S₁ 的 4 份、S₂ 的 4 份、S₃ 的 4 份、S₄ 的 4 份 |

扩胞后前 8 条分数坐标（验证"每个 Mo 复制 4 份、元素连续"）：

| 新序号 | 来源 | 分数坐标 |
| --- | --- | --- |
| 1 | Mo₁ 的 (a=0,b=0) | (0.00, 0.00, 0.25) |
| 2 | Mo₁ 的 (a=0,b=1) | (0.00, 0.50, 0.25) |
| 3 | Mo₁ 的 (a=1,b=0) | (0.50, 0.00, 0.25) |
| 4 | Mo₁ 的 (a=1,b=1) | (0.50, 0.50, 0.25) |
| 5 | Mo₂ 的 (a=0,b=0) | (0.25, 0.25, 0.75) |
| 6 | Mo₂ 的 (a=0,b=1) | (0.25, 0.75, 0.75) |
| 7 | Mo₂ 的 (a=1,b=0) | (0.75, 0.25, 0.75) |
| 8 | Mo₂ 的 (a=1,b=1) | (0.75, 0.75, 0.75) |

写回时 `write_poscar_text` 的动作：第 2 行写 `1.0`；3 行晶格写 Å；元素行 `Mo  S`；计数行 `8  16`；`Direct`；再写 24 行坐标（前 8 行如上表）。因为原结构没有重复元素，读回时也不会触发 `_regroup`。

整个过程中一直成立的不变量：

```text
st.natoms == sum(st.counts) == len(st.coords) == len(st.frac())
         == (len(st.flags) 若 selective) == (len(st.velocities) 若带速度)
```

扩胞、替换、删除、加原子、重排之后都要重新满足这个等式；`set_frac`（205 行）和 `_regroup`（563 行）是最后一道校验。

---

> 全文核对说明：本文的 11 个 `op_*`、行列号均以 `vasp_core/poscar.py`（906 行）为准；"12 个 op"的说法与当前源码不符，已在第 1、6 节注明。`op_add_atom` 的 `fixed` 参数语义与参数名相反、`op_supercell` 的 `new_sp` 未被使用，这两处按代码实际行为记录，作者本意未确认。

