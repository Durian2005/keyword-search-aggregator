"""S3a · 正文页抓取层：URL → 一份"可以拿去抽正文"的 HTML。

与 S2 的区别（这条边界要一直守住）：
    发现层只认 SERP/API 给的条目，**从不**打开候选链接；
    这里第一次真的去打开那些链接，所以它要面对的是"各式各样的第三方站点"，
    而不是"一个结构稳定的搜索引擎结果页"。

因此这一层的职责是四项"能不能用"，而不是"抽得好不好"：
    1. 能不能打开      —— robots / 状态码 / 超时，失败分类要能解释
    2. 打开的是什么    —— HTML？PDF？图片？这些不该进抽取器
    3. 解出来是不是字  —— GBK 站按 meta charset 重解，乱码不入库
    4. 是人看的页吗    —— 验证码页/空壳页要单独标出来，绝不冒充"抓到了"

真正的正文容器抽取是 S3b（`extract/body.py`）。这里唯一的文本处理是
"整页文本量"，用来判 challenge 与疑似 JS 站 —— 它不参与摘要，也不入库。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence
from urllib.parse import urlsplit

from kwsearch.config import AppConfig
from kwsearch.fetch.encoding import decode_html
from kwsearch.fetch.robots import (
    RobotsCache,
    RobotsVerdict,
    resolve_on_failure,
    resolve_robots_mode,
)
from kwsearch.fetch import (
    BaseFetcher,
    FetchError,
    RetryPolicy,
    default_headers,
    fetch_with_retry,
)
from kwsearch.htmlx import normalize, parse_html, strip_tags
from kwsearch.sanitize import DEFAULT_BLOCK_HOST_SUFFIXES, is_blocked_host

logger = logging.getLogger(__name__)

# ---- 失败分类 --------------------------------------------------------------
# 这些字符串会进报告和统计表，所以是"给人看的结论"，不是内部枚举。
F_ROBOTS = "robots_disallow"
F_BLOCKED = "http_403"        # 401/403：明确不放行
F_NOT_FOUND = "http_404"
F_SERVER = "http_5xx"
F_HTTP = "http_other"
F_TIMEOUT = "timeout"
F_CONN = "conn"
F_CHALLENGE = "challenge"     # 验证码/风控页
F_NON_HTML = "non_html"       # PDF / 图片 / 压缩包
F_TOO_LARGE = "too_large"
F_GARBLED = "garbled"         # 解码后仍是替换字符
F_EMPTY = "empty"             # 打开了但几乎没有文字（空壳页 / 纯 JS 站）
F_GOV = "gov_blocked"         # 政府网站：项目唯一红线，请求根本不发

# 这些失败说明"这个站不想让我们看"，与"这篇文章其实没内容"性质不同。
HARD_FAILURES = frozenset({F_ROBOTS, F_BLOCKED, F_CHALLENGE, F_TIMEOUT, F_CONN, F_SERVER})

# 算"正文量"时要整个跳过的标签：script 里的 JS 代码会被 HTMLParser 当文本吐出来，
# 一个验证码页往往有一大坨 JS，不跳过的话文本量能轻松超过 200 字，
# challenge 的"文本短"判据就彻底失效了。
_SKIP_TEXT_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "canvas"})

_HTML_LIKE = ("text/html", "application/xhtml+xml", "application/xml", "text/xml")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)

# 这些属性名/属性值组合出现在页面上，基本只有一个含义：这是一个验证/风控页。
_CAPTCHA_MARK_RE = re.compile(
    r"""(?:id|name|class)\s*=\s*["'][^"']{0,80}?"""
    r"""(?:captcha|verify[-_]?code|geetest|challenge|captcha[-_]?image)""",
    re.I,
)
_CF_MARK_RE = re.compile(r"__cf_chl|cf-browser-verification|cf_chl_opt", re.I)


