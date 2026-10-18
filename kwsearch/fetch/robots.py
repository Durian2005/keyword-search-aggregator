"""robots.txt 遵守（`crawl.robots_obey: true` 的实际落地）。

为什么自己做而不用 `RobotFileParser.read()`：
    那个方法自己开连接，绕过了本项目的 fetcher（TLS 指纹、超时、重试策略全都不生效），
    而且失败时只返回 None，没法区分"404 就是没有规则"和"500 表示暂时不可用"——
    这两种情况该做的决定完全相反。这里只借它的解析与匹配逻辑，抓取仍走采集层。

缓存粒度是 host（scheme + netloc），一个任务里每个 host 只拉一次 robots.txt。

RFC 9309 的失败语义（保守方向明确）：
    4xx / 200 空内容  → 视为没有限制，允许全部（404 是最常见的正常情况）
    5xx / 网络错误     → 视为暂时不可用，保守禁止
    ⚠️ 这里刻意选"宁可少抓"：robots 拿不到就硬抓，是最容易把项目性质搞坏的一步。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from kwsearch.fetch import BaseFetcher, FetchResult, RetryPolicy

logger = logging.getLogger(__name__)

_ROBOTS_PATH = "/robots.txt"


@dataclass(slots=True)
class RobotsVerdict:
    """一次 robots 判定的结论。reason 只用于展示，不参与控制流。"""

    allowed: bool
    reason: str                  # allow | disallow | missing | unavailable | skipped
    crawl_delay: float | None = None
    robots_url: str = ""


@dataclass(slots=True)
class _Entry:
    """一个 host 的 robots 状态。rp=None 表示"没有可用规则"。"""

    rp: RobotFileParser | None = None
    deny_all: bool = False
    note: str = ""
    robots_url: str = ""
    crawl_delay_hint: float | None = None


def host_key(url: str) -> str:
    """scheme://netloc —— robots.txt 的作用域是 origin，不是整个域名。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class RobotsCache:
    """按 host 缓存 robots.txt。一个实例对应一次任务，不要跨任务复用。"""

    def __init__(
        self,
        fetcher: BaseFetcher,
        user_agent: str,
        *,
        timeout: int = 10,
        policy: RetryPolicy | None = None,
        fetch: Callable[[str], FetchResult] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._ua = user_agent or "*"
        self._timeout = timeout
        # robots.txt 拿不到就跳过该站，重试 3 次只是把失败拖长，这里只给 1 次
        self._policy = policy or RetryPolicy(max_attempts=1)
        self._cache: dict[str, _Entry] = {}
        # 允许注入抓取函数，测试时不必碰网络
        self._fetch = fetch
        # 统计口径刻意分成两维：
        #   站点维（hosts / with_rules / no_rules / unreachable）—— 回答"碰到几个站、它们什么态度"
        #   请求维（denied）—— 回答"因此少抓了几条"
        # 混在一起会出现"允许 6"这种看不出是 6 个站还是 6 次请求的数字。
        self.stats: dict[str, int] = {
            "hosts": 0, "with_rules": 0, "no_rules": 0, "unreachable": 0, "denied": 0,
        }

    # ---- 公开入口 ---------------------------------------------------------
    def check(self, url: str) -> RobotsVerdict:
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return RobotsVerdict(True, "skipped")

        key = host_key(url)
        if not key:
            return RobotsVerdict(True, "skipped")

        entry = self._cache.get(key)
        if entry is None:
            entry = self._load(key)
            self._cache[key] = entry
            self._tally(entry)

        if entry.deny_all:
            self.stats["denied"] += 1
            return RobotsVerdict(False, "unavailable", entry.crawl_delay_hint, entry.robots_url)

        if entry.rp is None:
            return RobotsVerdict(True, "missing", None, entry.robots_url)

        allowed = entry.rp.can_fetch(self._ua, url)
        delay = entry.rp.crawl_delay(self._ua)
        if not allowed:
            self.stats["denied"] += 1
        return RobotsVerdict(
            allowed, "allow" if allowed else "disallow",
            float(delay) if delay else entry.crawl_delay_hint,
            entry.robots_url,
        )

    # ---- 内部 -------------------------------------------------------------
    def _tally(self, entry: _Entry) -> None:
        """只在某个 host 首次加载时记一次，后续命中缓存不重复计数。"""
        self.stats["hosts"] += 1
        if entry.deny_all:
            self.stats["unreachable"] += 1
        elif entry.rp is None:
            self.stats["no_rules"] += 1
        else:
            self.stats["with_rules"] += 1
    def _load(self, key: str) -> _Entry:
        robots_url = key + _ROBOTS_PATH
        try:
            res = self._do_fetch(robots_url)
        except Exception as e:  # 网络错误 / 超时 / TLS 失败
            logger.warning("robots 不可达（%s）：%s → 保守禁止", robots_url, e)
            return _Entry(deny_all=True, note=f"fetch error: {e}", robots_url=robots_url)

        if 400 <= res.status < 500:
            return _Entry(note=f"HTTP {res.status}", robots_url=robots_url)
        if res.status >= 500:
            logger.warning("robots 返回 %d（%s）→ 保守禁止", res.status, robots_url)
            return _Entry(deny_all=True, note=f"HTTP {res.status}", robots_url=robots_url)
        if res.status != 200:
            return _Entry(deny_all=True, note=f"HTTP {res.status}", robots_url=robots_url)

        body = (res.body or "").strip()
        if not body:
            return _Entry(note="empty", robots_url=robots_url)

        rp = RobotFileParser()
        rp.parse(body.splitlines())
        return _Entry(rp=rp, note="parsed", robots_url=robots_url)

    def _do_fetch(self, robots_url: str) -> FetchResult:
        if self._fetch is not None:
            return self._fetch(robots_url)
        # 不用 fetch_with_retry：它会把 403 抛成异常，而 403 在这里只意味着"没有规则"
        return self._fetcher.fetch(
            robots_url,
            {"User-Agent": self._ua, "Accept": "text/plain,*/*;q=0.8"},
            self._timeout,
        )


__all__ = ["RobotsCache", "RobotsVerdict", "host_key"]
