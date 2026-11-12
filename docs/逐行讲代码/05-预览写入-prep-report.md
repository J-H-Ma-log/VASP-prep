# 05 预览、写入、prep 主流程与报告逐行讲解

对应文件：

- `vasp_core\service.py`（556 行）—— 网页/终端共用的"预览 + 写入"服务层
- `vasp_core\prep.py`（512 行）—— 命令行入口，也是真正"把配方写成算例目录"的地方
- `vasp_core\report.py`（183 行）—— 生成 `change_report.md` / `INCAR.diff` / 审计日志

阅读顺序建议：先看 `service.preview`（一次预览的全流程），再看它调用的 `stage_recipe`，
然后跳到 `prep.run`，最后看 `report.make_change_report`。

> 约定：表格里"行"是文件真实行号；"代码"去掉了行首缩进，但标识符与原文完全一致。

---

# 第一部分：service.py

## 1.0 文件结构总览

| 行号 | 名称 | 类型 | 一句话 | 被谁调用 |
|---|---|---|---|---|
| 49 | `STAGE_ROOT` | 常量 | 临时舞台根目录 `<repo>\.tmp_service` | `preview`/`cleanup_all` |
| 50-51 | `_JOBS` / `_LOCK` | 全局 | 内存里的预览任务表 + 线程锁 | `preview`/`get_job`/`write` |
| 53 | `MAX_DIFF_LINES` | 常量 | diff 最多 400 行 | `_diff_text` |
| 56 | `_log` | 函数 | 往时间线追加一行 | 几乎所有函数 |
| 61 | `Job` | dataclass | 一次预览的完整现场 | `preview`/`write_job_obj` |
| 96 | `summarize_source` | 函数 | 只读源目录，产出摘要 | `preview`、`batch.scan_materials` |
| 143 | `_incar_pairs` | 函数 | 逐行抽 INCAR 的 `键=值` | `summarize_source` |
| 160 | `understand` | 函数 | 模型优先，失败退规则 | `compose_recipe` |
| 184 | `_detect_calc` | 函数 | 中文关键词 → relax/static/band/none | `_apply_calc_policy` |
| 203 | `_finalize_recipe` | 函数 | 补 `version/name/base_dir` | `compose_recipe` |
| 223 | `_strip_unrequested_ivdw` | 函数 | 用户没提色散就删掉 IVDW | `_apply_calc_policy` |
| 241 | `_apply_calc_policy` | 函数 | 纯结构编辑不套 INCAR 预设 | `compose_recipe` |
| 260 | `_prep_args` | 函数 | 用 prep 的 parser 造参数 | `stage_recipe` |
| 278 | `_diff_text` | 函数 | 带行数上限的 unified diff | `stage_recipe` |
| 291 | `_read_text` | 函数 | 读文本，失败返回空串 | `stage_recipe` |
| 298 | `_safe_name` | 函数 | 从中文里取短目录名 | `compose_recipe` |
| 308 | `compose_recipe` | 函数 | 中文 → 完整配方 dict | `preview`、`batch` |
| 341 | `stage_recipe` | 函数 | 在舞台真跑一遍 prep | `preview`、`batch` |
| 383 | `preview` | 函数 | 读→理解→试算→diff | `webui`/`chat`/`batch` |
| 425 | `_cleanup_old_jobs` | 函数 | 只留最近 10 个任务 | `preview` |
| 438 | `_preflight_out` | 函数 | 写前探测输出目录 | `write_job_obj` |
| 490 | `_probe_dir` | 函数 | 试建临时目录再删 | `_preflight_out` |
| 502 | `write_job_obj` | 函数 | 真正写进输出目录 | `write`、`batch.write_batch` |
| 537 | `write` | 函数 | 按 job_id 写入 | `webui`/`chat` |
| 545 | `get_job` | 函数 | 按 id 取任务 | `write` |
| 550 | `cleanup_all` | 函数 | 退出时删所有舞台 | `webui` 退出钩子 |

---

## 1.1 `_log`（56-58）

**干什么**：在 `logs` 列表里追加一条 `{"t","level","msg"}`，网页"过程"面板直接渲染它。
**参数**：`logs`（列表，原地改）、`level`（info/ok/warn/err/step）、`msg`（人话）。
**返回**：None。**异常**：无。

| 行 | 代码 | 说明 |
|---|---|---|
| 57 | `"""时间线里加一行。level: info / ok / warn / err / step"""` | 文档字符串，说明 level 的取值范围 |
| 58 | `logs.append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg})` | 时间戳只到秒；每条日志立刻可见，不缓存 |

---

## 1.2 `Job`（61-90）

**干什么**：把一次预览的所有现场（源、输出、配方、舞台、摘要、diff、日志）绑在一个对象上。
**为什么**：网页"写入"时只传 `job_id`，不传一堆路径——路径由服务端记着，前端改不了。

| 行 | 代码 | 说明 |
|---|---|---|
| 62 | `"""一次预览的完整现场。write 阶段只认 job_id，避免网页传一堆路径被篡改。"""` | 设计意图 |
| 65 | `id: str` | 10 位十六进制 |
| 66 | `source: Path` | 源目录（只读） |
| 67 | `output: Path` | 目标目录；写入前不创建 |
| 68 | `request: str` | 用户原话，报告与复现都用它 |
| 69 | `recipe: dict` | 最终配方（与 `recipe.json` 同源） |
| 70 | `recipe_path: Path` | 舞台里的 `recipe.json` |
| 71 | `stage: Path` | 舞台根（`<id>`） |
| 72 | `summary: dict = field(default_factory=dict)` | `prep.run` 的摘要 |
| 73 | `diffs: dict[str, str] = ...` | 文件名 → diff 文本 |
| 74 | `files: list[str] = ...` | 已生成文件名 |
| 75 | `logs: list[dict] = ...` | 时间线 |
| 76 | `created: float = ...` | 排序用（清理最旧任务） |
| 77 | `written_to: str \| None = None` | 写入成功后填输出路径 |
| 79 | `def to_dict(self) -> dict:` | 给 HTTP JSON 用 |
| 80-90 | 逐字段构造 dict | 只暴露前端需要的字段，`recipe` 不直接回传（体积大） |

---

## 1.3 `summarize_source`（96-140）

**干什么**：读 `POSCAR`（必需）、`INCAR`（可选）、`KPOINTS`（可选），产出"给人看也给模型看"的摘要。
**参数**：`source`（目录路径）。
**返回**：dict：`path/natoms/species/counts/lattice_abc/volume/has_incar/has_kpoints/incar_keys/calc_guess`。
**异常**：`PrepError`（目录不存在、没有 POSCAR）。

