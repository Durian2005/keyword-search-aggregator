"""CLI 层测试。

为什么单独测 CLI：`run`/`report` 的命令解析、参数传递、退出码都是"只在
真实敲命令时才会跑到"的代码。S4 开发中 `cmd_report` 就因为漏了一行 import
在测试里完全没暴露 —— 这类错误只能靠直接调 main() 抓。
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.__main__ import main  # noqa: E402
from kwsearch.models import Doc, Mention  # noqa: E402
from kwsearch.storage import Repository, init_db  # noqa: E402

_DRY_CFG = """
version: 1
defaults: {timeout: 5}
channels:
  - id: dry_ch
    type: serp
    parser: bing_html
    url_template: "https://x.test/?q={q}&first={offset}"
    paging: {mode: offset, param: first, start: 1, step: 10, max_pages: 1}
    throttle: {min_interval_s: 0}
"""

_CFG = """
version: 1
defaults: {timeout: 5}
channels:
  - id: none_ch
    type: serp
    parser: __not_registered__
    url_template: "https://x.test/?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
"""


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.db")
        self.cfg_path = str(self.dir / "channels.yaml")
        Path(self.cfg_path).write_text(_CFG, encoding="utf-8")

    def call(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", self.cfg_path, "--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def seed_task(self) -> str:
        conn = init_db(self.db)
        repo = Repository(conn)
        tid = repo.start_task("红黑树", "quick")
        repo.upsert_doc(Doc(url_canon="https://a.example.com/1", domain="a.example.com",
                            title="红黑树原理", summary="摘要", published_at="2026-10-01",
                            time_source="serp", channel="bing_html"))
        repo.upsert_mention(Mention(task_id=tid, url_canon="https://a.example.com/1", score=5.0))
        repo.finish_task(tid, "ok", False, {"docs_new": 1})
        conn.commit()
        conn.close()
        return tid


class TestReportCommand(CliCase):
    def test_report_with_task_id(self) -> None:
        tid = self.seed_task()
        code, out, _ = self.call("report", tid, "--out", str(self.dir / "rep"))
        self.assertEqual(code, 0)
        self.assertIn("报告", out)
        html = self.dir / "rep" / "红黑树" / "index.html"
        self.assertTrue(html.exists())
        self.assertIn("红黑树原理", html.read_text(encoding="utf-8"))

    def test_report_without_task_id_uses_latest(self) -> None:
        tid = self.seed_task()
        code, out, _ = self.call("report", "--out", str(self.dir / "rep"))
        self.assertEqual(code, 0)
        self.assertIn(tid, out)          # 打印了"按最近一次 <id> 生成"
        self.assertIn("最近的任务", out)
        self.assertTrue((self.dir / "rep" / "红黑树" / "index.html").exists())

    def test_report_unknown_task_exits_2(self) -> None:
        self.seed_task()
        code, _, err = self.call("report", "not-a-task")
        self.assertEqual(code, 2)
        self.assertIn("没有这个任务", err)

    def test_report_without_db_exits_2(self) -> None:
        code, _, err = self.call("report")
        self.assertEqual(code, 2)
        self.assertIn("数据库不存在", err)


class TestPageCommand(CliCase):
    """S3a 的 CLI。用假 fetcher 顶掉网络，否则这条命令的解析/退出码逻辑永远没人测。"""

    def _patch_fetcher(self, mapping: dict) -> None:
        import kwsearch.fetch as fetchmod
        from kwsearch.fetch import FetchResult

        class _Fake:
            name = "fake"

            def fetch(self, url, headers, timeout=20):  # noqa: ANN001
                item = mapping.get(url)
                if item is None:
                    raise AssertionError(f"未准备的 URL: {url}")
                return item

        orig = fetchmod.build_fetcher
        fetchmod.build_fetcher = lambda *a, **kw: _Fake()  # type: ignore[assignment]
        self.addCleanup(setattr, fetchmod, "build_fetcher", orig)
        self.FetchResult = FetchResult

    def test_page_without_urls_and_no_db_exits_2(self) -> None:
        code, _, err = self.call("page")
        self.assertEqual(code, 2)
        self.assertIn("数据库不存在", err)

    def test_page_with_url_ok(self) -> None:
        from kwsearch.fetch import FetchResult

        body = "<html><body>" + "一段足够长的正文。" * 40 + "</body></html>"
        raw = body.encode("utf-8")
        self._patch_fetcher({
            "https://a.example.com/robots.txt": FetchResult(404, "", content_type="text/plain"),
            "https://a.example.com/1": FetchResult(
                200, body, content_type="text/html; charset=utf-8",
                content_bytes=len(raw), raw=raw,
            ),
        })
        code, out, _ = self.call("page", "https://a.example.com/1")
        self.assertEqual(code, 0)
        self.assertIn("成功 1/1", out)
        self.assertIn("robots", out)

    def test_page_all_failed_exits_1(self) -> None:
        from kwsearch.fetch import FetchResult

        self._patch_fetcher({
            "https://a.example.com/robots.txt": FetchResult(404, "", content_type="text/plain"),
            "https://a.example.com/1": FetchResult(403, "no", content_type="text/html"),
        })
        code, out, _ = self.call("page", "https://a.example.com/1")
        self.assertEqual(code, 1)
        self.assertIn("http_403", out)


class TestOtherCommands(CliCase):
    def test_channels_lists_config(self) -> None:
        code, out, _ = self.call("channels")
        self.assertEqual(code, 0)
        self.assertIn("none_ch", out)
        self.assertIn("未注册", out)

    def test_run_dry_makes_no_request(self) -> None:
        # 换成已注册的 parser，才能验证"URL 真的被渲染出来了"。
        # --dry 会在发请求前返回，所以 url_template 指向假的域名是安全的。
        Path(self.cfg_path).write_text(_DRY_CFG, encoding="utf-8")
        code, out, _ = self.call("run", "红黑树", "--dry")
        self.assertEqual(code, 0)
        self.assertIn("--dry", out)
        self.assertIn("https://x.test/?q=", out)

    def test_run_all_skipped_returns_nonzero(self) -> None:
        # 唯一的通道 parser 未注册 → 一条也拿不到 → 退出码应表明"没成功"
        code, out, err = self.call("run", "红黑树")
        self.assertNotEqual(code, 0)
        self.assertIn("跳过", out + err)

    def test_db_init_creates_tables(self) -> None:
        code, out, _ = self.call("db", "init")
        self.assertEqual(code, 0)
        for table in ("task", "doc", "mention", "alert"):
            self.assertIn(table, out)

    def test_bad_config_exits_2(self) -> None:
        bad = self.dir / "bad.yaml"
        bad.write_text("version: 1\nchannels: []\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--config", str(bad), "--db", self.db, "channels"])
        self.assertEqual(code, 2)
        self.assertIn("配置错误", out.getvalue())


if __name__ == "__main__":
    unittest.main()
