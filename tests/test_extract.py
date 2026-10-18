"""S3b 抽取层的测试：容器打分 + 时间四级降级 + Doc 组装。

全部离线。HTML 用手写样本，刻意覆盖三类真实形态（都是实测踩过的）：
    1. 标准 <p> 文章 —— 多数博客/文档站
    2. 没有 <p> 的裸文本块 —— bilibili 视频简介，第一版算法在这里一无所获
    3. 导航/推荐位占大头 —— 链接密度必须把它们淘汰，否则会抽出"推荐标题"
"""
from __future__ import annotations

import unittest

from kwsearch.extract import extract_doc
from kwsearch.extract.body import (
    Article,
    best_container,
    collect_text,
    extract_article,
    extract_title,
    summary_start,
)
from kwsearch.extract.dates import (
    DateResult,
    context_kind,
    jsonld_dates,
    meta_dates,
    normalize_date,
    pick_date,
    regex_dates,
    regex_dates_tagged,
)
from kwsearch.htmlx import parse_html


# ---------------------------------------------------------------------------
#  样本
# ---------------------------------------------------------------------------
ARTICLE_HTML = """<html><head><title>红黑树详解_哔哩哔哩_bilibili</title></head>
<body>
  <nav><a href="/1">首页</a><a href="/2">关于我们</a><a href="/3">联系方式</a></nav>
  <div class="article">
    <h1>红黑树详解</h1>
    <div class="article-content">
      <p>红黑树是一种自平衡二叉查找树，它的查找、插入、删除操作的时间复杂度都是对数级，因此在工程实践中被广泛使用。</p>
      <p>红黑树的五条性质决定了它的平衡性，理解这五条性质是掌握红黑树的第一步，后面所有的旋转操作都是为了让这些性质在插入删除之后依然成立。</p>
      <p>插入操作分为三种情况，删除操作分为四种情况，每一种都对应不同的调整策略，熟练之后可以快速判断当前应该做哪种旋转。</p>
    </div>
    <div class="comment-list">
      <p>这是一条足够长的评论区留言，长度肯定超过二十五个字，它绝对不应该出现在最终抽出来的正文里面。</p>
    </div>
  </div>
  <footer>版权所有 某站点</footer>
</body></html>"""

# 没有 <p>：正文直接挂在 span 里（bilibili 视频简介的真实结构）
BARE_HTML = """<html><body>
  <div class="video-desc-container">
    <div class="basic-desc-info"><span class="desc-info-text">超级硬核讲解红黑树原理以及源码解析，通过二三四树和红黑树的等价关系，轻易化解困扰大家已久的红黑树难以理解的尴尬境况，本课程从等价关系当中带大家倒推红黑树的五大性质，并手写实现插入与删除，最后还会揭示经典源码的效率差异。</span></div>
  </div>
  <div class="video-pod">
    <a href="/r1"><p>推荐视频标题一，长度超过二十五个字的推荐内容，应当被链接密度淘汰掉。</p></a>
    <a href="/r2"><p>推荐视频标题二，长度超过二十五个字的推荐内容，应当被链接密度淘汰掉。</p></a>
  </div>
</body></html>"""

NAV_ONLY_HTML = """<html><body>
  <div class="menu">
    <a href="/1">这是一个导航链接的文字内容，长度足够长但全是链接</a>
    <a href="/2">这是另一个导航链接的文字内容，长度也足够长但全是链接</a>
    <a href="/3">第三个导航链接的文字内容，依然全部都是可以点击的链接</a>
  </div>
</body></html>"""


