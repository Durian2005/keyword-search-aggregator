"""web 层测试：payload 校验、作业注册表、HTTP 路由、静态文件穿越防护。

铁律：**绝不发真实请求**。runner 全部注入假实现，跑的是内存里的日志与状态机；
真实抓取由 smoke test / 手工验收覆盖，不进单测（否则 0.5s 变 17s，见
tests/test_pipeline.py 的同一约定）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.web.jobs import (  # noqa: E402
    MAX_LOG_LINES,
    JobRegistry,
    RunPayload,
    format_result_lines,
)
from kwsearch.web.server import WebContext, _safe_static, build_handler  # noqa: E402

_CFG = """
version: 1
defaults: {timeout: 5}
channels:
  - id: ok_ch
    type: serp
    parser: bing_html
    url_template: "https://x.test/?q={q}&first={offset}"
    paging: {mode: offset, param: first, start: 1, step: 10, max_pages: 1}
    throttle: {min_interval_s: 0}
  - id: none_ch
    type: serp
    parser: __not_registered__
    url_template: "https://x.test/?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
"""


def _write_cfg(dir_path: Path) -> str:
    p = dir_path / "channels.yaml"
    p.write_text(_CFG, encoding="utf-8")
    return str(p)


def _load_cfg(cfg_path: str):
    from kwsearch.config import load_config

    return load_config(cfg_path)


def _fake_runner(gate: threading.Event | None = None, fail: bool = False,
                 report_url: str | None = "/r/fake/index.html"):
    """记录每次调用；gate 用于把作业按在 running 状态测 409。"""
    calls: list[RunPayload] = []

    def runner(payload: RunPayload, log) -> str | None:
        calls.append(payload)
        log("第一行进度")
        if gate is not None:
            gate.wait(5)
        if fail:
            raise RuntimeError("boom")
        log("第二行进度")
        return report_url

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ── payload 校验 ──────────────────────────────────────────────────────────


class TestRunPayload(unittest.TestCase):
    def test_valid_minimal(self) -> None:
        p = RunPayload.from_mapping({"keyword": "红黑树"})
        self.assertEqual(p.keyword, "红黑树")
        self.assertFalse(p.person)
        self.assertIsNone(p.fetch_body)

    def test_keyword_required(self) -> None:
        for bad in ({}, {"keyword": ""}, {"keyword": "   "}, {"keyword": None}):
            with self.assertRaises(ValueError):
                RunPayload.from_mapping(bad)

    def test_keyword_illegal_chars(self) -> None:
        # 这些字符要么建不出 report/<关键词>/ 目录，要么直接穿越
        for kw in ("a/b", "a\\b", "a:b", "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b",
                   "a\x00b", "a\nb"):
            with self.assertRaises(ValueError, msg=kw):
                RunPayload.from_mapping({"keyword": kw})

    def test_keyword_too_long(self) -> None:
        with self.assertRaises(ValueError):
            RunPayload.from_mapping({"keyword": "x" * 101})

    def test_anchors_split_and_dedupe(self) -> None:
        p = RunPayload.from_mapping({
            "keyword": "张军", "person": True,
            "orgs": "某某大学，某某研究院", "fields": ["网络安全", "网络安全", "密码学"],
            "roles": "教授",
        })
        self.assertTrue(p.person)
        self.assertEqual(p.orgs, ("某某大学", "某某研究院"))
        self.assertEqual(p.fields, ("网络安全", "密码学"))
        self.assertEqual(p.roles, ("教授",))

    def test_bad_types(self) -> None:
        with self.assertRaises(ValueError):
            RunPayload.from_mapping({"keyword": "x", "max_docs": "abc"})
        with self.assertRaises(ValueError):
            RunPayload.from_mapping({"keyword": "x", "fetch_body": "yes"})
        with self.assertRaises(ValueError):
            RunPayload.from_mapping(["not", "a", "mapping"])

    def test_fetch_body_false_passes_through(self) -> None:
        p = RunPayload.from_mapping({"keyword": "x", "fetch_body": False})
        self.assertIs(p.fetch_body, False)


# ── 注册表 ────────────────────────────────────────────────────────────────


class TestJobRegistry(unittest.TestCase):
    def test_serializes_runs(self) -> None:
        gate = threading.Event()
        reg = JobRegistry(_fake_runner(gate=gate))
        first = reg.start(RunPayload(keyword="甲"))
        self.assertIsNotNone(first)
        self.assertTrue(reg.busy)
        self.assertIsNone(reg.start(RunPayload(keyword="乙")), "运行中必须拒绝第二个")
        gate.set()
        self._wait_done(reg, first.id)
        self.assertFalse(reg.busy)
        second = reg.start(RunPayload(keyword="乙"))
        self.assertIsNotNone(second, "上一个结束后必须能再提交")

    def test_done_captures_log_and_report(self) -> None:
        reg = JobRegistry(_fake_runner())
        job = reg.start(RunPayload(keyword="甲"))
        done = self._wait_done(reg, job.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.lines, ["第一行进度", "第二行进度"])
        self.assertEqual(done.report_url, "/r/fake/index.html")
        self.assertIsNone(done.error)

    def test_runner_error_becomes_error_job(self) -> None:
        reg = JobRegistry(_fake_runner(fail=True))
        job = reg.start(RunPayload(keyword="甲"))
        done = self._wait_done(reg, job.id)
        self.assertEqual(done.status, "error")
        self.assertIn("boom", done.error or "")
        self.assertIsNone(done.report_url)
        self.assertFalse(reg.busy, "失败也必须释放占用")

    def test_no_report_url_is_still_done(self) -> None:
        reg = JobRegistry(_fake_runner(report_url=None))
        job = reg.start(RunPayload(keyword="甲"))
        done = self._wait_done(reg, job.id)
        self.assertEqual(done.status, "done")
        self.assertIsNone(done.report_url)

    def test_log_lines_capped(self) -> None:
        def noisy(payload, log):
            for i in range(MAX_LOG_LINES + 100):
                log(f"#{i}")
            return None

        reg = JobRegistry(noisy)
        job = reg.start(RunPayload(keyword="甲"))
        done = self._wait_done(reg, job.id)
        self.assertEqual(len(done.lines), MAX_LOG_LINES)
        self.assertEqual(done.lines[0], f"#{100}", "超出上限应丢最旧的")

    def test_latest_and_get(self) -> None:
        reg = JobRegistry(_fake_runner())
        a = reg.start(RunPayload(keyword="甲"))
        self._wait_done(reg, a.id)
        time.sleep(0.01)
        b = reg.start(RunPayload(keyword="乙"))
        self._wait_done(reg, b.id)
        self.assertEqual(reg.latest().id, b.id)
        self.assertEqual(reg.get(a.id).id, a.id)
        self.assertIsNone(reg.get("nope"))

    def _wait_done(self, reg: JobRegistry, job_id: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = reg.get(job_id)
            if job is not None and job.status != "running":
                return job
            time.sleep(0.02)
        self.fail(f"作业 {job_id} 未在 {timeout}s 内结束")


# ── 静态路径解析 ──────────────────────────────────────────────────────────


class TestSafeStatic(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "红黑树").mkdir()
        (self.root / "红黑树" / "index.html").write_text("<html>报告</html>",
                                                      encoding="utf-8")
        (self.root / "data.json").write_text("{}", encoding="utf-8")

    def test_normal_keyword(self) -> None:
        p = _safe_static(self.root, "红黑树/index.html")
        self.assertIsNotNone(p)
        self.assertTrue(p.is_file())

    def test_percent_encoded(self) -> None:
        p = _safe_static(self.root, "%E7%BA%A2%E9%BB%91%E6%A0%91/index.html")
        self.assertIsNotNone(p)

    def test_traversal_rejected(self) -> None:
        for bad in ("..", "../x", "a/../../b", "./x", ".",
                    "..%2F..%2Fkwsearch.db", "红黑树/../../x"):
            self.assertIsNone(_safe_static(self.root, bad), msg=bad)

    def test_windows_reserved_rejected(self) -> None:
        for bad in ("C:/windows/win.ini", "a\\b", "a:b", 'a"b', "a|b", "a<b"):
            self.assertIsNone(_safe_static(self.root, bad), msg=bad)

    def test_missing_file(self) -> None:
        self.assertIsNone(_safe_static(self.root, "红黑树/nope.html"))
        self.assertIsNone(_safe_static(self.root, ""))
        self.assertIsNone(_safe_static(self.root, "/"))


# ── 收尾表 ────────────────────────────────────────────────────────────────


class TestFormatResultLines(unittest.TestCase):
    def test_reuses_cli_summary(self) -> None:
        result = types.SimpleNamespace(
            per_channel=[types.SimpleNamespace(
                channel="ok_ch", type="serp", skipped=False, queries=1, got=10,
                unique=9, error=None, blocked=0, stopped_reason=None)],
            task_id="t1", status="ok", truncated=False,
            kept=5, docs_new=5, unique=5, candidates=10, dropped_url=1,
            dropped_cap=0, pii_masked=0, alerts=0, elapsed_ms=1234,
            resolved_list=0,
            person_cards=0, person_name_missing=0, person_tiers={},
        )
        lines = format_result_lines(result)
        self.assertTrue(any("入库 5" in ln for ln in lines))
        self.assertTrue(any("ok_ch" in ln for ln in lines))


# ── HTTP 端到端（假 runner）───────────────────────────────────────────────


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _load_cfg(_write_cfg(self.dir))
        self.db = str(self.dir / "t.db")
        self.root = self.dir / "report"
        (self.root / "红黑树").mkdir(parents=True)
        (self.root / "红黑树" / "index.html").write_text(
            "<html>红黑树报告</html>", encoding="utf-8")
        self.registry = JobRegistry(_fake_runner())
        handler = build_handler(WebContext(cfg=self.cfg, db_path=self.db,
                                           out_dir=self.root, registry=self.registry))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    # -- 请求助手 --
    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get_json(self, path: str):
        code, body = self.get(path)
        return code, json.loads(body.decode("utf-8"))

    def post(self, path: str, obj, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body}

    def wait_job(self, job_id: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, d = self.get_json(f"/api/jobs/{job_id}")
            self.assertEqual(code, 200)
            if d["status"] != "running":
                return d
            time.sleep(0.05)
        self.fail(f"作业 {job_id} 未在 {timeout}s 内结束")


class TestRoutes(ServerCase):
    def test_index_html(self) -> None:
        code, body = self.get("/")
        self.assertEqual(code, 200)
        text = body.decode("utf-8")
        self.assertIn("kwsearch", text)
        self.assertIn("开始抓取", text)
        self.assertNotIn("<script src", text, "不许引外部脚本")

    def test_bootstrap(self) -> None:
        code, d = self.get_json("/api/bootstrap")
        self.assertEqual(code, 200)
        self.assertEqual([c["id"] for c in d["channels"]], ["ok_ch", "none_ch"])
        states = {c["id"]: c["state"] for c in d["channels"]}
        self.assertEqual(states["ok_ch"], "就绪")
        self.assertEqual(states["none_ch"], "未注册")
        self.assertEqual(d["recent_tasks"], [])   # 库还不存在
        self.assertIsNone(d["latest_job"])

    def test_run_then_poll(self) -> None:
        code, d = self.post("/api/run", {"keyword": "红黑树"})
        self.assertEqual(code, 202)
        self.assertTrue(d["id"])
        job = self.wait_job(d["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["keyword"], "红黑树")
        self.assertEqual(job["lines"], ["第一行进度", "第二行进度"])
        self.assertEqual(job["report_url"], "/r/fake/index.html")
        self.assertEqual(len(self.registry._runner.calls), 1)

    def test_person_payload_reaches_runner(self) -> None:
        code, d = self.post("/api/run", {
            "keyword": "张军", "person": True,
            "orgs": "某某大学，某某研究院", "fields": "网络安全",
            "roles": "教授", "fetch_body": False,
        })
        self.assertEqual(code, 202)
        self.wait_job(d["id"])
        payload = self.registry._runner.calls[-1]
        self.assertTrue(payload.person)
        self.assertEqual(payload.orgs, ("某某大学", "某某研究院"))
        self.assertEqual(payload.fields, ("网络安全",))
        self.assertEqual(payload.roles, ("教授",))
        self.assertIs(payload.fetch_body, False)

    def test_second_run_gets_409(self) -> None:
        gate = threading.Event()
        self.registry._runner = _fake_runner(gate=gate)
        code, d = self.post("/api/run", {"keyword": "甲"})
        self.assertEqual(code, 202)
        code2, d2 = self.post("/api/run", {"keyword": "乙"})
        self.assertEqual(code2, 409)
        self.assertIn("已有任务", d2["error"])
        gate.set()
        self.assertEqual(self.wait_job(d["id"])["status"], "done")

    def test_bad_payloads_400(self) -> None:
        for obj in ({"keyword": "a/b"}, {"keyword": ""}, {"keyword": "x", "max_docs": "abc"},
                    {"keyword": "x", "fetch_body": "yes"}):
            code, d = self.post("/api/run", obj)
            self.assertEqual(code, 400, msg=obj)
            self.assertIn("error", d)
        code, d = self.post("/api/run", None, raw=b"{not json")
        self.assertEqual(code, 400)

    def test_unknown_job_404(self) -> None:
        code, _ = self.get_json("/api/jobs/nope")
        self.assertEqual(code, 404)

    def test_unknown_route_404(self) -> None:
        code, _ = self.get("/api/nope")
        self.assertEqual(code, 404)
        code, _ = self.get("/r/")
        self.assertEqual(code, 404)

    def test_static_report_served(self) -> None:
        code, body = self.get("/r/" + urllib.parse.quote("红黑树") + "/index.html")
        self.assertEqual(code, 200)
        self.assertIn("红黑树报告", body.decode("utf-8"))

    def test_static_missing_404(self) -> None:
        # 浏览器永远发百分号编码后的路径，测试也必须这样发
        code, _ = self.get("/r/" + urllib.parse.quote("不存在") + "/index.html")
        self.assertEqual(code, 404)

    def test_static_traversal_404(self) -> None:
        for p in ("/r/..%2F..%2Fkwsearch.db", "/r/%2e%2e/index.html",
                  "/r/..%5C..%5Ckwsearch.db", "/r/C:/windows/win.ini"):
            code, _ = self.get(p)
            self.assertEqual(code, 404, msg=p)

    def test_bootstrap_after_run_lists_job(self) -> None:
        code, d = self.post("/api/run", {"keyword": "甲"})
        self.assertEqual(code, 202)
        self.wait_job(d["id"])
        code, b = self.get_json("/api/bootstrap")
        self.assertEqual(code, 200)
        self.assertIsNotNone(b["latest_job"])
        self.assertEqual(b["latest_job"]["status"], "done")


# ── 清空所有历史数据 ────────────────────────────────────────────────────────


class TestWipeRoutes(ServerCase):
    """GET /api/wipe 预览；POST /api/wipe 确认。库是临时目录里的真 SQLite。"""

    def _seed_db(self) -> None:
        from kwsearch.models import Doc, Mention
        from kwsearch.storage import Repository, init_db

        conn = init_db(self.db)
        repo = Repository(conn)
        tid = repo.start_task("红黑树", "quick")
        repo.upsert_doc(Doc(url_canon="https://a.example.com/1",
                            domain="a.example.com", title="红黑树原理",
                            channel="bing_html"))
        repo.upsert_mention(Mention(task_id=tid,
                                    url_canon="https://a.example.com/1", score=5.0))
        conn.commit()
        conn.close()

    def _counts(self) -> dict[str, int]:
        from kwsearch.storage import connect, table_counts

        conn = connect(self.db)
        try:
            return table_counts(conn)
        finally:
            conn.close()

    def test_preview_missing_db_is_empty(self) -> None:
        code, d = self.get_json("/api/wipe")
        self.assertEqual(code, 200)
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["tables"], {})

    def test_preview_counts_without_deleting(self) -> None:
        self._seed_db()
        code, d = self.get_json("/api/wipe")
        self.assertEqual(code, 200)
        self.assertEqual(d["total"], 3)                 # task + doc + mention
        self.assertEqual(d["tables"]["task"], 1)
        self.assertEqual(d["tables"]["doc"], 1)
        self.assertEqual(d["tables"]["mention"], 1)
        self.assertEqual(self._counts()["task"], 1, "预览不许删")

    def test_post_without_confirm_400(self) -> None:
        self._seed_db()
        for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}):
            code, d = self.post("/api/wipe", body)
            self.assertEqual(code, 400, msg=body)
            self.assertIn("confirm", d["error"])
        self.assertEqual(self._counts()["task"], 1, "缺确认一行都不能少")

    def test_post_confirm_wipes(self) -> None:
        self._seed_db()
        code, d = self.post("/api/wipe", {"confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(d["total"], 3)                 # task + doc + mention
        self.assertTrue(all(n == 0 for n in self._counts().values()))
        # 页面上的"最近任务"表随之清空
        code, b = self.get_json("/api/bootstrap")
        self.assertEqual(b["recent_tasks"], [])

    def test_post_confirm_while_running_409(self) -> None:
        gate = threading.Event()
        self.registry._runner = _fake_runner(gate=gate)
        code, d = self.post("/api/run", {"keyword": "甲"})
        self.assertEqual(code, 202)
        code2, d2 = self.post("/api/wipe", {"confirm": True})
        self.assertEqual(code2, 409)
        self.assertIn("任务", d2["error"])
        gate.set()
        self.assertEqual(self.wait_job(d["id"])["status"], "done")

    def test_wipe_missing_db_post_ok(self) -> None:
        code, d = self.post("/api/wipe", {"confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(d["total"], 0)


if __name__ == "__main__":
    unittest.main()
