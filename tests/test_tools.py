#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tests/test_tools.py —— 工具层 / MCP 服务的回归测试（纯标准库 unittest）。

覆盖任务要求的五类：
1. schema 结构合法（OpenAI 与 MCP 两种视图同源、必填/字段/额外属性）
2. 未知工具 / 缺参 / 类型错 / 未知参数 / 枚举与范围错误被拒
3. write_inputs 未 confirm 被拒（缺参 + 显式 false 两条路径），confirm 后能写新目录、
   且第二次写同一目录被拒（不覆盖）
4. submit_hint 只有文本、没有执行副作用（executed 恒 false，结果里没有 qsub/ssh/scp 命令）
5. MCP `--selftest` 退出 0；另外用真实子进程走一遍 initialize/tools/list/tools/call，
   确认协议层与"工具失败用 isError 表达"的约定。

所有测试产物只落在工作区 `.tmp_build/tool_test`（任务硬约束），每个用例独立目录、
结束即删；不联网、不提交作业、不碰用户数据。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # vasp-prep
sys.path.insert(0, str(ROOT))                        # 让 `import vasp_core` 在仓库根下可用

from vasp_core import tools as tools_mod  # noqa: E402
from vasp_core.tools import (  # noqa: E402
    TOOLS,
    dispatch,
    tool_specs_mcp,
    tool_specs_openai,
)

TMP_ROOT = ROOT.parent / ".tmp_build" / "tool_test"

# Cu fcc 原胞：1 个原子，用来验证结构读取与 2x2x2 扩胞（1 → 8 原子）。
POSCAR_CU = """Cu fcc primitive
1.0
  2.5527 0 0
  1.2763 2.2107 0
  1.2763 0.7369 2.0842
Cu
1
Direct
  0 0 0
"""

EXPECTED_TOOLS = [
    "workspace_status",
    "read_structure",
    "read_incar",
    "propose_recipe",
    "validate_recipe",
    "write_inputs",
    "verify_run",
    "submit_hint",
]


class Base(unittest.TestCase):
    """每个用例一个独立临时目录，放在工作区允许的 .tmp_build/tool_test 下。

    为什么不用 tempfile.mkdtemp：本仓库 test_core.py 已记录过 Python 3.14 的
    mkdtemp 权限怪癖（0o700 导致创建者自己不可写），统一用显式 mkdir 更稳，
    也满足"临时文件只放 .tmp_build/tool_test"的约束。
    """

    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.case = TMP_ROOT / f"case_{uuid.uuid4().hex[:10]}"
        self.case.mkdir(parents=True, exist_ok=False)
        self.addCleanup(shutil.rmtree, self.case, True)
        self.poscar = self.case / "POSCAR"
        self.poscar.write_text(POSCAR_CU, encoding="utf-8")

    def recipe_data(self, **overrides) -> dict:
        """最小可写算例：扩胞 + ENCUT，赝势 skip（CI 环境通常没有赝势库）。"""
        data = {
            "version": 2,
            "name": "cu",
            "calc": "relax",
            "base_dir": str(self.case),
            "structure": {
                "file": "POSCAR",
                "ops": [{"op": "supercell", "scaling": [2, 2, 2]}],
            },
            "incar": {"overrides": {"ENCUT": 600}},
            "potcar": {"mode": "skip"},
        }
        data.update(overrides)
        return data

    def write_recipe_file(self, data: dict, name: str = "recipe.json") -> Path:
        path = self.case / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# 1. schema 结构
# ---------------------------------------------------------------------------

