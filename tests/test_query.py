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
from kwsearch.query import expand_queries, query_key  # noqa: E402

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