class TestContainer(unittest.TestCase):
    def test_standard_article(self) -> None:
        art = extract_article(ARTICLE_HTML)
        self.assertEqual(art.container, "div.article-content")
        self.assertIn("自平衡二叉查找树", art.text)
        self.assertIn("五条性质", art.text)

    def test_noise_and_boilerplate_excluded(self) -> None:
        art = extract_article(ARTICLE_HTML)
        self.assertNotIn("评论区留言", art.text)      # class=comment-list
        self.assertNotIn("版权所有", art.text)        # footer
        self.assertNotIn("关于我们", art.text)        # nav
        self.assertEqual(art.confidence, "high")

    def test_paragraphs_are_separated(self) -> None:
        art = extract_article(ARTICLE_HTML)
        self.assertGreaterEqual(art.text.count("\n"), 2)

    def test_bare_text_without_p(self) -> None:
        # 第一版算法只认 <p>，对这种页面一条都抽不到
        art = extract_article(BARE_HTML)
        self.assertIn("二三四树", art.text)
        self.assertEqual(art.container, "div.basic-desc-info")

    def test_recommendation_block_rejected(self) -> None:
        art = extract_article(BARE_HTML)
        self.assertNotIn("推荐视频标题一", art.text)

    def test_nav_only_falls_back(self) -> None:
        art = extract_article(NAV_ONLY_HTML)
        # 全是链接的页面不该被当成正文；只能是低置信
        self.assertIn(art.confidence, ("low", "medium"))

    def test_anchor_is_never_container(self) -> None:
        node, _, _ = best_container(parse_html(BARE_HTML))
        self.assertIsNotNone(node)
        self.assertNotEqual(node.tag, "a")

    def test_heading_is_never_container(self) -> None:
        html = """<html><body>
          <h1>这是一个标题，长度也够，但它是标题不是正文内容，所以不能当容器</h1>
          <div class="body-text"><p>真正的正文在这里，它是普通段落，长度超过了二十五个字，因此应该被选中作为正文内容。</p></div>
        </body></html>"""
        art = extract_article(html)
        self.assertNotEqual(art.container, "h1")
        self.assertIn("真正的正文", art.text)

    def test_script_not_in_text(self) -> None:
        html = """<html><body><div class="content">
          <p>正文第一段足够长，应该被抽出来，不该受到脚本内容的影响，这里再多写几个字。</p>
          <script>var secret = "这段脚本文字绝不应该出现在正文里";</script>
          <p>正文第二段也足够长，同样应该被抽出来，不能因为中间夹了脚本就漏掉这一段内容。</p>
        </div></body></html>"""
        art = extract_article(html)
        self.assertIn("正文第一段", art.text)
        self.assertIn("正文第二段", art.text)
        self.assertNotIn("secret", art.text)

    def test_page_shell_downweighted(self) -> None:
        # 整个页面只有一个 div 包住全部内容 → 它是"整页壳"，不该因为文本最长而胜出
        html = """<html><body><div class="wrap">
          <div class="post-content"><p>这是文章正文，长度超过二十五个字，应当被选中作为最终结果。</p>
          <p>这是第二段正文，同样超过二十五个字，用来让打分稳定一些。</p></div>
          <div class="related"><a href="/x">相关推荐一</a><a href="/y">相关推荐二</a></div>
        </div></body></html>"""
        art = extract_article(html)
        self.assertEqual(art.container, "div.post-content")

    def test_empty_html(self) -> None:
        art = extract_article("")
        self.assertEqual(art.text, "")
        self.assertFalse(art.ok)


class TestTitle(unittest.TestCase):
    def test_h1_preferred(self) -> None:
        self.assertEqual(extract_title(ARTICLE_HTML), "红黑树详解")

    def test_title_split_takes_first_segment(self) -> None:
        html = "<html><head><title>红黑树详解_哔哩哔哩_bilibili</title></head><body></body></html>"
        # 取最长段会选到 "bilibili"，必须是第一段
        self.assertEqual(extract_title(html), "红黑树详解")

    def test_title_short_first_segment(self) -> None:
        html = "<html><head><title>CSDN - 专业开发者社区</title></head><body></body></html>"
        self.assertEqual(extract_title(html), "专业开发者社区")

    def test_no_title(self) -> None:
        self.assertEqual(extract_title("<html><body>x</body></html>"), "")


class TestCollectText(unittest.TestCase):
    def test_block_tags_break_lines(self) -> None:
        root = parse_html("<div><p>一</p><p>二</p></div>")
        self.assertEqual(collect_text(root), "一\n二")