| 行 | 代码 | 说明 |
|---|---|---|
| 97-101 | `"""读 POSCAR/INCAR/KPOINTS..."""` | 文档字符串：只读；不读会怎样 |
| 102 | `src = Path(source).expanduser().resolve()` | 统一绝对路径（`~` 也展开） |
| 103 | `if not src.is_dir():` | 目录检查 |
| 104 | `raise PrepError(f"源目录不存在：{src}")` | 直接给用户看的错误 |
| 105 | `poscar = src / "POSCAR"` | 只认大写 POSCAR（VASP 默认） |
| 106 | `if not poscar.exists():` | 没有 POSCAR 无法继续 |
| 107 | `raise PrepError(f"源目录里没有 POSCAR：{src}")` | 同上 |
| 109 | `st = read_poscar(poscar)` | 解析成 Structure（唯一一次读源结构） |
| 110 | `lat = st.lattice` | 取 Å 晶格（已乘 scale） |
| 111 | `abc = [sum(lat[i][j] ** 2 for j in range(3)) ** 0.5 for i in range(3)]` | 三边长 = 每行范数 |
| 112 | `species = list(dict.fromkeys(st.atom_species()))` | 去重且保序（Mo S） |
| 114 | `incar_keys: list[str] = []` | 没 INCAR 时是空列表 |
| 115 | `incar_path = src / "INCAR"` | |
| 116 | `if incar_path.exists():` | 可选文件 |
| 117 | `try:` | INCAR 坏了不该阻止"看结构" |
| 118 | `lines = parse_incar(...read_text(...))` | BOM 容错、非法字节替换 |
| 119 | `incar_keys = [ln.key for ln in lines if ln.key]` | 只取键名，值不进摘要 |
| 120 | `except Exception:` | 宽捕获：任何解析错误都降级 |
| 121 | `incar_keys = []` | 降级为空 |
| 123 | `calc = "relax"` | 默认猜测（后面用 NSW/ICHARG 覆盖） |
| 124 | `for key, val in ((k, v) for k, v in _incar_pairs(incar_path)):` | 再读一遍原始键值（不依赖 parse 成功） |
| 125 | `if key == "NSW" and str(val).strip() in ("0", "0.0"):` | NSW=0 → 静态 |
| 126 | `calc = "static"` | |
| 127 | `if key == "ICHARG" and str(val).strip() in ("11",):` | ICHARG=11 → 能带 |
| 128 | `calc = "band"` | |
| 129-140 | `return {...}` | 逐字段见"返回" |

**为什么只读**：预览阶段绝不能碰源目录；读坏了最坏是报错，不会毁数据。

---

## 1.4 `_incar_pairs`（143-154）

**干什么**：不依赖 `parse_incar`，逐行抽 `键=值`，用于"猜计算类型"。
**参数**：`path`。**返回**：`list[tuple[str,str]]`（键已大写）。**异常**：无（文件不存在返回空列表）。

| 行 | 代码 | 说明 |
|---|---|---|
| 144 | `"""INCAR 的 (键, 值) 迭代器；文件不存在就空。"""` | |
| 145 | `if not path.exists():` | 文件不存在 |
| 146 | `return []` | 空列表，不是 None |
| 147 | `pairs = []` | |
| 148 | `for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():` | 逐行 |
| 149 | `s = line.split("#", 1)[0].split("!", 1)[0].strip()` | 去掉 `#`/`!` 注释 |
| 150 | `if not s or "=" not in s:` | 空行或不是赋值 |
| 151 | `continue` | 跳过 |
| 152 | `k, v = s.split("=", 1)` | 只按第一个 `=` 切（值里可能有 `=`） |
| 153 | `pairs.append((k.strip().upper(), v.strip()))` | 键大写，值去空格 |
| 154 | `return pairs` | |

---

## 1.5 `understand`（160-181）

**干什么**：先探活本地模型（3 秒），在线就让模型翻；失败/离线退回规则模式。
**参数**：`text`、`summary`（给模型的脱敏摘要）、`cfg`、`logs`。
**返回**：nl 形状的 dict（`recipe/matched/needs_human/unmatched`，可能带 `meta.source`）。
**异常**：不抛——所有模型错误都降级。

| 行 | 代码 | 说明 |
|---|---|---|
| 161-165 | `"""优先模型..."""` | 说明为什么先探活（否则要等 120 秒超时） |
| 166 | `if cfg.llm.enabled and cfg.llm.privacy != "off":` | 只有启用且隐私模式不是 off 才试模型 |
| 167 | `pr = llm_mod.probe(cfg)` | GET `/models`，3 秒超时 |
| 168 | `if pr.available:` | 模型确实在 |
| 169 | `_log(logs, "step", f"模型在线：{cfg.llm.model}（探测 {pr.elapsed_ms} ms）")` | 时间线写明探测耗时 |
| 170 | `try:` | 模型调用可能失败 |
| 171 | `return llm_mod.text_to_recipe(cfg, text, summary)` | 成功就直接返回模型结果 |
| 172 | `except llm_mod.LlmError as exc:` | 超时/JSON 解析失败等 |
| 173 | `_log(logs, "warn", f"模型没给出可用结果（{exc}），改用规则模式")` | 把原因写给用户 |
| 174 | `else:` | 模型不可用 |
| 175 | `_log(logs, "warn", f"模型不可用（{pr.reason}），改用规则模式")` | |
| 176-177 | `else:` + `_log(... "未启用模型，使用规则模式")` | 配置里关了模型 |
| 179 | `res = nl_mod.text_to_recipe(text)` | 规则翻译 |
| 180 | `res.setdefault("meta", {})["source"] = "rules"` | 标记来源，便于排查 |
| 181 | `return res` | |

---

## 1.6 `_detect_calc`（184-200）

**干什么**：只从中文判断"要不要改计算设置"，返回 `band/static/relax/none`。
**为什么**：纯结构操作不该顺手把 INCAR 换成 relax 预设（会写入 ISIF/IBRION/NSW/EDIFFG 等 10 个键）。

| 行 | 代码 | 说明 |
|---|---|---|
| 185-192 | `"""..."""` | 文档说明"不做会怎样" |
| 193 | `t = text.lower()` | 英文/中文统一小写比较 |
| 194 | `if any(k in t for k in ("能带", "band", "nscf")):` | 能带优先 |
| 195 | `return "band"` | |
| 196 | `if any(k in t for k in ("静态", "单点", "static", "scf")):` | |
| 197 | `return "static"` | |
| 198 | `if any(k in t for k in ("弛豫", "优化", "relax", "几何优化")):` | |
| 199 | `return "relax"` | |
| 200 | `return "none"` | 什么都没说 → 不套预设 |

---

## 1.7 `_finalize_recipe`（203-217）

**干什么**：把翻译结果补成完整配方：`version=2`、`name`、`base_dir=源目录`、`structure.file=POSCAR`、`ops=[]`、`incar.overrides={}`。
**为什么补 `base_dir`**：配方里的相对路径都按它解析；补成绝对路径后，配方文件放哪都能复跑。

