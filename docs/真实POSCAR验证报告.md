# 用真实 POSCAR 扫出来的 6 个 bug（v2.2 → v2.3）

> 目的：把"VASP 不报错、但结果已经算错"的坑逐个堵死，并留下可复验的证据。
>
> 真实样本：`C:\Users\28476\Desktop\test\MoS2\POSCAR`（1076 B，6 原子，元素行 `Mo S Mo S`，
> 计数 `1 2 1 2`，`Selective dynamics`，缩放系数 3.18013，坐标后有速度块）。
> 用户网页上报的错：
> `structure.ops[1](supercell) 执行失败：坐标数量 8748 与元素计数 [729, 1458, 729, 1458]（共 4374 个原子）不一致`

---

## 1. 元素行重复（Mo S Mo S）导致扩胞铺两遍

- **现象**：9×9×9 扩胞时坐标 8748 条，而计数只乘了 729，两边对不上。
- **根因**：`op_supercell` 旧实现 `for sym in st.species` 逐个元素铺原子；
  `species` 里 `Mo`/`S` 各出现两次（`["Mo","S","Mo","S"]`），于是每种元素被铺了两遍。
- **为什么危险**：VASP 读 POSCAR 时不检查"坐标条数是否等于计数和"，
  多出来的坐标会被当成下一段读取或直接读乱——算完是另一个体系，且没有任何报错。
- **修复**：
  1. `op_supercell` 先对元素去重再铺；
  2. `read_poscar` 读到重复元素时立刻归一化（`Mo S Mo S / 1 2 1 2` → `Mo S / 2 4`），
     因为 VASP 的 POTCAR 是按元素行逐段对应的，4 段 POSCAR 必须配 4 段 POTCAR，
     不合并会与 `POTCAR.spec`（2 段）对不上；
  3. 归一化时同步重排原子并带走 `Selective` 标志与速度。

## 2. 固定原子写反：T/F 语义

- **现象**：要求"固定底部两层"，实际写成了 `T T T`（允许移动），表面原子照跑。
- **根因**：VASP 语义是 **T = 允许移动，F = 固定**；旧实现把"固定"直接写成 `T`。
- **修复**：`op_fix_atoms(value=True)` 现在写 `F`；`axes` 约定从 `"111"` 改为 `"123"`
  （`"111"` 会被理解成"只固定 x 三遍"），并兼容旧写法（三个相同数字 → 视为三方向）。
- **测试**：`test_fix_atoms_sets_selective` 断言 `[True, True, False]`，
  `test_axes_legacy_111_means_all_three` 断言旧写法等价。

## 3. 已有真空被"压小"

- **现象**：对已经有 15.5 Å 真空的 slab 再要求 5 Å 真空，晶格 c 被压短，原子挨到边界。
- **根因**：旧实现无条件把该方向长度设为 `原子跨度 + 要求厚度`。
- **修复**：先算现有真空 `gap = c - 跨度`；`gap >= thickness` 时原样返回并提示"保持不变"，
  只有不足时才加高。**真空只会变多，不会变少。**

## 4. 速度块与 ASE 不兼容

- **现象**：带速度的 POSCAR 被 ASE 读时 `AssertionError`（`ase/io/vasp.py:311`）。
- **根因**：
  - 读：旧 reader 只看坐标后第一行，真实文件这里是空行，速度被当"尾部文本"原样保留，
    扩胞后速度行数仍是 6（原子已 24），数量不匹配；
  - 写：速度块前没有空行时，ASE 的 `read_vasp` 会先读一行判断 `Lattice velocities`，
    把第 1 个原子速度吞掉，随后 EOF。
- **修复**：reader 跳过空行/`Velocities` 标题行后再按原子数读；
  writer 在速度块前**留一个空行**，且只在 `len(velocities) == natoms` 时才写。

## 5. INCAR "假 diff"

- **现象**：只改了 `ENCUT` 一个键，但网页 diff 整篇飘红（缩进、行尾空格全变）。
- **根因**：`render_incar` 把所有行统一重排成 `KEY = VALUE`。
- **修复**：没改过的行逐字节保留原文（含行尾空格、末尾空行）；改过的行只替换数值，
  保留原缩进与行内注释（`   ENCUT = 520  # 说明`）。现在 diff 恰好一行。

## 6. `Kpoints.from_recipe` 不存在

- **现象**：调用 `Kpoints.from_recipe(...)` 报 `AttributeError`。
- **修复**：正确入口是模块函数 `kpoints_from_recipe(cfg)` + `render_kpoints(kp)`；
  验证脚本已改。

---

## 四层验证（都可重复跑）

| 层 | 命令 | 结果 |
|---|---|---|
| 固定用例 | `python -m unittest discover -s tests` | **99 项全过** |
| 逐操作不变量 | `.tmp_build\validate_mos2.py`（外部验证脚本） | **137 PASS / 0 FAIL** |
| 随机操作序列 | `chgnet_env\python\python.exe tools\fuzz_poscar.py <POSCAR> 200` | **200 轮 / 517 个操作全过** |
| 第三方反向读 | `.tmp_build\validate_with_ecosystem.py`（pymatgen + ASE） | **13 PASS / 0 FAIL** |
| 端到端 | `.tmp_build\e2e\verify_e2e.py` | **16 PASS / 0 FAIL** |

端到端验证的两条关键结论（真实样本）：

1. **纯结构编辑（扩胞+空位+真空+固定层）**：23 原子、`Mo7S16`、
   4 个原子 `F F F`、19 个 `T T T`、真空 15.5 Å；**INCAR 与源文件逐字节相同**（SHA256 一致）。
2. **只改 ENCUT**：520 生效、`ISMEAR` 等未动、`INCAR.diff` 只有一行改动。

## 仍未覆盖的边界（别当已经验证）

- **VASP 实算没跑**：本机没有 VASP，验证的是"文件正确"，不是"能量正确"；
- **POTCAR 拼接**只在本机假赝势库上验证过顺序逻辑，真实 `~/Optpaw_PBE.54` 需你在集群上跑一次
  `python -m vasp_core.prep --potcar-only <目录> --pp-dir ~/Optpaw_PBE.54` 复核；
- **切表面 / 吸附质**依赖 pymatgen/ASE 后端，需 `chgnet_env` 解释器；系统 Python 上会明确报错而不是瞎算；
- **超大体系**（>5000 原子）只测到 4374 原子读写的量级，未做长时间压力测试。