# ---------------------------------------------------------------------------
#  时间
# ---------------------------------------------------------------------------
class TestSummaryStart(unittest.TestCase):
    """摘要该从第几段开始。全是实测踩过的形态。"""

    def test_plain_article_starts_at_zero(self) -> None:
        paras = ["红黑树是一种自平衡二叉查找树，它的查找复杂度是对数级。", "左旋与右旋是基本操作。"]
        self.assertEqual(summary_start(paras), 0)

    def test_toc_block_is_skipped(self) -> None:
        # CSDN 的目录是一行一段，每段只有几个字 —— 单段看都不像目录
        paras = ["目录", "一、红黑树简介", "二、为什么需要红黑树？", "三、红黑树的特性",
                 "4.1 红黑树效率", "4.2 与AVL的比较", "五、等价变换",
                 "红黑树是一种自平衡二叉查找树。"]
        self.assertEqual(summary_start(paras), 7)

    def test_toc_without_title(self) -> None:
        paras = ["一、简介", "二、特性", "三、操作", "正文从这里开始，没有目录标题。"]
        self.assertEqual(summary_start(paras), 3)

    def test_heading_like_leading_lines_skipped(self) -> None:
        # javaguide 的 h1/h2 会作为独立段落混在正文前面
        paras = ["红黑树", "红黑树介绍", "红黑树（Red Black Tree）是一种自平衡二叉查找树。"]
        self.assertEqual(summary_start(paras), 2)

    def test_short_sentence_with_punctuation_is_kept(self) -> None:
        # "代码如下："这种短句是正文的一部分，不能当标题跳掉
        paras = ["代码如下：", "int main() { return 0; }"]
        self.assertEqual(summary_start(paras), 0)

    def test_decor_line_skipped(self) -> None:
        paras = ["========", "红黑树的插入需要旋转与变色。"]
        self.assertEqual(summary_start(paras), 1)

    def test_all_toc_falls_back_to_zero(self) -> None:
        # 整页都是目录项时不许跳到空
        self.assertEqual(summary_start(["一、a", "二、b", "三、c"]), 0)

    def test_empty(self) -> None:
        self.assertEqual(summary_start([]), 0)


class TestNormalizeDate(unittest.TestCase):
    def test_formats(self) -> None:
        cases = {
            "2024-03-15": "2024-03-15",
            "2024/3/5": "2024-03-05",
            "2024.03.15": "2024-03-15",
            "2024年3月15日": "2024-03-15",
            "2024-03-15T10:30:00+08:00": "2024-03-15 10:30:00",
            "发布于 2024-03-15 10:30": "2024-03-15 10:30:00",
            # bilibili 的 jsonld 原文：带毫秒的 UTC
            "2020-08-25T04:09:58.000Z": "2020-08-25 12:09:58",
        }
        for raw, want in cases.items():
            self.assertEqual(normalize_date(raw), want, raw)

    def test_invalid(self) -> None:
        self.assertIsNone(normalize_date("2024-02-31"))   # 2 月没有 31 日
        self.assertIsNone(normalize_date("1899-01-01"))   # 早于 1990 多半是噪声
        self.assertIsNone(normalize_date("1800000000000"))
        self.assertIsNone(normalize_date("没有任何日期"))
        self.assertIsNone(normalize_date(""))