| 行 | 代码 | 说明 |
|---|---|---|
| 204-208 | `"""..."""` | |
| 209 | `recipe = res.get("recipe") or {}` | 允许翻译层没给 recipe |
| 210 | `recipe["version"] = 2` | 强制 v2 |
| 211 | `recipe.setdefault("name", name)` | 没名字就用短名 |
| 212 | `recipe["name"] = name` | 最终以短名为准 |
| 213 | `recipe["base_dir"] = str(source)` | 绝对路径 |
| 214 | `recipe.setdefault("structure", {}).setdefault("file", "POSCAR")` | 默认文件 |
| 215 | `recipe["structure"].setdefault("ops", [])` | 保证是列表 |
| 216 | `recipe.setdefault("incar", {}).setdefault("overrides", {})` | 保证是字典 |
| 217 | `return recipe` | |

---

## 1.8 `_strip_unrequested_ivdw`（223-238）

**干什么**：配方里有 `IVDW`，但用户原话没提色散 → 删掉并告警。
**为什么**：IVDW 是科学方法选择（D3/MBD/不加会改变结论），必须由人点名。

| 行 | 代码 | 说明 |
|---|---|---|
| 220 | `_VDW_WORDS = (...)` | 判定"提了色散"的关键词 |
| 224-230 | `"""..."""` | |
| 231 | `ov = (recipe.get("incar") or {}).get("overrides") or {}` | 安全取字典 |
| 232 | `if "IVDW" not in ov:` | 没有就不用管 |
| 233 | `return` | |
| 234 | `low = text.lower()` | |
| 235 | `if any(w in low for w in _VDW_WORDS):` | 用户提了 → 保留 |
| 236 | `return` | |
| 237 | `ov.pop("IVDW", None)` | 删除 |
| 238 | `_log(logs, "warn", "模型自行加了 IVDW，但你没提色散修正，已丢弃（需要就明说 D3/MBD 等）")` | 让用户知道被删了 |

---

## 1.9 `_apply_calc_policy`（241-254）

**干什么**：按 `_detect_calc` 的结果决定 `recipe.calc` 与 `incar.preset`；`none` 时不套预设。

| 行 | 代码 | 说明 |
|---|---|---|
| 242 | `"""按"用户是否明确要改计算类型"决定 calc/preset（纯结构编辑不套预设）。"""` | |
| 243 | `calc = _detect_calc(text)` | 关键词判断 |
| 244 | `recipe["calc"] = calc` | 写回配方 |
| 245 | `recipe.setdefault("incar", {})` | 保证 incar 段存在 |
| 246 | `if calc == "none":` | 纯结构编辑 |
| 247-248 | `if recipe["incar"].pop("preset", None):` + `_log(...)` | 若翻译层默认给了 relax，删掉并说明 |
| 249 | `else:` | 用户明确要改计算 |
| 250 | `recipe["incar"]["preset"] = calc` | 套对应预设 |
| 251 | `_log(logs, "info", f"检测到计算类型：{calc}（会套用对应 INCAR 预设）")` | |
| 254 | `_strip_unrequested_ivdw(recipe, text, logs)` | 顺手清理模型擅自加的 IVDW |

---

## 1.10 `_prep_args`（260-275）

**干什么**：构造 `prep.run` 需要的 `argparse.Namespace`——**复用 prep 自己的 parser**，避免手写漏字段。
**为什么**：`prep.run` 依赖十几个属性，手写漏一个就是 `AttributeError`。

| 行 | 代码 | 说明 |
|---|---|---|
| 261-265 | `"""..."""` | |
| 266 | `p = build_parser()` | 同一个 parser，字段永远对齐 |
| 267-270 | `args = p.parse_args([str(recipe_path), "--potcar-mode", "skip", "--no-audit", "--force"])` | 前处理不碰 POTCAR、不写全局审计、允许写舞台 |
| 271 | `args.write = True` | 舞台里要真写文件才能 diff |
| 272 | `args.out = str(out_dir)` | 写到舞台的 `out/` |
| 273 | `args.quiet = True` | 不打印人读摘要 |
| 274 | `args.json = True` | 但返回 dict |
| 275 | `return args` | |

---

## 1.11 `_diff_text`（278-288）

**干什么**：`difflib.unified_diff`，带 400 行上限；无变化返回空串。

| 行 | 代码 | 说明 |
|---|---|---|
| 279 | `"""unified diff，带行数上限。"""` | |
| 280-283 | `lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=..., tofile=..., lineterm="", n=2))` | 上下文 2 行；换行符不参与比较 |
| 284 | `if not lines:` | 完全相同 |
| 285 | `return ""` | 空串 → 网页不显示该页签 |
| 286 | `if len(lines) > MAX_DIFF_LINES:` | 太长 |
| 287 | `lines = lines[:MAX_DIFF_LINES] + [f"...（diff 太长，只显示前 {MAX_DIFF_LINES} 行）"]` | 截断并提示 |
| 288 | `return "\n".join(lines)` | |

---

## 1.12 `_read_text`（291-295）

**干什么**：读文本，BOM 容错、非法字节替换；读失败返回空串（diff 仍然可用）。

| 行 | 代码 | 说明 |
|---|---|---|
| 292 | `try:` | |
| 293 | `return path.read_text(encoding="utf-8-sig", errors="replace")` | |
| 294 | `except OSError:` | 权限/占用/不存在 |
| 295 | `return ""` | 当作空文件，不打断预览 |

---

## 1.13 `_safe_name`（298-305）

**干什么**：从中文需求里取一个 2–20 字符的 ASCII 名字做目录名；取不到用 `edit_HHMMSS`。
**为什么**：中文目录名在超算/压缩/传文件时容易出编码问题，先给个英文名，用户可改。

| 行 | 代码 | 说明 |
|---|---|---|
| 299 | `"""从中文需求里取一个短名字做目录名；取不到就用 edit_时间。"""` | |
| 300 | `import re` | 局部导入（只在需要时） |
| 302 | `m = re.search(r"[A-Za-z0-9_\-]{2,20}", text)` | 找第一段英文/数字/下划线 |
| 303 | `if m:` | |
| 304 | `return m.group(0)` | 如 "Co"、"2x2x1" 里的 "2x2x1" |
| 305 | `return "edit_" + datetime.now().strftime("%H%M%S")` | 兜底 |

---

## 1.14 `compose_recipe`（308-338）

> 完整逐行见 `02-理解与配方.md`（本节只讲它在主流程里的位置）。

**干什么**：`understand` → 记日志 → `_finalize_recipe` → `_apply_calc_policy`，产出可直接校验的配方。
**为什么单独抽出来**：批量要"同一句话只理解一次"，再套到 N 个材料上；否则同一句话在不同材料上可能被理解成不同操作。

---

## 1.15 `stage_recipe`（341-380）

