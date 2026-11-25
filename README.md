# vasp-prep

用一句中文改 VASP 的**前处理**输入（POSCAR / INCAR / KPOINTS）。本机运行，**不算能量、不碰 POTCAR、不改源文件**。

> **版本 2.2（2026-11-08）**：主入口变成**网页**（`启动网页.bat`）与终端（`python -m vasp_core.chat`）。
> 一条链路：中文 → 模型（本地 Ollama，可关）挑操作 → 程序在**新目录**里生成
> `POSCAR / INCAR / KPOINTS / INCAR.diff / change_report.md / recipe.json` → 你看 diff → 写盘 → 自己上传超算。
>
> 早期版本里的 POTCAR 拼接、ML 预弛豫、收敛核验、`cluster.py` 上传等能力**已移出主链路**
> （代码仍保留，见 §9 与 §11，按需使用）。

---

## 0. 先看这几份文档

| 文档 | 回答什么问题 |
|---|---|
| [`docs/架构与流程.md`](docs/架构与流程.md) | **代码怎么跑**：四层架构、主流程图、函数级调用链、出错看哪层 |
| [`docs/代码流程图.md`](docs/代码流程图.md) | **纯流程图**：模块依赖、启动、预览/写入时序、批量/数据库分支（Mermaid 图） |
| [`docs/小白也能看懂的程序流程.md`](docs/小白也能看懂的程序流程.md) | **零基础版**：用比喻 + 图示讲"一句话怎么变成算例目录" |
| [`docs/逐行讲代码/`](docs/逐行讲代码/README.md) | **逐行版**：10 份文档，按真实行号讲每个文件/函数/每一行，附一次真实执行的 trace |
| [`docs/网页使用.md`](docs/网页使用.md) | 怎么双击启动、界面每个控件干什么、模型怎么接、常见报错怎么办 |
| [`docs/工作流.md`](docs/工作流.md) | 模型和工具各干什么、一次需求经过哪几步、五道闸门、配置改哪里 |
| [`CHANGELOG.md`](CHANGELOG.md) | v1→v4 每一版做了什么、修了什么 |
| [`docs/Git与PyCharm新手.md`](docs/Git与PyCharm新手.md) | 不会 git 也能用：存档/回退/标签/推到 Gitee，以及 PyCharm 配置 |

统一入口（不用记模块名）：

```bash
python -m vasp_core webui      # 网页
python -m vasp_core chat       # 终端
python -m vasp_core batch -h   # 批量
python -m vasp_core db -h      # 数据库
```


---

## 1. 它做什么、不做什么

| 能做（自动） | 不能做（必须人工，或另找工具） |
| --- | --- |
| 扩胞、替换原子、批量换元素、空位、加原子、应变、真空层、固定原子、重排元素 | DFT+U（LDAU*）、杂化泛函、vdW、NELECT —— 一律拒绝自动写 |
| **切表面（任意 Miller 指数）**、加吸附质、固定底层 —— 需 pymatgen/ase，随 `chgnet_env` 自带 | POTCAR 内容生成（本工具**不生成** POTCAR，由你在超算按元素顺序拼） |
| 改 INCAR 的 41 个已知键（ENCUT/EDIFF/ISMEAR/SIGMA/ISIF/IBRION/MAGMOM/IVDW…） | 结构弛豫 / 收敛测试 / 磁序设计（可选 `ml.py` 预弛豫，默认关闭） |
| 生成 Gamma / Monkhorst-Pack / Line-mode / 显式 KPOINTS | 自动提交作业、上传下载、判读结果（`verify.py` 保留但不在网页链路） |
| 读现有 INCAR 保注释、只改指定键，出 unified diff | 物理正确性判断 |

结构类操作是**无损**的：Selective dynamics 的 T/F、速度块、注释行、尾部内容都会原样保留。

---

## 2. 目录结构