class TestDateSources(unittest.TestCase):
    def test_jsonld(self) -> None:
        html = ('<script type="application/ld+json">'
                '{"@type":"Article","datePublished":"2024-03-15T10:30:00+08:00"}'
                '</script>')
        self.assertEqual(jsonld_dates(html), ["2024-03-15T10:30:00+08:00"])
        d = pick_date(html)
        self.assertEqual((d.published_at, d.time_source), ("2024-03-15 10:30:00", "jsonld"))

    def test_jsonld_graph_nested(self) -> None:
        html = ('<script type="application/ld+json">'
                '{"@graph":[{"@type":"WebPage"},{"@type":"Article","datePublished":"2023-01-02"}]}'
                '</script>')
        self.assertEqual(pick_date(html).published_at, "2023-01-02")

    def test_jsonld_broken_is_skipped(self) -> None:
        html = ('<script type="application/ld+json">{bad json,}</script>'
                '<meta property="article:published_time" content="2022-05-06">')
        self.assertEqual(pick_date(html).time_source, "meta")

    def test_meta(self) -> None:
        html = '<html><head><meta property="article:published_time" content="2022-05-06T07:08:09Z"></head></html>'
        d = pick_date(html)
        # Z 是 UTC：换算成北京时间才是页面上看到的那个时间
        self.assertEqual((d.published_at, d.time_source), ("2022-05-06 15:08:09", "meta"))

    def test_meta_with_local_offset_is_not_shifted(self) -> None:
        html = ('<html><head><meta property="article:published_time" '
                'content="2022-05-06T07:08:09+08:00"></head></html>')
        self.assertEqual(pick_date(html).published_at, "2022-05-06 07:08:09")

    def test_utc_crossing_midnight(self) -> None:
        # 20:00Z + 8h = 次日 04:00，日期也要跟着进位
        self.assertEqual(normalize_date("2020-08-25T20:00:00Z"), "2020-08-26 04:00:00")

    def test_meta_prefers_published_over_generic_date(self) -> None:
        html = ('<html><head>'
                '<meta name="date" content="2019-01-01">'
                '<meta property="article:published_time" content="2022-05-06">'
                '</head></html>')
        self.assertEqual(pick_date(html).published_at, "2022-05-06")

    def test_meta_dates_returns_values(self) -> None:
        root = parse_html('<html><head><meta name="pubdate" content="2021-02-03"></head></html>')
        self.assertEqual(meta_dates(root), ["2021-02-03"])

    def test_regex_prefers_context(self) -> None:
        text = "本文参考了 2019 年的资料，实际发布于 2021-09-10，后来又做过修订。"
        html = f"<html><body><div class='content'><p>{text}</p></div></body></html>"
        d = pick_date(html, text=text)
        self.assertEqual(d.published_at, "2021-09-10")
        self.assertEqual(d.time_source, "regex")

    def test_regex_context_filter(self) -> None:
        text = "参考 2019 年的数据，发布于 2021-09-10。"
        self.assertEqual(regex_dates(text, context_only=True), ["2021-09-10"])

    def test_serp(self) -> None:
        d = pick_date("<html><body>没有日期</body></html>", serp_hint="2020-08-25")
        self.assertEqual((d.published_at, d.time_source), ("2020-08-25", "serp"))

    def test_fallback_is_not_trusted(self) -> None:
        d = pick_date("<html><body>没有日期</body></html>",
                      fetched_at="2026-10-18T09:00:00+08:00")
        self.assertEqual(d.time_source, "fallback")
        self.assertFalse(d.trusted)
        self.assertEqual(d.published_at, "2026-10-18 09:00:00")

    def test_source_order_is_respected(self) -> None:
        html = ('<script type="application/ld+json">{"datePublished":"2024-03-15"}</script>')
        d = pick_date(html, serp_hint="2020-08-25", sources=("serp", "jsonld"))
        self.assertEqual(d.time_source, "serp")


class TestUpdatedVsPublished(unittest.TestCase):
    """`最近更新` 不是发布时间。

    这是实测出来的：javaguide（VitePress）所有文章的时间都是同一个
    "最近更新: 2026/7/21"，那是站点构建时间。当发布时间用会让时间线凭空冒出一根高峰，
    而且从数据上完全看不出是错的 —— 所以必须单独标成 regex_updated。
    """

    def _html(self, body: str) -> str:
        return f"<html><body><div class='content'><p>{body}</p></div></body></html>"

    def test_context_kind(self) -> None:
        self.assertEqual(context_kind("发布于 2024-03-15", 4, 14), "publish")
        self.assertEqual(context_kind("发布时间: 2024-03-15", 6, 16), "publish")
        self.assertEqual(context_kind("最近更新: 2024-03-15", 5, 15), "update")
        self.assertEqual(context_kind("Last updated: 2024-03-15", 14, 24), "update")
        self.assertEqual(context_kind("参考 2024-03-15 的数据", 3, 13), "loose")

    def test_tagged_hits_carry_kind(self) -> None:
        hits = regex_dates_tagged("发布于 2021-09-10，最近更新 2024-03-15")
        self.assertEqual([(h.raw, h.kind) for h in hits],
                         [("2021-09-10", "publish"), ("2024-03-15", "update")])

    def test_update_only_is_not_publish_time(self) -> None:
        text = "正文内容。最近更新: 2026/7/21"
        d = pick_date(self._html(text), text=text)
        self.assertEqual(d.time_source, "regex_updated")
        self.assertEqual(d.published_at, "2026-07-21")   # 值保留，但标明它只是更新时间
        self.assertFalse(d.trusted)

    def test_publish_wins_over_update(self) -> None:
        text = "发布于 2024-01-01。最近更新: 2026-07-21"
        d = pick_date(self._html(text), text=text)
        self.assertEqual((d.published_at, d.time_source), ("2024-01-01", "regex"))

    def test_suffix_marker(self) -> None:
        # `2026/7/21 更新` 这种后置写法
        text = "正文。2026/7/21 更新"
        d = pick_date(self._html(text), text=text)
        self.assertEqual(d.time_source, "regex_updated")

    def test_header_publish_beats_body_update(self) -> None:
        # 正文容器里只有"最近更新"，页头明写"发布时间" —— 按类别优先，不按出现位置优先
        text = "正文内容。最近更新: 2026-07-21"
        html = ("<html><body><div class='meta'>发布时间：2024-01-01</div>"
                f"<div class='content'><p>{text}</p></div></body></html>")
        d = pick_date(html, text=text)
        self.assertEqual((d.published_at, d.time_source), ("2024-01-01", "regex"))

    def test_updated_flag_absent_when_no_date_at_all(self) -> None:
        d = pick_date("<html><body><p>没有日期</p></body></html>")
        self.assertEqual((d.published_at, d.time_source), (None, "fallback"))
        self.assertFalse(d.trusted)

    def test_nothing_found(self) -> None:
        d = pick_date("<html><body>没有日期</body></html>")
        self.assertIsNone(d.published_at)
        self.assertEqual(d.time_source, "fallback")