**干什么**：在 `stage` 下建目录、写 `recipe.json`、调 `prep.run` 写到 `stage/out`，再把产物读回来做 diff。
**参数**：`recipe`（dict）、`src`（源目录）、`stage`（舞台路径）、`logs`（可选）。
**返回**：`(summary2, diffs, files)`。**异常**：`prep.run` 的 `PrepError` 原样抛出（调用方负责删舞台）。

| 行 | 代码 | 说明 |
|---|---|---|
| 343-350 | `"""在 stage 目录里真跑一遍 prep..."""` | 说明"为什么真跑而不是假算" |
| 351 | `logs = logs if logs is not None else []` | 允许不传 |
| 352 | `stage.mkdir(parents=True, exist_ok=False)` | 已存在就报错（防止串台） |
| 353 | `recipe_path = stage / "recipe.json"` | 配方落盘，写入阶段要用同一份 |
| 354 | `recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")` | 人可读、可复跑 |
| 356 | `out_stage = stage / "out"` | 产物目录 |
| 357 | `_log(logs, "step", f"试算（临时舞台）：{out_stage}")` | 时间线 |
| 358 | `summary2 = prep_run(_prep_args(recipe_path, out_stage))` | 真正执行 |
| 360 | `for op in summary2.get("ops", []):` | 每个结构操作 |
| 361 | `_log(logs, "ok", f"{op.get('op')}：{op.get('detail', '')}")` | |
| 362 | `for ch in summary2.get("incar_changes", []):` | 每个 INCAR 变更 |
| 363 | `_log(logs, "ok", f"INCAR {ch['key']}：{ch.get('old')} → {ch.get('new')}")` | |
| 364 | `for issue in summary2.get("issues", []):` | 检查结果 |
| 365 | `lvl = {"ERROR": "err", "WARN": "warn"}.get(str(issue.get("level")), "info")` | 级别映射 |
| 366 | `_log(logs, lvl, f"[{issue.get('level')}] {issue.get('code')}: {issue.get('message')}")` | |
| 368-369 | `diffs: dict[str, str] = {}` | 只有真有产物的文件才 diff |
| 370 | `produced = [p for p in out_stage.iterdir() if p.is_file()] if out_stage.exists() else []` | 列产物 |
| 371 | `for p in sorted(produced, key=lambda x: x.name):` | 按名字排序，diff 顺序稳定 |
| 372 | `if p.name in ("POSCAR", "INCAR", "KPOINTS"):` | 只 diff 这三类文本 |
| 373 | `old = _read_text(src / p.name)` | 源文件可能不存在 → 空串 |
| 374 | `new = _read_text(p)` | |
| 375 | `d = _diff_text(old, new, p.name)` | |
| 376 | `if d:` | 无变化不加入 |
| 377 | `diffs[p.name] = d` | |
| 378 | `files = sorted(p.name for p in produced)` | 摘要用 |
| 379 | `_log(logs, "step", f"试算完成：将生成 {', '.join(files) or '（无文件）'}")` | |
| 380 | `return summary2, diffs, files` | |

---

## 1.16 `preview`（383-422）

**干什么**：唯一入口——校验源、决定输出、读摘要、理解、试算、diff，生成 `Job`。
**参数**：`source`、`output`（可空）、`text`、`cfg`（可空）。
**返回**：`Job`。**异常**：`PrepError`（源错/需求空/试算失败）；失败会先删舞台。

| 行 | 代码 | 说明 |
|---|---|---|
| 384 | `"""读文件 → 理解 → 试算 → diff，返回 Job（此时还没写最终输出目录）。"""` | |
| 385 | `cfg = cfg or config_mod.load()` | 允许调用方传配置 |
| 386 | `logs: list[dict] = []` | 本次任务的时间线 |
| 387 | `src = Path(source).expanduser().resolve()` | |
| 388 | `if not output.strip():` | 输出留空 → 默认位置 |
| 389-391 | `output = str((ROOT / cfg.paths.default_output / f"{src.name or 'run'}_out").resolve())` | 一定可写的 `runs/` |
| 392 | `_log(logs, "info", f"输出目录留空，默认写到：{output}")` | 明确告诉用户 |
| 394 | `_log(logs, "step", f"读取源目录：{src}")` | |
| 395 | `summary = summarize_source(src)` | 只读摘要 |
| 396-398 | `_log(logs, "ok", f"POSCAR：...")` | 元素/原子数/边长/体积 |
| 399-400 | `_log(logs, "info", f"INCAR：...KPOINTS：...")` | 有没有附带文件 |
| 402 | `if not text.strip():` | 需求为空 |
| 403 | `raise PrepError("需求是空的：请写一句要改什么，例如...")` | 给例子 |
| 405 | `recipe = compose_recipe(text, summary, src, cfg, logs)` | 翻译 + 补全 + 策略 |
| 407 | `job_id = uuid.uuid4().hex[:10]` | 10 位 |
| 408 | `stage = STAGE_ROOT / job_id` | 舞台路径 |
| 409 | `try:` | 试算可能失败 |
| 410 | `summary2, diffs, files = stage_recipe(recipe, src, stage, logs)` | |
| 411 | `except PrepError as exc:` | |
| 412 | `_log(logs, "err", str(exc))` | 错误也进时间线 |
| 413 | `shutil.rmtree(stage, ignore_errors=True)` | 失败不留半成品 |
| 414 | `raise` | 原样抛给网页 |
| 416-418 | `job = Job(...)` | 现场打包 |
| 419 | `with _LOCK:` | 线程安全 |
| 420 | `_JOBS[job_id] = job` | |
| 421 | `_cleanup_old_jobs()` | 控制内存/磁盘 |
| 422 | `return job` | |

---

## 1.17 `_cleanup_old_jobs`（425-432）

**干什么**：任务超过 10 个时，按创建时间删最旧的；**已写入的只移出表，不删输出目录**。

| 行 | 代码 | 说明 |
|---|---|---|
| 426 | `"""只保留最近 max_jobs 个预览..."""` | |
| 427 | `if len(_JOBS) <= max_jobs:` | 没超 |
| 428 | `return` | |
| 429 | `for jid in sorted(_JOBS, key=lambda x: _JOBS[x].created)[:-max_jobs]:` | 最旧的先删 |
| 430 | `old = _JOBS.pop(jid, None)` | |
| 431 | `if old and not old.written_to:` | 已写入的保留输出 |
| 432 | `shutil.rmtree(old.stage, ignore_errors=True)` | 只删舞台 |

---

## 1.18 `_preflight_out`（438-499）

**干什么**：写入前试建一个临时目录再删，确认"这个位置真的能写"；不能写时给出**可操作**的建议。
**参数**：`out`（目标目录）。**返回**：None。**异常**：`PrepError`（带系统原话、pid、用户、cwd、三条建议）。

