"""本地模型接入层：探测 Ollama / 调 chat/completions / 兜底规则。

【这条链的分工，先讲清楚】
    你打一句中文
        → 程序（本文件）把它和"结构摘要"发给本地模型
        → 模型只回一个 JSON：要改什么（扩胞？替换？改 ENCUT？）
        → 程序校验 JSON（白名单、范围），不合格就退回规则模式
        → 程序自己读 POSCAR、算新坐标、写新文件
    模型看不到文件内容，也不写文件。它只负责"听懂人话"。

【为什么用 OpenAI 兼容协议而不是 Ollama 私有 API】
    Ollama 在 /v1 路径上提供了 OpenAI 兼容接口，vLLM、llama.cpp server、
    LM Studio 也都提供同一套。程序只认这一个协议，以后换推理引擎只改 base_url。

【为什么不直接把整份 POSCAR 丢给模型】
    1) 准确性：200 行坐标让模型抄，迟早错一个数字；程序解析是确定性的。
    2) 隐私：结构可能是未发表数据；摘要足够让它理解"第 3 个原子"指谁。
    3) 成本：7B 模型上下文有限，塞满坐标反而更容易答错。

【不这么干会怎样】
    如果让模型直接输出 POSCAR 文本，就等于把"正确性"交给采样温度；
    错一个元素顺序，VASP 不会报错，只会算错体系——这是最危险的一类错误。

【别的办法】
    - 完全不用模型：nl.py 的规则模式能覆盖扩胞/替换/空位/真空/ENCUT/K 点/应变；
    - 用 tool-calling：把操作做成函数让模型选（本项目在 tools.py/agent.py 里有），
      但网页场景下"模型直接回 JSON"更简单、更容易校验，所以默认走 JSON。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config, ROOT

# 模型输出记录（排查"为什么没走模型/模型答了什么"用；只记摘要，不记结构）
TRACE_FILE = ROOT / "llm_trace.jsonl"


class LlmError(RuntimeError):
    """模型不可用/答非所问。调用方应捕获并退回规则模式。"""


@dataclass
class ProbeResult:
    """一次探活的结果。available=False 时 reason 说明原因。"""

    available: bool
    base_url: str
    models: list[str] = field(default_factory=list)
    reason: str = ""
    elapsed_ms: int = 0

    @property
    def has_model(self) -> bool:
        """配置里指定的模型是否真的在列表里（名字可能带 :latest 后缀）。"""
        return False  # 由 probe() 填好后覆盖；见 probe_for()

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "base_url": self.base_url,
            "models": self.models,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# 1) 探活：GET /models。只读、不加载模型（Ollama 在列表里不等于已加载进显存）
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def probe(cfg: Config, timeout: float = 3.0, model: str | None = None) -> ProbeResult:
    """探测模型端点。

    【做什么】请求 {base_url}/models，看服务在不在、配置的模型在不在。
    【为什么 3 秒超时】探活是为了"决定要不要用模型"，不是等结果；
        3 秒回不来就说明本机服务没起，没必要让用户干等。
    【不这么干会怎样】不探活直接调，每次都要等 120 秒超时才失败，界面像卡死。
    """
    base = cfg.llm.base_url.rstrip("/")
    t0 = time.time()
    try:
        data = _get_json(f"{base}/models", timeout=timeout)
    except urllib.error.HTTPError as exc:
        return ProbeResult(False, base, reason=f"HTTP {exc.code}（端点可能不是 OpenAI 兼容）",
                           elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:  # 连接拒绝/超时/DNS 全都归到这里
        return ProbeResult(False, base, reason=f"{type(exc).__name__}: {exc}",
                           elapsed_ms=int((time.time() - t0) * 1000))

    ids: list[str] = []
    for item in data.get("data", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    want = model or cfg.llm.model
    # 名字容错：配置写 qwen2.5-coder:7b，服务里可能是同名的 :latest
    matched = any(i == want or i.split(":")[0] == want.split(":")[0] for i in ids)
    if not matched:
        return ProbeResult(False, base, models=ids,
                           reason=f"服务在线，但没有找到模型 {want!r}；可用：{', '.join(ids) or '（空）'}",
                           elapsed_ms=int((time.time() - t0) * 1000))
    return ProbeResult(True, base, models=ids, reason="ok",
                       elapsed_ms=int((time.time() - t0) * 1000))


# ---------------------------------------------------------------------------
# 2) 调模型：POST /chat/completions
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是 VASP 前处理的"翻译器"。用户会用中文描述怎么改结构或参数，你把它翻译成一个 JSON 操作清单。

只输出 JSON，不要解释、不要 markdown 代码块。格式：
{"ops":[{"op":"操作名", ...参数}],"incar":{"preset":"relax|static|band","overrides":{键:值}},"kpoints":{"mode":"gamma","mesh":[a,b,c]},"calc":"relax|static|band","needs_human":["做不到/需要人确认的点"],"notes":"一句话说明"}

允许的操作（只能用这些）：
- supercell: {"op":"supercell","scaling":[a,b,c]}
- substitute: {"op":"substitute","index":N,"species":"Co"}   （index 从 1 开始，是当前结构的第 N 个原子）
- substitute_all: {"op":"substitute_all","mapping":{"Cu":"Ni"}}
- vacancy: {"op":"vacancy","indices":[N,...]}
- add_atom: {"op":"add_atom","species":"H","fractional":[x,y,z]} 或 "cartesian"
- strain: {"op":"strain","matrix":[[..],[..],[..]]}   （单位：小数，0.02=+2%）
- vacuum: {"op":"vacuum","thickness":15.0,"axis":2,"place":"center"}
- fix_atoms: {"op":"fix_atoms","indices":[N,...],"axes":"123"}
- reorder_species: {"op":"reorder_species","order":["Cu","O"]}
- translate: {"op":"translate","vector":[dx,dy,dz],"mode":"cartesian"}
- surface: {"op":"surface","miller":[1,1,1],"layers":4,"vacuum":15.0,"fix_layers":0}
- adsorbate: {"op":"adsorbate","species":"N","site":"ontop","height":2.0}
- fix_layers: {"op":"fix_layers","n_layers":2,"tol":0.1,"axes":"123"}

允许改的 INCAR 键（只能用这些）：ENCUT, EDIFF, EDIFFG, PREC, ALGO, ISMEAR, SIGMA, ISIF, IBRION, NSW, LREAL, LWAVE, LCHARG, ISPIN, MAGMOM, NELM, KSPACING, LORBIT, ICHARG
禁止（任何情况下都不要写）：LDAU, LDAUTYPE, LDAUL, LDAUU, LDAUJ, NELECT, LHFCALC, AEXX, HFSCREEN, METAGGA, IVDW, GGA

规则：
1) 用户没提到的字段不要编造；不确定就省略。
2) "第 N 个原子"按当前结构编号（从 1 开始）；用户如果说"所有 Cu"，用 substitute_all。
3) 切表面、加吸附质、固定层、ML 预弛豫属于高阶操作，能做就写，缺参数就放进 needs_human。
4) 危险键（DFT+U、泛函、NELECT、磁序设置）一律放进 needs_human，不要写进 overrides。
5) 中文里的百分比：拉伸 2% = strain 对角 0.02。
"""


