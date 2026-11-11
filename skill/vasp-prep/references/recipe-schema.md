# recipe 字段说明（vasp-prep-v2）

> **何时读**：要写/改一个 recipe、要确认某个 op 的参数名、或要判断 INCAR 覆盖键是否被允许时。
> 目标读者是调用工具的模型和使用命令行的用户；字段名以 `vasp_core/recipe.py` 与
> `vasp_core/incar.py` 的源码为准，本文件解释语义与常见坑。

recipe 是一个 UTF-8 编码的 JSON 对象。未知顶层字段会被**拒绝**（不是忽略），
避免拼错键名后"看起来成功、实际没生效"。

## 1. 顶层字段

| 字段 | 必需 | 类型 | 说明 |
|---|---|---|---|
| `version` | 否 | 2 | 目前只支持 2（缺省按 2 处理；写别的值报错） |
| `name` | 否 | 字符串 | 算例名，默认取文件名（不含 `.json`） |
| `calc` | 否 | `relax`/`scf`/`static`/`band` | 决定默认 INCAR preset：relax→relax，scf/static→static，band→band |
| `base_dir` | 否 | 字符串 | 结构文件与 `incar.base` 的基准目录；默认 `.`（**相对 recipe 文件所在目录**解析） |
| `structure` | **是** | 对象 | 见第 2 节 |
| `incar` | 否 | 对象 | 见第 3 节 |
| `kpoints` | 否 | 对象 | 见第 4 节；不写则用 KSPACING 或默认 |
| `potcar` | 否 | 对象 | 见第 5 节 |
| `notes` | 否 | 字符串/对象 | 只作备注，不参与计算 |

> 工具层收到"配方对象"时，如果 `base_dir` 是 `.` 或缺省，会改成**当前工作目录**；
> 收到"配方文件路径"时保持"相对 recipe 文件"的语义。

## 2. structure

```json
"structure": {
  "file": "POSCAR",
  "ops": [
    {"op": "supercell", "scaling": [2, 2, 2]},
    {"op": "substitute", "index": 3, "species": "Al"}
  ]
}
```

- `file`：默认 `POSCAR`，相对 `base_dir`。文件必须是 VASP5 格式（有元素行），
  否则 `read_structure` 可以读，但涉及元素的操作会失败；VASP4 请先用 `--elements`/脚本补元素行。
- `ops`：按**书写顺序**依次执行；顺序会改变结果（先扩胞再替换 ≠ 先替换再扩胞）。
- 原子编号 `index` 一律 **1 基**，与 `read_structure` 返回的 `index` 一致。
- 每个 op 执行失败时，错误信息会带 `structure.ops[i](op 名)` 定位。

### 2.1 常规 op（纯 Python 实现，无需 pymatgen/ase）

| op | 必填参数 | 可选参数 | 作用 / 坑 |
|---|---|---|---|
| `supercell` | `scaling`: 3 个正整数 | — | 扩胞；体积按乘积放大，原子顺序为"原胞内顺序 × 各方向重复" |
| `substitute` | `index`(1 基), `species` | — | 替换单个原子；`index` 是**当前**结构的原子号，前面 op 会改变它 |
| `substitute_all` | `mapping`: `{旧元素: 新元素}` | — | 按元素整体替换；mapping 里必须都是现有元素 |
| `vacancy` | `indices`: 整数或整数数组 | `index`（单个别名） | 删原子；一次删多个时序号都按**删除前**结构计算 |
| `add_atom` | `species` | `frac` 或 `cart`（**二选一**）、`fixed` | 加一个原子；两个位置都给/都不给会报错；新原子放在最后一个元素组 |
| `strain` | `matrix`: 3×3 | `mode`: `delta`(默认)/`absolute` | delta 是增量（0.02=+2%，内部 F=I+M），absolute 直接给 F；新晶格 L'=L·Fᵀ |
| `vacuum` | — | `axis`: 0/1/2（默认 2）、`thickness`: Å（默认 15）、`place`: `bottom`/`center` | 沿晶格矢量加真空；斜晶胞只是近似，六方/正交精确 |
| `fix_atoms` | `indices`: 整数或数组 | `axes`: `"111"`/`"12"` 等（默认 `"111"`）、`value`: bool（默认 true） | 写 Selective dynamics；`axes` 里的 1/2/3 对应 a/b/c 方向 |
| `reorder_species` | `order`: 元素数组 | — | 必须**恰好覆盖**现有元素；原子编号全部变化，后面 op 的 index 要重算 |
| `translate` | `vector`: 3 个数 | `mode`: `direct`(默认，分数)/`cart`(Å) | 整体平移，不改晶格 |
| `wrap` | — | — | 把分数坐标折回 [0,1)；对 slab 真空层无影响，但便于比较 |

### 2.2 扩展 op（需要 pymatgen / ase，环境没装就会失败）

| op | 必填 | 可选 |
|---|---|---|
| `surface` | `miller` | `layers`, `vacuum`, `center`, `min_slab_size`, `fix_layers` |
| `adsorbate` | `species` | `site`, `position`, `height` |
| `fix_layers` | — | `n_layers`, `tol`, `axes` |
| `ml_relax` | — | `fmax`, `steps`, `cell_mask`, `model`, `device` |

旧写法 `slab` 已废弃（会映射到 `surface`，但请直接用 `surface`）。
**切面/吸附位点选择属于领域决策**：工具不会替你选，Skill 也会把它列进 `needs_human`。

## 3. incar

```json
"incar": {
  "preset": "relax",
  "base": "INCAR.old",
  "overrides": {"ENCUT": 600, "EDIFFG": -0.03, "ISMEAR": 0, "SIGMA": 0.05}
}
```