| 行 | 代码 | 说明 |
|---|---|---|
| 439-447 | `"""写入前先试一下输出目录能不能建、能不能写。..."""` | 说明 WinError 5 的四种来源 |
| 448 | `parent = out.parent` | 只探测父目录（输出目录可能还不存在） |
| 449 | `ok, msg = _probe_dir(parent)` | 试建 |
| 450 | `if ok:` | |
| 451 | `return` | 可写，结束 |
| 457 | `from .config import ROOT as _ROOT` | 局部导入避免循环 |
| 459 | `repo_runs = _ROOT / "runs"` | 备选 1：程序自己的 runs |
| 460 | `repo_ok, repo_msg = _probe_dir(repo_runs)` | |
| 461 | `home = Path.home()` | |
| 462 | `suggested = home / "vasp_runs"` | 备选 2：用户目录下 |
| 463 | `home_ok, home_msg = _probe_dir(home)` | |
| 464 | `sug_ok, sug_msg = _probe_dir(suggested)` | |
| 465 | `if repo_ok:` | 优先推荐 runs |
| 466-467 | `advice = (...)` | "留空输出目录" |
| 468 | `elif sug_ok:` | |
| 469 | `advice = f"     → 建议改用：{suggested}（已实测可写）\n"` | |
| 470 | `elif home_ok:` | |
| 471 | `advice = f"     → 用户目录可写，但 {suggested} 建不了：{sug_msg}\n"` | |
| 472 | `else:` | 连 home 都写不了 |
| 473-475 | `advice = ("... 几乎可以确定这个 Python 进程跑在受限沙箱里 ...")` | 告诉用户用普通 cmd 启动 |
| 477-488 | `raise PrepError(...)` | 拼出完整报错：路径、系统原话、pid/用户/cwd、建议、三类常见原因 |

**为什么**：直接 `mkdir` 抛的 traceback 用户看不懂，也分不清该找谁；这里把"该换到哪"直接写进报错。

---

## 1.19 `_probe_dir`（490-499）

**干什么**：在父目录下建一个随机名临时目录，成功就删掉，返回 `(True,"")`；失败返回 `(False, "类型: 原话")`。

| 行 | 代码 | 说明 |
|---|---|---|
| 491 | `"""试建一个临时目录再删掉，返回 (是否可写, 失败原因)。"""` | |
| 492 | `try:` | |
| 493 | `parent.mkdir(parents=True, exist_ok=True)` | 父目录不存在也一起建 |
| 494 | `probe = parent / f".vasp_prep_probe_{uuid.uuid4().hex[:8]}"` | 随机名，避免撞车 |
| 495 | `probe.mkdir()` | 真正测试 |
| 496 | `probe.rmdir()` | 立刻删 |
| 497 | `return True, ""` | |
| 498 | `except OSError as exc:` | 权限/只读/被占用 |
| 499 | `return False, f"{type(exc).__name__}: {exc}"` | 保留系统原话 |

---

## 1.20 `write_job_obj`（502-534）

**干什么**：把已预览的 Job 真正写进 `job.output`：预检 → 重跑 `prep.run` → 记摘要 → 删舞台。
**为什么重跑而不是复制舞台**：保证"你看到的 diff"和"落盘的文件"来自同一份 `recipe.json`，并顺带再校验一次。

| 行 | 代码 | 说明 |
|---|---|---|
| 503-509 | `"""..."""` | 说明重跑的理由与覆盖规则 |
| 511 | `out = job.output` | |
| 512 | `_preflight_out(Path(out))` | 先确认可写 |
| 513 | `_log(job.logs, "step", f"写入输出目录：{out}" + (...))` | |
| 514 | `p = build_parser()` | |
| 515-517 | `args = p.parse_args([str(job.recipe_path), "--potcar-mode", "skip", "--no-audit"])` | 注意：**没有** `--force` |
| 518 | `args.write = True` | 这次真写 |
| 519 | `args.out = str(out)` | 目标目录 |
| 520 | `args.force = bool(force)` | 只有用户确认才覆盖 |
| 521 | `args.quiet = True` | |
| 522 | `args.json = True` | |
| 524 | `summary = prep_run(args)` | 第二次执行同一配方 |
| 525 | `job.summary = summary` | |
| 526 | `job.files = summary.get("files", job.files)` | |
| 527 | `job.written_to = str(out)` | 标记已写入（清理时不删输出） |
| 528 | `for issue in summary.get("issues", []):` | |
| 529 | `if issue.get("level") == "ERROR":` | |
| 530 | `_log(job.logs, "err", str(issue.get("message")))` | 错误进时间线 |
| 531 | `_log(job.logs, "ok", f"已写入 {len(job.files)} 个文件：{', '.join(job.files)}")` | |
| 533 | `shutil.rmtree(job.stage, ignore_errors=True)` | 舞台没用了 |
| 534 | `return job` | |

---

## 1.21 `write` / `get_job` / `cleanup_all`（537-556）

| 行 | 代码 | 说明 |
|---|---|---|
| 538 | `"""按 job_id 找到预览任务并写入（网页/终端用）。"""` | |
| 539 | `job = get_job(job_id)` | |
| 540 | `if job is None:` | 任务过期/服务重启 |
| 541 | `raise PrepError(f"找不到预览任务 {job_id}（可能已过期，请重新预览）")` | 提示重新预览 |
| 542 | `return write_job_obj(job, force=force)` | |
| 545 | `def get_job(job_id: str) -> Job \| None:` | |
| 546 | `with _LOCK:` | |
| 547 | `return _JOBS.get(job_id)` | |
| 551 | `"""退出时清理所有舞台目录（网页服务关闭时调用）。"""` | |
| 552 | `with _LOCK:` | |
| 553 | `for job in _JOBS.values():` | |
| 554 | `shutil.rmtree(job.stage, ignore_errors=True)` | |
| 555 | `_JOBS.clear()` | |
| 556 | `shutil.rmtree(STAGE_ROOT, ignore_errors=True)` | 整个 `.tmp_service` 删掉 |

---

# 第二部分：prep.py —— `run()` 逐行（156-373）

## 2.0 `run` 在整条链里的位置

`service.stage_recipe` 与 `service.write_job_obj` 都调用 `prep.run`；命令行 `python -m vasp_core prep` 也走它。
一次 `run` = 读配方 → 读结构 → 改结构 → 拼 INCAR → 拼 POTCAR → KPOINTS → 一致性检查 → （dry-run 停）→ 写盘 → 审计。

## 2.1 签名与文档

| 行 | 代码 | 说明 |
|---|---|---|
| 156 | `def run(args: argparse.Namespace) -> dict:` | 参数是 `build_parser()` 解析出的 Namespace |
| 157 | `"""主流程；返回摘要 dict（同时用于 --json 输出与退出码判断）。"""` | 返回值既是 JSON，也是"有没有 ERROR"的依据 |

## 2.2 读配方与源结构（158-164）