class SchemaTests(Base):
    def test_openai_specs_shape(self) -> None:
        specs = tool_specs_openai()
        self.assertEqual(len(specs), 8)
        names = []
        for spec in specs:
            self.assertEqual(spec["type"], "function")
            fn = spec["function"]
            self.assertIn("name", fn)
            self.assertIn("description", fn)
            self.assertTrue(fn["description"].strip())
            params = fn["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIs(params["additionalProperties"], False)
            self.assertIsInstance(params["properties"], dict)
            self.assertTrue(params["properties"])
            self.assertIsInstance(params.get("required", []), list)
            self.assertTrue(set(params.get("required", [])) <= set(params["properties"]))
            names.append(fn["name"])
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_mcp_specs_shape(self) -> None:
        specs = tool_specs_mcp()
        self.assertEqual([s["name"] for s in specs], EXPECTED_TOOLS)
        for spec in specs:
            self.assertIn("description", spec)
            schema = spec["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            self.assertIn("properties", schema)

    def test_mutating_flags(self) -> None:
        # 唯一允许写盘的是 write_inputs；submit_hint 只产文本，绝不能标记为 mutating。
        self.assertTrue(TOOLS["write_inputs"]["mutating"])
        self.assertFalse(TOOLS["submit_hint"]["mutating"])
        mutating = [name for name, tool in TOOLS.items() if tool["mutating"]]
        self.assertEqual(mutating, ["write_inputs"])

    def test_handlers_registered(self) -> None:
        for name, tool in TOOLS.items():
            self.assertEqual(tool["name"], name)
            self.assertTrue(callable(tool["handler"]))
            self.assertIsInstance(tool["parameters"], dict)


# ---------------------------------------------------------------------------
# 2. 参数校验
# ---------------------------------------------------------------------------

class ValidationTests(Base):
    def test_unknown_tool(self) -> None:
        result = dispatch("not_a_tool", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown-tool")

    def test_missing_argument(self) -> None:
        result = dispatch("read_structure", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "missing-argument")
        self.assertEqual(result["error"]["field"], "path")

    def test_type_error(self) -> None:
        result = dispatch("read_structure", {"path": str(self.poscar), "max_atoms": "many"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-type")
        self.assertEqual(result["error"]["field"], "max_atoms")

    def test_bool_is_not_integer(self) -> None:
        # JSON 里 true 不是整数；不显式排除会导致 `max_atoms: true` 被当成 1。
        result = dispatch("read_structure", {"path": str(self.poscar), "max_atoms": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-type")

    def test_unknown_argument(self) -> None:
        result = dispatch("workspace_status", {"bogus": 1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown-argument")
        self.assertEqual(result["error"]["field"], "bogus")

    def test_enum_error(self) -> None:
        result = dispatch("validate_recipe", {"recipe": {"version": 2}, "potcar_mode": "nope"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-enum")

    def test_range_error(self) -> None:
        result = dispatch("read_structure", {"path": str(self.poscar), "max_atoms": 0})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-range")

    def test_arguments_not_object(self) -> None:
        result = dispatch("read_structure", [str(self.poscar)])  # type: ignore[arg-type]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-arguments")

    def test_missing_file(self) -> None:
        result = dispatch("read_structure", {"path": str(self.case / "nope.vasp")})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "file-not-found")


# ---------------------------------------------------------------------------
# 3. 只读工具
# ---------------------------------------------------------------------------

class ReadToolsTests(Base):
    def test_read_structure(self) -> None:
        result = dispatch("read_structure", {"path": str(self.poscar)})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertEqual(data["summary"]["natoms"], 1)
        self.assertEqual(data["summary"]["species"], ["Cu"])
        self.assertEqual(data["atoms"][0]["index"], 1)
        self.assertEqual(data["atoms"][0]["species"], "Cu")
        self.assertEqual(data["atoms"][0]["frac"], [0.0, 0.0, 0.0])
        self.assertFalse(data["truncated"])

    def test_read_incar(self) -> None:
        incar = self.case / "INCAR"
        incar.write_text(
            "ENCUT = 520\nEDIFF = 1e-5\nISPIN = 1\nFOO = 1\nFOO = 2\n",
            encoding="utf-8",
        )
        result = dispatch("read_incar", {"path": str(incar)})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertEqual(data["keys"]["ENCUT"], "520")
        self.assertIn("FOO", data["unknown_keys"])
        self.assertIn("FOO", data["duplicates"])
        self.assertEqual(data["n_keys"], 4)

    def test_workspace_status(self) -> None:
        result = dispatch("workspace_status", {"workspace": str(self.case)})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertEqual(data["workspace"], str(self.case.resolve()))
        self.assertGreaterEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["kind"], "structure")
        for key in ("VASP_HOST", "PMG_VASP_PSP_DIR", "VASP_PP_PATH"):
            self.assertIn(key, data["env"])
        self.assertIn("python", data)
        self.assertIn("cluster_defaults", data)


# ---------------------------------------------------------------------------
# 4. 配方流程：propose → validate → write
# ---------------------------------------------------------------------------

class RecipeFlowTests(Base):
    def test_propose_recipe(self) -> None:
        result = dispatch("propose_recipe", {"text": "把铜扩成 2x2x2 超胞，ENCUT 设成 600"})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertIsInstance(data["recipe"], dict)
        self.assertEqual(data["recipe"]["version"], 2)
        self.assertIsInstance(data["matched"], list)
        self.assertIsInstance(data["needs_human"], list)
        # recipe_json 必须能原样解析回对象，模型可以直接把它传给 validate_recipe
        self.assertEqual(json.loads(data["recipe_json"])["version"], 2)

    def test_propose_recipe_with_base(self) -> None:
        base = self.write_recipe_file(self.recipe_data())
        result = dispatch("propose_recipe", {"text": "ENCUT 改成 700", "base_recipe": str(base)})
        self.assertTrue(result["ok"], result)
        self.assertIn("recipe", result["result"])

    def test_validate_recipe_object_ok(self) -> None:
        result = dispatch("validate_recipe", {"recipe": self.recipe_data(), "potcar_mode": "skip"})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertEqual(data["structure"]["natoms"], [1, 8])
        self.assertEqual(data["errors"], [])
        self.assertIn("ENCUT", data["incar_diff"])
        self.assertIn("600", data["incar_diff"])
        self.assertFalse(Path(data["out_dir_preview"]).exists(), "dry-run 不应创建输出目录")

    def test_validate_recipe_file_and_bad_calc(self) -> None:
        good = self.write_recipe_file(self.recipe_data())
        result = dispatch("validate_recipe", {"recipe": str(good), "potcar_mode": "skip"})
        self.assertTrue(result["ok"], result)

        bad = self.write_recipe_file(self.recipe_data(calc="not-a-calc"), "bad.json")
        result = dispatch("validate_recipe", {"recipe": str(bad), "potcar_mode": "skip"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-input")

    def test_write_inputs_requires_confirm(self) -> None:
        out_dir = self.case / "runs" / "cu"
        recipe = self.recipe_data()

        # 缺 confirm：schema 层就拒绝
        result = dispatch("write_inputs", {"recipe": recipe, "out_dir": str(out_dir)})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "missing-argument")
        self.assertEqual(result["error"]["field"], "confirm")
        self.assertFalse(out_dir.exists())

        # 显式 false：handler 层拒绝
        result = dispatch("write_inputs", {"recipe": recipe, "out_dir": str(out_dir),
                                           "confirm": False})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "confirm-required")
        self.assertFalse(out_dir.exists(), "未确认时绝不能创建目录")

    def test_write_inputs_writes_new_dir_only(self) -> None:
        out_dir = self.case / "runs" / "cu"
        result = dispatch("write_inputs", {
            "recipe": self.recipe_data(),
            "out_dir": str(out_dir),
            "confirm": True,
            "potcar_mode": "skip",
        })
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertFalse(data["dry_run"])
        # potcar_mode=skip 的语义就是"不碰赝势"，因此不该出现 POTCAR.spec
        for name in ("POSCAR", "INCAR", "change_report.md"):
            self.assertTrue((out_dir / name).exists(), f"缺少 {name}")
        self.assertFalse((out_dir / "POTCAR.spec").exists())
        self.assertIn("POSCAR", data["files"])
        incar_text = (out_dir / "INCAR").read_text(encoding="utf-8")
        self.assertIn("ENCUT", incar_text)
        self.assertIn("600", incar_text)
        # 2x2x2 后 POSCAR 应有 8 个原子（元素行 Cu 下一行计数为 8）
        self.assertIn("8", (out_dir / "POSCAR").read_text(encoding="utf-8"))
        self.assertTrue(data["incar_diff"])

        # 同一目录再写一次必须被拒（绝不覆盖已有算例）
        again = dispatch("write_inputs", {
            "recipe": self.recipe_data(),
            "out_dir": str(out_dir),
            "confirm": True,
            "potcar_mode": "skip",
        })
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"]["code"], "out-dir-exists")


# ---------------------------------------------------------------------------
# 5. 结果核验与提交提示
# ---------------------------------------------------------------------------

class VerifyTests(Base):
    def test_verify_empty_dir(self) -> None:
        result = dispatch("verify_run", {"run_dir": str(self.case)})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertFalse(data["converged"])
        self.assertEqual(data["status"], "not-converged")
        self.assertIsInstance(data["notes"], list)

    def test_verify_missing_dir(self) -> None:
        result = dispatch("verify_run", {"run_dir": str(self.case / "nope")})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "run-dir-not-found")


class SubmitHintTests(Base):
    def test_hint_has_no_execution_side_effect(self) -> None:
        result = dispatch("submit_hint", {"run_dir": str(self.case), "host": "me@ln01"})
        self.assertTrue(result["ok"], result)
        data = result["result"]
        self.assertIs(data["executed"], False)
        preview = data["command"]["preview"]
        self.assertIn("vasp_core.cluster up", preview)
        self.assertIn("--dry-run", preview)
        self.assertIn("me@ln01", preview)
        # 结果文本里不能出现真正提交作业的命令；工具只给预览。
        blob = json.dumps(data, ensure_ascii=False)
        for forbidden in ("qsub", "ssh ", "scp "):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(data["preflight"]["has_poscar"], True)
        self.assertIn("checklist", data)

    def test_hint_rejects_dangerous_host(self) -> None:
        result = dispatch("submit_hint", {"run_dir": str(self.case),
                                          "host": "me@ln01; rm -rf /"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid-host")

    def test_hint_missing_run_dir(self) -> None:
        result = dispatch("submit_hint", {"run_dir": str(self.case / "nope"),
                                          "host": "me@ln01"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "run-dir-not-found")


# ---------------------------------------------------------------------------
# 6. MCP 服务
# ---------------------------------------------------------------------------

class McpTests(Base):
    def _run_server(self, *extra: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "vasp_mcp.server", *extra],
            cwd=str(ROOT),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    def test_selftest_exit_zero(self) -> None:
        proc = self._run_server("--selftest")
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertIn("selftest PASS", proc.stdout)
        self.assertIn("tools/list", proc.stdout)

    def test_stdio_protocol_roundtrip(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "unittest", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "workspace_status", "arguments": {"workspace": str(self.case)}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        stdin = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
        proc = self._run_server(stdin=stdin)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 4)
        responses = {item["id"]: item for item in (json.loads(line) for line in lines)}
        self.assertEqual(responses[1]["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", responses[1]["result"]["capabilities"])
        self.assertEqual(len(responses[2]["result"]["tools"]), 8)
        call = responses[3]["result"]
        self.assertFalse(call["isError"])
        payload = json.loads(call["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(responses[4]["result"], {})

    def test_tool_failure_is_iserror_not_protocol_error(self) -> None:
        # 工具参数错必须走 tools/call 的 isError，而不是 JSON-RPC error；
        # 否则客户端会当成传输层故障，模型看不到"缺哪个参数"。
        request = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                   "params": {"name": "read_structure", "arguments": {}}}
        proc = self._run_server(stdin=json.dumps(request) + "\n")
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "missing-argument")

    def test_unknown_method_error_code(self) -> None:
        proc = self._run_server(
            stdin=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "no/such"}) + "\n")
        response = json.loads(proc.stdout.strip().splitlines()[0])
        self.assertEqual(response["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