# ---------------------------------------------------------------------------
#  纯函数：结构与文本判定（全部可离线单测）
# ---------------------------------------------------------------------------
def classify_content_type(content_type: str) -> str:
    """把 Content-Type 收敛成 html / pdf / image / other / unknown。

    空 Content-Type 按 html 处理：不少老站点干脆不返回这个头，
    一律拒收会把它们全部误杀。
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "unknown"
    if ct in _HTML_LIKE:
        return "html"
    if ct == "application/pdf" or ct.endswith("+pdf"):
        return "pdf"
    if ct.startswith("image/"):
        return "image"
    return "other"


def page_text(html: str) -> str:
    """整页纯文本（跳过 script/style 等），空白折叠。

    只用于算长度与找风控词，不当作正文使用 —— 正文由 S3b 从主容器里取。
    """
    if not html:
        return ""
    root = parse_html(html)
    out: list[str] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
        elif cur.tag in _SKIP_TEXT_TAGS:
            continue
        else:
            stack.extend(reversed(cur.children))
    return normalize("".join(out))


def page_title(html: str) -> str:
    """<title> 文本。用正则而不是走整棵树：title 一定在文档最前面。"""
    m = _TITLE_RE.search(html[:30_000])
    return strip_tags(m.group(1))[:200] if m else ""


@dataclass(slots=True)
class ChallengeVerdict:
    is_challenge: bool
    reason: str = ""          # text_too_short | captcha_mark | captcha_mark_short | ""
    keyword: str = ""
    text_chars: int = 0


def is_challenge(
    html: str,
    keywords: Sequence[str],
    *,
    text_chars: int | None = None,
    min_body_chars: int = 200,
) -> ChallengeVerdict:
    """是不是验证码/风控页。

    主判据来自配置语义："正文短于此值 **且** 命中风控词"。
    这个合取是必要的：一篇讲爬虫的文章里完全可能出现"请输入验证码"，
    但它的正文一定远长于 200 字。

    次级判据是验证码控件的痕迹（`id="captcha"` / `__cf_chl` 之类）：
    这类页面有时文本不短（说明文字很长），但控件存在就是硬证据。
    """
    text_chars = len(page_text(html)) if text_chars is None else text_chars
    haystack = html or ""
    hit = next((k for k in keywords if k and k.lower() in haystack.lower()), "")
    marked = bool(_CAPTCHA_MARK_RE.search(haystack[:300_000])
                  or _CF_MARK_RE.search(haystack[:300_000]))

    if hit and text_chars < min_body_chars:
        return ChallengeVerdict(True, "text_too_short", hit, text_chars)
    if marked and text_chars < min_body_chars:
        return ChallengeVerdict(True, "captcha_mark_short", hit, text_chars)
    if hit and marked:
        return ChallengeVerdict(True, "captcha_mark", hit, text_chars)
    return ChallengeVerdict(False, "", hit, text_chars)


def looks_like_js_shell(raw_bytes: int, text_chars: int, ratio: float = 0.3) -> bool:
    """纯 JS 渲染站：可见文字占原始字节的比例极低。

    配置里 `js_required_ratio` 的原话是"HTML 文本/总字节 低于此比例"。
    这类页面 S3b 抽不出东西，但不是错误 —— 是覆盖缺口，要如实记账。
    """
    if raw_bytes <= 0:
        return False
    return (text_chars / raw_bytes) < ratio


def classify_error(err: FetchError) -> str:
    """FetchError → 失败分类。status 优先于 kind，因为 404 和 403 的处理方式不同。"""
    status = err.status or 0
    if status in (401, 403):
        return F_BLOCKED
    if status == 404:
        return F_NOT_FOUND
    if status >= 500:
        return F_SERVER
    if status:
        return F_HTTP
    return F_TIMEOUT if err.kind == "timeout" else F_CONN


# ---------------------------------------------------------------------------
#  限速
# ---------------------------------------------------------------------------
class HostThrottle:
    """按 host 串行限速。clock / sleep 可注入，时间逻辑才能被单测覆盖。

    为什么不用全局 sleep：一个任务里的 35 条 URL 往往分散在 20+ 个域名上，
    全局串行会让总耗时变成"35 × 间隔"；按 host 分开记，各家互不阻塞。
    """

    def __init__(
        self,
        qps: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gap = 1.0 / qps if qps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self.slept_total = 0.0

    def wait(self, host: str, crawl_delay: float | None = None) -> float:
        """必要时睡到允许发下一次请求，返回本次实际等待的秒数。"""
        gap = max(self._gap, float(crawl_delay or 0.0))
        now = self._clock()
        last = self._last.get(host)
        waited = 0.0
        if last is not None and gap > 0:
            remain = gap - (now - last)
            if remain > 0:
                self._sleep(remain)
                waited = remain
                self.slept_total += remain
                now = self._clock()
        self._last[host] = now
        return waited


# ---------------------------------------------------------------------------
#  单页
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PageFetch:
    """一条候选链接被打开之后的全部事实。

    ok=True 才意味着"这份 HTML 可以交给 S3b 抽正文"。
    失败时 html 可能仍有内容（如 empty），保留下来是为了让 S3b 能解释"为什么抽不出"。
    """

    url: str
    final_url: str = ""
    status: int | None = None
    content_type: str = ""
    category: str = ""          # html | pdf | image | other | unknown
    html: str = ""
    title: str = ""
    encoding: str = ""
    enc_source: str = ""
    bytes: int = 0
    text_chars: int = 0
    elapsed_ms: int = 0
    js_suspected: bool = False
    ok: bool = False
    failure: str | None = None
    detail: str = ""
    robots: str = ""            # allow | disallow | missing | unavailable | skipped
    robots_forced: bool = False # robots 本该拒绝但被档位放行（record / 失败放行）
    crawl_delay: float | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.final_url or self.url).netloc.lower()

    @property
    def hard(self) -> bool:
        """这个失败是"被挡"还是"没内容"。报告里要分开算，否则覆盖率会失真。"""
        return self.failure in HARD_FAILURES


@dataclass(slots=True)
class PagePolicy:
    """抓正文页的全部阈值，统一从配置来。

    单独成一个对象而不是直接传 AppConfig：这样单测能造一个 policy，
    不必为了测一个判定去拼一份完整的 channels.yaml。
    """

    timeout: int = 20
    per_domain_qps: float = 1.0
    max_per_domain: int = 30
    max_page_bytes: int = 8 * 1024 * 1024
    min_body_chars: int = 200
    js_required_ratio: float = 0.3
    challenge_keywords: tuple[str, ...] = ()
    encoding_fallbacks: tuple[str, ...] = ("utf-8", "gb18030")
    robots_mode: str = "obey"          # obey | record | off（fetch/robots.py）
    robots_on_failure: str = "deny"    # deny | allow
    block_host_suffixes: tuple[str, ...] = DEFAULT_BLOCK_HOST_SUFFIXES  # 政府站红线
    limit: int | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def obey_robots(self) -> bool:
        """历史口径：是否做 robots 检查。record 也算"查"（只是不拦）。"""
        return self.robots_mode != "off"

    @classmethod
    def from_config(
        cls,
        cfg: AppConfig,
        *,
        limit: int | None = None,
        obey_robots: bool | None = None,
        robots_mode: str | None = None,
        robots_on_failure: str | None = None,
    ) -> PagePolicy:
        crawl = cfg.crawl or {}
        ext = cfg.extract or {}
        flt = cfg.filter or {}
        # 0 要原样保留（= 不限速，测试与离线回放用），不能被 `or 1` 吃掉：
        # 那样"显式设成 0"和"没设"会变成同一件事，测试会白等每秒 1 次的间隔。
        qps = crawl.get("per_domain_qps", 1)
        # 档位优先级：显式 robots_mode > CLI --no-robots > 配置 > 默认 obey
        mode = resolve_robots_mode(
            crawl,
            robots_mode if robots_mode is not None
            else (None if obey_robots is None else ("obey" if obey_robots else "off")),
        )
        raw_suffixes = flt.get("block_host_suffixes")
        return cls(
            timeout=int(cfg.defaults.get("timeout", 20)),
            per_domain_qps=float(qps if qps is not None else 1),
            max_per_domain=int(flt.get("max_per_domain", 30) or 30),
            max_page_bytes=int(ext.get("max_page_bytes", 8 * 1024 * 1024)),
            min_body_chars=int(ext.get("min_body_chars", 200)),
            js_required_ratio=float(ext.get("js_required_ratio", 0.3)),
            challenge_keywords=tuple(k for k in (ext.get("challenge_keywords") or ()) if k),
            encoding_fallbacks=tuple(ext.get("encoding_fallbacks") or ("utf-8", "gb18030")),
            robots_mode=mode,
            robots_on_failure=resolve_on_failure(crawl, robots_on_failure),
            block_host_suffixes=tuple(
                str(s).strip() for s in (raw_suffixes if raw_suffixes is not None
                                         else DEFAULT_BLOCK_HOST_SUFFIXES) if str(s).strip()
            ),
            limit=limit,
            retry=RetryPolicy.from_mapping(cfg.defaults.get("retry")),
        )


def fetch_page(
    url: str,
    headers: dict[str, str],
    *,
    fetcher: BaseFetcher,
    policy: PagePolicy,
    robots: RobotsVerdict | None = None,
) -> PageFetch:
    """打开一条链接。**不抛异常**：所有失败都变成 PageFetch.failure。"""
    page = PageFetch(
        url=url,
        robots=robots.reason if robots else "",
        crawl_delay=robots.crawl_delay if robots else None,
    )
    t0 = time.perf_counter()

    def done(failure: str | None, detail: str = "", ok: bool = False) -> PageFetch:
        page.failure = failure
        page.detail = detail
        page.ok = ok and failure is None
        page.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return page

    # 红线双保险：即使上游把政府站 URL 漏进来，请求也不发
    if is_blocked_host(urlsplit(url).netloc, policy.block_host_suffixes):
        return done(F_GOV, "政府网站（红线：不抓）")

    if robots is not None:
        page.robots_forced = robots.forced
        if not robots.allowed:
            return done(F_ROBOTS, f"robots 判定 {robots.reason}")

    try:
        res = fetch_with_retry(fetcher, url, headers, policy.retry, policy.timeout)
    except FetchError as e:
        kind = classify_error(e)
        logger.debug("抓取失败 [%s] %s: %s", kind, url, e)
        return done(kind, str(e))
    except Exception as e:  # noqa: BLE001 —— 单条链接的意外不该炸掉整批
        logger.warning("抓取异常 %s: %s", url, e)
        return done(F_CONN, str(e))

    page.status = res.status
    page.final_url = res.final_url or url
    page.content_type = res.content_type
    page.category = classify_content_type(res.content_type)
    page.bytes = res.size

    if page.category == "pdf":
        return done(F_NON_HTML, "application/pdf")
    if page.category in ("image", "other"):
        return done(F_NON_HTML, res.content_type or page.category)
    if page.bytes > policy.max_page_bytes:
        return done(F_TOO_LARGE, f"{page.bytes:,} B > {policy.max_page_bytes:,} B")

    # 有原始字节就自己解（能按 meta charset 救 GBK 站）；没有才退回 body
    if res.raw:
        dec = decode_html(res.raw, res.content_type, fallbacks=policy.encoding_fallbacks)
        html, page.encoding, page.enc_source = dec.text, dec.encoding, dec.source
        if dec.garbled:
            page.html = html
            return done(F_GARBLED, f"全部候选编码均失败（尝试过 {policy.encoding_fallbacks}）")
    else:
        html = res.body
        page.encoding = "unknown"
        page.enc_source = "body"

    page.html = html
    page.title = page_title(html)
    text_chars = len(page_text(html))
    page.text_chars = text_chars
    page.js_suspected = looks_like_js_shell(page.bytes, text_chars, policy.js_required_ratio)

    chk = is_challenge(
        html, policy.challenge_keywords,
        text_chars=text_chars, min_body_chars=policy.min_body_chars,
    )
    if chk.is_challenge:
        return done(F_CHALLENGE, f"{chk.reason}：命中「{chk.keyword}」" if chk.keyword
                    else chk.reason)

    if text_chars < policy.min_body_chars:
        # 不是错误，但交不出正文：JS 站、空壳页、"页面不存在"提示页都落在这里
        return done(F_EMPTY, f"整页仅 {text_chars} 字"
                             + ("（疑似 JS 渲染）" if page.js_suspected else ""))

    return done(None, "", ok=True)


# ---------------------------------------------------------------------------
#  批量
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PageBatch:
    pages: list[PageFetch] = field(default_factory=list)
    elapsed_ms: int = 0
    skipped: int = 0               # 配额 / 去重 / 政府站红线，没发请求
    slept_s: float = 0.0
    robots_stats: dict[str, int] = field(default_factory=dict)   # RobotsCache.stats 快照
    robots_hosts: list[dict] = field(default_factory=list)       # 逐域结论（报告用）

    @property
    def total(self) -> int:
        return len(self.pages)

    @property
    def ok_count(self) -> int:
        return sum(1 for p in self.pages if p.ok)

    def failures(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.pages:
            if p.failure:
                out[p.failure] = out.get(p.failure, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def hard_failures(self) -> int:
        return sum(1 for p in self.pages if p.hard)

    def by_domain(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.pages:
            out[p.host] = out.get(p.host, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    @property
    def success_rate(self) -> float:
        return self.ok_count / self.total if self.total else 0.0


def dedupe(urls: Sequence[str]) -> list[str]:
    """保序去重。用 set 而不是 canonicalize：调用方给的已经是 url_canon。"""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_pages(
    urls: Sequence[str],
    policy: PagePolicy,
    *,
    fetcher: BaseFetcher,
    headers: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    throttle: HostThrottle | None = None,
    robots: RobotsCache | None = None,
) -> PageBatch:
    """批量打开链接。串行执行 —— S3a 先求"准"，并发留给后面按需加。

    三个上限各自独立，触顶都只是"少抓几条"，不是错误：
        policy.limit        本次最多抓几条
        policy.max_per_domain  单域名最多几条（防止一个站垄断样本）
        policy.obey_robots  每 host 拉一次 robots.txt
    """
    say = progress or (lambda _m: None)
    t0 = time.perf_counter()
    batch = PageBatch()

    targets = dedupe(urls)
    if policy.limit:
        targets = targets[: policy.limit]

    headers = headers or default_headers()
    throttle = throttle or HostThrottle(policy.per_domain_qps)
    if robots is None and policy.robots_mode != "off":
        # 重档（隐身浏览器）只为抓正文服务；robots.txt 是一行纯文本，
        # 交给轻量档取（BaseFetcher.robots_fetcher），不启动浏览器。
        # getattr 兜底：单测注入的假 fetcher 是鸭子类型，未必有这个属性。
        robots_fetcher = getattr(fetcher, "robots_fetcher", None) or fetcher
        robots = RobotsCache(robots_fetcher, headers.get("User-Agent", "*"), timeout=10,
                            mode=policy.robots_mode,
                            on_failure=policy.robots_on_failure)

    per_domain: dict[str, int] = {}
    n = len(targets)

    for i, url in enumerate(targets, start=1):
        host = urlsplit(url).netloc.lower()
        # 政府站红线：连限速排队都不进，请求不发
        if is_blocked_host(host, policy.block_host_suffixes):
            batch.pages.append(PageFetch(
                url=url, failure=F_GOV, detail="政府网站（红线：不抓）"))
            say(f"  [{i}/{n}] ✗ {F_GOV} {host}  政府网站（红线：不抓）")
            continue
        if per_domain.get(host, 0) >= policy.max_per_domain:
            batch.skipped += 1
            say(f"  [{i}/{n}] ○ 跳过（{host} 已达单域上限 {policy.max_per_domain}）")
            continue

        verdict = robots.check(url) if robots is not None else None
        waited = throttle.wait(host, verdict.crawl_delay if verdict else None)
        batch.slept_s += waited

        page = fetch_page(url, headers, fetcher=fetcher, policy=policy, robots=verdict)
        per_domain[host] = per_domain.get(host, 0) + 1
        batch.pages.append(page)

        mark = "✓" if page.ok else f"✗ {page.failure}"
        extra = ""
        if page.ok:
            extra = f"{page.bytes:,}B · {page.text_chars} 字 · {page.encoding}"
        elif page.detail:
            extra = page.detail
        say(f"  [{i}/{n}] {mark} {host}  {extra}")

    # 批末收尾：释放抓取器资源（隐身档开着浏览器，不关会一直占内存）。
    # BaseFetcher.close 默认无操作，轻量档调用它是空转；用 getattr 兜底
    # 是为了容忍鸭子类型的假实现（单测注入的那些没有 close）。
    closer = getattr(fetcher, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception as e:  # noqa: BLE001 —— 收尾失败不该抹掉已经抓到的结果
            logger.warning("抓取器收尾失败: %s", e)

    # robots 统计随批带出去：调用方（enrich / CLI / 报告）不接触缓存对象本身
    if robots is not None:
        batch.robots_stats = dict(robots.stats)
        host_records = getattr(robots, "host_records", None)
        if callable(host_records):
            batch.robots_hosts = list(host_records())

    batch.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return batch


__all__ = [
    "ChallengeVerdict",
    "F_GOV",
    "HARD_FAILURES",
    "HostThrottle",
    "PageBatch",
    "PageFetch",
    "PagePolicy",
    "classify_content_type",
    "classify_error",
    "dedupe",
    "fetch_page",
    "fetch_pages",
    "is_challenge",
    "looks_like_js_shell",
    "page_text",
    "page_title",
]
