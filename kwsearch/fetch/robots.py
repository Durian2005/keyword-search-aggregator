"""robots.txt 遵守与绕过（`crawl.robots_mode` 的实际落地）。

档位（2026-10-22 用户拍板的全案，替代早前"必须遵守"的单一口径）：
    obey   （默认）按 RFC 9309 遵守 Disallow / Crawl-delay
    record 查 robots 但**不拦**：Disallow 的照抓，verdict.forced=True 留痕
           （报告里如实列"无视规则抓了 N 条"，这是审计档，不是隐形档）
    off    完全不拉 robots.txt（连请求都不发）

失败语义单独一档（`crawl.robots_on_failure`）：
    deny   （默认）robots.txt 5xx/网络错误 → 保守禁止
    allow  同上情形放行，但 forced=True 留痕
    ⚠️ "网络错误"先重试再下结论：2026-10-21 实测，任务进行中单次 robots.txt
    抓取会间歇性超时，而一次失败就把整个 host 禁掉整轮任务（目标机构的正文、
    列表页解析全部连坐丢失）。robots.txt 是几百字节的小文件，重试成本极低。

为什么自己做而不用 `RobotFileParser.read()`：
    那个方法自己开连接，绕过了本项目的 fetcher（TLS 指纹、超时、重试策略全都不生效），
    而且失败时只返回 None，没法区分"404 就是没有规则"和"500 表示暂时不可用"——
    这两种情况该做的决定完全相反。这里只借它的解析与匹配逻辑，抓取仍走采集层。

缓存粒度是 host（scheme + netloc），一个任务里每个 host 只拉一次 robots.txt。

Request-rate 指令（借 Scrapling 的 robots 合规模块设计）：
    `Request-rate: 1/5s` 这类"单位时间请求数"指引在 RobotFileParser 里不被解析，
    这里补上：换算成最小间隔并入 crawl_delay_hint，限速层照旧生效 ——
    绕过档只改"允不允许发"，**不改"发多快"**。

红线（2026-10-22 用户修订，替代 2026-10-21 口径）：
    项目唯一红线 = **不抓政府网站**（后缀硬拦截，见 sanitize.is_blocked_host）。
    robots 的 record/off 档是显式能力，不是漏洞；用哪一档由使用场景决定。
    留痕纪律不变：forced 的每一笔都要能进报告，不做隐形绕过。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from kwsearch.fetch import BaseFetcher, FetchResult, RetryPolicy

logger = logging.getLogger(__name__)

_ROBOTS_PATH = "/robots.txt"

#: robots 遵守档位。off 时调用方根本不该发 robots.txt 请求。
ROBOTS_MODES = ("obey", "record", "off")

#: robots.txt 拿不到时的处理。
ON_FAILURE_MODES = ("deny", "allow")

# robots.txt 抓取的重试：只重试**异常**（超时/连接失败/TLS），
# HTTP 状态码（403/404/5xx）由 _load 按 RFC 语义处理，不重试。
_ROBOTS_ATTEMPTS = 3
_ROBOTS_BACKOFF_S = (0.5, 1.0)

# Request-rate: 1/5s | 2/10m | 1/1h —— 数量/时间窗[单位]，单位缺省按秒
_RATE_RE = re.compile(r"^(\d+)\s*/\s*(\d+)\s*([smhd]?)$", re.I)
_RATE_UNIT_S = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


@dataclass(slots=True)
class RobotsVerdict:
    """一次 robots 判定的结论。reason 只用于展示，不参与控制流。

    forced=True 表示"按规则本该拒绝，但档位/失败语义选择了放行"——
    这一格是报告里"违规抓取清单"的唯一依据，绝不能省。
    """

    allowed: bool
    reason: str                  # allow | disallow | missing | unavailable | skipped
    crawl_delay: float | None = None
    robots_url: str = ""
    forced: bool = False


@dataclass(slots=True)
class _Entry:
    """一个 host 的 robots 状态。rp=None 表示"没有可用规则"。"""

    rp: RobotFileParser | None = None
    deny_all: bool = False
    note: str = ""
    robots_url: str = ""
    crawl_delay_hint: float | None = None
    # 请求维计数落到站点维，报告的"覆盖清单"才能逐域列出
    denied: int = 0
    forced: int = 0


def host_key(url: str) -> str:
    """scheme://netloc —— robots.txt 的作用域是 origin，不是整个域名。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _request_rate_delay(lines: list[str], ua: str) -> float | None:
    """解析 Request-rate 指令，换算成"每请求最小间隔秒数"。

    组语义与 robots.txt 一致：连续的 User-agent 行共享后续规则，
    规则行之后再出现 User-agent 行则另起一组。优先精确匹配本 UA，其次 `*`。
    """
    want = (ua or "*").strip().lower()
    current: list[str] = []
    saw_rule = False
    per_agent: dict[str, float] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name, value = field_name.strip().lower(), value.strip()
        if field_name == "user-agent":
            if saw_rule:
                current = []
                saw_rule = False
            if value:
                current.append(value.lower())
            continue
        saw_rule = True
        if field_name != "request-rate" or not current:
            continue
        m = _RATE_RE.match(value.replace(" ", ""))
        if not m:
            continue
        count, window, unit = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if count <= 0 or window <= 0:
            continue
        gap = (window * _RATE_UNIT_S.get(unit, 1.0)) / count
        for agent in current:
            # 同组多条取最保守（最大间隔）
            per_agent[agent] = max(per_agent.get(agent, 0.0), gap)
    if want in per_agent:
        return per_agent[want]
    return per_agent.get("*")