```
vasp-prep/
├── 启动网页.bat              # 双击启动（自动找 chgnet_env，开浏览器）
├── CHANGELOG.md              # v1→v4 版本历史
├── .gitignore                # 运行产物不进版本库
├── webui_launcher.py         # 嵌入式 Python 的垫片（把项目根塞进 sys.path）
├── web/index.html            # 网页（单文件，无框架）
├── vasp-prep.toml            # 配置：路径 / 模型 / 端口 / 隐私档
├── vasp_core/
│   ├── __main__.py           # 统一入口：python -m vasp_core webui|chat|prep|batch|db
│   ├── errors.py             # 异常 + 退出码
│   ├── poscar.py             # 读写 POSCAR + 15 个结构操作（无损）
│   ├── incar.py              # 类型表、白名单、预设（含 none）、变更表、一致性
│   ├── kpoints.py            # 四种 KPOINTS
│   ├── potcar.py             # 赝势库扫描/拼接/TITEL 校验（可选，不在主链路）
│   ├── recipe.py             # 配方 v2 校验 + 按顺序执行 ops
│   ├── report.py             # change_report.md / INCAR.diff / 审计日志
│   ├── prep.py               # 命令行入口（dry-run 默认）
│   ├── nl.py                 # 中文规则翻译（不用模型）
│   ├── llm.py                # 探测 Ollama / 调 chat / 约束 JSON / trace
│   ├── service.py            # 预览与写入的共用逻辑（网页与终端都调它）
│   ├── batch.py              # 批量：扫描材料 / 参数扫描 / 批次统计（v3）
│   ├── db.py                 # 数据库检索与下载：MP/OQMD/AFLOW/2Dmatpedia（v4）
│   ├── paths.py              # 统一目录规则：runs/batches/materials/imports/cache
│   ├── webui.py              # 本地网页服务（127.0.0.1:8765）
│   ├── chat.py               # 终端交互版
│   ├── config.py             # 读 vasp-prep.toml + 环境变量覆盖
│   ├── ml.py / verify.py     # 可选：CHGNet 预弛豫、OUTCAR 判收敛（默认不启用）
│   ├── cluster.py            # 可选：scp/qsub 上传提交（默认不启用）
│   └── backends/             # 可选后端：pymatgen（切面/对称性）、ase（吸附/弛豫）
├── tools.py / agent.py / vasp_mcp/   # 可选：OpenAI tools 与 MCP 服务
├── skill/vasp-prep/SKILL.md  # 给编码智能体看的技能说明（可选）
├── llm/                      # llama.cpp 方案（可选，网页方案已改用 Ollama）
├── tests/                    # 160 项 unittest
├── eval/                     # 50 句中文评测（规则层）
├── runs/                     # 单文件输出（运行时创建）
├── batches/                  # 批量输出（运行时创建）
├── materials/                # 数据库下载 / 导入的结构（运行时创建）
├── imports/                  # 手动导入区（选项，可完全不用）
├── cache/                    # 2Dmatpedia 等检索缓存（运行时创建）
└── docs/                     # 架构与流程、网页使用、工作流、批量处理、数据库下载、Git与PyCharm新手
```

---

## 3. 三分钟上手

无需安装任何东西，只要 Python ≥ 3.8：

```bash
cd vasp-prep
python -m vasp_core.prep --help
```

准备一个目录（里面放 POSCAR，可选放旧 INCAR）：

```
myrun/
├── POSCAR
├── INCAR          # 可选，作为底稿
└── recipe.json
```

`recipe.json` 最小例子：

```json
{
  "version": 2,
  "name": "cu_relax",
  "calc": "relax",
  "base_dir": ".",
  "structure": {
    "file": "POSCAR",
    "ops": [
      {"op": "supercell", "scaling": [2, 2, 1]},
      {"op": "substitute", "index": 3, "species": "Co"}
    ]
  },
  "incar": {"overrides": {"ENCUT": 520, "ISMEAR": 0, "SIGMA": 0.05}},
  "kpoints": {"mode": "gamma", "mesh": [4, 4, 1]},
  "potcar": {"dir": "~/Optpaw_PBE.54"}
}
```

先看会改什么（不写盘）：

```bash
python -m vasp_core.prep myrun/recipe.json
```

确认后写到一个**新目录**：

```bash
python -m vasp_core.prep myrun/recipe.json --write --out runs/cu_relax
```

---

## 4. 全部结构操作（按配方顺序依次执行）

