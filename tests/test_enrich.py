"""S3d 正文富化的测试：抓取 → 抽取 → 组 Doc 这一段，全部离线。

为什么值得单独一套测试：这是整条链路里**唯一会按"文档条数"发请求**的地方。
一旦"该抓几条""抓失败算谁的""摘要该不该换"这三件事判错，
表现是报告看起来正常、内容其实是错的 —— 比崩溃更难发现。

假 fetcher 是本文件唯一的外部依赖。真发请求的测试在这里一条都不能有：
16 秒 vs 0.5 秒只是表象，真正的问题是"测试依赖当天网络"。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch import discover  # noqa: E402
from kwsearch.config import load_config  # noqa: E402
from kwsearch.enrich import enrich  # noqa: E402
from kwsearch.fetch import BaseFetcher, FetchError, FetchResult  # noqa: E402
from kwsearch.models import Candidate, ChannelResult  # noqa: E402


# --------------------------------------------------------------- 假 fetcher
class FakeFetcher(BaseFetcher):
    """按 URL 查表返回。表里没有的一律 404 —— 比"抛异常"更接近真实站点行为。"""

    name = "fake"

    def __init__(self, pages: dict[str, str | FetchResult | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20) -> FetchResult:
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None:
            raise FetchError("404 not found", status=404, kind="http")
        if isinstance(v, Exception):
            raise v
        if isinstance(v, FetchResult):
            return v
        raw = v.encode("utf-8")
        return FetchResult(
            status=200, body=v, raw=raw,
            content_type="text/html; charset=utf-8",
            final_url=url, content_bytes=len(raw), fetcher=self.name,
        )


# --------------------------------------------------------------- 样本页
ARTICLE = """<html><head>
<title>红黑树详解 - 技术博客</title>
<script type="application/ld+json">{"datePublished":"2024-03-15T10:00:00+08:00"}</script>
</head><body>
<nav>首页 分类 标签 归档</nav>
<div class="article-body">
<p>红黑树是一种自平衡二叉查找树。它的插入和删除都要通过旋转来维持平衡，因此最坏情况下依然能保证对数级的查找效率。</p>
<p>红黑树的五条性质决定了它的查找复杂度为 O(log n)。左旋和右旋是最基本的两个操作，它们只改变树的局部结构而不破坏中序顺序。</p>
<p>工程实践中，红黑树被大量用于标准库的 map 与 set 实现。理解它的关键在于同时把握旋转与变色这两个手段。</p>
</div>
<footer>版权所有 翻版必究</footer>
</body></html>"""

# 只有导航、没有正文：抽不出可信容器时 body_chars 必须是 0
NAV_ONLY = """<html><head><title>导航页</title></head><body>
<nav><a href="/a">首页</a><a href="/b">分类</a><a href="/c">标签</a></nav>
</body></html>"""

# 整页只有几十个字：S3a 会判 empty，根本到不了抽取器
JS_SHELL = "<html><head><title>加载中</title></head><body><div id=app></div></body></html>"

# 不含任何日期线索，用来验"不拿抓取时间冒充发布时间"
ARTICLE_NO_DATE = ARTICLE.replace(
    '<script type="application/ld+json">{"datePublished":"2024-03-15T10:00:00+08:00"}</script>', "")


CFG = """version: 1
defaults:
  timeout: 5
  retry: {{max_attempts: 1, backoff_base: 0.01}}
  user_agent: "test-agent"
channels:
  - id: dummy
    type: serp
    parser: fake_serp_enrich
    url_template: "https://fake.test/s?q={{q}}&first={{offset}}"
    paging: {{mode: offset, param: first, start: 1, step: 10, max_pages: 1}}
    throttle: {{min_interval_s: 0}}
    enabled: false
crawl:
  robots_obey: false
  per_domain_qps: 0
extract:
  summary_chars: 200
  fetch_body: true
  max_fetch: {max_fetch}
  min_body_chars: 200
  challenge_keywords: ["安全验证"]
filter:
  max_per_domain: 10
