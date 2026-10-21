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


class TestPersonSection(unittest.TestCase):
    """人名模式：候选人区块。没有卡片时整块不能出现。"""

    def _data(self, cards: list) -> dict:
        d = _base_data()
        d["person_cards"] = cards
        return d

    def _card(self, label: str, tier: str, docs: int = 2, **kw) -> dict:
        base = {
            "label": label, "tier": tier, "score": 7, "org_hits": 1, "weak_hits": 2,
            "docs": docs, "negatives": [],
            "evidence": [{"title": f"{label} 的页面", "url": "https://a.example.com/1",
                          "matched": [label]}],
        }
        base.update(kw)
        return base

    def test_section_rendered_with_tiers(self) -> None:
        out = render_html(self._data([
            self._card("甲大学计算机学院", "confirmed"),
            self._card("乙大学", "suspect"),
        ]))
        self.assertIn("候选人", out)
        self.assertIn("确认", out)
        self.assertIn("疑似", out)
        self.assertIn("甲大学计算机学院", out)
        self.assertIn("不同机构绝不合并", out)

    def test_unmatched_folded_not_a_candidate(self) -> None:
        out = render_html(self._data([
            self._card("甲大学", "likely", docs=3),
            self._card("未匹配机构", "unrelated", docs=9),
        ]))
        self.assertIn("未匹配机构", out)
        self.assertIn("同名结果", out)
        self.assertNotIn('<b>未匹配机构</b>', out)     # 折叠，不当候选人展示

    def test_no_named_card_shows_explanation(self) -> None:
        out = render_html(self._data([self._card("未匹配机构", "unrelated", docs=4)]))
        self.assertIn("没有任何结果命中你给的机构锚点", out)

    def test_section_absent_without_cards(self) -> None:
        out = render_html(_base_data())
        self.assertNotIn('id="people"', out)

    def test_activity_span_and_secondary_orgs_shown(self) -> None:
        """活跃区间 + "还提到哪些机构"：卡片要能看出"先在哪、后来去了哪"。"""
        card = self._card("甲大学", "confirmed", also_orgs=["乙研究院"],
                          first_when="2021-03-05", last_when="2025-06-01")
        card["evidence"] = [
            {"title": "最近的", "url": "https://a.example.com/2", "matched": ["甲大学"],
             "when": "2025-06-01"},
            {"title": "更早的", "url": "https://a.example.com/1", "matched": ["甲大学"],
             "when": "2021-03-05"},
        ]
        out = render_html(self._data([card]))
        self.assertIn("活跃 2021-03-05", out)
        self.assertIn("2025-06-01", out)
        self.assertIn("还提到：乙研究院", out)
        self.assertIn("2021-03-05", out)          # 证据行也带日期

    def test_single_day_span_not_repeated(self) -> None:
        card = self._card("甲大学", "likely", first_when="2024-02-02",
                          last_when="2024-02-02")
        out = render_html(self._data([card]))
        self.assertIn("活跃 2024-02-02", out)
        self.assertNotIn("2024-02-02 ~ 2024-02-02", out)

    def test_negatives_shown_as_doubt(self) -> None:
        out = render_html(self._data([
            self._card("甲大学", "likely", negatives=["演艺", "招聘"]),
        ]))
        self.assertIn("同名疑点", out)
        self.assertIn("演艺", out)


class TestPersonDocsPool(unittest.TestCase):
    """人名模式的文档清单必须自称"池子"，并标出已归因条目。

    2026-10-21 张军实测：23 条池子里 1 条是目标人物，其余全是同名者 ——
    不把清单定性为"池子"、不把目标条目标出来，用户会把整份报告
    读成"搜出来的都是些什么玩意"。
    """

    def _data(self, cards: list) -> dict:
        d = _base_data()
        d["person_cards"] = cards
        return d

    def _card(self, label: str) -> dict:
        return {"label": label, "tier": "confirmed", "docs": 1, "score": 7,
                "org_hits": 1, "weak_hits": 1, "negatives": [],
                "evidence": [{"title": "目标人物主页",
                              "url": "https://a.example.com/1",
                              "matched": [label]}]}

    def test_person_mode_relabels_docs_section(self) -> None:
        out = render_html(self._data([self._card("甲大学")]))
        self.assertIn("同名结果池", out)
        self.assertIn("不是结论", out)
        self.assertIn("已归因", out)          # a.example.com/1 是卡片证据 → 打标

    def test_normal_mode_keeps_original_heading(self) -> None:
        out = render_html(_base_data())
        self.assertIn("按相关度降序", out)
        self.assertNotIn("同名结果池", out)
        self.assertNotIn("已归因", out)


class TestClusterFunnel(unittest.TestCase):
    """归并对账那几行：数字要分得清"有救的"和"没救的"。

    "无指纹未判 5 条"这种话等于没说 —— 用户既不知道该不该处理，
    也不知道处理完有没有用。三种原因必须分开写。
    """

    def _data(self, **totals) -> dict:
        data = _base_data()
        data["totals"] = {**data["totals"], "docs": 3, "clusters": 1, "groups": 2,
                          "merged": 0, "fingerprinted": 2, **totals}
        return data

    def test_pending_fingerprint_says_how_to_fix(self) -> None:
        out = render_html(self._data(unfingerprinted=1, pending_fingerprint=1))
        self.assertIn("待补算指纹", out)
        self.assertIn("重跑该词自动补", out)

    def test_short_and_no_body_are_separate(self) -> None:
        out = render_html(self._data(unfingerprinted=2, short_body=1, no_body=1))
        self.assertIn("正文过短", out)
        self.assertIn("无正文", out)
        self.assertNotIn("待补算指纹", out)

    def test_nothing_comparable_does_not_claim_no_reprint(self) -> None:
        """一条都没比过时，不能说"未发现同文转载"——那是在替没看过的文档背书。"""
        out = render_html(self._data(unfingerprinted=3, no_body=3, fingerprinted=0))
        self.assertIn("本轮没有可参与比对的正文", out)
        self.assertNotIn("未发现同文转载", out)


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
