"""搜狗微信文章搜索解析测试。

fixture 按 2026-10-21 真实页面结构合成（txt-box / txt-info / all-time-y2 /
s2 里的 timeConvert），内容中性化；单测零真实请求。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.config import ChannelConfig, PagingConfig  # noqa: E402
from kwsearch.discover import get_channel, get_parser  # noqa: E402
from kwsearch.discover.base import ChannelContext  # noqa: E402
from kwsearch.discover.sogou_weixin import (  # noqa: E402
    _RESOLVE_CACHE,
    ResolvePolicy,
    join_relay_fragments,
    parse_serp,
    resolve_candidates,
)
from kwsearch.fetch import FetchResult, RetryPolicy  # noqa: E402
from kwsearch.models import Candidate  # noqa: E402


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

    def test_rank_increases_within_page(self) -> None:
        # 名次必须逐条递增。曾经写的是 rank=页码，一页 10 条全等于 1，
        # relate() 的名次加成恒为最高值 → 公众号条目被系统性高估。
        r = parse_serp(_PAGE, 1, channel_id="sogou_weixin")
        self.assertEqual([c.rank for c in r.candidates], [1, 2])

    def test_rank_continues_across_pages(self) -> None:
        # 第 2 页第一条接着第 1 页往后数（每页 PAGE_SIZE 条），与 bilibili 同口径
        from kwsearch.discover.sogou_weixin import PAGE_SIZE

        r = parse_serp(_PAGE, 2, channel_id="sogou_weixin")
        self.assertEqual([c.rank for c in r.candidates],
                         [PAGE_SIZE + 1, PAGE_SIZE + 2])
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

    def test_boilerplate_page_dropped(self) -> None:
        # 2026-10-23 实测样本：搜不到时搜狗返回的兜底页 —— 列表结构完整，
        # 但 9 条"结果"标题全是站点名。整页必须丢。
        blocks = "".join(
            _article(n, "搜狗搜索", "", "某公众号", ts=None) for n in range(9)
        )
        page = f'<html><body><ul class="news-list">{blocks}</ul></body></html>'
        r = parse_serp(page, 1, channel_id="sogou_weixin")
        self.assertEqual(r.block_kind, "boilerplate")
        self.assertEqual(r.candidates, [])
        self.assertEqual(r.raw_count, 9)
        self.assertIn("整页丢弃", r.detail)

    def test_boilerplate_beats_relaxed_when_snippet_echoes_query(self) -> None:
        # 真实兜底页的提示语里写着查询词（"没有找到与「××」相关的文章"），
        # 语义体检会误判成"相关" —— 这正是当初 11 条假阳性入库的原因。
        # 结构判据不看查询词，所以能拦住。
        query = "生僻历史词条"
        snippet = f"抱歉，没有找到与「{query}」相关的文章"
        blocks = "".join(
            _article(n, "搜狗搜索", snippet, "某公众号", ts=None) for n in range(9)
        )
        page = f'<html><body><ul class="news-list">{blocks}</ul></body></html>'
        r = parse_serp(page, 1, channel_id="sogou_weixin", query=query)
        self.assertEqual(r.block_kind, "boilerplate")
        self.assertEqual(r.candidates, [])

    def test_same_titles_page_dropped(self) -> None:
        # 兜底页的另一种长相：标题不是站点名，但整页反复给同一条
        blocks = "".join(
            _article(n, "某活动通知", "活动详情", "某公众号") for n in range(4)
        )
        page = f'<html><body><ul class="news-list">{blocks}</ul></body></html>'
        r = parse_serp(page, 1, channel_id="sogou_weixin", query="某活动")
        self.assertEqual(r.block_kind, "boilerplate")

    def test_two_articles_same_title_kept(self) -> None:
        # 2 条同标题（同一篇被两家转载）不该被误杀
        blocks = "".join(
            _article(n, "红黑树入门", "红黑树是一种自平衡二叉查找树", "某公众号")
            for n in range(2)
        )
        page = f'<html><body><ul class="news-list">{blocks}</ul></body></html>'
        r = parse_serp(page, 1, channel_id="sogou_weixin", query="红黑树")
        self.assertEqual(len(r.candidates), 2)
        self.assertIsNone(r.block_kind)

    # ---- 跨跑批去重键（2026-10-24）----------------------------------------
    # 中转链的 token 每次请求都轮换，URL 去重失效；业务键必须与 URL 无关。

    def _one(self, href: str, title: str = "红黑树入门",
             account: str = "某公众号") -> object:
        page = ('<html><body><ul class="news-list">'
                + _article(0, title, "红黑树是一种自平衡二叉查找树", account, path=href)
                + "</ul></body></html>")
        return parse_serp(page, 1, channel_id="sogou_weixin", query="红黑树").candidates[0]

    def test_dedupe_key_stable_when_token_rotates(self) -> None:
        a = self._one("/link?url=TOKEN_AAA&amp;type=2")
        b = self._one("/link?url=TOKEN_BBB&amp;type=2")
        self.assertNotEqual(a.url_raw, b.url_raw)       # URL 确实变了
        self.assertEqual(a.dedupe_key, b.dedupe_key)    # 业务键不变
        self.assertTrue(a.dedupe_key)

    def test_dedupe_key_differs_across_articles_and_accounts(self) -> None:
        base = self._one("/link?url=T1&amp;type=2")
        other_title = self._one("/link?url=T1&amp;type=2", title="另一篇文章")
        other_account = self._one("/link?url=T1&amp;type=2", account="另一个公众号")
        self.assertNotEqual(base.dedupe_key, other_title.dedupe_key)
        self.assertNotEqual(base.dedupe_key, other_account.dedupe_key)

    def test_dedupe_key_normalizes_case_and_space(self) -> None:
        a = self._one("/link?url=T1&amp;type=2", title="Red Black Tree")
        b = self._one("/link?url=T2&amp;type=2", title=" red black tree ")
        self.assertEqual(a.dedupe_key, b.dedupe_key)

    def test_dedupe_key_is_hash_not_plaintext(self) -> None:
        # 标题是页面原文（可能含手机号），键要落进 doc 表 —— 必须是哈希，不含原文
        c = self._one("/link?url=T1&amp;type=2", title="招生咨询 13812345678")
        self.assertNotIn("13812345678", c.dedupe_key)
        self.assertEqual(len(c.dedupe_key), 20)


# ============================================================================
# 中转链解链（2026-10-25）
# ============================================================================
_REAL = ("https://mp.weixin.qq.com/s?src=11&timestamp=1792940236&ver=7048"
         "&signature=abcDEF123ghiJKL")


def _relay_page(real: str = _REAL, *, width: int = 20) -> str:
    """合成中转页：真实地址拆成多段 `url += '...'`，段尾插 @ 干扰（真实结构）。"""
    frags = [real[i:i + width] for i in range(0, len(real), width)]
    body = "\n".join(f"url += '{f}@';" for f in frags)
    return f"<!doctype html><html><head><title>搜狗微信</title></head><body>" \
           f"<script>var url = '';\n{body}\nwindow.location.replace(url);</script>" \
           f"</body></html>"


_CHALLENGE = "<!doctype html><html><head><title>搜狗搜索</title></head>" \
             "<body><div id='seccodeImage'></div></body></html>"


class _FakeFetcher:
    """按 URL 返回预置响应；记录调用顺序。单测零真实请求。"""

    def __init__(self, pages: dict[str, tuple[int, str]]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str, headers: dict, timeout: int = 20) -> FetchResult:
        self.calls.append(url)
        status, body = self.pages.get(url, (404, ""))
        raw = body.encode("utf-8")
        return FetchResult(status=status, body=body, content_type="text/html; charset=utf-8",
                           final_url=url, elapsed_ms=1, fetcher="fake",
                           content_bytes=len(raw), raw=raw)

    def close(self) -> None:
        pass


def _cand(url: str = "https://weixin.sogou.com/link?url=FAKE0&type=2") -> Candidate:
    return Candidate(url_raw=url, title="某篇红黑树文章", channel="sogou_weixin",
                     rank=1, extra={"domain": "mp.weixin.qq.com"})


class TestJoinRelayFragments(unittest.TestCase):
    def test_joins_and_strips_at_sign(self) -> None:
        self.assertEqual(join_relay_fragments(_relay_page()), _REAL)

    def test_keeps_timestamp_intact(self) -> None:
        # 回归：曾用 html.unescape() 把 `&times` 啃成 `×`，链接直接废掉
        got = join_relay_fragments(_relay_page())
        self.assertIn("&timestamp=", got)
        self.assertNotIn("×tamp", got)

    def test_handles_html_escaped_amp(self) -> None:
        page = _relay_page().replace("&", "&amp;")
        self.assertEqual(join_relay_fragments(page), _REAL)

    def test_rejects_challenge_page(self) -> None:
        self.assertIsNone(join_relay_fragments(_CHALLENGE))

    def test_rejects_non_mp_target(self) -> None:
        page = _relay_page("https://example.com/a?x=1")
        self.assertIsNone(join_relay_fragments(page))

    def test_rejects_mp_without_signature(self) -> None:
        # 没有 signature 的 mp 地址是失效的，认了反而更糟（点开是错误页）
        self.assertIsNone(join_relay_fragments(_relay_page("https://mp.weixin.qq.com/s?src=11")))

    def test_empty_input(self) -> None:
        self.assertIsNone(join_relay_fragments(""))


class TestResolvePolicy(unittest.TestCase):
    def test_off_by_default(self) -> None:
        p = ResolvePolicy.from_mapping({})
        self.assertFalse(p.enabled)
        self.assertEqual(p.max_links, 4)

    def test_clamps_negative_max(self) -> None:
        p = ResolvePolicy.from_mapping({"enabled": True, "max_links": -5})
        self.assertEqual(p.max_links, 0)

    def test_stop_after_fail_at_least_one(self) -> None:
        p = ResolvePolicy.from_mapping({"stop_after_fail": 0})
        self.assertEqual(p.stop_after_fail, 1)


class TestResolveCandidates(unittest.TestCase):
    def setUp(self) -> None:
        _RESOLVE_CACHE.clear()      # 模块级缓存，测试之间必须隔离

    def _policy(self, **kw) -> ResolvePolicy:
        base = {"enabled": True, "max_links": 4, "delay_s": 0, "stop_after_fail": 2}
        base.update(kw)
        return ResolvePolicy.from_mapping(base)

    def test_replaces_relay_with_real_link(self) -> None:
        c = _cand()
        f = _FakeFetcher({c.url_raw: (200, _relay_page())})
        stats = resolve_candidates([c], f, {}, referer="https://weixin.sogou.com/weixin?q=x",
                                   timeout=5, policy=self._policy(), sleep=lambda _s: None)
        self.assertEqual(c.url_raw, _REAL)
        self.assertIs(c.extra["link_resolved"], True)
        self.assertEqual((stats.tried, stats.ok, stats.failed), (1, 1, 0))

    def test_keeps_relay_when_challenged(self) -> None:
        c = _cand()
        f = _FakeFetcher({c.url_raw: (200, _CHALLENGE)})
        stats = resolve_candidates([c], f, {}, referer="x", timeout=5,
                                   policy=self._policy(), sleep=lambda _s: None)
        self.assertIn("weixin.sogou.com/link", c.url_raw)     # 原链保留，条目没丢
        self.assertIs(c.extra["link_resolved"], False)
        self.assertEqual(stats.failed, 1)

    def test_keeps_relay_on_http_error(self) -> None:
        c = _cand()
        f = _FakeFetcher({})                                   # 一律 404
        resolve_candidates([c], f, {}, referer="x", timeout=5,
                           policy=self._policy(), sleep=lambda _s: None)
        self.assertIn("weixin.sogou.com/link", c.url_raw)

    def test_respects_max_links(self) -> None:
        cands = [_cand(f"https://weixin.sogou.com/link?url=FAKE{i}") for i in range(5)]
        f = _FakeFetcher({c.url_raw: (200, _relay_page()) for c in cands})
        stats = resolve_candidates(cands, f, {}, referer="x", timeout=5,
                                   policy=self._policy(max_links=2),
                                   sleep=lambda _s: None)
        self.assertEqual(stats.ok, 2)
        self.assertEqual(len(f.calls), 2)
        self.assertIn("上限", stats.stopped)

    def test_stops_after_consecutive_failures(self) -> None:
        cands = [_cand(f"https://weixin.sogou.com/link?url=FAKE{i}") for i in range(5)]
        f = _FakeFetcher({})                                   # 全失败
        stats = resolve_candidates(cands, f, {}, referer="x", timeout=5,
                                   policy=self._policy(stop_after_fail=2),
                                   sleep=lambda _s: None)
        self.assertEqual(stats.failed, 2)
        self.assertEqual(len(f.calls), 2)                      # 第 3 条就不再发了
        self.assertIn("连续失败", stats.stopped)

    def test_skips_direct_links(self) -> None:
        c = _cand("https://mp.weixin.qq.com/s?src=11&signature=x")
        f = _FakeFetcher({})
        stats = resolve_candidates([c], f, {}, referer="x", timeout=5,
                                   policy=self._policy(), sleep=lambda _s: None)
        self.assertEqual(f.calls, [])
        self.assertEqual(stats.tried, 0)
        self.assertEqual(stats.note(), "")

    def test_disabled_makes_no_requests(self) -> None:
        c = _cand()
        f = _FakeFetcher({c.url_raw: (200, _relay_page())})
        resolve_candidates([c], f, {}, referer="x", timeout=5,
                           policy=ResolvePolicy(), sleep=lambda _s: None)
        self.assertEqual(f.calls, [])
        self.assertIn("weixin.sogou.com/link", c.url_raw)

    def test_reuses_resolution_across_variants(self) -> None:
        # 同一篇文章（业务键相同、但中转链 token 不同）在别的变体里再出现：
        # 不该再发一次请求 —— 搜狗的风控额度是最稀缺的资源
        a = _cand("https://weixin.sogou.com/link?url=VA")
        b = _cand("https://weixin.sogou.com/link?url=VB")
        a.dedupe_key = b.dedupe_key = "same-article"
        f = _FakeFetcher({a.url_raw: (200, _relay_page())})

        first = resolve_candidates([a], f, {}, referer="x", timeout=5,
                                   policy=self._policy(), sleep=lambda _s: None)
        self.assertEqual(first.ok, 1)

        second = resolve_candidates([b], f, {}, referer="x", timeout=5,
                                    policy=self._policy(), sleep=lambda _s: None)
        self.assertEqual(len(f.calls), 1)          # 第二次没有真的发请求
        self.assertEqual(b.url_raw, _REAL)         # 但链接照样换成永久地址
        self.assertIs(b.extra["link_resolved"], True)
        self.assertEqual(second.reused, 1)
        self.assertIn("复用 1", second.note())

    def test_failures_are_not_cached(self) -> None:
        # 失败不缓存：换个时间同一篇可能就解得出，一次失败不能永久拉黑
        a = _cand("https://weixin.sogou.com/link?url=VA")
        a.dedupe_key = "k-fail"
        f = _FakeFetcher({})
        resolve_candidates([a], f, {}, referer="x", timeout=5,
                           policy=self._policy(), sleep=lambda _s: None)
        self.assertEqual(len(f.calls), 1)

        b = _cand("https://weixin.sogou.com/link?url=VB")
        b.dedupe_key = "k-fail"
        f2 = _FakeFetcher({b.url_raw: (200, _relay_page())})
        stats = resolve_candidates([b], f2, {}, referer="x", timeout=5,
                                   policy=self._policy(), sleep=lambda _s: None)
        self.assertEqual(stats.ok, 1)
        self.assertEqual(b.url_raw, _REAL)


class TestChannelResolvesEndToEnd(unittest.TestCase):
    """通道整链：SERP → 解链 → 候选带着永久链接返回。"""

    def setUp(self) -> None:
        _RESOLVE_CACHE.clear()

    def _ctx(self, fetcher, *, resolve: dict | None = None) -> ChannelContext:
        ch = ChannelConfig(
            id="sogou_weixin", name="搜狗微信", type="site_search",
            parser="sogou_weixin",
            url_template="https://weixin.sogou.com/weixin?type=2&page={offset}&query={q}",
            headers={"Referer": "https://weixin.sogou.com/"},
            paging=PagingConfig(mode="offset", start=1, step=1, max_pages=1),
            throttle_min_interval_s=0,
            resolve=resolve if resolve is not None else {
                "enabled": True, "max_links": 4, "delay_s": 0, "stop_after_fail": 2},
        )
        return ChannelContext(channel=ch, defaults={}, extract={},
                              fetcher=fetcher, policy=RetryPolicy(), timeout=5)

    def test_candidates_carry_permanent_links(self) -> None:
        serp_url = ("https://weixin.sogou.com/weixin?type=2&page=1&query="
                    "%E7%BA%A2%E9%BB%91%E6%A0%91")
        relay0 = "https://weixin.sogou.com/link?url=FAKE0&type=2&query=test"
        relay1 = "https://weixin.sogou.com/link?url=FAKE1&type=2&query=test"
        f = _FakeFetcher({
            serp_url: (200, _PAGE),
            relay0: (200, _relay_page(_REAL)),
            relay1: (200, _relay_page(_REAL + "2")),
        })
        res = get_channel("sogou_weixin")("红黑树", 1, self._ctx(f))
        self.assertEqual(len(res.candidates), 2)
        for c in res.candidates:
            self.assertTrue(c.url_raw.startswith("https://mp.weixin.qq.com/"))
            self.assertIs(c.extra["link_resolved"], True)
        # 业务键不因换链而变：跨跑批去重仍然有效
        self.assertEqual(len({c.dedupe_key for c in res.candidates}), 2)
        self.assertIn("解链 2/2", res.detail)

    def test_falls_back_to_relay_when_resolve_disabled(self) -> None:
        serp_url = ("https://weixin.sogou.com/weixin?type=2&page=1&query="
                    "%E7%BA%A2%E9%BB%91%E6%A0%91")
        f = _FakeFetcher({serp_url: (200, _PAGE)})
        res = get_channel("sogou_weixin")("红黑树", 1,
                                          self._ctx(f, resolve={"enabled": False}))
        self.assertEqual(len(f.calls), 1)                      # 只打了 SERP
        self.assertIn("weixin.sogou.com/link", res.candidates[0].url_raw)


if __name__ == "__main__":
    unittest.main()