"""


class EnrichCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cfg(self, max_fetch: int = 5):
        p = self.dir / "c.yaml"
        p.write_text(CFG.format(max_fetch=max_fetch), encoding="utf-8")
        return load_config(p)

    def cand(self, url: str, title: str = "红黑树详解", snippet: str = "搜索摘要",
             hint: str | None = None) -> Candidate:
        return Candidate(
            url_raw=url, url_canon=url, title=title, snippet=snippet,
            channel="fake_serp", rank=1, published_hint=hint,
            extra={"domain": "x.test", "hint_kind": "absolute" if hint else ""},
        )

    def run_enrich(self, pages: dict, cands: list[Candidate], **kw):
        f = FakeFetcher(pages)
        cfg = kw.pop("cfg", None) or self.cfg()
        rep = enrich(cfg, cands, fetcher=f, **kw)
        return rep, f


# --------------------------------------------------------------- 抽取成功
class TestExtractOk(EnrichCase):
    def test_article_becomes_doc(self) -> None:
        rep, _ = self.run_enrich({"https://x.test/1": ARTICLE}, [self.cand("https://x.test/1")])
        e = rep.items["https://x.test/1"]
        self.assertEqual(rep.ok, 1)
        self.assertGreater(e.doc.body_chars, 150)
        self.assertIn("红黑树是一种自平衡二叉查找树", e.doc.summary)
        self.assertNotIn("首页 分类", e.doc.summary)      # 导航没混进摘要
        self.assertEqual(e.confidence, "high")

    def test_full_text_exposed_for_rescoring(self) -> None:
        """正文全文要返回给 pipeline 重算命中次数，但**不落库**（Doc 里只有摘要）。"""
        rep, _ = self.run_enrich({"https://x.test/1": ARTICLE}, [self.cand("https://x.test/1")])
        e = rep.items["https://x.test/1"]
        self.assertIn("左旋和右旋是最基本的两个操作", e.text)
        self.assertLessEqual(len(e.doc.summary), 200)

    def test_page_date_beats_serp_hint(self) -> None:
        c = self.cand("https://x.test/1", hint="2026-10-01")
        rep, _ = self.run_enrich({"https://x.test/1": ARTICLE}, [c])
        d = rep.items["https://x.test/1"].doc
        self.assertEqual((d.published_at, d.time_source), ("2024-03-15 10:00:00", "jsonld"))

    def test_no_fetched_at_leaks_into_published_at(self) -> None:
        """没有日期的页面，published_at 必须是 None —— 不许拿抓取时间冒充。"""
        rep, _ = self.run_enrich({"https://x.test/1": ARTICLE_NO_DATE},
                                 [self.cand("https://x.test/1")])
        d = rep.items["https://x.test/1"].doc
        self.assertIsNone(d.published_at)
        self.assertEqual(d.time_source, "fallback")

    def test_http_status_recorded(self) -> None:
        rep, _ = self.run_enrich({"https://x.test/1": ARTICLE}, [self.cand("https://x.test/1")])
        self.assertEqual(rep.items["https://x.test/1"].doc.http_status, 200)


# --------------------------------------------------------------- 各种失败
class TestFailures(EnrichCase):
    def test_404_counted_not_raised(self) -> None:
        rep, _ = self.run_enrich({}, [self.cand("https://x.test/missing")])
        self.assertEqual(rep.attempted, 1)
        self.assertEqual(rep.ok, 0)
        self.assertEqual(rep.failed.get("http_404"), 1)

    def test_js_shell_is_empty_not_ok(self) -> None:
        rep, _ = self.run_enrich({"https://x.test/js": JS_SHELL}, [self.cand("https://x.test/js")])
        self.assertEqual(rep.failed.get("empty"), 1)
        self.assertEqual(rep.ok, 0)

    def test_nav_only_is_not_trusted_body(self) -> None:
        """抽不出正文时不能拿导航文字凑数。"""
        rep, _ = self.run_enrich({"https://x.test/nav": NAV_ONLY}, [self.cand("https://x.test/nav")])
        self.assertIn("empty", rep.failed)
        self.assertEqual(rep.ok, 0)

    def test_network_error_recorded(self) -> None:
        rep, _ = self.run_enrich(
            {"https://x.test/timeout": FetchError("timed out", kind="timeout")},
            [self.cand("https://x.test/timeout")],
        )
        self.assertEqual(rep.failed.get("timeout"), 1)
        self.assertEqual(rep.ok, 0)

    def test_one_failure_does_not_stop_others(self) -> None:
        pages = {"https://x.test/good": ARTICLE}
        cands = [self.cand("https://x.test/bad"), self.cand("https://x.test/good")]
        rep, _ = self.run_enrich(pages, cands)
        self.assertEqual((rep.ok, rep.failed.get("http_404")), (1, 1))

    def test_summary_line_is_readable(self) -> None:
        rep, _ = self.run_enrich({"https://x.test/good": ARTICLE},
                                 [self.cand("https://x.test/good"), self.cand("https://x.test/bad")])
        line = rep.summary_line()
        self.assertIn("抓 2 条", line)
        self.assertIn("正文可用 1", line)


# --------------------------------------------------------------- 抓多少
class TestScope(EnrichCase):
    def test_limit_caps_requests(self) -> None:
        cands = [self.cand(f"https://x.test/{i}") for i in range(10)]
        rep, f = self.run_enrich({}, cands, limit=3)
        self.assertEqual(rep.attempted, 3)
        self.assertEqual(len(f.calls), 3)      # 请求数就是"抓几条"，没有额外请求

    def test_max_fetch_from_config(self) -> None:
        cands = [self.cand(f"https://x.test/{i}") for i in range(10)]
        rep, _ = self.run_enrich({}, cands, cfg=self.cfg(max_fetch=2))
        self.assertEqual(rep.attempted, 2)

    def test_explicit_limit_beats_config(self) -> None:
        cands = [self.cand(f"https://x.test/{i}") for i in range(10)]
        rep, _ = self.run_enrich({}, cands, cfg=self.cfg(max_fetch=1), limit=4)
        self.assertEqual(rep.attempted, 4)

    def test_skip_existing_does_not_request(self) -> None:
        cands = [self.cand("https://x.test/1"), self.cand("https://x.test/2")]
        rep, f = self.run_enrich({}, cands, skip={"https://x.test/1"})
        self.assertEqual(rep.skipped_existing, 1)
        self.assertEqual(f.calls, ["https://x.test/2"])

    def test_nothing_to_do(self) -> None:
        rep, f = self.run_enrich({}, [])
        self.assertEqual((rep.attempted, rep.ok), (0, 0))
        self.assertEqual(f.calls, [])


# --------------------------------------------------------------- pipeline 接线
# 用独立注册名，避免覆盖 test_pipeline.py 里的 fake_serp（那边是 5 条、这边 3 条）
_PIPE_ITEMS = [
    ("https://a.test/1", "红黑树 原理详解", "红黑树是一种自平衡二叉查找树", "2026-10-01"),
    ("https://b.test/2", "红黑树 插入操作", "插入时通过旋转恢复平衡", None),
    ("https://c.test/3", "红黑树 删除操作", "删除的四种情况", None),
]

_ART = """<html><head><title>{title} - 站点</title></head><body>
<div class="article">
<p>{title}是二叉查找树的一种改进。红黑树的插入与删除都需要旋转和变色，这保证了它最坏情况下依然是对数复杂度，因此被大量用在标准库的有序容器里。</p>
<p>理解红黑树的关键是同时把握左旋、右旋与节点变色这三件事。左旋与右旋只改变树的局部结构，不破坏中序遍历的顺序，而变色负责重新分配路径上的黑色节点数量。</p>
<p>工程实现时还要注意插入与删除的修复过程并不对称。删除比插入复杂得多，因为它可能让某条路径上的黑色节点变少，需要自底向上逐层修复，最坏要回到根节点。</p>
</div>
</body></html>"""


def fake_serp_enrich(keyword: str, offset: int, ctx):  # noqa: ANN001
    cands = [
        Candidate(url_raw=url, title=t, snippet=s, channel="fake_serp_enrich", rank=i + 1,
                  published_hint=hint,
                  extra={"domain": url.split("/")[2],
                         "hint_kind": "absolute" if hint else ""})
        for i, (url, t, s, hint) in enumerate(_PIPE_ITEMS)
    ]
    return ChannelResult(channel="fake_serp_enrich", query=keyword,
                         candidates=cands, raw_count=len(cands))


discover._REGISTRY["fake_serp_enrich"] = fake_serp_enrich

PIPE_CFG = """version: 1
defaults:
  timeout: 5
  retry: {{max_attempts: 1, backoff_base: 0.01}}
  user_agent: "test-agent"
