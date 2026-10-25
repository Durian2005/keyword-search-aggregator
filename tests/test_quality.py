"""降级体检（discover/quality）+ 两条路径的过滤口径。

核心用例全部来自 2026-10-18 的真实样本（人名与机构名已替换为中性占位，
标题结构与长度保持不变，判定逻辑与线下复现结果一致）：
    "张军 南京"                → 10 条「张」的百科/字典页（Bing 只用了开头的"张"）
    "网络安全张军"              → 测速网 / 网速测试（只用了"网络"）
    "张军"（裸名）              → 10 条高校教师主页（正常，但互不相同）
    "红黑树 左旋 右旋"          → 正常（这个变体的扩量以前被整串 require 杀光了）

跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kwsearch.discover.bing import parse_serp  # noqa: E402
from kwsearch.discover.quality import (  # noqa: E402
    bigrams,
    check_boilerplate,
    check_relaxed,
    keep_related,
    needles_of,
    query_segments,
)
from kwsearch.models import Candidate  # noqa: E402
from kwsearch.pipeline import _screen_single_page, _term_pattern  # noqa: E402

# --------------------------------------------------------------- 真实样本

# Bing 对 "张军 南京" 返回的那 10 条：一个"张军"都没有
_DEGRADED_NAME = [
    "张（汉语汉字）_百度百科",
    "张姓（中国姓氏）_百度百科",
    "张的意思,张的解释,张的拼音,张的部首-汉语国学",
    "张姓概况和53位古今名人 - 知乎",
    "《张》的拼音,张字的意思、组词、部首 - 汉语查",
    "基因张姓分支与张姓多祖源记载 - 知乎",
    "张姓简史，三支起源，六位先祖，两处祖地",
    "張的意思,張的解释,張的拼音,張的部首-汉语国学",
    "张氏家谱网 提供25个省区市的1777部张氏家谱",
    "扒一扒张姓起源和张姓祖先的故事_张累_祁姓",
]

# Bing 对 "网络安全张军" 返回的：只有"网络"，没有"张军"
_DEGRADED_NET = [
    "测速网 - 专业测网速, 网速测试, 宽带提速",
    "网络（由节点和连线构成的图）_百度百科",
    "一键测速网_网速测试_4G5G测速_全球节点",
    "一键网络测速_在线网速测试工具_实时监测",
    "计算机网络知识点全面总结（非常详细）",
    "互联网速度测试 | Fast.com",
    "在线网速测试 - 测网速 宽带测速",
    "计算机网络基础知识总结 | 菜鸟教程",
    "网络测速 - 在线测试你的宽带速度",
    "网速测试工具 - 一键检测网络延迟",
]

# 裸名 "张军" 的正常结果：10 条，但分属 8 个不同单位（这就是重名问题的实证）
# 样本按脱敏规则处理：真实姓名与机构名一律替换为中性占位，标题结构与长度不变
_REAL_NAME = [
    "张军（某某集团执行副总裁兼中国区总裁）_百度百科",
    "张军（某国科学院院士，某某大学教授）_百度百科",
    "张军-某某大学统计与数据科学系",
    "某某化工大学 张军个人主页-教授-博士生导师",
    "某某大学张军院士课题组",
    "张军-某某理工大学土木与交通学院",
    "某某矿业大学主页平台系统 张军--中文主页",
    "传感技术-医疗健康工程应用-张军-某某大学",
    "张军-某某大学地球科学与工程学院",
    "张军-某某大学力学与材料学院",
]

# "红黑树 左旋 右旋" 的正常结果：整串不含查询，但每个词元都在
_REAL_RB = [
    "一文带你彻底读懂红黑树（附详细图解）- 知乎",
    "红黑树 左旋 右旋 图解：插入与删除的调整过程 - CSDN",
    "红黑树（图解+秒懂+史上最全）- 博客园",
    "红黑树 - OI Wiki（含左旋右旋示意）",
    "红黑树_百度百科",
    "数据结构：红黑树的左旋、右旋与变色 - 简书",
    "红黑树实现（Java）：插入修复需要左旋/右旋 - 掘金",
    "从 2-3 树理解红黑树：左旋右旋的本质",
    "红黑树原理详解 - 阮一峰的网络日志",
    "手写红黑树：旋转操作的四种情况 - B站专栏",
]


def _cands(*titles: str) -> list[Candidate]:
    return [Candidate(url_raw=f"https://ex{i}.example.com/p{i}", title=t)
            for i, t in enumerate(titles)]


def _serp_html(titles: list[str]) -> str:
    blocks = "".join(
        f'<li class="b_algo" data-id iid=SERP.{i}><h2><a href="https://ex{i}.example.com/p{i}">'
        f"{t}</a></h2><div class=\"b_caption\">"
        f'<p class="b_lineclamp2">{t}</p></div></li>'
        for i, t in enumerate(titles)
    )
    return ('<!DOCTYPE html><html><head><title>张军 南京 - 搜索</title></head>'
            f'<body><ol id="b_results">{blocks}</ol></body></html>')


# ------------------------------------------------------------------ 分段


class TestSegments(unittest.TestCase):
    def test_split_on_space_and_punct(self) -> None:
        self.assertEqual(query_segments("张军 南京"), ["张军", "南京"])
        self.assertEqual(query_segments("张军，南京、教授"), ["张军", "南京", "教授"])

    def test_cjk_punct_and_fullwidth_space(self) -> None:
        self.assertEqual(query_segments("张军\u3000（教授）"), ["张军", "教授"])

    def test_drop_single_char_and_stopwords(self) -> None:
        # "与" 是虚词、"和" 是虚词，长度 <2 的也一律丢
        self.assertEqual(query_segments("红黑树 与 AVL 的区别"), ["红黑树", "avl", "的区别"])

    def test_casefold_and_dedup(self) -> None:
        self.assertEqual(query_segments("Liu Jun liu"), ["liu", "jun"])

    def test_continuous_cjk_stays_one_segment(self) -> None:
        # 用户真实输入：连写无空格，整串就是一个片段
        self.assertEqual(query_segments("某某大学信息学院张军"),
                         ["某某大学信息学院张军"])

    def test_empty(self) -> None:
        self.assertEqual(query_segments(""), [])
        self.assertEqual(query_segments("的 了 和"), [])


class TestBigrams(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(bigrams("张军"), ["张军"])
        self.assertEqual(bigrams("红黑树"), ["红黑", "黑树"])

    def test_dedup_keeps_order(self) -> None:
        self.assertEqual(bigrams("哈哈哈哈"), ["哈哈"])

    def test_short(self) -> None:
        self.assertEqual(bigrams("张"), [])
        self.assertEqual(bigrams(""), [])


class TestNeedles(unittest.TestCase):
    def test_short_segment_not_expanded(self) -> None:
        self.assertEqual(needles_of("张军 南京"), ["张军", "南京"])

    def test_long_segment_expanded(self) -> None:
        got = needles_of("网络安全张军")
        self.assertEqual(got[0], "网络安全张军")      # 整串保留
        self.assertIn("网络", got)
        self.assertIn("张军", got)                    # 关键：人名以 2-gram 形式留下
        self.assertEqual(len(got), 6)                 # 1 整串 + 5 个 bigram

    def test_empty(self) -> None:
        self.assertEqual(needles_of(""), [])


# -------------------------------------------------------------- 降级判定


class TestCheckRelaxed(unittest.TestCase):
    def test_no_query(self) -> None:
        self.assertFalse(check_relaxed(_cands(*_REAL_NAME), "").relaxed)

    def test_sample_too_small_never_relaxed(self) -> None:
        # 只有 3 条时不判定：宁可放过，不可误杀
        chk = check_relaxed(_cands(*_DEGRADED_NAME[:3]), "张军 南京")
        self.assertFalse(chk.relaxed)
        self.assertEqual(chk.basis, "sample_too_small")

    def test_no_candidates(self) -> None:
        self.assertFalse(check_relaxed([], "张军 南京").relaxed)

    # --- 多片段：首片段命中不算数 ---------------------------------------
    def test_multi_segment_tail_missing_is_relaxed(self) -> None:
        chk = check_relaxed(_cands(*_DEGRADED_NAME), "张军 南京")
        self.assertTrue(chk.relaxed)
        self.assertEqual(chk.basis, "tail")
        self.assertIn("南京", chk.detail)

    def test_multi_segment_tail_hit_is_fine(self) -> None:
        cands = _cands(*[f"张军-南京某大学 教师主页 {i}" for i in range(10)])
        chk = check_relaxed(cands, "张军 南京")
        self.assertFalse(chk.relaxed)

    def test_head_fully_hit_is_not_relaxed_even_if_tail_missing(self) -> None:
        # 回归保护："红黑树 实现" 实测被误判过 —— 10 条都含"红黑树"，
        # 只是标题里没有"实现"二字。这种情况必须放过：整页丢弃的代价远大于漏判。
        cands = _cands(*[f"红黑树（图解+秒懂）第 {i} 篇" for i in range(10)])
        self.assertFalse(check_relaxed(cands, "红黑树 实现").relaxed)

    def test_head_mostly_missing_is_relaxed(self) -> None:
        # 反过来：连开头的词都没被用上 → 降级，且 detail 要点名
        cands = _cands(*[f"张姓概况第 {i} 篇" for i in range(10)])
        chk = check_relaxed(cands, "张军 南京")
        self.assertTrue(chk.relaxed)
        self.assertIn("张军", chk.detail)

    # --- 单片段短串：看整串 ---------------------------------------------
    def test_short_segment_relaxed(self) -> None:
        chk = check_relaxed(_cands(*_DEGRADED_NAME), "张军")
        self.assertTrue(chk.relaxed)
        self.assertEqual(chk.basis, "phrase")

    def test_short_segment_ok(self) -> None:
        self.assertFalse(check_relaxed(_cands(*_REAL_NAME), "张军").relaxed)

    # --- 单片段长串：看 2-gram 覆盖率 -----------------------------------
    def test_long_segment_degraded_to_first_word(self) -> None:
        chk = check_relaxed(_cands(*_DEGRADED_NET), "网络安全张军")
        self.assertTrue(chk.relaxed)
        self.assertEqual(chk.basis, "bigram")
        self.assertLess(chk.ratio, 0.5)
        self.assertIn("张军", chk.detail)     # 缺的片段里必须点名到人名

    def test_long_segment_ok(self) -> None:
        cands = _cands(*[f"张军教授：网络安全方向的教学与科研 {i}" for i in range(10)])
        chk = check_relaxed(cands, "网络安全张军")
        self.assertFalse(chk.relaxed)
        self.assertGreaterEqual(chk.ratio, 0.5)

    def test_long_segment_broad_prefix_still_relaxed(self) -> None:
        # 实测边界（修复前漏判的那一次）：Bing 返回的全是"计算机网络/互联网百科"。
        # "网络""络安""安全"三个片段都命中，覆盖率 60% 已经过线，
        # 但"张军"一次没出现 —— 依然是"只用了查询的开头"，必须判降级。
        cands = _cands(
            "计算机网络知识点全面总结",
            "网络安全基础知识入门",
            "互联网（国际网络）_百度百科",
            "网络安全工程师要掌握的十项技能",
            "网络基础：TCP/IP 协议详解",
            "企业网络安全防护方案",
            "计算机网络（第7版）教材",
            "网络安全法全文",
            "网络测速工具",
            "信息安全与网络安全概述",
        )
        chk = check_relaxed(cands, "网络安全张军")
        self.assertTrue(chk.relaxed)
        self.assertGreaterEqual(chk.ratio, 0.5)     # 覆盖率够，是靠末尾规则抓到的
        self.assertIn("张军", chk.detail)

    # --- 回归保护：正常的多词查询不能被误杀 ------------------------------
    def test_variant_query_not_falsely_relaxed(self) -> None:
        # "红黑树 左旋 右旋" 的整串不可能出现在任何标题里，
        # 但它确实是好结果 —— 这条用例锁住"别把正常变体判成降级"
        chk = check_relaxed(_cands(*_REAL_RB), "红黑树 左旋 右旋")
        self.assertFalse(chk.relaxed)

    def test_real_name_page_not_relaxed(self) -> None:
        self.assertFalse(check_relaxed(_cands(*_REAL_NAME), "张军").relaxed)


# ------------------------------------------------------- 逐条体检 / 正则口径


class TestKeepRelated(unittest.TestCase):
    def test_drops_unrelated(self) -> None:
        cands = _cands("张军-某某大学", "王伟-导演", "张军-某某化工大学")
        kept, dropped = keep_related(cands, "张军")
        self.assertEqual(dropped, 1)
        self.assertEqual([c.title for c in kept], ["张军-某某大学", "张军-某某化工大学"])

    def test_keeps_all_when_query_empty(self) -> None:
        cands = _cands("随便什么", "另一条")
        kept, dropped = keep_related(cands, "")
        self.assertEqual((len(kept), dropped), (2, 0))

    def test_long_query_matches_via_bigram(self) -> None:
        cands = _cands("某校信息学院师资队伍", "无关页面")
        kept, dropped = keep_related(cands, "某某大学信息学院张军")
        # "学院"这个 2-gram 命中 → 保留；这比"整串包含"宽松，但不会放行完全无关的
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)


class TestTermPattern(unittest.TestCase):
    def test_multi_term_or_semantics(self) -> None:
        p = _term_pattern("红黑树 左旋 右旋")
        self.assertIsNotNone(p)
        self.assertTrue(p.search("红黑树是一种自平衡二叉查找树"))   # 命中词元
        self.assertTrue(p.search("插入调整需要左旋"))               # 命中另一个词元
        self.assertFalse(p.search("红黑人口库"))                    # 只含"红黑"→ 不算

    def test_old_whole_string_would_have_killed_it(self) -> None:
        # 旧实现用 re.escape(整串)，对任何真实标题都返回 False —— 变体扩量等于白做
        import re
        old = re.compile(re.escape("红黑树 左旋 右旋"), re.I)
        self.assertFalse(old.search("红黑树是一种自平衡二叉查找树"))
        self.assertTrue(_term_pattern("红黑树 左旋 右旋").search("红黑树是一种自平衡二叉查找树"))

    def test_empty_keyword(self) -> None:
        self.assertIsNone(_term_pattern(""))
        self.assertIsNone(_term_pattern("的 了"))


class TestScreenSinglePage(unittest.TestCase):
    def test_drops_off_topic(self) -> None:
        cands = _cands("张军-某某大学", "王伟-导演", "王伟-飞行员")
        kept, dropped = _screen_single_page(cands, "张军 南京", None)
        self.assertEqual(dropped, 2)
        self.assertEqual(len(kept), 1)

    def test_explicit_require_takes_precedence(self) -> None:
        cands = _cands("张军-某某大学东校区", "张军-某某大学西校区")
        kept, dropped = _screen_single_page(cands, "张军", "东校区")
        self.assertEqual((len(kept), dropped), (1, 1))

    def test_empty_list(self) -> None:
        self.assertEqual(_screen_single_page([], "张军", None), ([], 0))


# ------------------------------------------------------------ parse_serp 集成


class TestParseSerpRelaxed(unittest.TestCase):
    def test_degraded_page_dropped(self) -> None:
        r = parse_serp(_serp_html(_DEGRADED_NAME), 1, query="张军 南京")
        self.assertEqual(r.block_kind, "relaxed")
        self.assertEqual(r.candidates, [])       # 整页丢弃，不能进池子
        self.assertEqual(r.raw_count, 10)        # 但保留"解析到了 10 块"的证据
        self.assertFalse(r.blocked)              # 不是被拦：请求本身是成功的
        self.assertIn("南京", r.detail)

    def test_normal_page_untouched(self) -> None:
        r = parse_serp(_serp_html(_REAL_NAME), 1, query="张军")
        self.assertIsNone(r.block_kind)
        self.assertEqual(len(r.candidates), 10)

    def test_without_query_keeps_old_behaviour(self) -> None:
        # 不传 query = 只关心结构的用法，必须和以前完全一致
        r = parse_serp(_serp_html(_DEGRADED_NAME), 1)
        self.assertIsNone(r.block_kind)
        self.assertEqual(len(r.candidates), 10)

    def test_empty_page_still_empty_not_relaxed(self) -> None:
        r = parse_serp("<html><body>没有与此相关的结果</body></html>", 1, query="张军")
        self.assertEqual(r.block_kind, "empty")


class TestBoilerplate(unittest.TestCase):
    """兜底页判定（2026-10-23 实测样本：搜狗微信对搜不到的词返回站点名标题）。"""

    def test_site_name_titles(self) -> None:
        # 9 条结果标题全是「搜狗搜索」—— 没有任何一篇文章会叫站点名
        chk = check_boilerplate(_cands(*["搜狗搜索"] * 9))
        self.assertTrue(chk.is_boilerplate)
        self.assertEqual(chk.basis, "site_name")
        self.assertIn("兜底页", chk.detail)

    def test_site_name_case_and_space_insensitive(self) -> None:
        chk = check_boilerplate(_cands(" 搜狗搜索 ", "搜狗微信", "微信搜索"))
        self.assertTrue(chk.is_boilerplate)
        self.assertEqual(chk.basis, "site_name")

    def test_same_titles(self) -> None:
        chk = check_boilerplate(_cands(*["同一篇稿件转载"] * 4))
        self.assertTrue(chk.is_boilerplate)
        self.assertEqual(chk.basis, "same_titles")

    def test_two_same_titles_not_boilerplate(self) -> None:
        # 2 条同标题可能真是"同一篇被两家转载"，不误杀（门槛是 3）
        chk = check_boilerplate(_cands("同一篇稿件", "同一篇稿件"))
        self.assertFalse(chk.is_boilerplate)

    def test_distinct_titles_pass(self) -> None:
        chk = check_boilerplate(_cands("标题一", "标题二", "标题三", "标题四", "标题五"))
        self.assertFalse(chk.is_boilerplate)
        self.assertEqual(chk.unique, 5)

    def test_empty_title_never_judged(self) -> None:
        # 标题缺失是解析层该记账的事，不在这里判兜底页（否则会把残缺页误杀）
        chk = check_boilerplate(_cands("标题一", ""))
        self.assertFalse(chk.is_boilerplate)

    def test_no_candidates(self) -> None:
        self.assertFalse(check_boilerplate([]).is_boilerplate)

    def test_boilerplate_independent_of_query(self) -> None:
        # 同样的页面，带不带查询词结论一致 —— 这是结构判据的意义所在
        chk = check_boilerplate(_cands(*["搜狗搜索"] * 5))
        self.assertTrue(chk.is_boilerplate)


if __name__ == "__main__":
    unittest.main()
