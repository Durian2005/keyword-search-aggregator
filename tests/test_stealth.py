"""Scrapling 隐身档单测。**零真实请求**：scrapling 用假模块顶替，两种状态都覆盖：

    - 未安装（sys.modules 占位成 None → import 必炸）
    - 已安装（注入假 scrapling.fetchers，记录构造/调用参数）

覆盖点：配置解析、懒加载失败要大声、会话复用、批末关浏览器、
robots 走轻量档、build_fetcher 的显式 opt-in 不静默降级。
"""
from __future__ import annotations

import sys
import types
import unittest

from kwsearch.fetch import (
    FetchResult,
    RetryPolicy,
    build_fetcher,
)
from kwsearch.fetch.page import PagePolicy, fetch_pages
from kwsearch.fetch.stealth import (
    INSTALL_HINT,
    StealthConfig,
    StealthFetcher,
    StealthUnavailable,
)

# ---------------------------------------------------------------------------
# 假 scrapling
# ---------------------------------------------------------------------------
class _FakePage:
    """顶替 scrapling 的 Response：只实现适配器实际用到的属性。"""

    def __init__(self, url: str, *, status: int = 200,
                 body: bytes = b"<html><body><p>hello</p></body></html>",
                 headers: dict | None = None, url_attr: str | None = None):
        self.status = status
        self.body = body
        self.headers = headers if headers is not None else {}
        if url_attr is not None:      # 默认不给 url 属性，测 final_url 兜底
            self.url = url_attr


class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fetched: list[tuple[str, dict]] = []
        self.closed = 0

    def fetch(self, url: str, **kwargs):
        self.fetched.append((url, kwargs))
        return _FakePage(url)

    def close(self):
        self.closed += 1


def _install_fake_scrapling() -> list:
    """把假 scrapling 塞进 sys.modules，返回会话记录列表。"""
    sessions: list[_FakeSession] = []

    def _session(**kwargs):
        s = _FakeSession(**kwargs)
        sessions.append(s)
        return s

    fetchers = types.ModuleType("scrapling.fetchers")
    fetchers.StealthySession = _session
    fetchers.StealthyFetcher = types.SimpleNamespace(
        fetch=lambda url, **kw: _FakePage(url)
    )
    root = types.ModuleType("scrapling")
    root.fetchers = fetchers
    sys.modules["scrapling"] = root
    sys.modules["scrapling.fetchers"] = fetchers
    return sessions


def _block_scrapling():
    """让 `import scrapling` 必炸（sys.modules 占位 None 是官方行为）。"""
    for key in ("scrapling", "scrapling.fetchers"):
        sys.modules[key] = None  # type: ignore[assignment]


