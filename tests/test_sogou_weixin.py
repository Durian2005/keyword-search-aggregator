"""搜狗微信文章搜索解析测试。

fixture 按 2026-10-21 真实页面结构合成（txt-box / txt-info / all-time-y2 /
s2 里的 timeConvert），内容中性化；单测零真实请求。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.discover import get_channel, get_parser  # noqa: E402
from kwsearch.discover.sogou_weixin import parse_serp  # noqa: E402


def _article(n: int, title: str, snippet: str, account: str,
             *, ts: str | None = "1577765720", path: str | None = None) -> str:
    href = path or f"/link?url=FAKE{n}&amp;type=2&amp;query=test"
    time_span = (f'<span class="s2"><script>document.write(timeConvert(\'{ts}\'))'
                 f'</script></span>') if ts else '<span class="s2"></span>'
    return f'''<li id="sogou_vr_11002601_box_{n}" d="sig-{n}">
<div class="img-box"><a data-z="art" href="{href}"><img src="//img.example.com/{n}.jpg"></a></div>
<div class="txt-box">
<h3><a target="_blank" href="{href}" id="sogou_vr_11002601_title_{n}">{title}</a></h3>
<p class="txt-info" id="sogou_vr_11002601_summary_{n}">{snippet}</p>
<div class="s-p"><span class="all-time-y2">{account}</span>{time_span}</div>
</div></li>'''


_PAGE = f"""<!doctype html>
<html><head><title>红黑树的相关微信公众号文章 – 搜狗微信搜索</title></head><body>
<div id="main_wrap"><ul class="news-list">
{_article(0, "动画 | 什么是<em><!--red_beg-->红黑树<!--red_end--></em>?",
          "二叉树指的是每个节点最多只能有两个子树的有序树……", "某算法公众号")}
{_article(1, "<em><!--red_beg-->红黑树<!--red_end--></em>插入算法实现原理分析",
          "红黑树在实际工程中被广泛应用，比如 Linux 线程调度……", "某技术圈", ts=None)}
<li id="sogou_vr_11002601_box_nav"><a href="/">微信首页</a></li>
</ul></div>
</body></html>"""


class TestSogouWeixinParse(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertTrue(callable(get_channel("sogou_weixin")))
        self.assertIs(get_parser("sogou_weixin"), parse_serp)

    def test_parses_articles(self) -> None:
        r = parse_serp(_PAGE, 1, channel_id="sogou_weixin", query="红黑树")
        self.assertFalse(r.blocked)
        self.assertEqual(r.raw_count, 2)          # 导航 li 不算结果块
        self.assertEqual(len(r.candidates), 2)

        first, second = r.candidates
        self.assertEqual(first.title, "动画 | 什么是红黑树?")   # red_beg 注释与 <em> 都剥掉
        self.assertIn("二叉树", first.snippet)
        self.assertEqual(first.url_raw,
                         "https://weixin.sogou.com/link?url=FAKE0&type=2&query=test")
        self.assertEqual(first.extra["domain"], "mp.weixin.qq.com")
        self.assertEqual(first.extra["via"], "weixin.sogou.com")
        self.assertEqual(first.extra["account"], "某算法公众号")
        self.assertEqual(first.published_hint, "2019-12-31")
        self.assertEqual(first.extra["published_iso"], "2019-12-31T12:15:20+08:00")
        self.assertIn("块 2 → 有效 2", r.detail)
        self.assertIn("无时间 1", r.detail)

    def test_missing_time_leaves_hint_none(self) -> None:
        r = parse_serp(_PAGE, 1, channel_id="sogou_weixin")
        self.assertIsNone(r.candidates[1].published_hint)
        self.assertEqual(r.candidates[1].extra["published_iso"], "")

    def test_nav_li_ignored(self) -> None:
        # 没有 txt-box 的 li（导航等）不进结果
        r = parse_serp(_PAGE, 1, channel_id="sogou_weixin")
        self.assertEqual(len(r.candidates), 2)

    def test_challenge_page(self) -> None:
        page = ('<html><body><div class="result">用户您好，我们的系统检测到您网络中'
                '存在异常访问请求</div></body></html>')
        r = parse_serp(page, 1, channel_id="sogou_weixin")
        self.assertTrue(r.blocked)
        self.assertEqual(r.block_kind, "challenge")

    def test_no_result_page(self) -> None:
        page = '<html><body><div id="main_wrap">找不到相关文章</div></body></html>'
        r = parse_serp(page, 1, channel_id="sogou_weixin")
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "empty")

    def test_structure_change(self) -> None:
        r = parse_serp('<html><body>全新改版</body></html>', 1, channel_id="sogou_weixin")
        self.assertEqual(r.block_kind, "unknown_structure")
        self.assertIn("搜狗改版", r.detail)

    def test_relaxed_drops_whole_page(self) -> None:
        # 5 条起才做降级判定（quality._MIN_DOCS），fixture 必须凑够
        blocks = "".join(
            _article(n, f"张姓的来源与分布（{n}）", "张是汉语常见姓氏", "某语文公众号")
            for n in range(5)
        )
        page = f'<html><body><ul class="news-list">{blocks}</ul></body></html>'
        r = parse_serp(page, 1, channel_id="sogou_weixin", query="张军")
        self.assertEqual(r.block_kind, "relaxed")
        self.assertEqual(r.candidates, [])


if __name__ == "__main__":
    unittest.main()
