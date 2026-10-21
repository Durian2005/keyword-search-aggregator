"""百度 SERP 解析测试。

fixture 是按 2026-10-21 真实页面结构写的**合成** HTML（类名/属性/层级一致，
内容全部中性化）—— 单测绝不发真实请求，也不必把真实搜索结果搬进仓库。
结构变动时用 `probe --save` 存新样本，先比对再改 fixture。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.discover import get_channel, get_parser  # noqa: E402
from kwsearch.discover.baidu import parse_serp  # noqa: E402


def _result(mu: str, tpl: str, title: str, snippet: str = "",
            *, href: str = "http://www.baidu.com/link?url=FAKE") -> str:
    summary = (f'<div class="a b summary-text_15QGa c"><span>{snippet}</span></div>'
               if snippet else "")
    return f'''<div class="result c-container xpath-log new-pmd" srcid="1599" tpl="{tpl}" mu="{mu}">
  <h3 class="cosc-title"><a class="cosc-title-a" href="{href}">{title}</a></h3>
  {summary}
</div>'''


_PAGE = f"""<!DOCTYPE html>
<html><head><title>红黑树 - 百度搜索</title></head><body>
<div id="content_left">
{_result("https://blog.example.com/rbt-principle", "www_index",
         "<em>红黑树</em>原理与实现", "红黑树是一种自平衡二叉查找树，广泛用于工程实现")}
{_result("https://baike.baidu.com/item/%E7%BA%A2%E9%BB%91%E6%A0%91/2413209", "sg_kg_entity_san",
         "红黑树(自平衡二叉查找树) - 百度百科", "红黑树是每个节点都带有颜色属性的二叉查找树")}
{_result("http://image.baidu.com/search/index?tn=baiduimage&amp;word=%E7%BA%A2%E9%BB%91%E6%A0%91", "image_grid_san",
         "红黑树图片", "")}
{_result("http://28608.recommend_list.baidu.com", "recommend_list", "大家还在搜", "")}
{_result("https://www.baidu.com/s?tn=news&amp;rtt=1&amp;wd=%E7%BA%A2%E9%BB%91%E6%A0%91", "rel_base_realtime",
         "红黑树 - 相关新闻", "")}
{_result("http://nourl.ubs.baidu.com/61344", "new_baikan_index", "百家号推荐", "")}
<div class="result c-container" tpl="new_baikan_index" srcid="61344">
  <h3><a href="http://www.baidu.com/link?url=NOMU">没有 mu 的块</a></h3>
</div>
</div>
<div id="page">下一页</div>
</body></html>"""


class TestBaiduParse(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertTrue(callable(get_channel("baidu_html")))
        self.assertIs(get_parser("baidu_html"), parse_serp)

    def test_parses_organic_and_baike(self) -> None:
        r = parse_serp(_PAGE, 1, channel_id="baidu_html", query="红黑树")
        self.assertFalse(r.blocked)
        self.assertIsNone(r.block_kind)
        self.assertEqual(r.raw_count, 7)
        self.assertEqual(len(r.candidates), 2)
        first, second = r.candidates
        self.assertEqual(first.url_raw, "https://blog.example.com/rbt-principle")
        self.assertEqual(first.title, "红黑树原理与实现")     # <em> 被剥掉
        self.assertEqual(first.snippet, "红黑树是一种自平衡二叉查找树，广泛用于工程实现")
        self.assertEqual(first.extra["domain"], "blog.example.com")
        self.assertEqual(first.rank, 1)
        self.assertEqual(second.extra["domain"], "baike.baidu.com")
        self.assertEqual(second.rank, 2)
        self.assertIn("块 7 → 有效 2", r.detail)
        self.assertIn("聚合卡片 4", r.detail)
        self.assertIn("无直链 1", r.detail)

    def test_no_published_hint(self) -> None:
        # 百度 SERP 没有可信时间线索：宁可没有，不能瞎给
        r = parse_serp(_PAGE, 1, channel_id="baidu_html")
        self.assertTrue(all(c.published_hint is None for c in r.candidates))

    def test_dedupe_same_mu(self) -> None:
        page = ('<html><body><div id="content_left">'
                + _result("https://a.example.com/1", "www_index", "标题一", "摘要一")
                + _result("https://a.example.com/1", "www_index", "标题一重复", "摘要一")
                + "</div></body></html>")
        r = parse_serp(page, 1, channel_id="baidu_html", query="红黑树")
        self.assertEqual(len(r.candidates), 1)
        self.assertIn("重复 1", r.detail)

    def test_challenge_page(self) -> None:
        page = '<html><body><div class="wappass">百度安全验证</div></body></html>'
        r = parse_serp(page, 1, challenge_keywords=("安全验证",), channel_id="baidu_html")
        self.assertTrue(r.blocked)
        self.assertEqual(r.block_kind, "challenge")
        self.assertEqual(r.candidates, [])

    def test_no_result_page(self) -> None:
        page = '<html><body><div class="c-container">没有找到与“某某生造词”相关的网页</div></body></html>'
        r = parse_serp(page, 1, channel_id="baidu_html")
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "empty")

    def test_structure_change(self) -> None:
        r = parse_serp('<html><body>全新改版，什么都没有</body></html>', 1,
                       channel_id="baidu_html")
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "unknown_structure")
        self.assertIn("百度改版", r.detail)

    def test_relaxed_drops_whole_page(self) -> None:
        # 人名查询被放宽成同名字典页：解析到了，但解析出来的是别人。
        # 注意 check_relaxed 的 _MIN_DOCS=5：样本太小不做判定（宁可放过），
        # 所以 fixture 必须凑够 5 条。
        blocks = "".join(
            _result(f"https://x{n}.example.com/surname", "www_index",
                    f"张姓的来源与分布（{n}）", "张是汉语常见姓氏，人口众多")
            for n in range(5)
        )
        page = f'<html><body><div id="content_left">{blocks}</div></body></html>'
        r = parse_serp(page, 1, channel_id="baidu_html", query="张军")
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "relaxed")
        self.assertEqual(r.candidates, [])
        self.assertEqual(r.raw_count, 5)
        self.assertIn("整页丢弃", r.detail)


if __name__ == "__main__":
    unittest.main()