- `preset`：`relax`（默认）/`static`/`band`，也可由 `calc` 推导；三套内置值见
  `vasp_core/incar.py` 的 `PRESETS`。
- `base`：可选底稿 INCAR（相对 `base_dir`）。存在时在底稿上合并：preset 覆盖底稿的
  同名键，`overrides` 再覆盖 preset；不存在时报错而不是静默忽略。
- `overrides`：只允许 **`ALLOWED_OVERRIDE_KEYS`**（= `INCAR_TYPES` 去掉危险键）。
  危险键（如 `LDAU*`、`NELECT`、`NELMIN`、`LHFCALC`、`AEXX`、`HFSCREEN`、`METAGGA`、
  `IVDW*`、`VDW_*`、`LUSE_VDW`、`GGA`）**不能**通过配方覆盖；需要时手工编辑 INCAR，
  并在回复里明确说明"这是人工改动，不在工具校验范围内"。
- 值会按类型与范围校验：如 `ENCUT` 50–3000、`EDIFF` 1e-9–1、`ISMEAR` ∈ {-5,-4,-1,0,1,2}、
  `IBRION` ∈ {-1,0,1,2,3,5,6,7,8,44}、`ISIF` 0–7。完整表以源码 `INCAR_TYPES` 为准。

## 4. kpoints

```json
"kpoints": {"mode": "mp", "mesh": [8, 8, 8]}
```

- `gamma`：Γ 点，`mesh` 仍要 3 个 1–64 的整数（通常 1,1,1）。
- `mp`：Monkhorst-Pack 网格，`mesh` 3 个 1–64 整数。
- `line`：能带路径，`segments` 至少 1 段，每段：
  `{"from": [x,y,z], "to": [x,y,z], "points": 20, "label_from": "Γ", "label_to": "X"}`。
  所有段的 `points` 必须相同且 ≥2。
- `explicit`：显式点 `points: [{"coords": [x,y,z], "weight": 1.0, "label": "..."}]`，
  weight ≥0。

注意：写了 `KPOINTS` 就不要再在 INCAR 里写 `KSPACING`（工具会报 ERROR）。

## 5. potcar

```json
"potcar": {"dir": "~/Optpaw_PBE.54", "mode": "spec", "map": {"Cu": "Cu_pv"}}
```

- `dir`：赝势库目录；相对 `base_dir`，也支持 `~`。解析优先级：
  CLI `--pp-dir` > recipe 的 `potcar.dir` > 环境变量 `PMG_VASP_PSP_DIR` / `VASP_PP_PATH`。
- `mode`：
  - `spec`（推荐默认）：只写 `POTCAR.spec` 记录元素→TITEL 映射，超算上再拼真 POTCAR；
  - `auto`：本机能找到赝势就拼 `POTCAR`，找不到降级为 spec 并给 WARN；
  - `skip`：完全不碰赝势（不扫描目录、不写 spec），适合只调结构时。
- `map`：可选，指定某个元素用哪个 variant（如 `Cu` → `Cu_pv`）；不写时按
  `_pv` > 默认 > `_sv` > `_d` … 的优先级选。元素缺失或映射不中会报错。

## 6. 最小示例与完整示例

最小（只扩胞 + 改 ENCUT，赝势只记录 spec）：

```json
{
  "version": 2,
  "name": "cu_2x2x2",
  "calc": "relax",
  "base_dir": "D:/work/cu",
  "structure": {"file": "POSCAR", "ops": [{"op": "supercell", "scaling": [2, 2, 2]}]},
  "incar": {"overrides": {"ENCUT": 600}},
  "kpoints": {"mode": "mp", "mesh": [6, 6, 6]},
  "potcar": {"mode": "spec"}
}
```

完整（底稿 INCAR + 固定底层 + 真空层 + 赝势映射）：

```json
{
  "version": 2,
  "name": "cu_slab",
  "calc": "relax",
  "base_dir": "D:/work/cu",
  "structure": {
    "file": "POSCAR",
    "ops": [
      {"op": "surface", "miller": [1, 1, 1], "layers": 4, "vacuum": 15},
      {"op": "fix_atoms", "indices": [1, 2, 3], "axes": "111"}
    ]
  },
  "incar": {
    "preset": "relax",
    "base": "INCAR.old",
    "overrides": {"ENCUT": 520, "EDIFFG": -0.02, "ISMEAR": 0, "SIGMA": 0.05}
  },
  "kpoints": {"mode": "mp", "mesh": [9, 9, 1]},
  "potcar": {"dir": "~/Optpaw_PBE.54", "mode": "spec", "map": {"Cu": "Cu_pv"}}
}
```

## 7. 常见错误对照

| 报错关键词 | 原因 | 处理 |
|---|---|---|
| `未知顶层字段` | 顶层键拼错/多写 | 对照第 1 节，删掉或改名 |
| `version 必须是 2` | 用了 v1 配方 | 迁移格式或删掉 version |
| `structure.ops[i](...) 执行失败` | op 参数错/索引越界/元素不存在 | 看括号里的 op 名，按第 2 节改 |
| `未知 op` | op 名拼错或用了已废弃名字 | 用第 2 节表中的名字 |
| `不允许覆盖` / `危险键` | overrides 写了 LDAU 等 | 移出 overrides，人工处理 |
| `KSPACING 与 KPOINTS 冲突` | 两者同时存在 | 删一个 |
| `ENCUT < POTCAR ENMAX` | 截断能低于赝势推荐 | 提高 ENCUT 或换赝势 |
| `ISPIN=2 但 MAGMOM 数量不匹配` | 自旋极化没给对磁矩 | 给 MAGMOM（个数=原子数）或关 ISPIN |
| `文件不存在` | `base_dir`/`file`/`base` 路径错 | 用 `workspace_status` 确认真实路径 |