# ---------------------------------------------------------------------------
#  Doc 组装
# ---------------------------------------------------------------------------
class TestExtractDoc(unittest.TestCase):
    def test_basic(self) -> None:
        doc = extract_doc(
            "https://a.com/p", ARTICLE_HTML,
            cfg={"summary_chars": 60, "date_sources": ["regex", "fallback"]},
            channel="bing_html",
        )
        self.assertEqual(doc.title, "红黑树详解")
        self.assertEqual(doc.domain, "a.com")
        self.assertGreater(doc.body_chars, 50)
        self.assertEqual(doc.channel, "bing_html")
        self.assertEqual(doc.time_source, "fallback")
        self.assertLessEqual(len(doc.summary), 60)
        self.assertIn("红黑树", doc.summary)

    def test_summary_strips_paragraph_breaks(self) -> None:
        doc = extract_doc("https://a.com/p", ARTICLE_HTML, cfg={"summary_chars": 500})
        self.assertNotIn("\n", doc.summary)

    def test_date_from_meta(self) -> None:
        html = ARTICLE_HTML.replace(
            "<title>", '<meta property="article:published_time" content="2021-07-08T09:10:11">'
                        "<title>",
        )
        doc = extract_doc("https://a.com/p", html)
        self.assertEqual(doc.published_at, "2021-07-08 09:10:11")
        self.assertEqual(doc.time_source, "meta")

    def test_serp_date_is_used_when_page_has_none(self) -> None:
        doc = extract_doc("https://a.com/p", ARTICLE_HTML, serp_date="2020-01-02")
        self.assertEqual((doc.published_at, doc.time_source), ("2020-01-02", "serp"))

    def test_low_confidence_keeps_serp_snippet(self) -> None:
        doc = extract_doc(
            "https://a.com/p", NAV_ONLY_HTML,
            cfg={"summary_chars": 100},
            serp_snippet="搜索引擎给的摘要，比整页导航干净得多",
        )
        self.assertEqual(doc.extra["extract_confidence"], "low")
        self.assertEqual(doc.summary, "搜索引擎给的摘要，比整页导航干净得多")
        self.assertEqual(doc.body_chars, 0)          # 没有可信正文，不许冒充

    def test_cfg_none(self) -> None:
        doc = extract_doc("https://a.com/p", ARTICLE_HTML)
        self.assertEqual(doc.summary.count(" "), doc.summary.count(" "))  # 不抛异常即可
        self.assertTrue(doc.summary)

    def test_serp_title_used_when_page_has_none(self) -> None:
        html = "<html><body><div class='content'><p>" + "正文内容。" * 20 + "</p></div></body></html>"
        doc = extract_doc("https://a.com/p", html, serp_title="搜索引擎标题")
        self.assertEqual(doc.title, "搜索引擎标题")


if __name__ == "__main__":
    unittest.main()
