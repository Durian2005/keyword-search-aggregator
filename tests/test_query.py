"""M1.5 验收：URL 规范化 + 查询变体 + 扩展驱动（早停/去重/过滤）。

全部离线：扩展驱动用一个假通道替身，不联网、不 sleep。

跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kwsearch.aggregate import canonicalize, host_of  # noqa: E402
from kwsearch.config import ChannelConfig, PagingConfig  # noqa: E402
from kwsearch.discover.base import ChannelContext  # noqa: E402
from kwsearch.expand import domain_distribution, expand_channel  # noqa: E402
from kwsearch.fetch import RetryPolicy  # noqa: E402
from kwsearch.models import Candidate, ChannelResult  # noqa: E402
from kwsearch.query import expand_queries, probe_profile, query_key  # noqa: E402

SPEC = {
    "enabled": True,
    "max_queries": 5,
    "zero_gain_streak": 2,
    "suffixes": ["左旋 右旋", "实现", "图解"],
    "extra_queries": ["red black tree"],
}


class TestCanonicalize(unittest.TestCase):
    def test_tracking_params_removed(self) -> None:
        u = "https://a.com/p?utm_source=x&utm_medium=y&id=7&spm=abc"
        self.assertEqual(canonicalize(u), "https://a.com/p?id=7")

    def test_fragment_and_trailing_slash(self) -> None:
        self.assertEqual(canonicalize("https://a.com/p/#comment"), "https://a.com/p")

    def test_www_and_default_port(self) -> None:
        self.assertEqual(canonicalize("https://www.a.com:443/p"), "https://a.com/p")
        self.assertEqual(canonicalize("http://a.com:80/p"), "http://a.com/p")

    def test_param_order_normalized(self) -> None:
        self.assertEqual(canonicalize("https://a.com/p?b=2&a=1"), canonicalize("https://a.com/p?a=1&b=2"))

    def test_idempotent(self) -> None:
        for u in ["https://www.a.com/p/?utm_source=x#top", "https://a.com/", "http://a.com:80"]:
            once = canonicalize(u)
            self.assertEqual(canonicalize(once), once, u)

    def test_identity_params_kept(self) -> None:
        # 微信文章的 __biz/mid/idx/sn 看着像追踪参数，删了就打不开 —— 必须保留
        u = "https://mp.weixin.qq.com/s?__biz=MzA&mid=265&idx=1&sn=abc&chksm=def"
        out = canonicalize(u)
        for k in ("__biz", "mid", "idx", "sn", "chksm"):
            self.assertIn(k + "=", out)

    def test_relative_and_empty_passthrough(self) -> None:
        self.assertEqual(canonicalize(""), "")
        self.assertEqual(canonicalize("/a/b?utm_source=x"), "/a/b?utm_source=x")

    def test_path_case_preserved(self) -> None:
        self.assertEqual(canonicalize("https://a.com/AbC"), "https://a.com/AbC")

    def test_host_of(self) -> None:
        self.assertEqual(host_of("https://www.cnblogs.com/x"), "cnblogs.com")
        self.assertEqual(host_of("oi-wiki.org"), "oi-wiki.org")
        self.assertEqual(host_of("http://a.com:8080/y"), "a.com")


class TestExpandQueries(unittest.TestCase):
    def test_base_first_and_order(self) -> None:
        vs = expand_queries("红黑树", SPEC)
        self.assertEqual([v.strategy for v in vs],
                         ["base", "suffix", "suffix", "suffix", "extra"])
        self.assertEqual(vs[0].q, "红黑树")
        self.assertEqual(vs[1].q, "红黑树 左旋 右旋")
        self.assertEqual(vs[-1].q, "red black tree")
        self.assertTrue(vs[0].is_base)

    def test_max_queries_includes_base(self) -> None:
        vs = expand_queries("红黑树", {**SPEC, "max_queries": 3})
        self.assertEqual(len(vs), 3)

    def test_dedupe_and_blank(self) -> None:
        vs = expand_queries("红黑树", {"suffixes": ["", "   ", "树"], "extra_queries": ["红黑树"]})
        self.assertEqual([v.q for v in vs], ["红黑树", "红黑树 树"])

    def test_disabled_returns_base_only(self) -> None:
        self.assertEqual([v.q for v in expand_queries("x", {**SPEC, "enabled": False})], ["x"])

    def test_empty_keyword(self) -> None:
        self.assertEqual(expand_queries("   ", SPEC), [])

    def test_custom_separator(self) -> None:
        vs = expand_queries("a", {"suffixes": ["b"], "separator": ""})
        self.assertEqual(vs[1].q, "ab")

    def test_query_key_ignores_case_and_space(self) -> None:
        self.assertEqual(query_key("  Red  Black "), query_key("red black"))


def _ctx(throttle: float = 0.0) -> ChannelContext:
    ch = ChannelConfig(
        id="fake", name="fake", type="serp", parser="fake",
        url_template="https://x/?q={q}",
        paging=PagingConfig(mode="offset", start=1, step=10, max_pages=1),
        throttle_min_interval_s=throttle,
    )
    return ChannelContext(channel=ch, defaults={}, extract={},
                          fetcher=None, policy=RetryPolicy(), timeout=5)  # type: ignore[arg-type]


def _result(q: str, urls: list[str], *, blocked: bool = False) -> ChannelResult:
    return ChannelResult(
        channel="fake", query=q, blocked=blocked,
        block_kind="challenge" if blocked else None,
        candidates=[Candidate(url_raw=u, title=f"{q} | {u}", channel="fake", rank=i + 1)
                    for i, u in enumerate(urls)],
    )


class TestExpandChannel(unittest.TestCase):
    def _run(self, mapping, variants=None, **kw):
        def fn(q, offset, ctx):
            return mapping[q]

        vs = variants or expand_queries("k", {"suffixes": [f"s{i}" for i in range(1, 6)]})
        return expand_channel(fn, _ctx(), vs, **kw)

    def test_dedupe_across_queries(self) -> None:
        mapping = {
            "k": _result("k", ["https://a.com/1", "https://a.com/2"]),
            "k s1": _result("k s1", ["https://a.com/2?utm_source=x", "https://a.com/3"]),
        }
        vs = expand_queries("k", {"suffixes": ["s1"]})
        cands, st = expand_channel(lambda q, o, c: mapping[q], _ctx(), vs)
        self.assertEqual(len(cands), 3)                       # utm 版本被归并
        self.assertEqual(st.duplicated, 1)
        self.assertEqual([c.url_raw for c in cands],
                         ["https://a.com/1", "https://a.com/2", "https://a.com/3"])

    def test_provenance_recorded(self) -> None:
        mapping = {"k": _result("k", ["https://a.com/1"])}
        vs = expand_queries("k", {})
        cands, _ = expand_channel(lambda q, o, c: mapping[q], _ctx(), vs)
        self.assertEqual(cands[0].extra["found_by"], "k")
        self.assertEqual(cands[0].extra["strategy"], "base")
        self.assertEqual(cands[0].url_canon, "https://a.com/1")

    def test_early_stop_after_two_zero_gain(self) -> None:
        # s1 有新增，s2/s3 全是重复 → 连续 2 次零新增就该停，s4/s5 不该被执行
        mapping = {
            "k": _result("k", ["https://a.com/1"]),
            "k s1": _result("k s1", ["https://a.com/2"]),
            "k s2": _result("k s2", ["https://a.com/1"]),
            "k s3": _result("k s3", ["https://a.com/1"]),
            "k s4": _result("k s4", ["https://a.com/9"]),
            "k s5": _result("k s5", ["https://a.com/8"]),
        }
        cands, st = self._run(mapping, zero_gain_streak=2)
        self.assertEqual(st.queries, 4)          # k, s1, s2, s3
        self.assertEqual(len(cands), 2)
        self.assertIn("连续 2 个变体零新增", st.stopped_reason)
        self.assertEqual([s.q for s in st.per_query], ["k", "k s1", "k s2", "k s3"])

    def test_no_early_stop_when_every_variant_useful(self) -> None:
        mapping = {f"k s{i}": _result(f"k s{i}", [f"https://a.com/{i}"]) for i in range(0, 6)}
        mapping["k"] = _result("k", ["https://a.com/base"])
        cands, st = self._run(mapping, zero_gain_streak=2)
        self.assertEqual(st.queries, 6)
        self.assertEqual(len(cands), 6)
        self.assertEqual(st.stopped_reason, "变体已全部执行")

    def test_blocked_aborts_immediately(self) -> None:
        mapping = {
            "k": _result("k", ["https://a.com/1"]),
            "k s1": _result("k s1", [], blocked=True),
        }
        cands, st = self._run(mapping)
        self.assertTrue(st.blocked)
        self.assertEqual(st.queries, 2)          # 被拦后不再继续
        self.assertIn("被拦", st.stopped_reason)
        self.assertEqual(len(cands), 1)

    def test_require_filters_noise(self) -> None:
        import re
        mapping = {
            "k": _result("k", ["https://hongheiku.com/1", "https://zhihu.com/2"]),
        }
        vs = expand_queries("k", {})
        cands, st = expand_channel(lambda q, o, c: mapping[q], _ctx(), vs,
                                   require=re.compile("k \\|"))
        self.assertEqual(len(cands), 2)          # 假数据标题都含 "k |"，不过滤

        cands2, st2 = expand_channel(
            lambda q, o, c: ChannelResult(channel="fake", query=q, candidates=[
                Candidate(url_raw="https://hongheiku.com/1", title="红黑彩票开奖"),
                Candidate(url_raw="https://zhihu.com/2", title="红黑树详解"),
            ]),
            _ctx(), vs, require=re.compile(r"红黑树"))
        self.assertEqual(len(cands2), 1)
        self.assertEqual(st2.filtered, 1)

    def test_no_sleep_when_throttle_zero(self) -> None:
        calls = []
        mapping = {f"k s{i}": _result(f"k s{i}", [f"https://a.com/{i}"]) for i in range(0, 6)}
        mapping["k"] = _result("k", ["https://a.com/base"])
        self._run(mapping, sleep=calls.append)
        self.assertEqual(calls, [])              # throttle=0 不该 sleep

    def test_domain_distribution(self) -> None:
        cands = [
            Candidate(url_raw="https://a.com/1", title="1", extra={"domain": "a.com"}),
            Candidate(url_raw="https://a.com/2", title="2", extra={"domain": "a.com"}),
            Candidate(url_raw="https://b.com/3", title="3", extra={"domain": "b.com"}),
        ]
        self.assertEqual(domain_distribution(cands), [("a.com", 2), ("b.com", 1)])


class _PC:
    """probe_profile 的 duck-typing 替身：只有 title 和 extra.domain。"""

    def __init__(self, title: str, domain: str = ""):
        self.title = title
        self.extra = {"domain": domain} if domain else {}


class TestProbeProfile(unittest.TestCase):
    """探测分类：技术/新闻/人名/兜底，以及三条设计红线。"""

    def test_empty_is_generic(self) -> None:
        self.assertEqual(probe_profile([]), "generic")
        self.assertEqual(probe_profile(None), "generic")   # type: ignore[arg-type]

    def test_tech_domains(self) -> None:
        cands = [_PC("红黑树详解", "blog.csdn.net"),      # 子域要能命中主域
                 _PC("红黑树实现", "www.cnblogs.com"),
                 _PC("GitHub 上的可视化", "github.com")]
        self.assertEqual(probe_profile(cands), "tech")

    def test_same_news_domain_one_vote(self) -> None:
        # 10 条全是新华网只是 1 票，不到 news 门槛 —— 大站垄断不许伪装成共识
        cands = [_PC(f"央行降准 {i}", "www.xinhuanet.com") for i in range(10)]
        self.assertEqual(probe_profile(cands), "generic")

    def test_tech_counted_per_candidate(self) -> None:
        # tech 与 news 的计票单位不同：技术域名或标题技术词，命中一条算一票
        cands = [_PC(f"红黑树 {i}", "blog.csdn.net") for i in range(10)]
        self.assertEqual(probe_profile(cands), "tech")

    def test_tech_title_signals_without_tech_domain(self) -> None:
        # 2026-10-18 实测复现：bing 对技术词的结果常落在
        # zhihu / 百家号 / B站 / 文库这些"什么词都收"的站上，
        # 纯技术域名可能只有 1 票 —— 标题信号（数据结构/算法/复杂度）必须认
        cands = [
            _PC("一文带你彻底读懂红黑树（附详细图解）", "zhuanlan.zhihu.com"),
            _PC("红黑树（AVL树的兄弟）_百度百科", "baike.baidu.com"),
            _PC("【动画详解】红黑树 插入与旋转", "www.bilibili.com"),
            _PC("数据结构：红黑树的删除操作", "wenku.baidu.com"),
            _PC("二叉树遍历总结", "zhuanlan.zhihu.com"),
        ]
        self.assertEqual(probe_profile(cands), "tech")

    def test_news_domains(self) -> None:
        cands = [_PC("央行宣布降准", "finance.people.com.cn"),
                 _PC("降准落地", "www.xinhuanet.com"),
                 _PC("央行降准解读", "www.thepaper.cn")]
        self.assertEqual(probe_profile(cands), "news")

    def test_person_edu_faculty(self) -> None:
        cands = [_PC("张军-某某大学 教师个人主页", "cs.xxx.edu.cn"),
                 _PC("某某大学师资队伍", "js.xxx.edu.cn")]
        self.assertEqual(probe_profile(cands), "person")

    def test_weak_tech_below_threshold_is_generic(self) -> None:
        # 2 条技术信号不到门槛（≥3）：宁可漏判（少扩量），不可错判（白打请求）
        cands = [_PC("红黑树", "blog.csdn.net"), _PC("算法", "www.cnblogs.com")]
        self.assertEqual(probe_profile(cands), "generic")

    def test_tie_between_weak_tech_and_news_goes_news(self) -> None:
        # tech 2 票不过门槛，news 2 域过门槛 → 判 news。
        # news 和 generic 行为一样（只打基词），这个方向的倾斜是安全的
        cands = [_PC("a", "blog.csdn.net"), _PC("b", "www.cnblogs.com"),
                 _PC("c", "www.xinhuanet.com"), _PC("d", "www.thepaper.cn")]
        self.assertEqual(probe_profile(cands), "news")

    def test_suffix_lookalike_host_not_matched(self) -> None:
        # notcsdn.net 不许命中 csdn.net；匹配必须是整段域名边界
        cands = [_PC("x", "notcsdn.net"), _PC("y", "alsocnblogs.com")]
        self.assertEqual(probe_profile(cands), "generic")


PROFILES_SPEC = {
    "enabled": True,
    "profiles": {
        "tech": ["左旋 右旋", "实现"],
        "person": [],
        "news": [],
        "generic": [],
    },
}


class TestProfilesSpec(unittest.TestCase):
    def test_auto_with_tech_probe(self) -> None:
        probe = [_PC("t1", "blog.csdn.net"), _PC("t2", "www.cnblogs.com"),
                 _PC("t3", "github.com")]
        vs = expand_queries("红黑树", PROFILES_SPEC, probe=probe)
        self.assertEqual([v.q for v in vs], ["红黑树", "红黑树 左旋 右旋", "红黑树 实现"])

    def test_auto_without_probe_is_base_only(self) -> None:
        # 没有证据就不加后缀 —— discover --expand 之外的场景都该走探测
        vs = expand_queries("红黑树", PROFILES_SPEC)
        self.assertEqual([v.q for v in vs], ["红黑树"])

    def test_auto_news_probe_is_base_only(self) -> None:
        probe = [_PC("n1", "www.xinhuanet.com"), _PC("n2", "www.thepaper.cn"),
                 _PC("n3", "finance.people.com.cn")]
        vs = expand_queries("央行降准", PROFILES_SPEC, probe=probe)
        self.assertEqual([v.q for v in vs], ["央行降准"])

    def test_explicit_profile_overrides_probe(self) -> None:
        vs = expand_queries("红黑树", {**PROFILES_SPEC, "profile": "tech"})
        self.assertEqual(len(vs), 3)

    def test_unknown_profile_is_base_only(self) -> None:
        vs = expand_queries("x", {**PROFILES_SPEC, "profile": "nope"})
        self.assertEqual([v.q for v in vs], ["x"])

    def test_suffixes_still_work_without_profiles(self) -> None:
        # 向后兼容：旧配置（顶层 suffixes）行为不变
        vs = expand_queries("k", {"suffixes": ["s1"]})
        self.assertEqual([v.q for v in vs], ["k", "k s1"])

    def test_extra_queries_apply_in_all_profiles(self) -> None:
        vs = expand_queries("k", {**PROFILES_SPEC, "extra_queries": ["k english"]})
        self.assertEqual([v.q for v in vs], ["k", "k english"])

    def test_max_queries_caps_profiles_too(self) -> None:
        spec = {**PROFILES_SPEC, "profiles": {"tech": ["a", "b", "c", "d"]},
                "max_queries": 3}
        probe = [_PC("t1", "blog.csdn.net"), _PC("t2", "www.cnblogs.com"),
                 _PC("t3", "github.com")]
        self.assertEqual(len(expand_queries("k", spec, probe=probe)), 3)


class TestPlanHook(unittest.TestCase):
    """expand_channel 的 plan_hook：基词打完后边打边定后续变体。"""

    def _mapping_fn(self, mapping):
        return lambda q, o, c: mapping[q]

    def test_hook_extends_plan_and_executes(self) -> None:
        from kwsearch.query import QueryVariant
        mapping = {
            "k": _result("k", ["https://a.com/1"]),
            "k s1": _result("k s1", ["https://a.com/2"]),
        }
        calls = []

        def hook(cands):
            calls.append(list(cands))
            return [QueryVariant(q="k s1", strategy="suffix", priority=1)]

        cands, st = expand_channel(self._mapping_fn(mapping), _ctx(),
                                   expand_queries("k", {}), plan_hook=hook)
        self.assertEqual(len(calls), 1)
        self.assertEqual([c.url_raw for c in calls[0]], ["https://a.com/1"])  # 拿到的是去重后的基词结果
        self.assertEqual(len(cands), 2)
        self.assertEqual([s.q for s in st.per_query], ["k", "k s1"])

    def test_hook_empty_means_base_only(self) -> None:
        mapping = {"k": _result("k", ["https://a.com/1"])}
        cands, st = expand_channel(self._mapping_fn(mapping), _ctx(),
                                   expand_queries("k", {}), plan_hook=lambda c: [])
        self.assertEqual(st.queries, 1)
        self.assertEqual(len(cands), 1)
        self.assertEqual(st.stopped_reason, "变体已全部执行")

    def test_hook_not_called_when_base_blocked(self) -> None:
        mapping = {"k": _result("k", [], blocked=True)}
        called = []
        cands, st = expand_channel(self._mapping_fn(mapping), _ctx(),
                                   expand_queries("k", {}),
                                   plan_hook=lambda c: called.append(c) or [])
        self.assertTrue(st.blocked)
        self.assertEqual(called, [])          # 被拦就该收手，探测证据也不可信

    def test_hook_result_respects_early_stop(self) -> None:
        from kwsearch.query import QueryVariant
        mapping = {
            "k": _result("k", ["https://a.com/1"]),
            "k s1": _result("k s1", ["https://a.com/1"]),   # 零新增
            "k s2": _result("k s2", ["https://a.com/1"]),   # 零新增 → 连续 2 次，停
            "k s3": _result("k s3", ["https://a.com/9"]),
        }

        def hook(cands):
            return [QueryVariant(q=f"k s{i}", strategy="suffix", priority=i)
                    for i in (1, 2, 3)]

        _, st = expand_channel(self._mapping_fn(mapping), _ctx(),
                               expand_queries("k", {}), plan_hook=hook)
        self.assertEqual(st.queries, 3)       # k, s1, s2 —— s3 没打
        self.assertIn("零新增", st.stopped_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
