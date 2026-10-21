"""脱敏与丢弃规则的单元测试。

这些规则是"公开信息聚合"的边界，不是可选装饰 —— 测不住就等于没有。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.sanitize import Sanitizer, is_list_url  # noqa: E402


class TestScrub(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Sanitizer({})

    def test_phone_masked_keep_head_tail(self) -> None:
        out, n = self.s.scrub("请联系 13812345678 咨询")
        self.assertEqual(n, 1)
        self.assertNotIn("13812345678", out)
        self.assertIn("138****5678", out)

    def test_id_card_masked(self) -> None:
        out, n = self.s.scrub("身份证 110101199001011234")
        self.assertEqual(n, 1)
        self.assertNotIn("110101199001011234", out)
        self.assertTrue(out.startswith("身份证 1101"))
        self.assertTrue(out.endswith("234"))

    def test_email_masked_keeps_domain(self) -> None:
        out, n = self.s.scrub("写信到 zhang.san@example.com")
        self.assertEqual(n, 1)
        self.assertNotIn("zhang.san", out)
        self.assertIn("@example.com", out)     # 域名保留：能看出是哪家邮箱

    def test_multiple_hits_counted(self) -> None:
        _, n = self.s.scrub("13812345678 / 13900001111 / a@b.cn")
        self.assertEqual(n, 3)

    def test_clean_text_returns_same_object(self) -> None:
        text = "红黑树是一种自平衡二叉查找树"
        out, n = self.s.scrub(text)
        self.assertEqual(n, 0)
        self.assertIs(out, text)              # 没命中就不造新字符串

    def test_empty_input(self) -> None:
        self.assertEqual(self.s.scrub(""), ("", 0))

    def test_18_digit_not_double_counted_as_phone(self) -> None:
        # 身份证里可能包含 11 位连续数字，不能被手机号规则重复计一次
        _, n = self.s.scrub("110101199001011234")
        self.assertEqual(n, 1)

    def test_custom_pattern_overrides_builtin(self) -> None:
        s = Sanitizer({"pii_patterns": {"phone_cn": r"400-\d{3}-\d{4}"}})
        out, n = s.scrub("客服 400-123-4567")
        self.assertEqual(n, 1)
        self.assertNotIn("400-123-4567", out)

    def test_broken_regex_falls_back(self) -> None:
        # 配置写错正则不该让任务起不来，应退回内置规则
        s = Sanitizer({"pii_patterns": {"phone_cn": r"([0-9"}})
        out, n = s.scrub("电话 13812345678")
        self.assertEqual(n, 1)
        self.assertIn("138****5678", out)


class TestCheckUrl(unittest.TestCase):
    def test_drop_pattern_download(self) -> None:
        s = Sanitizer({"drop_url_patterns": [r"\.(zip|exe)$"]})
        self.assertIsNotNone(s.check_url("https://a.com/setup.exe"))
        self.assertIsNone(s.check_url("https://a.com/post/1"))

    def test_drop_domain_including_subdomain(self) -> None:
        s = Sanitizer({"drop_domains": ["spam.com"]})
        self.assertIsNotNone(s.check_url("https://spam.com/x", "spam.com"))
        self.assertIsNotNone(s.check_url("https://m.spam.com/x", "m.spam.com"))
        self.assertIsNone(s.check_url("https://notspam.com/x", "notspam.com"))

    def test_www_prefix_normalized(self) -> None:
        s = Sanitizer({"drop_domains": ["spam.com"]})
        self.assertIsNotNone(s.check_url("https://www.spam.com/x", "www.spam.com"))

    def test_no_rules_allows_everything(self) -> None:
        s = Sanitizer({})
        self.assertIsNone(s.check_url("https://anything.example/1", "anything.example"))

    def test_max_per_domain_read_from_config(self) -> None:
        self.assertEqual(Sanitizer({"max_per_domain": 12}).max_per_domain, 12)
        self.assertEqual(Sanitizer({}).max_per_domain, 0)
        self.assertEqual(Sanitizer({"max_per_domain": -3}).max_per_domain, 0)


class TestListUrl(unittest.TestCase):
    """列表页/站点首页拦截：点开一条结果要看到正文，不是一屏目录。

    规则是内置的（不走配置），因为它是 URL 形态判断，和 .zip/.exe 同一性质。
    样本全部来自百度 `site:官网域` 查询的真实返回。
    """

    def test_real_list_pages_dropped(self) -> None:
        for u in (
            "https://www.xxx.edu.cn/xsjs/list38.htm",     # 学术动态
            "https://www.xxx.edu.cn/677/list3.htm",       # 院系风采
            "https://www.xxx.edu.cn/_s2/677/list.psp",
            "https://www.xxx.edu.cn/55/list21.psp",       # 校园动态
            "https://www.xxx.edu.cn/85/list1.htm",        # 党员管理
            "https://www.xxx.edu.cn/53/list19.psp",       # 校园新闻
            "https://www.xxx.edu.cn/677/list50.psp",
        ):
            self.assertTrue(is_list_url(u), u)

    def test_homepage_dropped(self) -> None:
        self.assertTrue(is_list_url("https://www.xxx.edu.cn/"))
        self.assertTrue(is_list_url("https://www.xxx.edu.cn"))
        self.assertTrue(is_list_url("https://www.xxx.edu.cn/index.htm"))
        self.assertTrue(is_list_url("https://a.com/main.aspx"))

    def test_article_pages_survive(self) -> None:
        # 同站文章页形态：/日期/栏目ID/page.htm —— "page" 不在词表里
        for u in (
            "https://zsb.xxx.edu.cn/2021/0525/c282a3495/page.htm",
            "https://www.xxx.edu.cn/2019/0426/c90a139/page.htm",
            "https://jwc.xxx.edu.cn/2025/1120/c186a21918/page.htm",
        ):
            self.assertFalse(is_list_url(u), u)

    def test_list_word_inside_segment_survives(self) -> None:
        # 整段精确匹配，不做子串："my-list-of-tools" 不是列表页
        self.assertFalse(is_list_url("https://example.com/2024/my-list-of-tools.html"))
        self.assertFalse(is_list_url("https://example.com/posts/listing-rules"))

    def test_query_and_fragment_ignored(self) -> None:
        self.assertTrue(is_list_url("https://example.com/list?page=2#top"))
        self.assertFalse(is_list_url("https://example.com/post/1?from=list"))

    def test_empty_url_not_dropped(self) -> None:
        self.assertFalse(is_list_url(""))

    def test_check_url_reports_reason(self) -> None:
        s = Sanitizer({})
        self.assertIn("列表页", s.check_url("https://a.com/x/list1.htm") or "")
        self.assertIsNone(s.check_url("https://a.com/x/page.htm"))

    def test_org_probe_path_not_affected(self) -> None:
        # 机构官网探测要的恰恰是首页/列表页来认域名 —— 它不走 check_url，
        # 这里守住 is_list_url 本身对首页返回 True（探测侧不使用本函数）
        self.assertTrue(is_list_url("https://www.example.edu.cn/"))


class TestRelate(unittest.TestCase):
    def test_title_beats_body(self) -> None:
        from kwsearch.aggregate import relate

        in_title = relate("红黑树", "红黑树详解", "")
        in_body = relate("红黑树", "数据结构详解", "红黑树是一种平衡树")
        self.assertGreater(in_title.score, in_body.score)

    def test_exact_phrase_distinguishes_spacing(self) -> None:
        from kwsearch.aggregate import relate

        # "苹果手机" 与 "苹果 手机" 是两个不同查询，判定必须不同
        self.assertFalse(relate("苹果手机", "苹果 手机来了", "").exact_phrase)
        self.assertTrue(relate("苹果手机", "苹果手机发布了", "").exact_phrase)
        self.assertTrue(relate("苹果 手机", "苹果  手机来了", "").exact_phrase)

    def test_rank_bonus_smaller_than_one_title_hit(self) -> None:
        from kwsearch.aggregate import relate

        best_rank = relate("红黑树", "无关", "", query_rank=1)
        tenth = relate("红黑树", "无关", "", query_rank=10)
        one_title = relate("红黑树", "红黑树", "", query_rank=10)
        self.assertGreater(best_rank.score, tenth.score)      # 名次确有影响
        self.assertGreater(one_title.score, best_rank.score)  # 但不该盖过一次标题命中

    def test_hits_counted_case_insensitively(self) -> None:
        from kwsearch.aggregate import relate

        m = relate("Rock", "rock and ROCK", "")
        self.assertEqual(m.hit_title, 2)

    def test_chinese_not_split(self) -> None:
        from kwsearch.aggregate import relate

        # 中文整串当一个 token：分词器会把"红黑树"切成"红黑/树"，反而判不出精确短语
        m = relate("红黑树", "红黑树", "")
        self.assertEqual(m.hit_title, 1)
        self.assertTrue(m.exact_phrase)


if __name__ == "__main__":
    unittest.main()