class RobotsCache:
    """按 host 缓存 robots.txt。一个实例对应一次任务，不要跨任务复用。

    mode / on_failure 决定"查完之后放不放行"；查询本身永远照做
    （off 档由调用方跳过整个缓存，不发 robots.txt 请求）。
    """

    def __init__(
        self,
        fetcher: BaseFetcher,
        user_agent: str,
        *,
        timeout: int = 10,
        policy: RetryPolicy | None = None,
        fetch: Callable[[str], FetchResult] | None = None,
        mode: str = "obey",
        on_failure: str = "deny",
    ) -> None:
        if mode not in ROBOTS_MODES:
            raise ValueError(f"robots_mode 必须是 {ROBOTS_MODES} 之一，收到 {mode!r}")
        if on_failure not in ON_FAILURE_MODES:
            raise ValueError(
                f"robots_on_failure 必须是 {ON_FAILURE_MODES} 之一，收到 {on_failure!r}")
        self._fetcher = fetcher
        self._ua = user_agent or "*"
        self._timeout = timeout
        # robots.txt 拿不到就跳过该站，重试 3 次只是把失败拖长，这里只给 1 次
        self._policy = policy or RetryPolicy(max_attempts=1)
        self._cache: dict[str, _Entry] = {}
        # 允许注入抓取函数，测试时不必碰网络
        self._fetch = fetch
        self._mode = mode
        self._on_failure = on_failure
        # 统计口径刻意分成两维：
        #   站点维（hosts / with_rules / no_rules / unreachable）—— 回答"碰到几个站、它们什么态度"
        #   请求维（denied / forced）—— 回答"因此少抓了几条 / 无视规则抓了几条"
        # 混在一起会出现"允许 6"这种看不出是 6 个站还是 6 次请求的数字。
        self.stats: dict[str, int] = {
            "hosts": 0, "with_rules": 0, "no_rules": 0, "unreachable": 0,
            "denied": 0, "forced": 0,
        }

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def on_failure(self) -> str:
        return self._on_failure

    # ---- 公开入口 ---------------------------------------------------------
    def check(self, url: str) -> RobotsVerdict:
        if self._mode == "off":
            return RobotsVerdict(True, "skipped")
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
            if self._on_failure == "allow":
                entry.forced += 1
                self.stats["forced"] += 1
                return RobotsVerdict(True, "unavailable", entry.crawl_delay_hint,
                                     entry.robots_url, forced=True)
            entry.denied += 1
            self.stats["denied"] += 1
            return RobotsVerdict(False, "unavailable", entry.crawl_delay_hint,
                                 entry.robots_url)

        if entry.rp is None:
            return RobotsVerdict(True, "missing", None, entry.robots_url)

        allowed = entry.rp.can_fetch(self._ua, url)
        delay = entry.rp.crawl_delay(self._ua)
        gap = float(delay) if delay else entry.crawl_delay_hint
        if allowed:
            return RobotsVerdict(True, "allow", gap, entry.robots_url)
        if self._mode == "record":
            entry.forced += 1
            self.stats["forced"] += 1
            return RobotsVerdict(True, "disallow", gap, entry.robots_url, forced=True)
        entry.denied += 1
        self.stats["denied"] += 1
        return RobotsVerdict(False, "disallow", gap, entry.robots_url)

    def host_records(self) -> list[dict[str, Any]]:
        """逐域结论（报告的「robots 覆盖清单」数据源）。按域名排序，输出稳定。"""
        out: list[dict[str, Any]] = []
        for key in sorted(self._cache):
            e = self._cache[key]
            out.append({
                "host": key,
                "status": e.note,
                "robots_url": e.robots_url,
                "crawl_delay": e.crawl_delay_hint,
                "denied": e.denied,
                "forced": e.forced,
            })
        return out

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
            res = self._fetch_robots(robots_url)
        except Exception as e:  # 网络错误 / 超时 / TLS 失败（已重试过）
            logger.warning("robots 不可达（%s）：%s → %s", robots_url, e, self._fail_label())
            return _Entry(deny_all=True, note=f"fetch error: {e}", robots_url=robots_url)

        if 400 <= res.status < 500:
            return _Entry(note=f"HTTP {res.status}", robots_url=robots_url)
        if res.status >= 500:
            logger.warning("robots 返回 %d（%s）→ %s", res.status, robots_url,
                           self._fail_label())
            return _Entry(deny_all=True, note=f"HTTP {res.status}", robots_url=robots_url)
        if res.status != 200:
            return _Entry(deny_all=True, note=f"HTTP {res.status}", robots_url=robots_url)

        body = (res.body or "").strip()
        if not body:
            return _Entry(note="empty", robots_url=robots_url)

        rp = RobotFileParser()
        lines = body.splitlines()
        rp.parse(lines)
        # Request-rate 换算成最小间隔，只有 Crawl-delay 缺席时才顶上：
        # Crawl-delay 是明确声明，优先级更高
        hint = _request_rate_delay(lines, self._ua)
        return _Entry(rp=rp, note="parsed", robots_url=robots_url,
                      crawl_delay_hint=hint)

    def _fail_label(self) -> str:
        return "放行（robots_on_failure=allow，留痕）" if self._on_failure == "allow" \
            else "保守禁止"

    def _fetch_robots(self, robots_url: str) -> FetchResult:
        """带重试的 robots.txt 抓取。只重试**异常**；HTTP 状态码不重试
        （403/404 在 _load 里各有语义，重试它们没有意义）。"""
        last: Exception | None = None
        for attempt in range(_ROBOTS_ATTEMPTS):
            try:
                return self._do_fetch(robots_url)
            except Exception as e:  # noqa: BLE001 —— 网络层错误类型太杂，统一兜住重试
                last = e
                if attempt < _ROBOTS_ATTEMPTS - 1:
                    time.sleep(_ROBOTS_BACKOFF_S[attempt])
        raise last  # type: ignore[misc]

    def _do_fetch(self, robots_url: str) -> FetchResult:
        if self._fetch is not None:
            return self._fetch(robots_url)
        # 不用 fetch_with_retry：它会把 403 抛成异常，而 403 在这里只意味着"没有规则"
        return self._fetcher.fetch(
            robots_url,
            {"User-Agent": self._ua, "Accept": "text/plain,*/*;q=0.8"},
            self._timeout,
        )


