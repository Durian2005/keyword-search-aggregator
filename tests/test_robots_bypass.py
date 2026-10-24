"""robots 绕过档 + 政府站红线的测试（2026-10-22 全案）。

原则与其它测试一致：**一个真实请求都不发** —— robots 判定用注入的 fetch 函数，
政府站拦截看 FakeFetcher.calls 里根本没有那条 URL。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.config import load_config  # noqa: E402
from kwsearch.fetch import FetchError, FetchResult  # noqa: E402
from kwsearch.fetch.page import F_GOV, PagePolicy, fetch_pages  # noqa: E402
from kwsearch.fetch.robots import (  # noqa: E402
    RobotsCache,
    resolve_on_failure,
    resolve_robots_mode,
)
from kwsearch.sanitize import (  # noqa: E402
    DEFAULT_BLOCK_HOST_SUFFIXES,
    Sanitizer,
    is_blocked_host,
)


# ---------------------------------------------------------------------------
#  测试替身（与 test_page 同口径）
# ---------------------------------------------------------------------------
class FakeFetcher:
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


def _res(body: str, *, status: int = 200,
         ct: str = "text/html; charset=utf-8") -> FetchResult:
    data = body.encode("utf-8")
    return FetchResult(status=status, body=body, content_type=ct, final_url="",
                       content_bytes=len(data), raw=data)


_DISALLOW = "User-agent: *\nDisallow: /private\n"


def _cache(mapping: dict[str, FetchResult | Exception], **kw) -> RobotsCache:
    def fake_fetch(url: str) -> FetchResult:
        item = mapping[url]
        if isinstance(item, Exception):
            raise item
        return item

    return RobotsCache(None, "kwsearch-test", fetch=fake_fetch, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
#  三档模式
# ---------------------------------------------------------------------------
class TestRobotsModes(unittest.TestCase):
    def test_obey_default_denies_disallow(self) -> None:
        rc = _cache({"https://a.com/robots.txt": _res(_DISALLOW, ct="text/plain")})
        v = rc.check("https://a.com/private/x")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "disallow")
        self.assertFalse(v.forced)
        self.assertEqual(rc.stats["denied"], 1)

    def test_record_forces_but_marks(self) -> None:
        rc = _cache({"https://a.com/robots.txt": _res(_DISALLOW, ct="text/plain")},
                    mode="record")
        v = rc.check("https://a.com/private/x")
        self.assertTrue(v.allowed)          # 抓
        self.assertEqual(v.reason, "disallow")
        self.assertTrue(v.forced)           # 但留痕
        self.assertEqual(rc.stats["forced"], 1)
        self.assertEqual(rc.stats["denied"], 0)

    def test_off_skips_fetch_entirely(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str) -> FetchResult:
            calls.append(url)
            return _res(_DISALLOW, ct="text/plain")

        rc = RobotsCache(None, "kwsearch-test", fetch=fake_fetch, mode="off")  # type: ignore[arg-type]
        v = rc.check("https://a.com/private/x")
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, "skipped")
        self.assertEqual(calls, [])         # robots.txt 请求根本不发

    def test_on_failure_allow_forces(self) -> None:
        rc = _cache({"https://a.com/robots.txt": _res("", status=500)},
                    on_failure="allow")
        v = rc.check("https://a.com/any")
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, "unavailable")
        self.assertTrue(v.forced)
        self.assertEqual(rc.stats["forced"], 1)

    def test_on_failure_allow_network_error(self) -> None:
        rc = _cache({"https://a.com/robots.txt": FetchError("boom", kind="conn")},
                    on_failure="allow")
        v = rc.check("https://a.com/any")
        self.assertTrue(v.allowed)
        self.assertTrue(v.forced)

    def test_on_failure_deny_still_denies(self) -> None:
        rc = _cache({"https://a.com/robots.txt": _res("", status=500)})
        self.assertFalse(rc.check("https://a.com/any").allowed)

    def test_host_records_listed(self) -> None:
        rc = _cache({"https://a.com/robots.txt": _res(_DISALLOW, ct="text/plain")},
                    mode="record")
        rc.check("https://a.com/private/x")
        rows = rc.host_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "https://a.com")
        self.assertEqual(rows[0]["forced"], 1)
        self.assertEqual(rows[0]["denied"], 0)

    def test_bad_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _cache({}, mode="bypass")
        with self.assertRaises(ValueError):
            _cache({}, on_failure="maybe")


# ---------------------------------------------------------------------------
#  Request-rate（借 Scrapling 的合规模块设计）
# ---------------------------------------------------------------------------
class TestRequestRate(unittest.TestCase):
    def test_request_rate_becomes_delay(self) -> None:
        rc = _cache({"https://a.com/robots.txt":
                     _res("User-agent: *\nRequest-rate: 1/5s\n", ct="text/plain")})
        self.assertEqual(rc.check("https://a.com/x").crawl_delay, 5.0)

    def test_request_rate_count_per_window(self) -> None:
        rc = _cache({"https://a.com/robots.txt":
                     _res("User-agent: *\nRequest-rate: 2/10s\n", ct="text/plain")})
        self.assertEqual(rc.check("https://a.com/x").crawl_delay, 5.0)

    def test_crawl_delay_wins_over_request_rate(self) -> None:
        rc = _cache({"https://a.com/robots.txt":
                     _res("User-agent: *\nCrawl-delay: 3\nRequest-rate: 1/5s\n",
                          ct="text/plain")})
        self.assertEqual(rc.check("https://a.com/x").crawl_delay, 3.0)

    def test_request_rate_scoped_to_agent_group(self) -> None:
        body = "User-agent: googlebot\nRequest-rate: 1/9s\n\nUser-agent: *\nRequest-rate: 1/2s\n"
        rc = _cache({"https://a.com/robots.txt": _res(body, ct="text/plain")})
        self.assertEqual(rc.check("https://a.com/x").crawl_delay, 2.0)


# ---------------------------------------------------------------------------
#  档位解析优先级
# ---------------------------------------------------------------------------
class TestResolve(unittest.TestCase):
    def test_mode_priority(self) -> None:
        self.assertEqual(resolve_robots_mode({}), "obey")
        self.assertEqual(resolve_robots_mode({"robots_obey": False}), "off")
        self.assertEqual(resolve_robots_mode({"robots_mode": "record"}), "record")
        # 显式覆盖 > 配置
        self.assertEqual(resolve_robots_mode({"robots_mode": "record"}, "off"), "off")
        # robots_mode > 旧写法 robots_obey
        self.assertEqual(resolve_robots_mode(
            {"robots_mode": "record", "robots_obey": False}), "record")

    def test_on_failure_priority(self) -> None:
        self.assertEqual(resolve_on_failure({}), "deny")
        self.assertEqual(resolve_on_failure({"robots_on_failure": "allow"}), "allow")
        self.assertEqual(resolve_on_failure({"robots_on_failure": "allow"}, "deny"), "deny")

    def test_bad_values_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_robots_mode({}, "nope")
        with self.assertRaises(ValueError):
            resolve_robots_mode({"robots_mode": "nope"})
        with self.assertRaises(ValueError):
            resolve_on_failure({}, "nope")


# ---------------------------------------------------------------------------
#  政府站红线（唯一红线）
# ---------------------------------------------------------------------------
class TestGovRedLine(unittest.TestCase):
    def test_is_blocked_host(self) -> None:
        self.assertTrue(is_blocked_host("www.gov.cn"))
        self.assertTrue(is_blocked_host("moe.gov.cn"))
        self.assertTrue(is_blocked_host("gov.cn"))
        self.assertTrue(is_blocked_host("WWW.GOV.CN"))
        self.assertFalse(is_blocked_host("a.com"))
        self.assertFalse(is_blocked_host("notgov.cn"))
        self.assertFalse(is_blocked_host("gov.cn.example.com"))

    def test_default_suffixes(self) -> None:
        self.assertIn(".gov.cn", DEFAULT_BLOCK_HOST_SUFFIXES)

    def test_sanitizer_drops_gov(self) -> None:
        s = Sanitizer({})
        reason = s.check_url("https://www.gov.cn/xinwen.htm", "www.gov.cn")
        self.assertIsNotNone(reason)
        self.assertIn("政府网站", reason or "")

    def test_sanitizer_custom_suffixes(self) -> None:
        s = Sanitizer({"block_host_suffixes": [".mil.cn"]})
        self.assertIsNotNone(s.check_url("https://x.mil.cn/a", "x.mil.cn"))
        self.assertIsNone(s.check_url("https://www.gov.cn/a", "www.gov.cn"))

    def test_fetch_pages_never_requests_gov(self) -> None:
        f = FakeFetcher({
            "https://a.com/p": _res("<html><body>" + "正文" * 150 + "</body></html>"),
        })
        policy = PagePolicy(timeout=5, per_domain_qps=0, robots_mode="off")
        batch = fetch_pages(
            ["https://www.gov.cn/x", "https://a.com/p"], policy, fetcher=f)
        self.assertNotIn("https://www.gov.cn/x", f.calls)   # 请求根本没发
        gov = [p for p in batch.pages if p.failure == F_GOV]
        self.assertEqual(len(gov), 1)
        self.assertIn("红线", gov[0].detail)
        self.assertEqual(batch.ok_count, 1)                 # 普通站不受影响


# ---------------------------------------------------------------------------
#  PagePolicy 配置映射
# ---------------------------------------------------------------------------
class TestPolicyConfig(unittest.TestCase):
    def _cfg(self, crawl_yaml: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "c.yaml"
        p.write_text(
            "version: 1\n"
            "defaults:\n  timeout: 5\n"
            "channels:\n"
            "  - id: dummy\n    type: serp\n    parser: fake\n"
            "    url_template: \"https://fake.test/s?q={q}\"\n"
            "    paging: {mode: none}\n    throttle: {min_interval_s: 0}\n"
            "    enabled: false\n"
            f"crawl:\n{crawl_yaml}\n", encoding="utf-8")
        return load_config(p)

    def test_default_mode_from_real_config(self) -> None:
        pol = PagePolicy.from_config(load_config())
        self.assertEqual(pol.robots_mode, "obey")
        self.assertTrue(pol.obey_robots)

    def test_config_record_mode(self) -> None:
        pol = PagePolicy.from_config(self._cfg("  robots_mode: record\n"))
        self.assertEqual(pol.robots_mode, "record")

    def test_legacy_robots_obey_false_maps_to_off(self) -> None:
        pol = PagePolicy.from_config(self._cfg("  robots_obey: false\n"))
        self.assertEqual(pol.robots_mode, "off")
        self.assertFalse(pol.obey_robots)

    def test_override_beats_config(self) -> None:
        cfg = self._cfg("  robots_mode: record\n")
        pol = PagePolicy.from_config(cfg, robots_mode="obey")
        self.assertEqual(pol.robots_mode, "obey")
        # 旧口径 obey_robots=False 仍然等价 off
        pol2 = PagePolicy.from_config(cfg, obey_robots=False)
        self.assertEqual(pol2.robots_mode, "off")

    def test_on_failure_from_config(self) -> None:
        pol = PagePolicy.from_config(self._cfg("  robots_on_failure: allow\n"))
        self.assertEqual(pol.robots_on_failure, "allow")


# ---------------------------------------------------------------------------
#  报告留痕
# ---------------------------------------------------------------------------
class TestReportRobots(unittest.TestCase):
    def _data(self, stats: dict) -> dict:
        return {
            "task": {"task_id": "t1", "keyword": "红黑树", "mode": "quick",
                     "started_at": "", "status": "ok", "truncated": 0, "stats": stats},
            "totals": {"docs": 1, "docs_shown": 1, "domains": 1, "channels": 1,
                       "alerts": 1, "with_time": 1, "no_time": 0, "new_docs": 1,
                       "db_docs": 1},
            "channels": [], "docs": [], "domains": [], "timeline": [], "queries": [],
            "alerts": [{"id": 1, "channel": None, "kind": "robots_forced",
                        "detail": "1 条 URL 无视 robots.txt 规则强制抓取",
                        "created_at": ""}],
            "clusters": [], "person_cards": [],
        }

    def test_section_absent_when_obey_and_clean(self) -> None:
        from kwsearch.report import render_html
        html = render_html(self._data({"robots_mode": "obey", "robots_forced": 0,
                                       "gov_blocked": 0, "robots_hosts": []}))
        self.assertNotIn("<h2>robots 覆盖", html)

    def test_section_and_banner_when_forced(self) -> None:
        from kwsearch.report import render_html
        html = render_html(self._data({
            "robots_mode": "record", "robots_forced": 2, "gov_blocked": 1,
            "robots_hosts": [{"host": "https://weixin.sogou.com", "status": "parsed",
                              "denied": 0, "forced": 2, "crawl_delay": None}],
        }))
        self.assertIn("<h2>robots 覆盖", html)
        self.assertIn("robots 档位", html)          # 顶部 banner
        self.assertIn("weixin.sogou.com", html)
        self.assertIn("强制抓取", html)
        self.assertIn("政府站红线拦截 1 条", html)
        self.assertIn("无视 robots 规则强制抓取", html)   # 告警类型翻译

    def test_footer_states_gov_red_line(self) -> None:
        from kwsearch.report import render_html
        html = render_html(self._data({"robots_mode": "obey"}))
        self.assertIn("不抓政府网站", html)


# ---------------------------------------------------------------------------
#  交互页面输入
# ---------------------------------------------------------------------------
class TestRunPayload(unittest.TestCase):
    def test_defaults_are_conservative(self) -> None:
        from kwsearch.web.jobs import RunPayload
        p = RunPayload.from_mapping({"keyword": "红黑树"})
        self.assertEqual(p.robots_mode, "obey")
        self.assertEqual(p.robots_on_failure, "deny")
        self.assertFalse(p.stealth)

    def test_accepts_modes(self) -> None:
        from kwsearch.web.jobs import RunPayload
        p = RunPayload.from_mapping({"keyword": "红黑树", "robots_mode": "record",
                                     "robots_on_failure": "allow", "stealth": True})
        self.assertEqual(p.robots_mode, "record")
        self.assertEqual(p.robots_on_failure, "allow")
        self.assertTrue(p.stealth)

    def test_rejects_bad_modes(self) -> None:
        from kwsearch.web.jobs import RunPayload
        with self.assertRaises(ValueError):
            RunPayload.from_mapping({"keyword": "x", "robots_mode": "bypass"})
        with self.assertRaises(ValueError):
            RunPayload.from_mapping({"keyword": "x", "robots_on_failure": "yes"})


if __name__ == "__main__":
    unittest.main()
