"""webui.py 的冒烟测试：起一个真服务、用 HTTP 请求打它，而不是直接调函数。

【为什么用真 HTTP，而不是只测函数】
    网页问题的典型形态是"函数没错，但少了个请求头/返回码不对/JSON 没序列化"。
    真发一次请求，才能覆盖路由、编码、错误包装这些壳子。

【为什么端口用 0】
    让操作系统分配空闲端口，测试并发跑也不会撞 8765。

运行：python -m unittest tests.test_webui
"""

from __future__ import annotations

import json
import shutil
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from vasp_core import service
from vasp_core import webui

ROOT = Path(__file__).resolve().parent.parent
POSCAR_CU = """Cu fcc primitive
   1.00000000000000
     2.5527000000    0.0000000000    0.0000000000
     1.2763500000    2.2107000000    0.0000000000
     1.2763500000    0.7369000000    2.0842000000
   Cu
     1
Direct
     0.0000000000    0.0000000000    0.0000000000
"""


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 关掉模型：测试不依赖本机 Ollama，也不该因为 Ollama 在线而变慢
        webui.CFG.llm.enabled = False
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self) -> None:
        tmp_root = ROOT / "tests" / "_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_root / f"web_{uuid.uuid4().hex[:10]}"
        self.tmp.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(service.cleanup_all)
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "POSCAR").write_text(POSCAR_CU, encoding="utf-8")
        self.out = self.tmp / "out"

    # ---- HTTP 小工具 ----
    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str):
        with urllib.request.urlopen(self._url(path), timeout=30) as r:
            return r.status, r.read().decode("utf-8")

    def _post(self, path: str, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # ---- 用例 ----
    def test_index_html(self):
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("VASP 前处理", html)
        self.assertIn("/api/preview", html)

    def test_status(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("config", data)
        self.assertFalse(data["model"]["available"])  # 测试里关掉了模型

    def test_preview_and_write(self):
        status, data = self._post("/api/preview", {
            "source": str(self.src), "name": "webui_test",
            "text": "扩成 2x2x1，第 3 个原子换成 Co",
        })
        self.assertEqual(status, 200, data)
        job = data["job"]
        self.assertTrue(job["summary"]["ok"])
        self.assertEqual(job["summary"]["structure"]["natoms"], [1, 4])
        self.assertIn("POSCAR", job["diffs"])

        status, data = self._post("/api/write", {"job_id": job["id"], "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["job"]["written"])
        from vasp_core.config import ROOT
        out_dir = Path(job["output"])
        self.assertEqual(out_dir.parent, ROOT / "runs")
        self.addCleanup(lambda: shutil.rmtree(out_dir, ignore_errors=True))
        self.assertTrue((out_dir / "POSCAR").exists())
        self.assertTrue((out_dir / "change_report.md").exists())

    def test_preview_missing_dirs(self):
        status, data = self._post("/api/preview", {"source": "", "name": "", "text": "扩胞"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("源目录", data["error"])

    def test_write_needs_confirm(self):
        status, data = self._post("/api/preview", {
            "source": str(self.src), "name": "webui_test", "text": "扩成 2x2x1"})
        job = data["job"]
        status, data = self._post("/api/write", {"job_id": job["id"]})
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])

    def test_browse_endpoint(self):
        # 测试机可能没有可交互桌面，真弹窗会卡住；把函数换成假的，只验证路由与 JSON 形状。
        orig = webui.browse.pick_directory
        webui.browse.pick_directory = lambda initial=None, title="", timeout=600: r"C:\fake\picked"
        try:
            status, body = self._get("/api/browse?kind=source&initial=")
        finally:
            webui.browse.pick_directory = orig
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["path"], r"C:\fake\picked")
        self.assertFalse(data["cancelled"])

    def test_custom_name_and_invalid_name(self):
        # 自定义名称：结果文件夹就叫这个名字
        status, data = self._post("/api/preview", {
            "source": str(self.src), "name": "我的算例_d3", "text": "扩成 2x2x1"})
        self.assertEqual(status, 200, data)
        out = Path(data["job"]["output"])
        self.assertEqual(out.parent, ROOT / "runs")
        self.assertTrue(out.name.startswith("我的算例_d3"))

        # 名称里带路径分隔符：必须拒绝，不能让它写到 runs 外面
        status, data = self._post("/api/preview", {
            "source": str(self.src), "name": "../escape", "text": "扩成 2x2x1"})
        self.assertEqual(status, 400)
        self.assertIn("不能有这些字符", data["error"])

    def test_index_has_tabs_and_batch_page(self):
        # 页签与批量页是"人能用"的最低要求：少了任何一个，用户就找不到批量入口
        status, html = self._get("/")
        self.assertEqual(status, 200)
        for needle in ("pageTabs", "page-batch", "btnBatchPreview", "btnBatchWrite",
                       "/api/batch/preview", "/api/open"):
            self.assertIn(needle, html)

    def test_open_folder_endpoint(self):
        # 打开目录会真的弹资源管理器，测试里换成记录参数
        orig = webui._open_folder
        seen = {}
        webui._open_folder = lambda p: (seen.setdefault("path", Path(p)), True, "ok")[1:]
        try:
            status, body = self._get("/api/open?path=" + urllib.parse.quote(str(self.tmp)))
        finally:
            webui._open_folder = orig
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(Path(data["path"]), self.tmp)
        self.assertTrue(data["opened"])
        self.assertEqual(seen["path"], self.tmp)

    def test_open_folder_restricted_returns_path(self):
        # 受限进程启动不了 explorer：接口仍要成功，只要把路径与原因带回来
        orig = webui._open_folder
        webui._open_folder = lambda p: (False, "PermissionError: [WinError 5] 拒绝访问")
        try:
            status, body = self._get("/api/open?kind=runs")
        finally:
            webui._open_folder = orig
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["opened"])
        self.assertIn("WinError 5", data["message"])
        self.assertTrue(Path(data["path"]).name == "runs")

    def _make_batch_root(self) -> Path:
        root = self.tmp / "models"
        root.mkdir()
        for name in ("a", "b"):
            d = root / name
            d.mkdir()
            (d / "POSCAR").write_text(POSCAR_CU, encoding="utf-8")
        return root

    def test_batch_scan(self):
        root = self._make_batch_root()
        status, body = self._get("/api/batch/scan?root=" + urllib.parse.quote(str(root)))
        self.assertEqual(status, 200)
        scan = json.loads(body)["scan"]
        self.assertEqual(scan["n_found"], 2)
        self.assertEqual(scan["n_ok"], 2)
        self.assertEqual(sorted(m["name"] for m in scan["materials"]), ["a", "b"])

    def test_batch_preview_and_write(self):
        root = self._make_batch_root()
        status, data = self._post("/api/batch/preview", {
            "root": str(root), "text": "扩成 2x2x1", "name": "web_batch", "ivdw": "11,12",
        })
        self.assertEqual(status, 200, data)
        b = data["batch"]
        self.assertEqual(b["n_total"], 4)      # 2 个材料 × 2 个 IVDW
        self.assertEqual(b["n_ok"], 4)
        self.assertTrue(b["batch_dir"].endswith("web_batch"))
        self.addCleanup(lambda: shutil.rmtree(Path(b["batch_dir"]), ignore_errors=True))

        # 没 confirm 不许写
        status, data2 = self._post("/api/batch/write", {"id": b["id"]})
        self.assertEqual(status, 400)
        self.assertIn("confirm", data2["error"])

        status, data = self._post("/api/batch/write", {"id": b["id"], "confirm": True})
        self.assertEqual(status, 200, data)
        bw = data["batch"]
        self.assertEqual(bw["written"], 4)
        self.assertEqual(bw["failed"], 0)
        batch_dir = Path(bw["batch_dir"])
        # 材料优先 + 参数子目录：batches/<批次>/<材料>/ivdw_<值>/
        self.assertTrue((batch_dir / "a" / "ivdw_11" / "POSCAR").exists())
        self.assertTrue((batch_dir / "b" / "ivdw_12" / "INCAR").exists())
        self.assertTrue((batch_dir / "batch_summary.csv").exists())

    def test_batch_preview_needs_root_and_text(self):
        status, data = self._post("/api/batch/preview", {"root": "", "text": "扩成 2x2x1"})
        self.assertEqual(status, 400)
        self.assertIn("批量根目录", data["error"])
        status, data = self._post("/api/batch/preview", {"root": str(self.tmp), "text": ""})
        self.assertEqual(status, 400)

    def test_db_providers(self):
        status, body = self._get("/api/db/providers")
        self.assertEqual(status, 200)
        ids = [p["id"] for p in json.loads(body)["providers"]]
        self.assertIn("mp", ids)
        self.assertIn("c2db", ids)

    def test_db_search_and_download(self):
        # 不联网：把 search/download 换成假函数，只验证路由、JSON 形状与参数透传
        orig_search, orig_download = webui.db_mod.search, webui.db_mod.download
        webui.db_mod.search = lambda provider, formula, limit, cfg: [
            webui.db_mod.Candidate(provider="mp", id="mp-1", formula=formula, nsites=2)]
        webui.db_mod.download = lambda cand, cfg, **kw: self.tmp / "materials" / "mp_mp-1"
        try:
            status, body = self._get("/api/db/search?provider=mp&formula=Cu")
            self.assertEqual(status, 200)
            items = json.loads(body)["items"]
            self.assertEqual(items[0]["id"], "mp-1")
            self.assertEqual(items[0]["nsites"], 2)

            status, data = self._post("/api/db/download", {"provider": "mp", "id": "mp-1"})
            self.assertEqual(status, 200, data)
            self.assertTrue(data["path"].endswith("mp_mp-1"))
        finally:
            webui.db_mod.search, webui.db_mod.download = orig_search, orig_download

    def test_db_import_endpoint(self):
        # 真导入一个 POSCAR（不联网、不需要 pymatgen）
        status, data = self._post("/api/db/import", {"path": str(self.src / "POSCAR")})
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["paths"]), 1)
        self.assertTrue((Path(data["paths"][0]) / "POSCAR").exists())

    def test_ls_endpoint(self):
        (self.tmp / "sub").mkdir()
        (self.tmp / "a.txt").write_text("x", encoding="utf-8")
        status, body = self._get("/api/ls?path=" + urllib.parse.quote(str(self.tmp)))
        self.assertEqual(status, 200)
        data = json.loads(body)
        names = [i["name"] for i in data["items"]]
        self.assertIn("sub", names)
        self.assertIn("a.txt", names)
        self.assertEqual(data["parent"], str(self.tmp.parent))

    def test_ls_missing_dir(self):
        # 不存在的目录：返回 400 + 中文原因（do_GET 里把 PrepError 当输入错误）
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/ls?path=" + urllib.parse.quote(str(self.tmp / "nope")))
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_route(self):
        status, data = self._post("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