| op | 参数 | 说明 |
| --- | --- | --- |
| `supercell` | `scaling: [a,b,c]` | 扩成 a×b×c |
| `substitute` | `index`, `species` | 第几个原子（1 基）换成什么 |
| `substitute_all` | `mapping: {"Cu":"Ni"}` | 按元素批量替换 |
| `vacancy` | `indices: [3]` | 删原子 |
| `add_atom` | `frac` 或 `cart`, `species`, `fixed`(可选) | 加一个原子 |
| `strain` | `matrix`(3×3), `mode: delta/absolute` | delta 是"+2%"；absolute 是绝对形变梯度 |
| `vacuum` | `axis`, `thickness`, `place` | 加真空（center 两侧各一半） |
| `fix_atoms` | `indices`, `axes: [true,true,true]` | 打开 Selective dynamics |
| `reorder_species` | `order: ["Cu","Co"]` | 重排元素顺序（记得同步 POTCAR！） |
| `translate` | `vector`, `mode: frac/cart` | 平移 |
| `wrap` | — | 把所有原子折回原胞 |

**顺序很重要**：索引指的是"执行到这一步时的结构"。
"扩成 2x2x1，第 3 个原子换成 Co" = 先扩胞、再换第 3 个；反过来会换到另一个原子上，而 VASP 不会报错。

---

## 5. 中文两条路

### 路线 A：规则层（不用模型，推荐先跑这个）

```bash
python -m vasp_core.nl "扩成 2x2x1，第 3 个原子换成 Co，截断能 520"
python -m vasp_core.nl "..." --base myrun/recipe.json --out myrun/recipe2.json
```

覆盖 20 类固定句式；识别不了的、以及切表面/固定层/DFT+U 这类，会明确列出"必须人工"。
`eval/run_eval.py` 用 50 句真实说法评测：**当前 50/50 通过**。

### 路线 B：本地小模型（可选，跑不通 A 的句子时）

1. 按 `超算大模型部署包/00_分步手册.md` 在超算编好 llama.cpp 与 7B 模型；
2. 放到 `~/soft/llm/`，然后：

```bash
python llm/nl2recipe.py "把第 3 个原子换成 Co，扩成 2x2x1" \
  --model ~/soft/models/qwen2.5-coder-7b-instruct-q4_k_m.gguf \
  --pp-dir ~/Optpaw_PBE.54 --out recipes/run1.json
```

内部流程：提示词 + **GBNF 约束解码**（保证语法合法）→ 括号配平取 JSON →
用 `prep.py --json` 做真校验 → 通过才写出配方；不通过会把模型原文留在 `.tmp_nl/` 里供排查。

---

## 6. 部署（超算 / 无网 / 无 pip）

```bash
# 1) 把整个目录拷上去
scp -r vasp-prep user@ln01:~/

# 2) 直接跑（不需要装包、不需要 pymatgen）
ssh user@ln01
cd ~/vasp-prep-v2
python3 -m vasp_core.prep --help

# 3) 用 TSCE(PBS) 提交
qsub vasp.sh
```

如果集群 Python 是 3.6 以下（本仓库用到 `from __future__ import annotations`，实际要求 3.8+），
就在计算节点用 module 加载 3.10（你的集群已有 `~/soft/Python-3.10`）。

---

## 7. 安全设计（为什么敢让它改文件）

1. **只写新目录**：输出目录已存在 → 退出码 5，绝不覆盖；`--force` 才覆盖。
2. **白名单**：INCAR 只接受 41 个有类型定义的键；LDAU*/NELECT/泛函等 16 个危险键一律拒绝（退出码 3）；`IVDW` 只有你点名色散时才写。
3. **写前校验**：12 条一致性规则（ISPIN/MAGMOM、ISMEAR=-5 与 NSW、ENCUT 区间、POTCAR 顺序…）。
4. **必须给 diff**：每个输出目录都有 `INCAR.diff` 与 `change_report.md`（结构变化、操作清单、检查结果）。
5. **可追溯**：默认往 `~/.vasp-prep/audit.jsonl` 追加一条记录（不可写时降级为提示，不阻塞）。

退出码：`0` 成功 / `2` 输入错误 / `3` 策略禁止 / `4` 结构错误 / `5` 目录冲突 / `6` 校验失败 / `7` 模型调用失败。

---

## 8. 测试与评测

