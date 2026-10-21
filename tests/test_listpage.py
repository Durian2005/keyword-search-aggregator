"""列表页 → 文章链接的单元测试（extract/listpage.py，纯函数不碰网络）。

这一层是「点开结果就是正文」的兑现点：百度 site: 查询返回的栏目页，
在这里换成它链着的相关文章。判错的方向只有两种代价：
    漏 = 文章没进池子（结果变少）
    误 = 目录页/导航链接进池子（用户又看到一屏目录）
所以两类方向都要有用例钉住。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.extract.listpage import article_links  # noqa: E402
from kwsearch.sanitize import is_list_url  # noqa: E402

# 栏目列表页样本：结构仿计算机学院官网（/xsjs/list38.htm 同款形态）——
# 相对链接、日期路径、两条锚文本含关键词、一条不含、一条链向另一个列表页。
LIST_HTML = """<html><head><title>学术动态</title></head><body>
<div class="list">
  <a href="/2021/0525/c282a3495/page.htm">张军教授首次全校公开课顺利开讲</a>
  <a href="/2019/0426/c90a139/page.htm">【最美教师】张军教授：信息化浪潮席卷生活</a>
  <a href="/2025/0508/c676a19624/page.htm">激发AI创新活力，校园大赛决赛圆满落幕</a>
  <a href="/677/list3.htm">院系风采</a>
  <a href="javascript:void(0)">张军教授空链接</a>
  <a href="mailto:a@b.cn">联系张军教授</a>
  <a href="/2021/0525/c282a3495/page.htm">张军教授首次全校公开课顺利开讲</a>
</div>
<nav><a href="/">首页</a><a href="/sitemap.htm">网站地图</a></nav>
</body></html>"""


class TestArticleLinks(unittest.TestCase):
    def test_extracts_matching_articles_only(self) -> None:
        out = article_links(LIST_HTML, ["张军"], "https://www.example.edu.cn/xsjs/list38.htm")
        self.assertEqual([u for u, _ in out], [
            "https://www.example.edu.cn/2021/0525/c282a3495/page.htm",
            "https://www.example.edu.cn/2019/0426/c90a139/page.htm",
        ])
        # 标题取锚文本，不是页面标题
        self.assertEqual(out[0][1], "张军教授首次全校公开课顺利开讲")

    def test_relative_links_resolved_against_base(self) -> None:
        out = article_links(LIST_HTML, ["张军"], "https://www.example.edu.cn/xsjs/list38.htm")
        for u, _ in out:
            self.assertTrue(u.startswith("https://www.example.edu.cn/"), u)

    def test_duplicate_links_deduped(self) -> None:
        out = article_links(LIST_HTML, ["张军"], "https://a.cn/list.htm")
        urls = [u for u, _ in out]
        self.assertEqual(len(urls), len(set(urls)))

    def test_list_links_skipped_when_reject_given(self) -> None:
        # 「院系风采」的锚文本不含关键词，本来就不会进；这里显式验 reject 语义：
        # 即使锚文本含关键词，链向列表页的也不跟进（只解析一层）
        html = ('<a href="/677/list3.htm">张军专题列表</a>'
                '<a href="/2021/x/page.htm">张军教授开讲</a>')
        out = article_links(html, ["张军"], "https://a.cn/", reject=is_list_url)
        self.assertEqual([u for u, _ in out], ["https://a.cn/2021/x/page.htm"])

    def test_javascript_and_mailto_skipped(self) -> None:
        out = article_links(LIST_HTML, ["张军"], "https://a.cn/")
        for u, _ in out:
            self.assertFalse(u.startswith(("javascript:", "mailto:")))

    def test_fragment_stripped_query_kept(self) -> None:
        html = '<a href="/a.htm?page=2#top">张军教授</a>'
        out = article_links(html, ["张军"], "https://a.cn/")
        self.assertEqual(out, [("https://a.cn/a.htm?page=2", "张军教授")])

    def test_empty_needles_returns_empty(self) -> None:
        self.assertEqual(article_links(LIST_HTML, [], "https://a.cn/"), [])

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(article_links(LIST_HTML, ["不存在的人"], "https://a.cn/"), [])

    def test_limit_respected(self) -> None:
        many = "".join(f'<a href="/{i}.htm">张军教授第{i}讲</a>' for i in range(30))
        out = article_links(many, ["张军"], "https://a.cn/", limit=5)
        self.assertEqual(len(out), 5)

    def test_overlong_label_skipped(self) -> None:
        html = f'<a href="/a.htm">{"张军教授" * 40}</a>'
        self.assertEqual(article_links(html, ["张军"], "https://a.cn/"), [])

    def test_case_insensitive_match(self) -> None:
        html = '<a href="/a.htm">LiuJun Professor Talk</a>'
        out = article_links(html, ["liujun"], "https://a.cn/")
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
