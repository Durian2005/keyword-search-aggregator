"""S3a 抓取层的测试。

原则：**测试里一个真实请求都不发**。
    页面体检（challenge / 编码 / 空壳）是纯函数，直接造字符串；
    抓取流程用 FakeFetcher + 注入的 robots 抓取函数，时间逻辑用注入的 clock。
    只有这样才能离线复现"GBK 站""验证码页""robots 500"这些线上很难碰到的分支。
"""
from __future__ import annotations

import unittest

from kwsearch.config import load_config
from kwsearch.fetch import FetchError, FetchResult, RetryPolicy
from kwsearch.fetch.encoding import decode_html, garble_ratio, normalize_encoding
from kwsearch.fetch.page import (
    F_BLOCKED,
    F_CHALLENGE,
    F_EMPTY,
    F_GARBLED,
    F_NON_HTML,
    F_ROBOTS,
    F_TOO_LARGE,
    HostThrottle,
    PagePolicy,
    classify_content_type,
    dedupe,
    fetch_page,
    fetch_pages,
    is_challenge,
    looks_like_js_shell,
    page_text,
    page_title,
)
from kwsearch.fetch.robots import RobotsCache, host_key


# ---------------------------------------------------------------------------
#  测试替身
# ---------------------------------------------------------------------------
class FakeFetcher:
    """按 URL 返回预先准备好的结果；值是 FetchResult 或要抛的异常。"""

    name = "fake"

    def __init__(self, mapping: dict[str, FetchResult | Exception]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20) -> FetchResult:
        self.calls.append(url)
        item = self.mapping.get(url)
        if item is None:
            raise FetchError(f"未准备 {url}", kind="conn")
        if isinstance(item, Exception):
            raise item
        return item


def _res(body: str, *, status: int = 200, ct: str = "text/html; charset=utf-8",
         raw: bytes | None = None) -> FetchResult:
    data = raw if raw is not None else body.encode("utf-8")
    return FetchResult(
        status=status, body=body, content_type=ct, final_url="",
        content_bytes=len(data), raw=data,
    )


def _policy(**kw) -> PagePolicy:
    base = dict(
        timeout=5, per_domain_qps=1000, max_per_domain=99,
        min_body_chars=200, challenge_keywords=("安全验证", "请输入验证码"),
        obey_robots=False, retry=RetryPolicy(max_attempts=1),
    )
    base.update(kw)
    return PagePolicy(**base)


_LONG = "<p>" + "这是一段足够长的正文内容，用来模拟真实文章的可读文本。" * 20 + "</p>"


# ---------------------------------------------------------------------------
#  纯函数
# ---------------------------------------------------------------------------
class TestContentType(unittest.TestCase):
    def test_common(self) -> None:
        self.assertEqual(classify_content_type("text/html; charset=utf-8"), "html")
        self.assertEqual(classify_content_type("application/xhtml+xml"), "html")
        self.assertEqual(classify_content_type("application/pdf"), "pdf")
        self.assertEqual(classify_content_type("image/png"), "image")
        self.assertEqual(classify_content_type("application/zip"), "other")

    def test_empty_is_unknown_not_rejected(self) -> None:
        # 不少老站不返回 Content-Type，一律拒收会把它们全部误杀
        self.assertEqual(classify_content_type(""), "unknown")


class TestPageText(unittest.TestCase):
    def test_skips_script_and_style(self) -> None:
        html = (
            "<html><head><title>标题在这</title><style>body{color:red}</style></head>"
            "<body><script>var a='一大坨不该被算作正文的代码';</script>"
            "<p>真正的正文</p></body></html>"
        )
        txt = page_text(html)
        self.assertIn("真正的正文", txt)
        self.assertIn("标题在这", txt)          # title 保留：验证码页的标题就是线索
        self.assertNotIn("不该被算作正文", txt)
        self.assertNotIn("color:red", txt)

    def test_empty(self) -> None:
        self.assertEqual(page_text(""), "")


class TestPageTitle(unittest.TestCase):
    def test_with_attrs_and_entities(self) -> None:
        self.assertEqual(page_title('<title lang="zh">A &amp; B</title>'), "A & B")

    def test_missing(self) -> None:
        self.assertEqual(page_title("<html><body>x</body></html>"), "")


