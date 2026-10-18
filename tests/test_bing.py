"""S2 验收：Bing 发现层解析。

只测纯函数（不联网）。样本用内联 HTML 复刻真实页面里那几个坑：
    无引号属性、<strong> 高亮、cite 面包屑、&ensp; 与 &#0183; 实体、
    摘要前缀日期（绝对 + 相对）、ck/a 中转链、同页重复结果。

跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kwsearch.config import load_config  # noqa: E402
from kwsearch.discover.base import ChannelContext, check_assertions  # noqa: E402
from kwsearch.aggregate import host_of  # noqa: E402
from kwsearch.discover.bing import (  # noqa: E402
    absolute,
    parse_date_hint,
    parse_serp,
    unwrap_redirect,
)
from kwsearch.models import Candidate, ChannelResult  # noqa: E402

_HREF = "https://example.com/p/1"

_BLOCK = """<li class="b_algo" data-id iid=SERP.1>
<div class="b_tpcn"><a class="tilk" href="{href}"><div class="tptxt"><div class="tptt">{tp}</div>
<div class="tpmeta"><div class="b_attribution"><cite>{cite}</cite></div></div></div></a></div>
<h2><a target="_blank" href="{href}">{title}</a></h2>
<div class="b_caption"><p class="b_lineclamp2">{snip}<a class="b_algoReadMore" href="{href}">阅读更多</a></p></div>
</li>"""


def _page(*blocks: str) -> str:
    return ("<!DOCTYPE html><html><head><title>红黑树 - 搜索</title></head><body>"
            "<ol id=\"b_results\">" + "".join(blocks) + "</ol></body></html>")


PAGE = _page(
    _BLOCK.format(
        href="https://zhuanlan.zhihu.com/p/91960960",
        tp="zhihu.com",
        cite="https://zhuanlan.zhihu.com",
        title="一文带你彻底读懂<strong>红黑树</strong>（附详细图解）",
        snip="2019年11月15日&ensp;&#0183;&ensp;红黑树简介 红黑树是一种自平衡的二叉查找树 …",
    ),
    _BLOCK.format(   # cite 是面包屑；摘要只有相对日期
        href="https://blog.csdn.net/u/article/details/122543826",
        tp="blog.csdn.net",
        cite="https://blog.csdn.net › u › article › details",
        title="【数据结构】史上最好理解的红黑树讲解",
        snip="5 天之前&ensp;&#0183;&ensp;红黑树是一种自平衡的二叉查找树 …",
    ),
    _BLOCK.format(   # 中转链：必须解出真实 URL
        href="https://www.bing.com/ck/a?!&&p=abc&u=a1"
             + base64.urlsafe_b64encode(b"https://oi-wiki.org/ds/rbtree/").decode().rstrip("=") + "&ntb=1",
        tp="oi-wiki.org",
        cite="https://oi-wiki.org › ds › rbtree",
        title="红黑树 - OI Wiki",
        snip="红黑树是一种自平衡的二叉搜索树。",
    ),
    _BLOCK.format(   # 与第 1 条完全重复
        href="https://zhuanlan.zhihu.com/p/91960960",
        tp="zhihu.com",
        cite="https://zhuanlan.zhihu.com",
        title="一文带你彻底读懂红黑树（附详细图解）",
        snip="2019年11月15日&ensp;&#0183;&ensp;红黑树简介 …",
    ),
)


class TestRedirect(unittest.TestCase):
    def test_base64_form(self) -> None:
        real = "https://example.com/a?b=1"
        u = ("https://www.bing.com/ck/a?!&&p=x&u=a1"
             + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=") + "&ntb=1")
        self.assertEqual(unwrap_redirect(u), real)

    def test_plain_url_passthrough(self) -> None:
        self.assertEqual(unwrap_redirect("https://zhihu.com/x"), "https://zhihu.com/x")

    def test_undecodable_returns_none(self) -> None:
        # 解不出来必须返回 None，绝不能把中转链当正文 URL
        self.assertIsNone(unwrap_redirect("https://www.bing.com/ck/a?u=%%%"))
        self.assertIsNone(unwrap_redirect("https://www.bing.com/ck/a?p=1"))

    def test_percent_encoded_form(self) -> None:
        self.assertEqual(unwrap_redirect("https://www.bing.com/ck/a?u=https%3A%2F%2Fe.org%2Fy"),
                         "https://e.org/y")

    def test_relative_href(self) -> None:
        self.assertTrue(absolute("/ck/a?u=x").startswith("https://cn.bing.com/"))
        self.assertEqual(absolute("//e.com/x"), "https://e.com/x")


class TestDomain(unittest.TestCase):
    def test_strip_www_and_port(self) -> None:
        self.assertEqual(host_of("https://www.cnblogs.com/x"), "cnblogs.com")
        self.assertEqual(host_of("http://a.com:8080/y"), "a.com")

    def test_bare_host(self) -> None:
        self.assertEqual(host_of("oi-wiki.org"), "oi-wiki.org")


class TestDateHint(unittest.TestCase):
    def test_absolute(self) -> None:
        # 入参是 htmlx.text() 的输出：&ensp; 已还原成 \u2002，&#0183; 已还原成 ·
        hint, rest, kind = parse_date_hint("2019年11月15日\u2002·\u2002红黑树简介")
        self.assertEqual((hint, kind), ("2019-11-15", "absolute"))
        self.assertEqual(rest, "红黑树简介")

    def test_dash_form(self) -> None:
        hint, _, _ = parse_date_hint("2026-01-07 · 某文")
        self.assertEqual(hint, "2026-01-07")

    def test_relative(self) -> None:
        hint, rest, kind = parse_date_hint("5 天之前 · 某文", today=date(2026, 10, 18))
        self.assertEqual((hint, kind), ("2026-10-13", "relative"))
        self.assertEqual(rest, "某文")

    def test_invalid_date_kept_verbatim(self) -> None:
        hint, rest, kind = parse_date_hint("2026年13月40日 x")
        self.assertEqual((hint, kind), (None, ""))
        self.assertEqual(rest, "2026年13月40日 x")   # 判不出来就原样留着，不许瞎猜

    def test_no_date(self) -> None:
        self.assertEqual(parse_date_hint("红黑树是一种树"), (None, "红黑树是一种树", ""))


class TestParseSerp(unittest.TestCase):
    def setUp(self) -> None:
        self.r = parse_serp(PAGE, 1, challenge_keywords=("安全验证",))

    def test_counts(self) -> None:
        self.assertEqual(self.r.raw_count, 4)          # 4 个结果块
        self.assertEqual(len(self.r.candidates), 3)    # 1 条重复被去掉
        self.assertFalse(self.r.blocked)
        self.assertIn("重复 1", self.r.detail)

    def test_title_stripped(self) -> None:
        c = self.r.candidates[0]
        self.assertEqual(c.title, "一文带你彻底读懂红黑树（附详细图解）")
        self.assertNotIn("<strong>", c.title)

    def test_urls_never_bing(self) -> None:
        for c in self.r.candidates:
            self.assertNotIn("bing.com/ck", c.url_raw)
            self.assertTrue(c.url_raw.startswith("http"))

    def test_ck_a_unwrapped(self) -> None:
        self.assertIn("https://oi-wiki.org/ds/rbtree/", [c.url_raw for c in self.r.candidates])

    def test_domain_from_cite_breadcrumb(self) -> None:
        self.assertEqual(self.r.candidates[1].extra["domain"], "blog.csdn.net")

    def test_snippet_clean(self) -> None:
        c = self.r.candidates[0]
        self.assertNotIn("阅读更多", c.snippet)
        self.assertFalse(c.snippet.startswith("2019"), "日期前缀应被剥离")
        self.assertEqual(c.published_hint, "2019-11-15")
        self.assertEqual(c.extra["hint_kind"], "absolute")
        # &ensp; / &#0183; 必须已还原，不能留实体字面量
        self.assertNotIn("&ensp;", c.snippet)
        self.assertNotIn("&#0183;", c.snippet)

    def test_rank_follows_offset(self) -> None:
        r = parse_serp(PAGE, 11)
        self.assertEqual(r.candidates[0].rank, 11)

    def test_empty_page_is_unknown_structure(self) -> None:
        r = parse_serp("<html><body><div>完全不同的版式</div></body></html>")
        self.assertEqual(r.block_kind, "unknown_structure")
        self.assertFalse(r.blocked)          # 不是被拦，是我们自己坏了

    def test_challenge_page(self) -> None:
        r = parse_serp("<html><body>请输入验证码，完成安全验证后继续</body></html>",
                       challenge_keywords=("安全验证", "请输入验证码"))
        self.assertEqual(r.block_kind, "challenge")
        self.assertTrue(r.blocked)

    def test_no_result_page(self) -> None:
        r = parse_serp("<html><body>没有与此相关的结果</body></html>")
        self.assertEqual(r.block_kind, "empty")
        self.assertFalse(r.blocked)          # 冷门词 0 条是正常结果，不算被拦


class TestAssertions(unittest.TestCase):
    def setUp(self) -> None:
        self.ch = load_config().channel("bing_html")

    def test_pass(self) -> None:
        r = ChannelResult(channel="bing_html", query="q",
                          candidates=[Candidate(url_raw="https://a.com", title="t")] * 5)
        self.assertEqual(check_assertions(r, self.ch), [])

    def test_too_few(self) -> None:
        r = ChannelResult(channel="bing_html", query="q",
                          candidates=[Candidate(url_raw="https://a.com", title="t")])
        self.assertTrue(any("条目不足" in p for p in check_assertions(r, self.ch)))

    def test_missing_title(self) -> None:
        r = ChannelResult(channel="bing_html", query="q",
                          candidates=[Candidate(url_raw=f"https://a.com/{i}") for i in range(5)])
        self.assertTrue(any("缺 title" in p for p in check_assertions(r, self.ch)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