channels:
  - id: fake_serp_enrich
    type: serp
    parser: fake_serp_enrich
    url_template: "https://fake.test/s?q={{q}}&first={{offset}}"
    paging: {{mode: offset, param: first, start: 1, step: 10, max_pages: 1}}
    throttle: {{min_interval_s: 0}}
    assertions: {{min_items: 1, required_fields: [url, title]}}
expand:
  enabled: false
  max_queries: 1
filter:
  pii_patterns: {{}}
  drop_url_patterns: []
  drop_domains: []
  max_per_domain: 10
  drop_zero_hit: false
limits:
  max_docs: 20
  max_minutes: 5
extract:
  summary_chars: 200
  fetch_body: {fetch_body}
  max_fetch: 10
  min_body_chars: 200
  challenge_keywords: ["安全验证"]
crawl:
  robots_obey: false
  per_domain_qps: 0
"""


class TestPipelineWiring(EnrichCase):
    def run_pipe(self, fetch_body: str):
        from kwsearch.pipeline import run_task

        p = self.dir / "p.yaml"
        p.write_text(PIPE_CFG.format(fetch_body=fetch_body), encoding="utf-8")
        pages = {url: _ART.format(title=t) for url, t, _, _ in _PIPE_ITEMS}
        return run_task(
            load_config(p), "红黑树", db_path=str(self.dir / "t.db"),
            progress=None, fetcher=FakeFetcher(pages),
        )

    def docs(self) -> list[dict]:
        from kwsearch.storage import connect

        conn = connect(str(self.dir / "t.db"))
        try:
            return [dict(x) for x in conn.execute(
                "SELECT url_canon, summary, body_chars, time_source, published_at FROM doc")]
        finally:
            conn.close()

    def test_summary_comes_from_article(self) -> None:
        r = self.run_pipe("true")
        self.assertEqual(r.bodies_ok, 3)
        rows = self.docs()
        self.assertTrue(all(x["body_chars"] > 0 for x in rows))
        # 摘要换成正文开头了，不再是 SERP 那句
        self.assertFalse(any(x["summary"] == "红黑树是一种自平衡二叉查找树" for x in rows))
        self.assertTrue(any("红黑树的插入与删除都需要旋转" in x["summary"] for x in rows))

    def test_disabled_keeps_serp_snippet(self) -> None:
        r = self.run_pipe("false")
        self.assertEqual((r.bodies_ok, r.bodies_low), (0, 0))
        rows = self.docs()
        self.assertTrue(all(x["body_chars"] == 0 for x in rows))
        self.assertTrue(any(x["summary"] == "红黑树是一种自平衡二叉查找树" for x in rows))

    def test_rerun_skips_already_fetched(self) -> None:
        self.run_pipe("true")
        r2 = self.run_pipe("true")
        self.assertEqual(r2.bodies_ok, 0)
        self.assertEqual(r2.bodies_skipped, 3)

    def test_summary_replaced_on_second_run(self) -> None:
        """先关正文跑一次（SERP 摘要），再开着跑 —— 库里的摘要要被正文替换。"""
        self.run_pipe("false")
        self.run_pipe("true")
        rows = self.docs()
        self.assertTrue(all(x["body_chars"] > 0 for x in rows))
        self.assertFalse(any(x["summary"] == "红黑树是一种自平衡二叉查找树" for x in rows))


if __name__ == "__main__":
    unittest.main()
