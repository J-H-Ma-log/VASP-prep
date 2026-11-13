# 版本历史（v1 → v4）

> v2 / v3 当时没有单独存档，代码是连续演进的，**无法还原成独立快照**；
> 这里按功能阶段如实记录"每一版做了什么、为什么改、怎么验证"。
> Git 里只有当前代码的提交与 `v4.0.0` 标签；往后每改一次就多一个提交。

## 文档补充 — 逐行讲代码 + 逻辑树（2026-11-12/13）

**新增**

- `docs\逐行讲代码\`：10 份文档，按真实行号逐文件/逐函数/逐行讲解（约 9,600 行），附索引 README。
- 每份文档开头新增一张 ASCII **逻辑树**（03–09）：先看调用关系，再看逐行表。
- `00-总表与一次真实执行.md` 的轨迹来自 `sys.settrace` 真实执行（0.182 s / 10,831 行事件 / 1,628 次调用）。

**说明**

- 写作期间源码零改动；发现的 12 处"代码与注释/预期不一致"记录在索引 README 第 7 节，未修（改源码会移动行号）。
- 验证：`python -m unittest discover -s tests` → 167 项全部通过。

## v4.0.0 — 批量 + 数据库（2026-11-09）

**新增**

- 批量页/命令：一个根目录下每个材料各生成一套输入；支持参数扫描（IVDW）。
  - `scan_materials()` 只读扫描；`preview_batch()` 先算目标路径不建目录；`write_batch()` 逐项落盘。
  - 输出：`batches\<批次>\<材料>\ivdw_<值>\`，附 `batch_summary.csv`、`batch_report.md`。
  - 单项失败只标红该项；源目录只读、mtime 不变；上限 200 材料 / 500 目录（可配）。
- 数据库页/命令：MP、OQMD 免 key 检索与下载（OPTIMADE），AFLOW 给链接，2Dmatpedia 走整库缓存，
  ICSD / Materials Cloud / 中科院二维铁电库走"官网导出 → 导入 CIF/POSCAR/zip"。
  - 下载目录 `materials\<provider>_<id>\{POSCAR,SOURCE.json}`。
- 统一入口 `python -m vasp_core <webui|chat|prep|batch|db>`：只做转发，减少"记模块名"的负担。
- 受限模式兜底：进程被降权时无法启动资源管理器，改为网页内目录浏览（`/api/ls` + `#dirPanel`）。

**修复**

- 批量组合漏项（`zip(good, targets)` 只产出材料数项）→ 改为按目标清单逐个配对。
- 下载候选的 URL 为空、OPTIMADE 返回结构差异、zip 内 POSCAR 无扩展名等兼容问题。
- CIF 导入对 pymatgen 的依赖给出明确报错，而不是抛 ImportError。

**验证**

- `python -m unittest discover -s tests` → 163 项全部通过。
- 真实网络：MP 检索 `MoS2` → `mp-1238797` → 下载 → pymatgen 读回 `MoS2, 3 atoms, 56.94 Å³`。
- 真实 HTTP：批量 2 材料 × 2 参数 = 4 项预览/写入；`runs` 目录列表可读。

## v3.0.0 — 批量处理（2026-11-09）

- 新增 `batch.py`：`scan_materials` / `preview_batch` / `write_batch`，以及网页「批量」页。
- 批量"同一句话只理解一次"，所有材料共用同一份 ops，避免逐材料调用模型造成前后不一致。
- 命名：批次名可留空（`batch_<日期-时间>_<关键词>`），同名自动 `_2`，不覆盖。
- 预览表暴露"将要生成"的完整路径，解决"文件到底写哪去了"。

## v2.0.0 — 单文件前处理（2026-11-08）

**核心**

- `poscar.py`：无损读写（保留 Selective 标记、速度、注释），12+ 个结构操作，操作后校验
  `natoms == sum(counts) == len(fracs)`。
- `incar.py`：42 个常用键的类型表、白名单、预设（`relax/static/band/none`）、危险键拒绝、一致性检查。
- `kpoints.py`：手动网格 / 长度 / Monkhorst-Pack / 线模式四种。
- `recipe.py`：配方 v2 schema，未知字段一律报错（防止"写错键名却以为生效"）。
- `prep.py`：命令行，默认**只试算**，`--write` 才落盘；只写新目录。
- `nl.py` + `llm.py`：中文 → 配方；本地 Ollama 不可用时自动退规则模式；模型只输出 JSON。

**入口**

- 网页 `webui.py`（127.0.0.1:8765，三页签），终端 `chat.py`，双击 bat 启动。
- 输出名可自定义，固定落在 `runs\` 下，重名自动 `_2`。

**修复过的真实 bug（都用真实 POSCAR 复现过）**

- 元素行写成 `Mo S Mo S` 时，扩胞把元素铺两遍 → 坐标数翻倍（8748 vs 4374）。现在读取时归一化，扩胞按去重元素铺。
- `fix_atoms` 的 T/F 写反：T 才是"可动"，旧代码把"固定"写成 T → 表面会乱跑。
- `vacuum` 会把已有大真空压小 → 改为"只加不减"。
- 速度块与 ASE 不兼容（少了空行导致 ASE 断言失败）→ 写出时保留空行、按原子数对齐。
- 纯结构编辑重新排版 INCAR，产生整片假 diff → 无改动时原样复制。

## v1.0.0 — 验证过的雏形（2026-11-06 ~ 11-07）

- `超算大模型部署包\`：分步手册、`vasp_prep_lite.py`、`validate.py`、llama.cpp 提示词与 GBNF。
- 验证过：POSCAR 读写、12 个操作、INCAR 白名单、POTCAR 拼接与 TITEL 校验。
- 已知问题（v2 全部重写）：Selective/速度丢失、缩放系数处理、POTCAR 只认一种命名。

---

## 版本号怎么定

- 主版本（v4）：能力范围变化（单文件 → 批量 → 数据库）。
- 次版本：同一范围内加操作/加页面。
- 修订：只修 bug，不改行为与输出路径。