class _SysModulesGuard:
    """进测试前快照 sys.modules 的相关键，退出时原样还原。"""

    KEYS = ("scrapling", "scrapling.fetchers")

    def __enter__(self):
        self._saved = {k: sys.modules.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return False


# ---------------------------------------------------------------------------
class TestStealthConfig(unittest.TestCase):
    def test_defaults(self):
        c = StealthConfig()
        self.assertTrue(c.headless)
        self.assertTrue(c.solve_cloudflare)
        self.assertTrue(c.network_idle)
        self.assertFalse(c.disable_resources)
        self.assertFalse(c.humanize)
        self.assertFalse(c.use_camoufox)
        self.assertEqual(c.timeout_ms, 60_000)

    def test_from_mapping_reads_yaml_shape(self):
        c = StealthConfig.from_mapping({
            "enabled": True,               # 开关键，本类不认也不算错
            "headless": False,
            "solve_cloudflare": False,
            "disable_resources": True,
            "humanize": True,
            "use_camoufox": True,
            "timeout_ms": 120000,
            "unknown_future_key": 1,       # 未知键忽略
        })
        self.assertFalse(c.headless)
        self.assertFalse(c.solve_cloudflare)
        self.assertTrue(c.disable_resources)
        self.assertTrue(c.humanize)
        self.assertTrue(c.use_camoufox)
        self.assertEqual(c.timeout_ms, 120_000)

    def test_from_mapping_empty(self):
        self.assertEqual(StealthConfig.from_mapping(None), StealthConfig())
        self.assertEqual(StealthConfig.from_mapping({}), StealthConfig())

    def test_timeout_ms_none_when_null(self):
        c = StealthConfig.from_mapping({"timeout_ms": None})
        self.assertIsNone(c.timeout_ms)

    def test_session_kwargs_only_confirmed_keys(self):
        kw = StealthConfig().session_kwargs()
        self.assertEqual(kw, {
            "headless": True,
            "solve_cloudflare": True,
            "network_idle": True,
            "disable_resources": False,
        })
        # 增强项只在显式开启时才出现：默认不传 = 用引擎默认行为
        self.assertNotIn("humanize", kw)
        self.assertNotIn("use_camoufox", kw)

    def test_session_kwargs_optional_flags(self):
        kw = StealthConfig(humanize=True, use_camoufox=True).session_kwargs()
        self.assertTrue(kw["humanize"])
        self.assertTrue(kw["use_camoufox"])

    def test_fetch_kwargs_timeout_ms(self):
        self.assertEqual(StealthConfig(timeout_ms=90_000).fetch_kwargs(),
                         {"timeout": 90_000})
        # 配置权威：调用方的 timeout_s 不覆盖配置值
        # （浏览器导航不该被通用 HTTP 策略的 20s 压低）
        self.assertEqual(StealthConfig(timeout_ms=90_000).fetch_kwargs(5),
                         {"timeout": 90_000})
        # 配置显式为 None 时才用调用方秒数兜底，换算成毫秒
        self.assertEqual(StealthConfig(timeout_ms=None).fetch_kwargs(5),
                         {"timeout": 5000})
        # 没配超时也不传键，让引擎用默认
        self.assertEqual(StealthConfig(timeout_ms=None).fetch_kwargs(), {})


# ---------------------------------------------------------------------------
class TestStealthFetcherUnavailable(unittest.TestCase):
    """没装 scrapling 时：显式 opt-in 必须大声失败，带安装提示。"""

    def test_construct_without_scrapling_raises(self):
        with _SysModulesGuard(), self.assertRaises(StealthUnavailable) as ctx:
            _block_scrapling()
            StealthFetcher()
        self.assertIn(INSTALL_HINT, str(ctx.exception))
        self.assertIn("scrapling", str(ctx.exception))

    def test_build_fetcher_scrapling_not_silently_degraded(self):
        """prefer=scrapling 却装不上 → 上抛，不允许退回 curl_cffi。

        静默降级是最难发现的假象：日志显示 fetcher=curl_cffi，
        用户以为自己开着隐身档。
        """
        with _SysModulesGuard(), self.assertRaises(StealthUnavailable):
            _block_scrapling()
            build_fetcher("scrapling")

    def test_build_fetcher_auto_with_stealth_not_degraded(self):
        with _SysModulesGuard(), self.assertRaises(StealthUnavailable):
            _block_scrapling()
            build_fetcher("auto", stealth=True)

    def test_stealth_unavailable_is_runtime_error(self):
        # build_fetcher 的降级循环只捕 ImportError；RuntimeError 才会穿透出去
        self.assertTrue(issubclass(StealthUnavailable, RuntimeError))
        self.assertFalse(issubclass(StealthUnavailable, ImportError))


# ---------------------------------------------------------------------------
class TestStealthFetcher(unittest.TestCase):
    def setUp(self):
        self._guard = _SysModulesGuard()
        self._guard.__enter__()
        self.sessions = _install_fake_scrapling()

    def tearDown(self):
        self._guard.__exit__(None, None, None)

    def _make(self, config: StealthConfig | None = None) -> StealthFetcher:
        return StealthFetcher(config)

    def test_fetch_returns_fetch_result(self):
        sf = self._make()
        res = sf.fetch("https://example.edu.cn/a", {"User-Agent": "x"})
        self.assertIsInstance(res, FetchResult)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.body, "<html><body><p>hello</p></body></html>")
        self.assertEqual(res.raw, b"<html><body><p>hello</p></body></html>")
        self.assertEqual(res.fetcher, "scrapling_stealth")
        self.assertEqual(res.content_bytes, len(res.raw))
        self.assertEqual(res.final_url, "https://example.edu.cn/a")  # url 兜底
        self.assertGreaterEqual(res.elapsed_ms, 0)

    def test_fetch_content_type_from_headers(self):
        sf = self._make()
        session = self.sessions  # 第一次 fetch 才建会话
        # 换一个带响应头的假页面
        orig = _FakeSession.fetch

        def fetch(self_, url, **kw):
            return _FakePage(url, headers={"content-type": "text/html; charset=utf-8"})
        _FakeSession.fetch = fetch  # type: ignore[method-assign]
        try:
            res = sf.fetch("https://a.cn/", {})
        finally:
            _FakeSession.fetch = orig  # type: ignore[method-assign]
        self.assertEqual(res.content_type, "text/html; charset=utf-8")

    def test_content_type_fallback_html(self):
        sf = self._make()
        res = sf.fetch("https://a.cn/", {})   # 假页面默认无 content-type
        self.assertEqual(res.content_type, "text/html; charset=utf-8")

    def test_status_missing_becomes_zero(self):
        sf = self._make()
        session = sf._ensure_session()
        session.fetch = lambda url, **kw: types.SimpleNamespace(
            body=b"", headers={}, status=None)  # type: ignore[assignment]
        res = sf.fetch("https://a.cn/", {})
        self.assertEqual(res.status, 0)

    def test_session_reused_across_fetches(self):
        """整批复用一个浏览器会话 —— 一次性 fetch 是每次开一个浏览器。"""
        sf = self._make()
        sf.fetch("https://a.cn/1", {})
        sf.fetch("https://a.cn/2", {})
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual([u for u, _ in self.sessions[0].fetched],
                         ["https://a.cn/1", "https://a.cn/2"])
        self.assertEqual(sf.fetches, 2)

    def test_session_gets_config_kwargs(self):
        cfg = StealthConfig(headless=False, disable_resources=True,
                            humanize=True, use_camoufox=True)
        sf = self._make(cfg)
        sf.fetch("https://a.cn/", {})
        kw = self.sessions[0].kwargs
        self.assertFalse(kw["headless"])
        self.assertTrue(kw["disable_resources"])
        self.assertTrue(kw["humanize"])
        self.assertTrue(kw["use_camoufox"])

    def test_fetch_kwargs_passed_with_timeout(self):
        sf = self._make(StealthConfig(timeout_ms=45_000))
        sf.fetch("https://a.cn/", {}, timeout=12)
        self.assertEqual(self.sessions[0].fetched[0][1], {"timeout": 45_000})

    def test_close_closes_session_and_is_idempotent(self):
        sf = self._make()
        sf.fetch("https://a.cn/", {})
        sf.close()
        self.assertEqual(self.sessions[0].closed, 1)
        sf.close()                       # 幂等：不能炸，也不能二次 close
        self.assertEqual(self.sessions[0].closed, 1)
        # 关掉之后下次 fetch 会开新会话（懒重建）
        sf.fetch("https://a.cn/2", {})
        self.assertEqual(len(self.sessions), 2)

    def test_close_without_session_is_noop(self):
        self._make().close()             # 从未 fetch 过

    def test_close_swallows_session_error(self):
        sf = self._make()
        sf.fetch("https://a.cn/", {})
        self.sessions[0].close = lambda: (_ for _ in ()).throw(OSError("boom"))
        sf.close()                       # 收尾失败只警告不抛

    def test_robots_fetcher_is_lightweight(self):
        sf = self._make()
        rf = sf.robots_fetcher
        # 轻量档已装（curl_cffi/httpx）；两者都没装才退化成自己
        self.assertIn(rf.name, ("curl_cffi", "httpx", "scrapling_stealth"))
        self.assertIsNot(rf, sf)