def _post_json(url: str, payload: dict, timeout: float, api_key: str = "") -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def chat(cfg: Config, messages: list[dict], timeout: float | None = None) -> str:
    """发一次对话，返回模型文本。失败抛 LlmError（调用方负责退回规则）。"""
    base = cfg.llm.base_url.rstrip("/")
    payload = {
        "model": cfg.llm.model,
        "messages": messages,
        "temperature": 0,   # 翻译任务不需要创造性；0 让同一句话每次结果一致
        "stream": False,
        "max_tokens": 1024,
    }
    try:
        data = _post_json(f"{base}/chat/completions", payload,
                          timeout=timeout or cfg.llm.timeout, api_key=cfg.llm.api_key)
    except Exception as exc:
        raise LlmError(f"调用模型失败：{type(exc).__name__}: {exc}") from exc

    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"模型返回格式不对：{str(data)[:300]}") from exc


def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。

    【为什么不用正则】JSON 里可能有嵌套数组，正则匹配到第一个 } 就断，
        得到半截 JSON。这里用括号配平（跳过字符串内部的括号），能正确处理嵌套。
    【不这么干会怎样】模型多打一句话（"好的，这是结果："）就解析失败，
        整个流程退回规则模式，明明模型答对了却用不上。
    """
    s = text.strip()
    if s.startswith("```"):
        # 去掉 ```json ... ``` 包装
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    start = s.find("{")
    if start < 0:
        raise LlmError("模型输出里没有 JSON（找不到 {）")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise LlmError(f"模型给的 JSON 不合法：{exc}") from exc
                if not isinstance(obj, dict):
                    raise LlmError("模型给的 JSON 顶层不是对象")
                return obj
    raise LlmError("模型输出的 JSON 括号没有闭合（可能被长度截断）")


def _summary_for_model(summary: dict, privacy: str) -> dict:
    """把结构摘要压缩到"够模型理解、又不泄露细节"的程度。

    strict：原子数、元素、晶格长度（取整）、有没有 INCAR/KPOINTS；不含坐标。
    normal：再加 INCAR 键名列表（不含值）与计算类型提示。
    """
    out = {
        "natoms": summary.get("natoms"),
        "species": summary.get("species"),
        "counts": summary.get("counts"),
        "lattice_abc": [round(x, 2) for x in summary.get("lattice_abc", [])],
        "has_incar": summary.get("has_incar"),
        "has_kpoints": summary.get("has_kpoints"),
    }
    if privacy != "strict":
        out["incar_keys"] = summary.get("incar_keys", [])
        out["calc_guess"] = summary.get("calc_guess")
    return out


def text_to_recipe(cfg: Config, text: str, summary: dict) -> dict:
    """让模型把中文翻译成操作 JSON；返回与 nl.text_to_recipe 相同的形状。

    返回值：
      {"recipe": {...}, "matched": ["model:qwen2.5-coder:7b"], "needs_human": [...],
       "unmatched": [], "meta": {"source":"model","model":...,"elapsed_ms":...}}
    """
    t0 = time.time()
    user_msg = (
        "当前结构摘要：\n"
        + json.dumps(_summary_for_model(summary, cfg.llm.privacy), ensure_ascii=False)
        + "\n\n用户需求：" + text
    )
    content = chat(cfg, [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_msg}])
    data = _extract_json(content)

    ops = data.get("ops") or []
    if not isinstance(ops, list):
        raise LlmError("ops 不是数组")
    clean_ops: list[dict] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise LlmError(f"第 {i + 1} 个 op 不是对象")
        name = op.get("op") or op.get("name")
        if not name:
            raise LlmError(f"第 {i + 1} 个 op 没有名字")
        item = dict(op)
        item["op"] = str(name)
        item.pop("name", None)
        clean_ops.append(item)

    recipe = {
        "version": 2,
        "name": "from_nl",
        "calc": str(data.get("calc") or "relax"),
        "base_dir": ".",
        "structure": {"file": "POSCAR", "ops": clean_ops},
        "incar": {"overrides": _clean_overrides(data.get("incar", {}))},
    }
    if data.get("kpoints"):
        recipe["kpoints"] = data["kpoints"]

    needs = [str(x) for x in (data.get("needs_human") or []) if str(x).strip()]
    elapsed = int((time.time() - t0) * 1000)
    trace({"ts": datetime.now().isoformat(timespec="seconds"), "ok": True,
           "source": "model", "model": cfg.llm.model, "elapsed_ms": elapsed,
           "ops": [o["op"] for o in clean_ops], "needs_human": len(needs),
           "privacy": cfg.llm.privacy, "request_chars": len(text)})
    return {"recipe": recipe, "matched": [f"model:{cfg.llm.model}"],
            "needs_human": needs, "unmatched": [],
            "meta": {"source": "model", "model": cfg.llm.model, "elapsed_ms": elapsed}}


def _clean_overrides(incar: dict) -> dict:
    """只保留 {overrides:{...}} 里的字典部分；模型偶尔会多套一层。"""
    if not isinstance(incar, dict):
        return {}
    ov = incar.get("overrides", incar)
    if not isinstance(ov, dict):
        return {}
    return {str(k).upper(): v for k, v in ov.items() if str(k).strip()}


def trace(record: dict) -> None:
    """追加一行调用记录。失败不影响主流程（磁盘满/没权限就跳过）。"""
    try:
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
