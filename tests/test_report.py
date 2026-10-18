"""报告层测试：渲染正确、注入挡住、目录名安全。

报告是本机双击打开的 HTML 文件 —— 标题里带 `<script>` 的页面真实存在，
一旦没转义，就等于执行了别人写的本地脚本。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.report import render, render_html, safe_dirname  # noqa: E402
from kwsearch.storage import Repository, init_db  # noqa: E402


def _base_data() -> dict:
    return {
        "task": {
            "task_id": "abc123def456", "keyword": "红黑树", "mode": "quick",
            "timespan": "30d", "started_at": "2026-10-18T00:47:51+08:00",
            "finished_at": "2026-10-18T00:48:30+08:00", "status": "partial",
            "truncated": 1,
            "stats": {"candidates": 7, "filtered": 1, "unique": 4, "dropped_url": 0,
                      "dropped_cap": 1, "docs_new": 3, "max_per_domain": 2},
        },
        "totals": {"docs": 3, "docs_shown": 3, "domains": 2, "channels": 2, "alerts": 2,
                   "with_time": 1, "no_time": 2, "new_docs": 3, "db_docs": 3},
        "channels": [
            {"channel": "bing_html", "queries": 2, "got": 7, "new": 4, "blocked": 0,
             "elapsed_ms": 520, "block_kinds": None, "first_at": "", "last_at": ""},
            {"channel": "bilibili_search", "queries": 1, "got": 0, "new": 0, "blocked": 1,
             "elapsed_ms": 300, "block_kinds": "rate_limit", "first_at": "", "last_at": ""},
        ],
        "docs": [
            {"url_canon": "https://a.example.com/1", "url_raw": "https://a.example.com/1",
             "domain": "a.example.com", "title": "红黑树 原理详解",
             "summary": "红黑树是一种自平衡二叉查找树", "published_at": "2026-10-01",
             "time_source": "serp", "channel": "bing_html", "body_chars": 0,
             "score": 7.5, "hit_title": 1, "hit_body": 1, "exact_phrase": 1},
            {"url_canon": "https://b.example.com/2", "url_raw": "javascript:alert(1)",
             "domain": "b.example.com", "title": "<script>alert('x')</script>红黑树",
             "summary": "恶意标题用例", "published_at": None,
             "time_source": "fallback", "channel": "bing_html", "body_chars": 0,
             "score": 3.0, "hit_title": 0, "hit_body": 0, "exact_phrase": 0},
        ],
        "domains": [{"domain": "a.example.com", "n": 2}, {"domain": "b.example.com", "n": 1}],
        "timeline": [{"date": "2026-10-01", "n": 1}],
        "time_sources": [{"source": "serp", "n": 1}, {"source": "fallback", "n": 2}],
        "alerts": [
            {"id": 1, "channel": "bilibili_search", "kind": "blocked",
             "detail": "第 1 次调用被拦（rate_limit）", "created_at": ""},
            {"id": 2, "channel": None, "kind": "limit_reached",
             "detail": "单域配额 2 条：1 条未入库", "created_at": ""},
        ],
        "queries": [
            {"channel": "bing_html", "seq": 1, "query_text": "红黑树", "got_count": 5,
             "new_count": 4, "blocked": 0, "block_kind": None, "elapsed_ms": 260},
            {"channel": "bing_html", "seq": 2, "query_text": "红黑树 原理", "got_count": 2,
             "new_count": 0, "blocked": 0, "block_kind": None, "elapsed_ms": 240},
        ],
    }


class TestRenderHtml(unittest.TestCase):
    def test_contains_core_sections(self) -> None:
        out = render_html(_base_data())
        for needle in ("概览", "覆盖率自评", "来源分布", "时间分布", "文档清单", "告警"):
            self.assertIn(needle, out)
        self.assertIn("红黑树", out)

    def test_no_external_resources(self) -> None:
        out = render_html(_base_data())
        # 报告必须能离线双击打开：不许有外链 css/js/字体/图片
        self.assertNotIn("<link", out)
        self.assertNotIn("<script", out.lower())
        self.assertNotIn("http://", out.replace('href="http://', ""))
        self.assertNotIn("https://cdn", out)

    def test_title_escaped(self) -> None:
        out = render_html(_base_data())
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>alert", out)

    def test_javascript_url_not_linked(self) -> None:
        out = render_html(_base_data())
        self.assertNotIn('href="javascript', out.lower())
        # 但标题文字本身要照常显示
        self.assertIn("恶意标题用例", out)

    def test_truncated_banner_shown(self) -> None:
        out = render_html(_base_data())
        self.assertIn("结果不完整", out)

    def test_no_time_banner_shown(self) -> None:
        out = render_html(_base_data())
        self.assertIn("时间不完整", out)

    def test_alert_kind_translated(self) -> None:
        out = render_html(_base_data())
        self.assertIn("被限流", out)              # blocked → 中文
        self.assertIn("触发上限", out)            # limit_reached → 中文

    def test_funnel_numbers_present(self) -> None:
        out = render_html(_base_data())
        for label in ("原始候选", "关键词过滤", "去重后", "单域配额", "入库"):
            self.assertIn(label, out)

    def test_empty_data_does_not_crash(self) -> None:
        data = {
            "task": {"task_id": "t0", "keyword": "空", "mode": "quick", "status": "failed",
                     "started_at": "2026-10-18T00:00:00+08:00", "truncated": 0, "stats": {}},
            "totals": {"docs": 0, "domains": 0, "channels": 0, "alerts": 0,
                       "with_time": 0, "no_time": 0},
            "channels": [], "docs": [], "domains": [], "timeline": [],
            "time_sources": [], "alerts": [], "queries": [],
        }
        out = render_html(data)
        self.assertIn("本次无结果", out)
        self.assertIn("没有入库文档", out)

    def test_unreliable_time_marked(self) -> None:
        out = render_html(_base_data())
        self.assertIn("无日期", out)
        self.assertIn("tag warn", out)


class TestSafeDirname(unittest.TestCase):
    def test_strips_illegal_chars(self) -> None:
        # / : * ? " < > | 共 8 个字符各替换成一个下划线
        self.assertEqual(safe_dirname('a/b:c*?"<>|d'), "a_b_c______d")

    def test_keeps_chinese(self) -> None:
        self.assertEqual(safe_dirname("红黑树"), "红黑树")

    def test_empty_falls_back(self) -> None:
        self.assertEqual(safe_dirname("   "), "untitled")
        self.assertEqual(safe_dirname(""), "untitled")

    def test_windows_reserved(self) -> None:
        self.assertEqual(safe_dirname("con"), "_con")
        self.assertEqual(safe_dirname("NUL"), "_NUL")

    def test_length_capped(self) -> None:
        self.assertLessEqual(len(safe_dirname("长" * 200)), 40)


class TestRenderToDisk(unittest.TestCase):
    def test_writes_html_and_json(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "r.db")

        conn = init_db(db)
        repo = Repository(conn)
        tid = repo.start_task("红黑树", "quick")
        from kwsearch.models import Doc, Mention

        repo.upsert_doc(Doc(url_canon="https://a.example.com/1", domain="a.example.com",
                            title="红黑树原理", summary="摘要", published_at="2026-10-01",
                            time_source="serp", channel="bing_html"))
        repo.upsert_mention(Mention(task_id=tid, url_canon="https://a.example.com/1", score=5.0))
        repo.finish_task(tid, "ok", False, {"docs_new": 1})
        conn.commit()

        out_dir = Path(tmp.name) / "report"
        path = Path(render(conn, tid, out_dir))
        conn.close()

        self.assertTrue(path.exists())
        self.assertEqual(path.name, "index.html")
        self.assertEqual(path.parent.name, "红黑树")
        html = path.read_text(encoding="utf-8")
        self.assertIn("红黑树原理", html)

        data = json.loads((path.parent / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(data["totals"]["docs"], 1)
        self.assertEqual(data["task"]["task_id"], tid)

    def test_unknown_task_raises(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = init_db(str(Path(tmp.name) / "r.db"))
        with self.assertRaises(KeyError):
            render(conn, "nope", tmp.name)
        conn.close()


if __name__ == "__main__":
    unittest.main()