```bash
# 1) 固定用例（纯标准库，99 项；不需要 pymatgen/ase）
python -m unittest discover -s tests

# 2) 中文理解的评测集（规则层，50 句，阈值 90%）
python eval/run_eval.py

# 3) 随机找坑：用真实 POSCAR 跑 200 轮随机操作序列（需要带 pymatgen 的解释器）
chgnet_env\python\python.exe tools\fuzz_poscar.py "C:\Users\28476\Desktop\test\MoS2\POSCAR" 200
```

第 3 项检查的是"VASP 不会报错、但结果已经算错"的那些点：
原子数==计数和==坐标数、元素不重复且连续、写读往返无损、pymatgen 能独立读回。
真实 MoS2 POSCAR 上用这套扫出来的 6 个 bug 与修复记录见
[docs/真实POSCAR验证报告.md](docs/真实POSCAR验证报告.md)。

---

## 9. 能力边界（诚实清单）

- `substitute` 的 `index` 是**当前结构**的 1 基序号，不是"元素内第几个"；
- 不做 slab / 表面钝化 / 对称性标准化（这些要么人工，要么接 pymatgen 可选后端）；
- POTCAR 只拼接与校验顺序，不生成内容；真正的赝势文件必须在集群上（你有 `~/Optpaw_PBE.54`）；
- 7B 模型在"没见过的说法"上仍会漏字段——所以永远先 `--json` 校验、再看 diff。

---

## 10. 与 v1（`超算大模型部署包`）的区别

v1 的 `vasp_prep_lite.py` 是单文件原型，已知会丢 Selective/速度、scale≠1 时坐标换算错、不支持负 scale、
MAGMOM 写成 Python 列表。v2 是重写：无损读写、类型系统、12 条校验、diff/审计、44 项测试。
v1 仍可用于最小演示，正式用 v2。

---

## 11. 本地化工作流：Windows/mac 前处理 + 超算只负责算

推荐用这个分工，而不是把模型/脚本全搬到超算：

```
Windows/mac（有网、有界面）                    超算（Linux + VASP + TSCE/PBS）
  中文/配方                                     只做三件事：
    ↓ vasp_core.nl / llm/nl2recipe.py            1) 拼 POTCAR（读 POTCAR.spec + 赝势库）
  recipe.json                                     2) qsub vasp.sh
    ↓ vasp_core.prep --write                      3) qstat 看状态
  runs/cu/{POSCAR,INCAR,KPOINTS,POTCAR.spec,
           INCAR.diff,change_report.md}
    ↓ python -m vasp_core.cluster up
  scp + ssh ───────────────────────────────────▶  ~/vasp-work/cu/
                                                    ↓ 自动 --potcar-only
                                                  POTCAR（顺序已校验）
                                                    ↓ qsub
                                                  作业号
```

### 三条命令

```bash
# 0) 一次性：把工具拷到超算（之后不再传）
scp -r vasp-prep me@ln01:~/vasp-prep-v2

# 1) 本机生成（不写 POTCAR 内容，只写 POTCAR.spec）
python -m vasp_core.prep recipes/cu.json --write --out runs/cu --potcar-mode spec

# 2) 上传 + 拼 POTCAR + 提交（一条命令）
python -m vasp_core.cluster up runs/cu --host me@ln01 --pp-dir ~/Optpaw_PBE.54

# 3) 看状态 / 拉回结果
python -m vasp_core.cluster status --host me@ln01
python -m vasp_core.cluster pull runs/cu --host me@ln01
```

省事的办法：设一次环境变量

```powershell
# Windows PowerShell（写进 $PROFILE 就永久生效）
$env:VASP_HOST  = "20249980004@219.231.162.66"
$env:VASP_PP_DIR = "~/Optpaw_PBE.54"
```

```bash
# macOS / Linux
export VASP_HOST=20249980004@219.231.162.66
export VASP_PP_DIR=~/Optpaw_PBE.54
```

之后只需 `python -m vasp_core.cluster up runs/cu`。

### 为什么这样最优

