"""人名模式：锚点解析、四档定级、按机构分组。

全部纯函数，不联网 —— 归因判断用的是标题/摘要字符串，正是最该被单测钉死的部分。
红线：**不同机构绝不合并**。合并两个单位的人等于凭空造出一个不存在的身份，
比漏掉一个人更糟。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.person import (  # noqa: E402
    TIER_CONFIRMED,
    TIER_LIKELY,
    TIER_SUSPECT,
    TIER_UNRELATED,
    UNMATCHED_LABEL,
    anchor_warnings,
    build_cards,
    judge,
    parse_person,
    summarize,
)

P = parse_person("张军", ["某某大学计算机学院"], ["网络安全"], ["教授"])


class _C:
    def __init__(self, title: str, url: str = "", when: str | None = None,
                 snippet: str = ""):
        self.title = title
        self.summary = snippet
        self.url_raw = url or f"https://x.example.com/{abs(hash(title)) % 9999}"
        self.url_canon = self.url_raw
        self.published_hint = when


class TestParsePerson(unittest.TestCase):
    def test_comma_and_repeat(self) -> None:
        p = parse_person("张军", ["某某大学，某某研究院"], ["网络安全,密码学"], ["教授"])
        self.assertEqual(p.orgs, ("某某大学", "某某研究院"))
        self.assertEqual(p.fields, ("网络安全", "密码学"))
        self.assertEqual(p.roles, ("教授",))

    def test_dedupe_preserves_order(self) -> None:
        p = parse_person("张军", ["甲大学", " 甲大学 ", "乙大学"])
        self.assertEqual(p.orgs, ("甲大学", "乙大学"))

    def test_has_anchors(self) -> None:
        self.assertFalse(parse_person("张军").has_anchors)
        self.assertTrue(parse_person("张军", ["甲大学"]).has_anchors)
        self.assertTrue(parse_person("张军", roles=["教授"]).has_anchors)

    def test_weak_anchors_order(self) -> None:
        p = parse_person("张军", fields=["网络安全"], roles=["教授"])
        self.assertEqual(p.weak_anchors, ("网络安全", "教授"))


class TestJudge(unittest.TestCase):
    def test_org_plus_weak_is_confirmed(self) -> None:
        j = judge("张军-某某大学计算机学院 教授，研究方向：网络安全", "", P)
        self.assertEqual(j.tier, TIER_CONFIRMED)
        self.assertEqual(j.orgs, ("某某大学计算机学院",))
        self.assertEqual(j.weak, ("网络安全", "教授"))
        self.assertEqual(j.matches, ("某某大学计算机学院", "网络安全", "教授"))
        self.assertGreater(j.score, 0)

    def test_org_only_is_likely(self) -> None:
        j = judge("某某大学计算机学院师资队伍", "张军", P)
        self.assertEqual(j.tier, TIER_LIKELY)

    def test_name_missing_is_not_evidence(self) -> None:
        """机构对上了但整页没有这个人名 → 那是机构页面，不是这个人的证据。"""
        j = judge("某某大学计算机学院师资队伍", "", P)
        self.assertFalse(j.name_present)
        self.assertEqual(j.tier, TIER_UNRELATED)

    def test_name_with_inner_space_matches(self) -> None:
        """网页里的姓名常被排成"张 军"（换行、表格对齐），归一化后必须能对上。"""
        self.assertTrue(judge("张 军 - 某某大学", "", P).name_present)

    def test_org_does_not_eat_大学生(self) -> None:
        """「北京大学生就业指导中心」里的「北京大学」是「大学生」，不是这所大学。"""
        p = parse_person("张军", ["北京大学"])
        self.assertEqual(judge("张军 北京大学生就业指导中心", "", p).orgs, ())
        self.assertEqual(judge("张军 北京大学计算机学院", "", p).orgs, ("北京大学",))

    def test_weak_only_is_suspect(self) -> None:
        # 同方向、同职称，但机构没对上 —— 这个领域里还有别人
        j = judge("网络安全教授张军谈行业趋势", "", P)
        self.assertEqual(j.tier, TIER_SUSPECT)

    def test_nothing_matches_is_unrelated(self) -> None:
        self.assertEqual(judge("今日天气", "", P).tier, TIER_UNRELATED)

    def test_negative_alone_vetoes(self) -> None:
        j = judge("张军（歌手）_百度百科", "", P)
        self.assertEqual(j.tier, TIER_UNRELATED)
        self.assertIn("演艺", j.negatives)

    def test_negative_does_not_veto_org_hit(self) -> None:
        # 机构对上之后，页面里出现"判决书"可能是同一人的案子 —— 不能因此踢人
        j = judge("某某大学计算机学院 张军教授 相关判决书", "", P)
        self.assertEqual(j.tier, TIER_CONFIRMED)
        self.assertIn("法律", j.negatives)

    def test_score_not_negative(self) -> None:
        self.assertGreaterEqual(judge("张军（歌手）", "", P).score, 0)


class TestBuildCards(unittest.TestCase):
    def test_different_orgs_never_merged(self) -> None:
        p = parse_person("张军", ["甲大学", "乙大学"], ["网络安全"])
        items = [
            (_C("张军-甲大学教授 网络安全"), judge("张军-甲大学教授 网络安全", "", p)),
            (_C("张军-乙大学教师 网络安全"), judge("张军-乙大学教师 网络安全", "", p)),
        ]
        cards = build_cards(p, items)
        labels = {c.label for c in cards}
        self.assertEqual(labels, {"甲大学", "乙大学"})
        self.assertTrue(all(c.tier == TIER_CONFIRMED for c in cards))

    def test_no_org_goes_to_unmatched(self) -> None:
        p = parse_person("张军", ["甲大学"])
        items = [(_C("张军谈天气"), judge("张军谈天气", "", p))]
        cards = build_cards(p, items)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].label, UNMATCHED_LABEL)
        self.assertEqual(cards[0].tier, TIER_UNRELATED)

    def test_card_tier_takes_best_member(self) -> None:
        p = parse_person("张军", ["甲大学"], ["网络安全"])
        items = [
            (_C("张军-甲大学"), judge("张军-甲大学", "", p)),
            (_C("张军-甲大学教授 网络安全"), judge("张军-甲大学教授 网络安全", "", p)),
        ]
        cards = build_cards(p, items)
        self.assertEqual(cards[0].tier, TIER_CONFIRMED)
        self.assertEqual(cards[0].docs, 2)

    def test_evidence_capped(self) -> None:
        p = parse_person("张军", ["甲大学"])
        items = [(_C(f"张军-甲大学 {i}"), judge(f"张军-甲大学 {i}", "", p)) for i in range(9)]
        cards = build_cards(p, items, max_evidence=3)
        self.assertEqual(len(cards[0].evidence), 3)
        self.assertEqual(cards[0].docs, 9)

    def test_sort_confirmed_first(self) -> None:
        p = parse_person("张军", ["甲大学"], ["网络安全"])
        items = [
            (_C("张军-乙大学"), judge("张军-乙大学", "", p)),
            (_C("张军-甲大学教授 网络安全"), judge("张军-甲大学教授 网络安全", "", p)),
        ]
        cards = build_cards(p, items)
        self.assertEqual(cards[0].label, "甲大学")

    def test_summarize_excludes_unmatched(self) -> None:
        p = parse_person("张军", ["甲大学"])
        items = [
            (_C("张军-甲大学"), judge("张军-甲大学", "", p)),
            (_C("张军谈天气"), judge("张军谈天气", "", p)),
        ]
        s = summarize(build_cards(p, items))
        self.assertEqual(s[TIER_LIKELY], 1)
        self.assertEqual(sum(s.values()), 1)      # 未匹配机构那张卡不计入


class TestPrimaryOrg(unittest.TestCase):
    """多机构命中时只选一个主机构 —— 拼成 "A · B" 等于造出第三种身份。"""

    def test_title_wins_over_snippet(self) -> None:
        p = parse_person("张军", ["甲大学", "乙大学"])
        j = judge("张军-乙大学计算机学院", "曾就读于甲大学", p)
        self.assertEqual(j.primary_org, "乙大学")
        self.assertEqual(j.also_orgs, ("甲大学",))

    def test_title_hits_beat_snippet_hits(self) -> None:
        p = parse_person("张军", ["甲大学", "乙大学"])
        # 甲在摘要里出现两次、乙在标题里出现一次 → 标题优先
        j = judge("张军-乙大学", "甲大学 甲大学 合作项目", p)
        self.assertEqual(j.primary_org, "乙大学")

    def test_user_order_breaks_tie(self) -> None:
        p = parse_person("张军", ["甲大学", "乙大学"])
        j = judge("张军-甲大学与乙大学联合培养", "", p)
        self.assertEqual(j.primary_org, "甲大学")
        self.assertEqual(j.also_orgs, ("乙大学",))

    def test_cards_split_by_primary_not_merged(self) -> None:
        p = parse_person("张军", ["甲大学", "乙大学"])
        items = [
            (_C("张军-甲大学教授"), judge("张军-甲大学教授", "", p)),
            (_C("张军-乙大学研究员"), judge("张军-乙大学研究员", "", p)),
            # 同时提到两家的页面：进甲那张卡，乙只作为"还提到"标注
            (_C("张军-甲大学与乙大学联合项目"), judge("张军-甲大学与乙大学联合项目", "", p)),
        ]
        cards = {c.label: c for c in build_cards(p, items)}
        self.assertEqual(set(cards), {"甲大学", "乙大学"})
        self.assertEqual(cards["甲大学"].docs, 2)
        self.assertEqual(cards["甲大学"].also_orgs, ("乙大学",))


class TestTimeline(unittest.TestCase):
    """证据要带时间并按时间排 —— 这是"人物轨迹"最小可用的形态。"""

    def test_evidence_sorted_newest_first(self) -> None:
        p = parse_person("张军", ["甲大学"])
        items = [
            (_C("张军-甲大学 2021", when="2021-03-05"), judge("张军-甲大学 2021", "", p)),
            (_C("张军-甲大学 2025", when="2025-06-01"), judge("张军-甲大学 2025", "", p)),
            (_C("张军-甲大学 2023", when="2023-01-09"), judge("张军-甲大学 2023", "", p)),
        ]
        card = build_cards(p, items)[0]
        self.assertEqual([e.when for e in card.evidence],
                         ["2025-06-01", "2023-01-09", "2021-03-05"])
        self.assertEqual((card.first_when, card.last_when), ("2021-03-05", "2025-06-01"))

    def test_undated_evidence_goes_last(self) -> None:
        p = parse_person("张军", ["甲大学"])
        items = [
            (_C("张军-甲大学 无日期"), judge("张军-甲大学 无日期", "", p)),
            (_C("张军-甲大学 有日期", when="2024-02-02"),
             judge("张军-甲大学 有日期", "", p)),
        ]
        card = build_cards(p, items)[0]
        self.assertEqual([e.when for e in card.evidence], ["2024-02-02", None])

    def test_datetime_truncated_to_day(self) -> None:
        p = parse_person("张军", ["甲大学"])
        items = [(_C("张军-甲大学", when="2024-02-02T08:30:00+08:00"),
                  judge("张军-甲大学", "", p))]
        self.assertEqual(build_cards(p, items)[0].evidence[0].when, "2024-02-02")


class TestNameMissingAccounting(unittest.TestCase):
    def test_counted_but_not_carded(self) -> None:
        """没出现姓名的条目：不进卡片，但必须被数出来（否则像"一条都没找到"）。"""
        p = parse_person("张军", ["甲大学"])
        items = [
            (_C("张军-甲大学"), judge("张军-甲大学", "", p)),
            (_C("甲大学计算机学院师资队伍"), judge("甲大学计算机学院师资队伍", "", p)),
            (_C("甲大学招生简章"), judge("甲大学招生简章", "", p)),
        ]
        stats: dict[str, int] = {}
        cards = build_cards(p, items, stats=stats)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].docs, 1)
        self.assertEqual(stats, {"total": 3, "name_missing": 2, "attributed": 1})


class TestAnchorWarnings(unittest.TestCase):
    def test_generic_org_warned(self) -> None:
        w = anchor_warnings(parse_person("张军", ["大学"]))
        self.assertTrue(any("太泛" in x for x in w))

    def test_org_only_warned(self) -> None:
        w = anchor_warnings(parse_person("张军", ["甲大学"]))
        self.assertTrue(any("最高只能到" in x for x in w))

    def test_full_anchors_no_warning(self) -> None:
        self.assertEqual(anchor_warnings(P), [])

    def test_no_org_warned(self) -> None:
        w = anchor_warnings(parse_person("张军", roles=["教授"]))
        self.assertTrue(any("没给机构锚点" in x for x in w))


if __name__ == "__main__":
    unittest.main(verbosity=2)