| 行 | 代码 | 说明 |
|---|---|---|
| 158 | `recipe = load_recipe(args.recipe)` | 解析+校验配方；相对路径按 `base_dir` 解析 |
| 159 | `if not recipe.structure_file.exists():` | 结构文件可能被移走 |
| 160 | `raise PrepError(f"源结构不存在：{recipe.structure_file}", detail="检查配方 structure.file 与 base_dir")` | `detail` 是给用户的下一步 |
| 162 | `# 1) 结构` | 注释分段 |
| 163 | `before = read_poscar(recipe.structure_file)` | 读原始结构（只读） |
| 164 | `after, ops_log = apply_ops(before, recipe.ops)` | 顺序执行所有结构操作，返回新结构与日志 |

**关键**：`before` 一直留着——报告里的"改前"、体积对比、diff 都靠它。

## 2.3 可选的 ML 预弛豫（169-189）

| 行 | 代码 | 说明 |
|---|---|---|
| 166-168 | 注释 | 说明为什么放在 INCAR 之前：不影响原子数/元素，只改坐标与晶胞 |
| 169 | `ml_info: dict \| None = None` | 没开就是 None |
| 170 | `ml_issues: list[dict] = []` | 预弛豫自己的警告 |
| 171 | `if args.ml in ("relax", "both"):` | 只有显式要求才跑 |
| 172-175 | `after, ml_info = pre_relax(after, fmax=..., steps=..., model=..., device=...)` | 调 CHGNet/EMT 弛豫 |
| 176-177 | `detail = f"{model} 预弛豫：{n_steps} 步，E=... eV/atom，fmax=... eV/Å"` | 一句话摘要 |
| 178 | `ops_log.append({"step": len(ops_log)+1, "op": "ml_relax", "detail": detail})` | 当成一个"操作"进日志 |
| 179 | `if not ml_info["converged"]:` | 没收敛 |
| 180-184 | `ml_issues.append({... "ml-not-converged" ...})` | 给 WARN，不中断 |
| 185 | `if args.ml_model == "emt":` | EMT 只是冒烟测试 |
| 186-189 | `ml_issues.append({... "ml-emt-smoke-only" ...})` | 明确"能量不能作科研结论" |

## 2.4 磁矩建议与对称性（192-202）

| 行 | 代码 | 说明 |
|---|---|---|
| 192 | `magmom: dict \| None = None` | |
| 193 | `if args.ml in ("magmom", "both"):` | |
| 194 | `magmom = predict_magmom(after, device=args.ml_device)` | 只预测 |
| 195 | `magmom["suggestion"] = suggest_incar(magmom)` | 生成建议文本 |
| 198 | `sym_info: dict \| None = None` | |
| 199 | `if args.symmetry:` | |
| 200 | `from .backends.pmg_backend import symmetry as _symmetry` | 延迟导入（pymatgen 可选） |
| 202 | `sym_info = _symmetry(after)` | 只做分析，不改结构 |

## 2.5 INCAR：底稿、预设、覆盖（205-226）

| 行 | 代码 | 说明 |
|---|---|---|
| 205 | `base_text = None` | 没有底稿就从头生成 |
| 206 | `if recipe.incar_base:` | 配方显式指定底稿 |
| 207 | `bp = (recipe.base_dir / recipe.incar_base).resolve()` | 相对 base_dir |
| 208 | `if not bp.exists():` | |
| 209 | `raise PrepError(...)` | 指定了却不存在 → 直接报错（不能悄悄换底稿） |
| 210 | `base_text = bp.read_text(encoding="utf-8-sig")` | |
| 211 | `else:` | 没指定 |
| 212 | `default_base = recipe.base_dir / "INCAR"` | 默认用源目录 INCAR |
| 213 | `if default_base.exists():` | |
| 214 | `base_text = default_base.read_text(encoding="utf-8-sig")` | |
| 216-221 | `incar_text, changes, incar_issues, lines = build_incar(base_text=..., preset=..., overrides=..., natoms=after.natoms)` | 一次生成：文本/变更列表/问题/解析后的行 |
| 222-224 | 注释 | 解释"为什么要原样复制"：build_incar 会重排行内空格 |
| 225 | `if base_text is not None and not changes and not recipe.incar_overrides:` | 三个条件同时满足 |
| 226 | `incar_text = base_text` | 原样复制，避免假 diff |

## 2.6 POTCAR 三种模式（229-253）

| 行 | 代码 | 说明 |
|---|---|---|
| 229 | `potcar_mode = args.potcar_mode or recipe.potcar.get("mode") or "auto"` | 命令行 > 配方 > 默认 |
| 230 | `titles: list[str] = []` | 元素顺序对应 POTCAR 的 TITEL |
| 231 | `potcar_text: str \| None = None` | 真正拼好的 POTCAR |
| 232 | `potcar_issues: list[dict] = []` | |
| 233 | `potcar_note = ""` | 报告里的一句话说明 |
| 234 | `pp_dir = _resolve_potcar_dir(recipe, args.pp_dir)` | 找赝势库目录 |
| 235 | `species_order = list(dict.fromkeys(after.atom_species()))` | 去重保序（POTCAR 顺序） |
| 236 | `if potcar_mode == "skip":` | 本工具默认 |
| 237 | `potcar_note = "按配方要求跳过了赝势拼接；请自行确认 POTCAR。"` | |
| 238 | `elif pp_dir is None:` | 找不到库 |
| 239 | `if potcar_mode == "auto":` | |
| 240-242 | `potcar_issues.append({... "potcar-nodir" ...})` | WARN：改只写 spec |
| 243 | `potcar_mode = "spec"` | 降级 |
| 244 | `else:` | 有库 |
| 245 | `lib = scan_library(pp_dir)` | 扫目录 |
| 246 | `entries = choose(lib, species_order, mapping=recipe.potcar.get("map"))` | 按元素挑条目 |
| 247 | `titles = [e.title for e in entries]` | |
| 248 | `potcar_issues.extend(check_order(entries, species_order))` | 顺序检查 |
| 249 | `if potcar_mode == "auto":` | |
| 250 | `potcar_text = concat(entries)` | 拼成完整 POTCAR |
| 251 | `potcar_note = f"POTCAR 由 {pp_dir} 拼接（{' '.join(titles)}）"` | |
| 252 | `else:` | spec 模式 |
| 253 | `potcar_note = f"只写了 POTCAR.spec（...），POTCAR 请在超算上拼接"` | |

## 2.7 KPOINTS 与一致性检查（258-283）