| 方案 | 好处 | 代价 |
| --- | --- | --- |
| **A 本机前处理（推荐）** | 改完立刻看 diff；模型吃本机 CPU/GPU；不占 Lustre 配额；超算只算 VASP | 要传文件；POTCAR 只能在超算拼（用 `POTCAR.spec` 传递） |
| B 全在超算（v1 手册） | 不传文件 | 登录节点无网、98% 满、无 pymatgen；模型占计算队列；改一句要重登 |
| C 两边都装 | 灵活 | 两份配置会漂移，最容易出现"本机对了超算不对" |

A 的关键设计是 **POTCAR 用"清单"传递**：本机只写 `POTCAR.spec`（一行一个变体，如 `Cu_pv`），
超算上用 `--potcar-only` 按 POSCAR 元素顺序拼真赝势并校验 TITEL 顺序。
这样本机不需要几个 GB 的赝势库，也不会把 POTCAR 内容外传。

### 注意事项

- 第一次连超算建议配 ssh key（`ssh-keygen` + `ssh-copy-id`），否则每条命令都要输密码；
- 超算 PATH 里**没有 rsync**，所以这里用 tar.gz + scp；
- `pull` 只拉回 `runs/cu/remote/`，不会覆盖你本机的 POSCAR/INCAR；
- 本机是 Windows 时 `scp`/`ssh` 用系统自带 OpenSSH（Win10+ 默认有）；mac 自带；
- 传到超算的目录名默认取本地目录名，可用 `--name` 改。



---

## 12. 网页与终端（v2.2 主入口）

### 12.1 网页（推荐）

双击 `启动网页.bat` → 浏览器自动打开 `http://127.0.0.1:8765`。

界面四步：点**选择…**用系统资源管理器选**源目录**（也可直接粘贴路径）→
填**输出名称**（结果固定写到 `vasp-prep\runs\<名称>`；留空 = `<源目录名>_out`，同名自动加 `_2`）与写**一句中文** →
点**预览**看过程与 diff → 没问题点**写入**。

产出（只写到 `runs\<名称>`，源目录不改）：

```
POSCAR  INCAR  KPOINTS  INCAR.diff  change_report.md  recipe.json
```

不生成 POTCAR；不改源目录；输出目录已存在会拒绝（防覆盖）。

### 12.2 终端

```bash
python -m vasp_core.chat          # 系统 Python 即可
```

流程与网页一致：源目录 → 输出目录 → 需求 → 预览（打印日志与 diff）→ `y` 写入。

### 12.3 模型怎么接（不接也能用）

```powershell
ollama serve
ollama pull qwen2.5-coder:7b
ollama list          # 确认模型名
```

默认配置就去 `http://127.0.0.1:11434/v1` 找它；网页右上角显示"在线（xx ms）"即接通。
不接模型时自动走规则模式（扩胞/替换/空位/真空/ENCUT/K 点/应变/批量换元素都支持）。

### 12.4 为什么网页要一个 `webui_launcher.py`

`chgnet_env` 是 Windows 嵌入式 Python：`sys.path` 被 `._pth` 钉死，`python -m vasp_core.webui`
会报 `ModuleNotFoundError`，`PYTHONPATH` 也被忽略。运行 `.py` 文件时脚本目录自动进
`sys.path[0]`，所以用 5 行垫片接管入口。系统 Python 无此问题。

---

## 13. 配置与换机部署

### 13.1 配置优先级

```
命令行参数 > 环境变量 > vasp-prep.toml > 代码默认值
```

| 配置项 | toml | 环境变量 |
|---|---|---|
| 模型地址 | `[llm] base_url` | `VASP_PREP_LLM_BASE` |
| 模型名 | `[llm] model` | `VASP_PREP_LLM_MODEL` |
| 端口 | `[ui] port` | `VASP_PREP_PORT` |
| 不自动开浏览器 | `[ui] auto_open=false` | `VASP_PREP_NO_BROWSER=1` |
| 只用规则 | `[llm] enabled=false` | — |

### 13.2 换到另一台电脑（3 步）

1. 拷整个 `vasp-prep` 目录（含 `web/`、`docs/`、`vasp-prep.toml`）；
2. 装 Python 3.10+；要做"切表面/加吸附质"再装 `pip install pymatgen ase`（或复制 `chgnet_env`）；
3. 双击 `启动网页.bat`（它优先找 `../chgnet_env/python/python.exe`，找不到用系统 python）。

