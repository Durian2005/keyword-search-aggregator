"""so360 解析器：真实样本 + 结构边界。

样本 `tests/fixtures/so360_p{1,2}.html` 由 `tools/capture_serp.py` 从线上裁剪生成
（2026-11-02，关键词「红黑树」，只保留 `.res-list` 块）。

⚠️ 这里的绿色只证明"给定这份 HTML 能解析出什么"，**不证明线上还能用**。
   站点改版后 fixture 会过期而测试照旧通过 —— 那是本项目最阴的一种假绿灯。
   看到线上报 unknown_structure 时，先用 capture_serp.py 重抓一次样本。

全部离线：不发任何请求。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.discover import get_channel  # noqa: E402
from kwsearch.discover.so360 import parse_serp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(page: int) -> str:
    return (FIXTURES / f"so360_p{page}.html").read_text(encoding="utf-8")


def _block(*, title: str = "红黑树详解", href: str = "",
           mdurl: str = "", snippet: str = "正文摘要",
           cite: str = "example.com", rtype: str = "normal") -> str:
    """拼一个最小结果块，用来压边界情况（真实样本里不一定有这些组合）。"""
    attrs = f' data-mdurl="{mdurl}"' if mdurl else ""
    href_attr = f' href="{href}"' if href else ""
    return (
        f'<li class="res-list" data-result-type="{rtype}" data-result-pos="1">'
        f'<h3 class="res-title"><a{href_attr}{attrs}>'
        f'{title}</a></h3>'
        f'<p class="res-desc">{snippet}</p>'
        f'<p class="g-linkinfo"><cite><a>{cite}</a></cite></p></li>'
    )


def _page(*blocks: str) -> str:
    return ("<!doctype html><html><body>"
            + "".join(blocks) + "</body></html>")


class TestRealPage(unittest.TestCase):
    """真实样本：这两页的结论直接写在 so360.py 的 docstring 里。"""

    def test_page1_parses(self):
        r = parse_serp(_fixture(1), 1, channel_id="so360")
        self.assertGreaterEqual(len(r.candidates), 4)
        self.assertIsNone(r.block_kind)          # 正常页不该带 block_kind
        for c in r.candidates:
            self.assertTrue(c.url_raw.startswith("http"), c.url_raw)
            self.assertTrue(c.title.strip())
            self.assertTrue(c.extra.get("domain"), c.title)

    def test_page2_all_richsum_still_parses(self):
        """pn=2 的 7 个块**全是 richsum**，没有 normal。

        第一版按 data-result-type=="normal" 筛，第二页整页丢弃、报 unknown_structure
        —— 看起来像"360 改版"，其实是自己筛错。这条用例专门守它。
        """
        r = parse_serp(_fixture(2), 2, channel_id="so360")
        self.assertGreaterEqual(len(r.candidates), 5, r.detail)
        self.assertIsNone(r.block_kind)

    def test_ai_card_filtered(self):
        """360 自家的 ai.so.com 聚合页被标成 normal、也有 res-title，必须剔掉。"""
        r = parse_serp(_fixture(1), 1, channel_id="so360")
        hosts = [c.url_raw for c in r.candidates]
        self.assertFalse([u for u in hosts if "ai.so.com" in u], hosts)
        self.assertIn("AI 卡片", r.detail)

    def test_rank_is_global_not_page_local(self):
        """pn 是页码：第 2 页的 rank 要接在第 1 页后面，否则两页抢同一个名次加成。"""
        p1 = parse_serp(_fixture(1), 1, channel_id="so360")
        p2 = parse_serp(_fixture(2), 2, channel_id="so360")
        self.assertEqual(p1.candidates[0].rank, 1)
        self.assertGreaterEqual(p2.candidates[0].rank, 11)

    def test_direct_href_without_mdurl_kept(self):
        """少数块没有 data-mdurl，href 本身就是直连 —— 这种要收，别一起丢掉。"""
        r = parse_serp(_fixture(2), 2, channel_id="so360")
        direct = [c for c in r.candidates if "so.com/link" not in c.url_raw]
        self.assertTrue(direct, "第 2 页应至少有一条直连 href 的结果")
        for c in direct:
            self.assertNotIn("/link?", c.url_raw)


class TestStructureEdges(unittest.TestCase):
    def test_relay_without_mdurl_dropped(self):
        """中转链解不出真实地址 → 宁可少一条，也不能把 link?m= 落库。

        落库的后果不只是"链接点不开"：抓正文会把 360 导航页（hao.360.com）
        当成文章存进去，250 字节的导航页冒充正文。
        """
        html = _page(_block(href="https://www.so.com/link?m=abc", mdurl=""))
        r = parse_serp(html, 1, channel_id="so360")
        self.assertEqual(r.candidates, [])
        self.assertIn("中转未解", r.detail)

    def test_mdurl_wins_over_relay_href(self):
        html = _page(_block(href="https://www.so.com/link?m=abc",
                            mdurl="https://blog.example.com/p/1"))
        r = parse_serp(html, 1, channel_id="so360")
        self.assertEqual(r.candidates[0].url_raw, "https://blog.example.com/p/1")

    def test_date_prefix_stripped(self):
        html = _page(_block(mdurl="https://a.example.com/x",
                            snippet="2024年1月23日 - 红黑树是自平衡二叉查找树"))
        c = parse_serp(html, 1, channel_id="so360").candidates[0]
        self.assertEqual(c.published_hint, "2024-01-23")
        self.assertNotIn("2024年", c.snippet)
        self.assertTrue(c.snippet.startswith("红黑树"))

    def test_no_date_leaves_hint_empty(self):
        html = _page(_block(mdurl="https://a.example.com/x", snippet="没有日期的摘要"))
        c = parse_serp(html, 1, channel_id="so360").candidates[0]
        self.assertEqual(c.published_hint, "")

    def test_duplicate_url_counted_once(self):
        b = _block(mdurl="https://a.example.com/x")
        r = parse_serp(_page(b, b), 1, channel_id="so360")
        self.assertEqual(len(r.candidates), 1)
        self.assertIn("重复", r.detail)


class TestDiagnosis(unittest.TestCase):
    """0 块时要把三类失败分开 —— 它们在报告里的含义完全不同。"""

    def test_challenge(self):
        r = parse_serp("<html><body>请输入验证码</body></html>", 1,
                       challenge_keywords=("验证码",), channel_id="so360")
        self.assertTrue(r.blocked)
        self.assertEqual(r.block_kind, "challenge")

    def test_no_result_marker(self):
        r = parse_serp("<html><body>抱歉，没有找到相关结果</body></html>", 1,
                       challenge_keywords=("验证码",), channel_id="so360")
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "empty")

    def test_unknown_structure(self):
        """既没风控词也没"无结果"提示 = 我们自己坏了（多半是改版），要显眼。"""
        r = parse_serp("<html><body><div>什么都没有</div></body></html>", 1,
                       challenge_keywords=("验证码",), channel_id="so360")
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "unknown_structure")
        self.assertIn("capture_serp", r.detail)


class TestRegistration(unittest.TestCase):
    def test_channel_registered(self):
        fn = get_channel("so360")
        self.assertTrue(callable(fn))

    def test_parse_registered(self):
        """parse 要登记进 _PARSE_REGISTRY —— probe --save 复跑旧样本靠它。"""
        from kwsearch.discover import get_parser

        self.assertIs(get_parser("so360"), parse_serp)


if __name__ == "__main__":
    unittest.main()