# ---------------------------------------------------------------------------
class TestBuildFetcherStealth(unittest.TestCase):
    def setUp(self):
        self._guard = _SysModulesGuard()
        self._guard.__enter__()

    def tearDown(self):
        self._guard.__exit__(None, None, None)

    def test_auto_never_picks_scrapling_by_default(self):
        f = build_fetcher("auto")
        self.assertIn(f.name, ("curl_cffi", "httpx"))
        self.assertNotEqual(f.name, "scrapling_stealth")

    def test_stealth_true_uses_default_config(self):
        self.sessions = _install_fake_scrapling()
        f = build_fetcher("auto", stealth=True)
        self.assertIsInstance(f, StealthFetcher)
        self.assertTrue(f._cfg.solve_cloudflare)

    def test_stealth_dict_uses_yaml_shape(self):
        _install_fake_scrapling()
        f = build_fetcher("auto", stealth={"enabled": True, "headless": False,
                                           "timeout_ms": 3000})
        self.assertIsInstance(f, StealthFetcher)
        self.assertFalse(f._cfg.headless)
        self.assertEqual(f._cfg.timeout_ms, 3000)

    def test_stealth_config_instance(self):
        _install_fake_scrapling()
        cfg = StealthConfig(headless=False)
        f = build_fetcher("auto", stealth=cfg)
        self.assertIs(f._cfg, cfg)

    def test_stealth_false_ignored(self):
        _install_fake_scrapling()        # 装着也不用
        f = build_fetcher("auto", stealth=False)
        self.assertNotIsInstance(f, StealthFetcher)