class TestIsChallenge(unittest.TestCase):
    def test_short_page_with_keyword(self) -> None:
        html = "<html><head><title>安全验证</title></head><body>请稍候</body></html>"
        v = is_challenge(html, ("安全验证",), min_body_chars=200)
        self.assertTrue(v.is_challenge)
        self.assertEqual(v.reason, "text_too_short")
        self.assertEqual(v.keyword, "安全验证")

    def test_long_article_mentioning_captcha_is_not_challenge(self) -> None:
        # 讲爬虫的文章里完全可能写"请输入验证码"，但正文远长于阈值
        html = f"<html><body>{_LONG}<p>遇到请输入验证码时可以稍后再试。</p></body></html>"
        v = is_challenge(html, ("安全验证", "请输入验证码"), min_body_chars=200)
        self.assertFalse(v.is_challenge)

    def test_captcha_mark_on_short_page(self) -> None:
        html = '<html><body><form><input id="captcha_input" name="captcha"></form></body></html>'
        v = is_challenge(html, (), min_body_chars=200)
        self.assertTrue(v.is_challenge)
        self.assertEqual(v.reason, "captcha_mark_short")

    def test_cloudflare_mark(self) -> None:
        html = '<html><body><script>window.__cf_chl_opt={};</script></body></html>'
        v = is_challenge(html, (), min_body_chars=200)
        self.assertTrue(v.is_challenge)

    def test_normal_long_page(self) -> None:
        html = f"<html><body>{_LONG}</body></html>"
        self.assertFalse(is_challenge(html, ("安全验证",), min_body_chars=200).is_challenge)


class TestJsShell(unittest.TestCase):
    def test_ratio(self) -> None:
        self.assertTrue(looks_like_js_shell(100_000, 500, 0.3))
        self.assertFalse(looks_like_js_shell(10_000, 5_000, 0.3))

    def test_zero_bytes(self) -> None:
        self.assertFalse(looks_like_js_shell(0, 0, 0.3))


class TestDedupe(unittest.TestCase):
    def test_order_preserved(self) -> None:
        self.assertEqual(dedupe(["b", "a", "b", "", "  ", "a"]), ["b", "a"])


# ---------------------------------------------------------------------------
#  编码
# ---------------------------------------------------------------------------
class TestEncoding(unittest.TestCase):
    def test_gbk_by_meta(self) -> None:
        # 响应头没写 charset，只能靠 <meta> 救 —— 这是中文站最常见的情形
        raw = ('<html><head><meta charset="gbk"></head><body>中文标题</body></html>'
               ).encode("gbk")
        d = decode_html(raw, "text/html")
        self.assertTrue(d.ok)
        self.assertIn("中文标题", d.text)
        self.assertEqual(d.source, "meta")
        self.assertEqual(d.encoding, "gb18030")

    def test_header_wins_over_meta(self) -> None:
        raw = '<html><head><meta charset="gbk"></head><body>标题</body></html>'.encode("utf-8")
        d = decode_html(raw, "text/html; charset=utf-8")
        self.assertTrue(d.ok)
        self.assertIn("标题", d.text)
        self.assertEqual(d.source, "header")

    def test_utf8_without_declaration(self) -> None:
        raw = "<html><body>没有声明编码的中文</body></html>".encode("utf-8")
        d = decode_html(raw, "")
        self.assertTrue(d.ok)
        self.assertEqual(d.source, "utf-8")

    def test_gb18030_must_not_swallow_utf8(self) -> None:
        # utf-8 页面若能"成功"按 gb18030 解，会得到乱码却不报错 —— 顺序保证不会
        raw = "<html><body>红黑树</body></html>".encode("utf-8")
        d = decode_html(raw, "")
        self.assertIn("红黑树", d.text)

    def test_bom(self) -> None:
        raw = b"\xef\xbb\xbf" + "<p>带 BOM</p>".encode("utf-8")
        d = decode_html(raw, "")
        self.assertEqual(d.source, "bom")
        self.assertIn("带 BOM", d.text)

    def test_hopeless_bytes_marked_garbled(self) -> None:
        raw = bytes([0xFF, 0xFE, 0x00, 0x41, 0xFF])
        d = decode_html(raw, "", fallbacks=("utf-8",))
        self.assertFalse(d.ok)
        self.assertTrue(d.garbled)
        self.assertGreater(garble_ratio(d.text), 0)

    def test_alias(self) -> None:
        self.assertEqual(normalize_encoding("GB2312"), "gb18030")
        self.assertEqual(normalize_encoding("utf8"), "utf-8")
        self.assertIsNone(normalize_encoding("no-such-charset"))