| 行 | 代码 | 说明 |
|---|---|---|
| 258 | `kpoints_text = render_kpoints(recipe.kpoints) if recipe.kpoints else None` | 配方有就生成 |
| 259 | `kpoints_copied = False` | 标记"是复制的" |
| 260 | `if kpoints_text is None:` | 配方没写 KPOINTS |
| 261 | `src_kp = recipe.base_dir / "KPOINTS"` | 看源目录 |
| 262 | `if src_kp.exists():` | |
| 263 | `kpoints_text = src_kp.read_text(...)` | 原样复制 |
| 264 | `kpoints_copied = True` | |
| 265 | `is_slab = any(o["op"] == "vacuum" for o in recipe.ops)` | 有真空层 → slab 提示 |
| 266-267 | `kp_issues = _issue_dicts(check_kpoints(recipe.kpoints, lattice=after.lattice, is_slab=is_slab))` | KPOINTS 合理性检查 |
| 269-270 | `enmax = total_enmax(choose(scan_library(pp_dir), species_order, mapping=...)) if potcar_text else None` | 只有真拼了 POTCAR 才有 ENMAX |
| 271-279 | `cons_issues = _issue_dicts(check_consistency(incar=..., natoms=..., counts=..., species=..., calc_type=..., potcar_enmax=..., has_kpoints_file=...))` | 跨文件一致性检查 |
| 280 | `incar_issues_d = _issue_dicts(incar_issues)` | INCAR 自身问题转 dict |
| 281 | `all_issues = incar_issues_d + cons_issues + potcar_issues + kp_issues` | 汇总 |
| 282 | `errors = [i for i in all_issues if i["level"] == "ERROR"]` | |
| 283 | `warns = [i for i in all_issues if i["level"] == "WARN"]` | |

## 2.8 输出目录与冲突（286-293）

| 行 | 代码 | 说明 |
|---|---|---|
| 286 | `stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")` | 时间戳 |
| 287-288 | `out_dir = Path(args.out)... if args.out else (recipe.path.parent / "runs" / f"{recipe.name}_{stamp}")` | 没给 out 就放配方旁边的 runs |
| 289 | `if out_dir.exists() and not args.force:` | 已存在且没确认 |
| 290-293 | `raise FileConflict("输出目录已存在：...（不会覆盖已有算例...）")` | 不覆盖，保护已有算例 |

## 2.9 摘要 dict（295-313）

| 行 | 代码 | 说明 |
|---|---|---|
| 295-297 | `summary = {"ok": not errors, "dry_run": not args.write,` | 两个总开关 |
| 298 | `"recipe": str(recipe.path),` | 配方路径 |
| 299 | `"out_dir": str(out_dir),` | 目标目录 |
| 300-304 | `"structure": {"natoms": [before, after], "species":..., "volume": [round(...), round(...)]}` | 结构前后对比 |
| 305 | `"ops": ops_log,` | 每个操作 |
| 306-307 | `"incar_changes": [{"key","old","new","reason"} ...],` | 只列真改的键 |
| 308 | `"kpoints": recipe.kpoints.as_dict() if recipe.kpoints else None,` | |
| 309 | `"potcar": {"mode", "titles", "note"},` | |
| 310 | `"issues": all_issues,` | 全部问题 |
| 311-312 | `"errors": [...], "warnings": [...]` | 只留 code，方便程序判断 |
| 313 | `}` | |

## 2.10 三个出口：dry-run、校验失败、真写（315-373）

| 行 | 代码 | 说明 |
|---|---|---|
| 315 | `if not args.write:` | 预览/干跑 |
| 316 | `summary["message"] = "dry-run：没有写任何文件；确认无误后加 --write"` | 明确告诉用户 |
| 317 | `return summary` | **不建目录、不写文件** |
| 319 | `if errors:` | 有 ERROR |
| 320-323 | `raise ValidationError("校验未通过，已中止，没有写任何文件。\n" + 逐条 ERROR)` | 中止，零写入 |
| 325 | `# 6) 真正写盘` | |
| 326 | `out_dir.mkdir(parents=True, exist_ok=True)` | 这时才建目录 |
| 327 | `_atomic_write(out_dir/"POSCAR", write_poscar_text(after, comment=None))` | 原子写 |
| 328 | `_atomic_write(out_dir/"INCAR", incar_text)` | |
| 329-330 | `if kpoints_text is not None: _atomic_write(.../"KPOINTS", ...)` | 有才写 |
| 331-332 | `if potcar_text is not None: _atomic_write(.../"POTCAR", ...)` | auto 模式 |
| 333-334 | `if potcar_mode != "skip" and titles: _atomic_write(.../"POTCAR.spec", ...)` | spec 模式；skip 不写 |
| 336 | `diff_text = make_diff(base_text or "", incar_text, "INCAR")` | 底稿 vs 新文本 |
| 337-338 | `if diff_text: _atomic_write(.../"INCAR.diff", diff_text)` | 有变更才写 |
| 339-353 | `report = make_change_report(...)` | 传齐全部上下文 |
| 354 | `_atomic_write(out_dir/"change_report.md", report)` | |
| 356 | `if not args.no_audit:` | 审计可关 |
| 357 | `try:` | 审计失败不能拖垮主流程 |
| 358-367 | `write_audit(...)` | ts/用户/host/平台/配方/输出/改动/警告 |
| 368 | `except OSError as exc:` | |
| 369 | `summary.setdefault("notes", []).append(f"审计日志写入失败：{exc}")` | 记下来但不报错 |
| 371 | `summary["files"] = sorted(p.name for p in out_dir.iterdir() if p.is_file())` | 真实落盘文件清单 |
| 372 | `summary["message"] = f"已写入 {out_dir}"` | |
| 373 | `return summary` | |

---

# 第三部分：report.py 逐行

## 3.0 文件结构总览

| 行号 | 名称 | 一句话 | 被谁调用 |
|---|---|---|---|
| 26 | `_fmt` | 浮点去尾零 | 报告里所有数字 |
| 31 | `structure_delta` | 结构前后硬指标 | `make_change_report` |
| 45 | `make_diff` | unified diff，相同返回空 | `prep.run`（INCAR.diff）、`service._diff_text` 类似逻辑 |
| 55 | `_table` | 生成 Markdown 表格 | 报告 3 张表 |
| 63 | `make_change_report` | 生成整份 `change_report.md` | `prep.run:339` |
| 179 | `write_audit` | 追加一行 JSON 审计 | `prep.run:358` |

## 3.1 `_fmt`（26-28）

| 行 | 代码 | 说明 |
|---|---|---|
| 26 | `def _fmt(x: float, nd: int = 6) -> str:` | 默认 6 位小数 |
| 27 | `s = f"{float(x):.{nd}f}".rstrip("0").rstrip(".")` | 先格式化，再去尾零、去小数点 |
| 28 | `return s if s not in ("", "-0") else "0"` | 处理 `-0` 与空串 |

## 3.2 `structure_delta`（31-42）