# ---------------------------------------------------------------------------
class TestFetchPagesWithStealth(unittest.TestCase):
    """批量抓取与隐身档的两个交互：robots 走轻量档、批末关浏览器。"""

    POLICY = PagePolicy(timeout=5, per_domain_qps=0, robots_mode="off")

    def _html(self) -> str:
        return "<html><body><p>" + "正" * 300 + "</p></body></html>"

    def test_batch_closes_fetcher_with_close(self):
        closed = []

        class _Fake:
            name = "fake"

            def fetch(self, url, headers, timeout=20):
                return FetchResult(status=200, body=self_html,
                                   content_type="text/html", final_url=url,
                                   fetcher="fake", raw=self_html.encode("utf-8"))

            def close(self):
                closed.append(1)

        self_html = self._html()
        batch = fetch_pages(["https://a.cn/x"], self.POLICY, fetcher=_Fake(),
                            headers={"User-Agent": "x"})
        self.assertEqual(batch.ok_count, 1)
        self.assertEqual(closed, [1])

    def test_batch_tolerates_fetcher_without_close(self):
        """鸭子类型的假实现没有 close 也不能炸（单测注入的那些）。"""
        class _Fake:
            name = "fake"

            def fetch(self, url, headers, timeout=20):
                return FetchResult(status=200, body=self_html,
                                   content_type="text/html", final_url=url,
                                   fetcher="fake", raw=self_html.encode("utf-8"))

        self_html = self._html()
        batch = fetch_pages(["https://a.cn/x"], self.POLICY, fetcher=_Fake(),
                            headers={"User-Agent": "x"})
        self.assertEqual(batch.ok_count, 1)

    def test_robots_fetched_via_light_fetcher(self):
        """robots.txt 必须走 robots_fetcher，不启动浏览器。"""
        robots_calls = []

        class _RobotsFake:
            name = "light"

            def fetch(self, url, headers, timeout=20):
                robots_calls.append(url)
                return FetchResult(status=200,
                                   body="User-agent: *\nAllow: /\n",
                                   content_type="text/plain", final_url=url,
                                   fetcher="light",
                                   raw=b"User-agent: *\nAllow: /\n")

        class _HeavyFake:
            name = "scrapling_stealth"

            def fetch(self, url, headers, timeout=20):
                raise AssertionError("隐身档不该被用来抓 robots.txt")

            @property
            def robots_fetcher(self):
                return _RobotsFake()

        policy = PagePolicy(timeout=5, per_domain_qps=0, robots_mode="obey")
        batch = fetch_pages(["https://a.cn/x"], policy, fetcher=_HeavyFake(),
                            headers={"User-Agent": "x"})
        # robots 放行 + 正文抓取失败（HeavyFake 故意抛）→ 失败分类是 conn，
        # 但关键是 robots_calls 里有记录，且没碰到隐身档的 fetch
        self.assertTrue(robots_calls)
        self.assertTrue(robots_calls[0].endswith("robots.txt"))


if __name__ == "__main__":
    unittest.main()