def resolve_robots_mode(crawl: dict[str, Any] | None,
                        override: str | None = None) -> str:
    """最终生效的 robots 档位。优先级：显式覆盖 > robots_mode > robots_obey > obey。

    `robots_obey: false` 是历史配置写法，等价于 mode=off，继续吃。
    """
    if override is not None:
        if override not in ROBOTS_MODES:
            raise ValueError(f"robots_mode 必须是 {ROBOTS_MODES} 之一，收到 {override!r}")
        return override
    crawl = crawl or {}
    mode = crawl.get("robots_mode")
    if mode is not None:
        if mode not in ROBOTS_MODES:
            raise ValueError(f"crawl.robots_mode 必须是 {ROBOTS_MODES} 之一，收到 {mode!r}")
        return str(mode)
    return "obey" if crawl.get("robots_obey", True) else "off"


def resolve_on_failure(crawl: dict[str, Any] | None,
                       override: str | None = None) -> str:
    """最终生效的失败语义。优先级：显式覆盖 > 配置 > deny。"""
    if override is not None:
        if override not in ON_FAILURE_MODES:
            raise ValueError(
                f"robots_on_failure 必须是 {ON_FAILURE_MODES} 之一，收到 {override!r}")
        return override
    value = (crawl or {}).get("robots_on_failure", "deny")
    if value not in ON_FAILURE_MODES:
        raise ValueError(
            f"crawl.robots_on_failure 必须是 {ON_FAILURE_MODES} 之一，收到 {value!r}")
    return str(value)


__all__ = [
    "ON_FAILURE_MODES",
    "ROBOTS_MODES",
    "RobotsCache",
    "RobotsVerdict",
    "host_key",
    "resolve_on_failure",
    "resolve_robots_mode",
]