# ---------------------------------------------------------------------------
#  限速
# ---------------------------------------------------------------------------
class TestHostThrottle(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def _th(self, qps: float = 1.0) -> HostThrottle:
        def clock() -> float:
            return self.now

        def sleep(s: float) -> None:
            self.slept.append(s)
            self.now += s

        return HostThrottle(qps, clock=clock, sleep=sleep)

    def test_first_call_does_not_wait(self) -> None:
        self.assertEqual(self._th().wait("a.com"), 0.0)

    def test_second_call_waits_remaining_gap(self) -> None:
        th = self._th(1.0)
        th.wait("a.com")
        self.now += 0.3
        waited = th.wait("a.com")
        self.assertAlmostEqual(waited, 0.7, places=3)

    def test_hosts_are_independent(self) -> None:
        th = self._th(1.0)
        th.wait("a.com")
        self.assertEqual(th.wait("b.com"), 0.0)

    def test_crawl_delay_wins_when_larger(self) -> None:
        th = self._th(10.0)          # 间隔 0.1s
        th.wait("a.com")
        waited = th.wait("a.com", crawl_delay=2.0)
        self.assertAlmostEqual(waited, 2.0, places=3)


# ---------------------------------------------------------------------------
#  robots
# ---------------------------------------------------------------------------
class TestRobots(unittest.TestCase):
    def _cache(self, mapping: dict[str, FetchResult | Exception]) -> RobotsCache:
        def fake_fetch(url: str) -> FetchResult:
            item = mapping[url]
            if isinstance(item, Exception):
                raise item
            return item

        return RobotsCache(None, "kwsearch-test", fetch=fake_fetch)  # type: ignore[arg-type]

    def test_disallow(self) -> None:
        rc = self._cache({"https://a.com/robots.txt":
                          _res("User-agent: *\nDisallow: /private\n", ct="text/plain")})
        self.assertFalse(rc.check("https://a.com/private/x").allowed)
        self.assertTrue(rc.check("https://a.com/public").allowed)
        # 站点维只计一次（同一个 host 不重复加载），请求维只计被拒的那一次
        self.assertEqual(rc.stats["hosts"], 1)
        self.assertEqual(rc.stats["with_rules"], 1)
        self.assertEqual(rc.stats["denied"], 1)

    def test_404_means_no_rules(self) -> None:
        rc = self._cache({"https://a.com/robots.txt": _res("", status=404)})
        v = rc.check("https://a.com/any")
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, "missing")

    def test_500_means_conservative_deny(self) -> None:
        rc = self._cache({"https://a.com/robots.txt": _res("", status=500)})
        self.assertFalse(rc.check("https://a.com/any").allowed)

    def test_network_error_denies(self) -> None:
        rc = self._cache({"https://a.com/robots.txt": FetchError("boom", kind="conn")})
        self.assertFalse(rc.check("https://a.com/any").allowed)

    def test_network_error_retried_then_allowed(self) -> None:
        # 2026-10-21：任务进行中的间歇性 hiccup 不能一棒子打死整个 host ——
        # 实测 www.xxx.edu.cn 的 robots.txt 平静时 133ms 返回 404，
        # 任务中却两次被判 unavailable，目标机构的正文和列表页解析连坐丢失。
        # 前两次超时、第三次成功 → 必须放行，且真的重试了 3 次。
        calls: list[str] = []

        def fake_fetch(url: str) -> FetchResult:
            calls.append(url)
            if len(calls) < 3:
                raise FetchError("timeout", kind="timeout")
            return _res("", status=404)

        rc = RobotsCache(None, "kwsearch-test", fetch=fake_fetch)  # type: ignore[arg-type]
        v = rc.check("https://a.com/any")
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, "missing")
        self.assertEqual(len(calls), 3)

    def test_persistent_network_error_still_denies(self) -> None:
        # 一直失败才保守禁止（宁可少抓的底线不退）
        calls: list[str] = []

        def fake_fetch(url: str) -> FetchResult:
            calls.append(url)
            raise FetchError("timeout", kind="timeout")

        rc = RobotsCache(None, "kwsearch-test", fetch=fake_fetch)  # type: ignore[arg-type]
        self.assertFalse(rc.check("https://a.com/any").allowed)
        self.assertEqual(len(calls), 3)        # 重试遍数有上限，不会无限打

    def test_http_status_not_retried(self) -> None:
        # 403/404 是"没有规则"的正常答案，重试它们没有意义
        calls: list[str] = []

        def fake_fetch(url: str) -> FetchResult:
            calls.append(url)
            return _res("", status=403)

        rc = RobotsCache(None, "kwsearch-test", fetch=fake_fetch)  # type: ignore[arg-type]
        self.assertTrue(rc.check("https://a.com/any").allowed)
        self.assertEqual(len(calls), 1)

    def test_crawl_delay(self) -> None:
        rc = self._cache({"https://a.com/robots.txt":
                          _res("User-agent: *\nCrawl-delay: 3\n", ct="text/plain")})
        self.assertEqual(rc.check("https://a.com/x").crawl_delay, 3.0)

    def test_non_http_scheme_skipped(self) -> None:
        rc = self._cache({})
        self.assertTrue(rc.check("ftp://a.com/x").allowed)

    def test_host_key(self) -> None:
        self.assertEqual(host_key("https://A.com/x/y"), "https://a.com")