| 行 | 代码 | 说明 |
|---|---|---|
| 32 | `"""结构层面的硬指标：原子数、元素顺序、体积、晶格矩阵。"""` | |
| 33 | `return {` | 只放"可核对"的事实，不放解释 |
| 34 | `"natoms_before": before.natoms,` | |
| 35 | `"natoms_after": after.natoms,` | |
| 36 | `"species_before": list(dict.fromkeys(before.atom_species())),` | 去重保序 |
| 37 | `"species_after": list(dict.fromkeys(after.atom_species())),` | |
| 38 | `"volume_before": before.volume(),` | |
| 39 | `"volume_after": after.volume(),` | |
| 40 | `"lattice_before": before.lattice,` | 3×3（Å） |
| 41 | `"lattice_after": after.lattice,` | |
| 42 | `}` | |

## 3.3 `make_diff`（45-52）

| 行 | 代码 | 说明 |
|---|---|---|
| 46 | `"""生成 unified diff；内容相同时返回空字符串。"""` | |
| 47 | `old = old_text.splitlines(keepends=True)` | 保留换行符，diff 才准确 |
| 48 | `new = new_text.splitlines(keepends=True)` | |
| 49 | `diff = list(difflib.unified_diff(old, new, fromfile=f"a/{filename}", tofile=f"b/{filename}"))` | 标准 a/ b/ 前缀，`patch -p1` 可用 |
| 50 | `if not diff and old_text == new_text:` | 完全一致 |
| 51 | `return ""` | 空串 → 不写 `INCAR.diff` |
| 52 | `return "".join(diff)` | |

## 3.4 `_table`（55-60）

| 行 | 代码 | 说明 |
|---|---|---|
| 56-57 | `lines = [表头, 分隔行]` | `\| --- \| --- \|` |
| 58 | `for r in rows:` | |
| 59 | `lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")` | 单元格里的 `\|` 转义，避免把表格撑破 |
| 60 | `return "\n".join(lines)` | |

## 3.5 `make_change_report`（63-176）

**干什么**：把"结构怎么变、做了什么操作、INCAR 改了哪些键、有什么风险、生成了哪些文件、下一步做什么"写成一份 Markdown。

| 行 | 代码 | 说明 |
|---|---|---|
| 63-78 | `def make_change_report(*, recipe_name, recipe_path, source_structure, before, after, ops_log, incar_changes, incar_issues, potcar_issues, kpoints_text, potcar_titles, out_dir, potcar_note="")` | 全关键字参数，避免位置错乱 |
| 79 | `"""生成 change_report.md 的完整文本。"""` | |
| 80 | `delta = structure_delta(before, after)` | 一次算好 |
| 81 | `ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")` | |
| 83 | `lines: list[str] = []` | 逐行拼 |
| 84-90 | `lines.append(f"# 输入变更报告：{recipe_name}")`、空行、生成时间、配方文件、源结构、输出目录、空行 | 报告头（7 行） |
| 92-93 | 注释 + `lines.append("## 1. 结构变化")`、空行 | 第 1 节 |
| 95-102 | `rows = [("原子数",...), ("元素顺序",...), ("体积 (Å³)",...)]` | 三行"改前/改后/说明" |
| 103 | `lines.append(_table(rows, ("指标","改前","改后","说明")))`、空行 | 第 1 张表 |
| 105-109 | `lat_rows` 循环：`a1/a2/a3`，每行三向量按 4 位小数 | 晶格矩阵 |
| 110-111 | `_table(lat_rows, ("晶格矢量","改前 (Å)","改后 (Å)",""))` + 空行 | 第 2 张表 |
| 113-114 | 注释 + `## 2. 按顺序执行的结构操作` + 空行 | |
| 116 | `if ops_log:` | 有操作 |
| 117-118 | 每个 `item` → `f"{step}. `{op}` —— {detail}"` | 顺序与人话 |
| 119-120 | `else:` + `（配方里没有结构操作，POSCAR 按原样复制）` | |
| 121 | `lines.append("")` | |
| 123-124 | `## 3. INCAR 变更` + 空行 | |
| 126 | `if incar_changes:` | |
| 127 | `rows = [(c.key, str(c.old), str(c.new), c.reason) ...]` | |
| 128 | `lines.append(_table(rows, ("参数","改前","改后","原因")))` | |
| 129-130 | `else:` + `（没有覆盖任何 INCAR 参数，沿用预设模板）` | |
| 131 | 空行 | |
| 133-137 | 注释 + `issues = incar+potcar`、`errs`、`warns` + `## 4. 检查结果` + 空行 | |
| 139-140 | `if not errs and not warns: "- 全部检查通过。"` | |
| 141-142 | `for i in errs: f"- **ERROR** `{code}`：{message}"` | |
| 143-144 | `for i in warns: f"- WARN `{code}`：{message}"` | |
| 145 | 空行 | |
| 147-149 | `## 5. 生成的文件` + 空行 | |
| 150 | `produced = ["POSCAR", "INCAR"]` | 必写 |
| 151-152 | `if kpoints_text is not None: produced.append("KPOINTS")` | |
| 153-154 | `if potcar_titles: produced.append("POTCAR.spec")` | skip 模式不出现 |
| 155 | `lines.append("- " + "、".join(produced))` | |
| 156-157 | `if potcar_titles: "- POTCAR 顺序（spec）：..."` | |
| 158-159 | `if potcar_note: "- {potcar_note}"` | |
| 160 | 空行 | |
| 162-163 | `## 6. 下一步（请人工确认后再提交）` + 空行 | |
| 164-173 | 代码块：`cd 输出目录` / `cat change_report.md` / 有 spec 则 `cat POTCAR.spec` + 超算拼接提示 / `qsub vasp.sh` | 可复制的操作步骤 |
| 174 | 空行 | |
| 175 | `> 本报告由脚本生成，只记录实际写入的内容；不判断参数的科学合理性。` | 免责声明：工具不评价科学 |
| 176 | `return "\n".join(lines) + "\n"` | 末尾保证一个换行 |

## 3.6 `write_audit`（179-183）

| 行 | 代码 | 说明 |
|---|---|---|
| 180 | `"""向全局审计日志追加一行 JSON（不存在则创建）。"""` | |
| 181 | `path.parent.mkdir(parents=True, exist_ok=True)` | 日志目录可能不存在 |
| 182 | `with path.open("a", encoding="utf-8") as f:` | 追加模式 |
| 183 | `f.write(json.dumps(entry, ensure_ascii=False) + "\n")` | 一行一条 JSON，中文不转义 |

---

# 附：这一份文档没展开的部分

- `build_incar` 逐行 → `04-INCAR与KPOINTS.md`
- `apply_ops` 与各 `op_*` → `03-结构操作-poscar.md`
- `webui` 如何调到 `preview/write` → `06-网页与前端.md`
- `batch` 如何复用 `compose_recipe/stage_recipe/write_job_obj` → `07-批量与数据库.md`
- `build_parser`/`main`/`_print_human`/`run_potcar_only` → 本文件末尾的"命令行走查"段落（见 `00` 总表阶段一）