Windows/macOS/Linux 都一样：**不需要 root、不开外网端口、不写系统目录**。

### 13.3 换到服务器（多人共用）

```bash
# 1) 放到共享目录，所有人只读
cp -r vasp-prep-v2 /share/apps/vasp-prep
# 2) 每人各自的项目目录放 POSCAR，输出到 ~/vasp-work/runs/...
# 3) 启动（无浏览器的服务器）
VASP_PREP_NO_BROWSER=1 python3 -m vasp_core.webui --port 8765
# 4) 本机做 SSH 端口转发后访问 http://127.0.0.1:8765
ssh -N -L 8765:localhost:8765 user@server
```

注意：`127.0.0.1` 监听意味着**只有本机能访问**；多人用请各自转发自己的端口，不要绑 `0.0.0.0`。

### 13.4 超算上要做什么（与本工具无关）

本工具只产前处理文件。超算端你只需要：

```bash
cat ~/Optpaw_PBE.54/Cu/POTCAR ~/Optpaw_PBE.54/Co/POTCAR > POTCAR   # 顺序 = POSCAR 第 6 行
grep TITEL POTCAR                                                  # 核对
qsub vasp.sh
```

---

## 14. 批量处理（v3）

一句话：**一个根目录、一句需求，对下面每个材料各生成一套输入**；可选参数扫描（例如 IVDW 取 0/11/12）。

### 14.1 网页

1. 「批量」页签 → 选批量根目录（每个子文件夹里一个 POSCAR）→ **扫描材料**；
2. 写需求（如 `扩成 2x2x1，加 DFT-D3(BJ)`）；参数扫描填 `0,11,12`（留空 = 不扫描）；
3. **预览**：表里逐项列出材料、参数、**将要生成**的完整路径、原子数前后、改了什么；
4. 确认无误 → **全部写入**。

### 14.2 命令行

```bash
python -m vasp_core.batch --root D:\vasp_models --text "扩成 2x2x1" --ivdw 0,11,12          # 预览
python -m vasp_core.batch --root D:\vasp_models --text "扩成 2x2x1" --ivdw 0,11,12 --write  # 写入
```

### 14.3 输出与命名

```
batches\<批次名>\<材料名>\{POSCAR,INCAR,KPOINTS,INCAR.diff,change_report.md}
                <材料名>\ivdw_<值>\...     ← 只有参数扫描才有
        \batch_summary.csv  \batch_report.md
```

- 批次名：你填；留空 = `batch_<日期-时间>_<关键词>`；同名自动 `_2`，不覆盖。
- 材料名：取文件夹名，非法字符换 `_`；单项目标已存在 → `skip`（`--force` 才覆盖）。
- 上限：材料 ≤ 200、材料×参数 ≤ 500（`vasp-prep.toml` 的 `[batch]` 可改）。
- 任一材料失败只标红那一项，其余照常；源目录只读、mtime 不变。

细节与排错：`docs\批量处理.md`。

---

## 15. 数据库下载与导入（v4）

一句话：**按化学式检索，下载成 `materials\<库>_<id>\POSCAR`**，下载目录可直接当批量根目录。

| 库 | 方式 |
|---|---|
| Materials Project / OQMD | 免 key 直连（OPTIMADE），检索 + 下载 |
| AFLOW | 检索，给官网链接，手动下载后导入 |
| 2Dmatpedia | 首次下载整库缓存，本地检索 |
| C2DB / ICSD / Materials Cloud / 中科院二维铁电库 | 官网导出 CIF/POSCAR/zip → 「导入」 |

### 15.1 网页

「数据库」页签：选库 → 填化学式（如 `MoS2`）→ 检索 → 对可下载的行点「下载」；
本地导出的文件在下方「导入」框里选路径即可。

### 15.2 命令行

```bash
python -m vasp_core.db search --provider mp --formula MoS2 --limit 10
python -m vasp_core.db download --provider mp --id mp-2815 --subdir 二维材料
python -m vasp_core.db import --path D:\exported\c2db
```

每个下载目录都带 `SOURCE.json`（库、id、URL、时间、许可），半年后也查得清来源。

细节与许可说明：`docs\数据库下载.md`。