# ---------------------------------------------------------------------------
#  单页抓取
# ---------------------------------------------------------------------------
class TestFetchPage(unittest.TestCase):
    def test_ok(self) -> None:
        f = FakeFetcher({"https://a.com/p": _res(f"<html><head><title>T</title></head>"
                                                 f"<body>{_LONG}</body></html>")})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy())
        self.assertTrue(p.ok, p.detail)
        self.assertIsNone(p.failure)
        self.assertEqual(p.title, "T")
        self.assertGreater(p.text_chars, 200)

    def test_403_is_blocked_and_not_retried(self) -> None:
        f = FakeFetcher({"https://a.com/p": _res("no", status=403)})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy())
        self.assertEqual(p.failure, F_BLOCKED)
        self.assertTrue(p.hard)
        self.assertEqual(len(f.calls), 1)

    def test_404(self) -> None:
        f = FakeFetcher({"https://a.com/p": _res("", status=404)})
        self.assertEqual(fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy()).failure,
                         "http_404")

    def test_pdf_rejected(self) -> None:
        f = FakeFetcher({"https://a.com/p.pdf": _res("%PDF-1.4", ct="application/pdf")})
        p = fetch_page("https://a.com/p.pdf", {}, fetcher=f, policy=_policy())
        self.assertEqual(p.failure, F_NON_HTML)
        self.assertEqual(p.category, "pdf")

    def test_too_large(self) -> None:
        big = "<html><body>" + "x" * 2000 + "</body></html>"
        f = FakeFetcher({"https://a.com/p": _res(big)})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy(max_page_bytes=500))
        self.assertEqual(p.failure, F_TOO_LARGE)

    def test_challenge(self) -> None:
        html = "<html><head><title>安全验证</title></head><body>请稍候</body></html>"
        f = FakeFetcher({"https://a.com/p": _res(html)})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy())
        self.assertEqual(p.failure, F_CHALLENGE)
        self.assertTrue(p.hard)

    def test_empty_shell(self) -> None:
        f = FakeFetcher({"https://a.com/p": _res("<html><body>页面建设中</body></html>")})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy())
        self.assertEqual(p.failure, F_EMPTY)
        self.assertFalse(p.hard)      # 没内容 ≠ 被挡

    def test_garbled(self) -> None:
        f = FakeFetcher({"https://a.com/p": _res("", raw=bytes([0xFF, 0xFE, 0x01]))})
        p = fetch_page("https://a.com/p", {}, fetcher=f,
                       policy=_policy(encoding_fallbacks=("utf-8",)))
        self.assertEqual(p.failure, F_GARBLED)

    def test_robots_denied_skips_request(self) -> None:
        from kwsearch.fetch.robots import RobotsVerdict
        f = FakeFetcher({})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy(),
                       robots=RobotsVerdict(False, "disallow"))
        self.assertEqual(p.failure, F_ROBOTS)
        self.assertEqual(f.calls, [])

    def test_gbk_page_decoded(self) -> None:
        raw = ('<html><head><meta charset="gbk"><title>中文标题</title></head>'
               f'<body>{_LONG}中文正文</body></html>').encode("gbk")
        f = FakeFetcher({"https://a.com/p": _res("", raw=raw, ct="text/html")})
        p = fetch_page("https://a.com/p", {}, fetcher=f, policy=_policy())
        self.assertTrue(p.ok)
        self.assertIn("中文标题", p.title)
        self.assertEqual(p.encoding, "gb18030")


# ---------------------------------------------------------------------------
#  批量
# ---------------------------------------------------------------------------
class TestFetchPages(unittest.TestCase):
    def test_counts_and_quota(self) -> None:
        html = f"<html><body>{_LONG}</body></html>"
        f = FakeFetcher({
            "https://a.com/1": _res(html),
            "https://a.com/2": _res(html),
            "https://a.com/3": _res(html),
            "https://b.com/1": _res(html),
        })
        urls = ["https://a.com/1", "https://a.com/2", "https://a.com/3", "https://b.com/1"]
        batch = fetch_pages(urls, _policy(max_per_domain=2), fetcher=f)
        self.assertEqual(batch.total, 3)      # a.com 的第 3 条被配额挡掉
        self.assertEqual(batch.skipped, 1)
        self.assertEqual(batch.ok_count, 3)
        self.assertAlmostEqual(batch.success_rate, 1.0)

    def test_limit(self) -> None:
        html = f"<html><body>{_LONG}</body></html>"
        f = FakeFetcher({f"https://a.com/{i}": _res(html) for i in range(5)})
        batch = fetch_pages([f"https://a.com/{i}" for i in range(5)],
                            _policy(limit=2), fetcher=f)
        self.assertEqual(batch.total, 2)

    def test_dedupes_before_fetch(self) -> None:
        html = f"<html><body>{_LONG}</body></html>"
        f = FakeFetcher({"https://a.com/1": _res(html)})
        batch = fetch_pages(["https://a.com/1", "https://a.com/1"], _policy(), fetcher=f)
        self.assertEqual(batch.total, 1)
        self.assertEqual(len(f.calls), 1)

    def test_failure_breakdown(self) -> None:
        f = FakeFetcher({
            "https://a.com/ok": _res(f"<html><body>{_LONG}</body></html>"),
            "https://a.com/403": _res("no", status=403),
            "https://b.com/pdf": _res("%PDF", ct="application/pdf"),
        })
        batch = fetch_pages(
            ["https://a.com/ok", "https://a.com/403", "https://b.com/pdf"],
            _policy(), fetcher=f,
        )
        self.assertEqual(batch.ok_count, 1)
        self.assertEqual(batch.failures(), {F_BLOCKED: 1, F_NON_HTML: 1})
        self.assertEqual(batch.hard_failures(), 1)

    def test_by_domain(self) -> None:
        html = f"<html><body>{_LONG}</body></html>"
        f = FakeFetcher({f"https://a.com/{i}": _res(html) for i in range(2)})
        f.mapping["https://b.com/1"] = _res(html)
        batch = fetch_pages(["https://a.com/1", "https://a.com/2", "https://b.com/1"],
                            _policy(), fetcher=f)
        self.assertEqual(batch.by_domain(), {"a.com": 2, "b.com": 1})

    def test_empty_input(self) -> None:
        batch = fetch_pages([], _policy(), fetcher=FakeFetcher({}))
        self.assertEqual(batch.total, 0)
        self.assertEqual(batch.success_rate, 0.0)


# ---------------------------------------------------------------------------
#  配置
# ---------------------------------------------------------------------------
class TestPolicyFromConfig(unittest.TestCase):
    def test_reads_crawl_and_extract(self) -> None:
        cfg = load_config()
        pol = PagePolicy.from_config(cfg)
        self.assertTrue(pol.obey_robots)                       # crawl.robots_obey: true
        self.assertEqual(pol.per_domain_qps, float(cfg.crawl.get("per_domain_qps", 1)))
        self.assertEqual(pol.max_per_domain, int(cfg.filter.get("max_per_domain", 30)))
        self.assertIn("gb18030", pol.encoding_fallbacks)        # 新增的配置项
        self.assertTrue(pol.challenge_keywords)

    def test_obey_robots_override(self) -> None:
        pol = PagePolicy.from_config(load_config(), obey_robots=False)
        self.assertFalse(pol.obey_robots)


if __name__ == "__main__":
    unittest.main()
