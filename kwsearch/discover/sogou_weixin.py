"""搜狗微信文章搜索（微信公众号内容的唯一公开入口）。

实测 2026-10-21（weixin.sogou.com/weixin?type=2&query=红黑树）：
    200 / 33,590B / ~0.4s / `ul.news-list` 下 10 条 `<li>`，无验证码

⚠️ **单页配额通道**：搜狗微信的风控比网页搜索更敏感（历史版本对连续请求直接出
   验证码），所以 channels.yaml 里 max_pages=1 + 10s 间隔。被拦就停，
   不重试硬顶 —— 拦截本身是"这份报告缺了谁"的证据。

解析要点（按真实样本验证，fixture 见 tests/test_sogou_weixin.py）：
    结果块  : `ul.news-list > li`，块内有 `div.txt-box`；
              导航等杂 li 没有 txt-box → 按"含 txt-box"过滤，不按 li 的 id 前缀
              （id 里的 11002601 是业务号，会变）
    标题    : `h3 > a`，关键词高亮是 `<!--red_beg--><em>`，注释被 htmlx 丢掉
    摘要    : `p.txt-info`
    公众号  : `span.all-time-y2`（如"吴师兄学算法"）→ extra.account，
              人名模式下可当弱证据，不参与查询
    时间    : `span.s2` 里是 `document.write(timeConvert('1577765720'))`
              —— unix 秒。**显式按 UTC+8 换算**（不用本地时区，否则单测结果
              随机器时区漂移）；日期精度到天，与 bing/gdelt 的 published_hint 口径一致
    链接    : href 是 `/link?url=...` 搜狗中转。**不再原样存进库**：token 逐请求
              轮换且几分钟即失效，存下来点开就是验证页（2026-10-25）。改为在同一个
              session 里立刻跟随中转页，解出永久有效的 mp.weixin.qq.com 链接；
              解不出才退回中转链，并记 `link_resolved: false`。
              extra.domain 仍记真实内容域 mp.weixin.qq.com —— 否则"来源分布"会被
              单一中转域名垄断，单域配额也会错杀。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urljoin

from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.discover.quality import check_boilerplate, check_relaxed
from kwsearch.fetch import BaseFetcher, FetchError, fetch_with_retry
from kwsearch.htmlx import Node, find, find_all, parse_html, raw_text, text
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

BASE = "https://weixin.sogou.com"
TZ_CN = timezone(timedelta(hours=8))          # 显式 +8，不依赖机器时区
PAGE_SIZE = 10                                # 实测固定 10 条/页（与 bing 同量级）

# span.s2 里的时间：document.write(timeConvert('1577765720'))
_TS_RE = re.compile(r"timeConvert\('(\d{9,11})'\)")

# 0 结果块时的页面级判定
_CHALLENGE_MARKERS = ("异常访问请求", "antispider", "安全验证", "验证码")
_NO_RESULT_MARKERS = ("没有找到", "暂无", "找不到")


def _blocks(root: Node) -> list[Node]:
    """含 div.txt-box 的 li 才是文章结果（导航/广告 li 没有）。"""
    out: list[Node] = []
    for li in find_all(root, "li"):
        if find(li, "div", "txt-box") is not None:
            out.append(li)
    return out


def _time_of(block: Node) -> tuple[str | None, str]:
    """从 span.s2 的 timeConvert 脚本里取发布时间。

    返回 (date_iso, full_iso)；取不到返回 ("", "")。
    """
    for span in find_all(block, "span"):
        if not span.has_class("s2"):
            continue
        m = _TS_RE.search(raw_text(span))
        if not m:
            return "", ""
        try:
            dt = datetime.fromtimestamp(int(m.group(1)), tz=TZ_CN)
        except (OverflowError, OSError, ValueError):
            return "", ""
        iso = dt.isoformat(timespec="seconds")
        return iso[:10], iso
    return "", ""


def _dedupe_key(account: str, title: str, date_iso: str) -> str:
    """跨跑批去重键：公众号 + 标题 + 日期 的哈希（2026-10-24）。

    为什么需要它：href 是 `link?url=<token>` 中转链，**token 每次请求都轮换**，
    同一篇文章每次跑批都是新 URL —— 只按 URL 去重的话，每跑一次就多一堆
    doc 行，"新增 N 条"也全是假的。

    为什么取这三项：同一篇文章在这三项上稳定（标题由作者定、日期是发布时间、
    公众号是发布者），而 URL 每次都变。日期拿不到时只用 公众号+标题，
    仍能拦住绝大多数重复（同一公众号不会有同名的两篇文章）。

    为什么存哈希不存原文：标题是页面原文，可能含手机号/邮箱；这个键要落进
    doc 表，存哈希既能去重，又不额外多存一份未经脱敏的原文。
    """
    raw = "|".join(("sogou_weixin", (account or "").strip().casefold(),
                    (title or "").strip().casefold(), date_iso or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ============================================================================
# 中转链解链（2026-10-25）
# ============================================================================
# 为什么必须做：SERP 的 href 是 `/link?url=<token>`，token 逐请求轮换、且几分钟
# 就死。实测把库里的中转链拿出来再请求，直接 302 到 `/antispider/`（页面标题
# "搜狗搜索"）—— 报告里 54 条公众号链接全点不开，正文一条也抓不到。
#
# 怎么解：中转页把真实地址拆成若干段 `url += '...'` 藏在脚本里，段间插 `@` 干扰。
# 前提是**同一次会话**：先打 SERP 会种下 SUID/SNUID，带着 cookie 请求中转页才
# 返回 200；没有 cookie 的单次请求一律撞 antispider。所以这依赖 fetcher 持有
# 持久 Session（见 fetch/__init__.py 的 CurlCffiFetcher）。
_FRAG_RE = re.compile(r"url\s*\+=\s*'([^']*)'")
_MP_PREFIX = "https://mp.weixin.qq.com/"


def join_relay_fragments(html: str) -> str | None:
    """把中转页脚本里的地址片段拼成真实链接；拼不出合法 mp 链接返回 None。

    ⚠️ 只做 `&amp;` → `&` 一处替换，**绝不能 html.unescape()**：
       真实地址里 `&timestamp=` 的 `&times` 会被 HTML 实体解析器啃成 `×`，
       拼出来是 `×tamp=1792...`，mp 服务端不认，页面回 200 但没有 js_content
       （2026-10-25 实测踩过一次，查了半小时）。
    """
    frags = _FRAG_RE.findall(html or "")
    if not frags:
        return None
    raw = "".join(frags).replace("@", "").replace("&amp;", "&").strip()
    if not raw.startswith(_MP_PREFIX) or "signature=" not in raw:
        return None
    return raw


def _is_relay(url: str) -> bool:
    return "/link?url=" in (url or "")


@dataclass(slots=True, frozen=True)
class ResolvePolicy:
    """解链节奏。默认关闭 —— 空配置的通道不该凭空多发请求。"""

    enabled: bool = False
    max_links: int = 4       # 每次 SERP 最多解几条
    delay_s: float = 2.5     # 条间间隔
    stop_after_fail: int = 2  # 连续失败几条就收手

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "ResolvePolicy":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            # 负数没有意义：max_links<=0 等价于关闭，这里夹到 0 让调用方统一判
            max_links=max(0, int(raw.get("max_links", 4))),
            delay_s=max(0.0, float(raw.get("delay_s", 2.5))),
            stop_after_fail=max(1, int(raw.get("stop_after_fail", 2))),
        )


@dataclass(slots=True)
class ResolveStats:
    """解链结果，只用于 detail 记账（报告里要说清"为什么这篇还是中转链"）。"""

    tried: int = 0
    ok: int = 0
    failed: int = 0
    reused: int = 0          # 同一篇文章在别的变体里已经解过，直接复用
    stopped: str = ""

    def note(self) -> str:
        if not self.tried and not self.reused:
            return ""
        parts = [f"解链 {self.ok}/{self.tried}"]
        if self.reused:
            parts.append(f"复用 {self.reused}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.stopped:
            parts.append(self.stopped)
        return "；".join(parts)


# 解链结果缓存：键是业务键（公众号+标题+日期），值是永久链接。
# 为什么要跨调用共享：一次任务会拿同一个词打 6 个变体，同一篇文章常常在
# 2~3 个变体里都出现（实测「红黑树」60 条解析里 30 条是同文换链）。不缓存的话
# 同一篇要解 2~3 次，纯浪费搜狗的风控额度 —— 而那正是最稀缺的资源。
# 只缓存成功结果：失败的这一篇在别的变体里大概率还是失败，但换个时间可能就好了，
# 不该被一次失败永久拉黑。
# 容量上限防止长驻进程（web serve）无限增长；满了就整体清空，不做 LRU ——
# 这东西重算一次只多一个请求，不值得为它写淘汰逻辑。
_RESOLVE_CACHE: dict[str, str] = {}
_CACHE_MAX = 512


def _cache_get(key: str) -> str | None:
    return _RESOLVE_CACHE.get(key) if key else None


def _cache_put(key: str, url: str) -> None:
    if not key:
        return
    if len(_RESOLVE_CACHE) >= _CACHE_MAX:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[key] = url


def resolve_candidates(
    cands: list[Candidate],
    fetcher: BaseFetcher,
    headers: dict[str, str],
    *,
    referer: str,
    timeout: int,
    policy: ResolvePolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> ResolveStats:
    """就地把中转链换成真实 mp 链接。成功改写 url_raw，失败保留原链并记账。

    **只取链接，不取内容**：中转页返回的 HTML 只用来拼那串 `url += '...'` 片段，
    一个字都不落库。所以这一步属于「索引获取」，不经过 robots 判定（正文抓取
    仍由 fetch_pages 逐域判定，`mp.weixin.qq.com` 的 `Disallow: /` 在那里生效）。
    把它错当"抓正文"会得出"解链要受 robots 管"的结论 —— 那等于要求同一个域名的
    搜索结果页照发、解链却不准发，逻辑上讲不通。详见 README「robots 分层口径」。

    这里的 sleep 是**页内节奏**（同一次通道调用里连续解几条），不是驱动层的
    通道限速 —— base.py 禁止通道自己限速指的是"两次通道调用之间"，那仍归驱动层。
    """
    stats = ResolveStats()
    if not policy.enabled or policy.max_links <= 0:
        return stats

    streak = 0
    for c in cands:
        if not _is_relay(c.url_raw):
            continue

        # 先查缓存：同一篇文章可能已经在上一个变体里解过了
        key = c.dedupe_key or c.url_raw
        cached = _cache_get(key)
        if cached:
            c.url_raw = cached
            c.extra["link_resolved"] = True
            stats.reused += 1
            continue

        if stats.tried >= policy.max_links:
            stats.stopped = f"已达上限 {policy.max_links} 条"
            break

        stats.tried += 1
        real = _resolve_one(fetcher, c.url_raw, {**headers, "Referer": referer}, timeout)
        if real:
            c.url_raw = real
            c.extra["link_resolved"] = True
            stats.ok += 1
            _cache_put(key, real)
            streak = 0
        else:
            # 保留中转链：它至少是个可读的来源凭据，比丢条目强
            c.extra["link_resolved"] = False
            stats.failed += 1
            streak += 1
            if streak >= policy.stop_after_fail:
                stats.stopped = f"连续失败 {streak} 条，提前收手"
                break
        sleep(policy.delay_s)
    return stats


def _resolve_one(fetcher: BaseFetcher, relay_url: str,
                 headers: dict[str, str], timeout: int) -> str | None:
    """解一条。任何异常都算失败 —— 解链是"锦上添花"，不该让整页结果作废。"""
    try:
        res = fetcher.fetch(relay_url, headers, timeout)
    except Exception as e:  # noqa: BLE001 —— 含 FetchError / 连接错误
        logger.debug("搜狗中转链解链异常 %s: %s", relay_url[:80], e)
        return None
    if res.status != 200:
        logger.debug("搜狗中转链解链 %s 返回 %s", relay_url[:80], res.status)
        return None
    return join_relay_fragments(res.body)


def parse_serp(
    html: str,
    offset: int = 1,
    *,
    challenge_keywords: tuple[str, ...] = (),
    channel_id: str = "sogou_weixin",
    query: str = "",
) -> ChannelResult:
    """把一页搜狗微信文章搜索解析成 ChannelResult（口径与 bing/baidu 一致）。"""
    root = parse_html(html)
    blocks = _blocks(root)

    if not blocks:
        kind, detail = _diagnose_empty(html, challenge_keywords)
        logger.warning("sogou_weixin 0 块（%s）", kind)
        return ChannelResult(
            channel=channel_id, query="", candidates=[], raw_count=0,
            blocked=kind == "challenge", block_kind=kind, detail=detail,
        )

    seen: set[str] = set()
    out: list[Candidate] = []
    no_title = no_url = dup = no_time = 0
    # 名次口径必须与 bing/baidu/bilibili 一致：base_rank 是本页第一条的**全局**
    # 名次，页内逐条 +i。曾经这里写的是 `rank=offset`（页码），结果一页 10 条的
    # rank 全等于 1，relate() 的 1/(1+log2(1+rank)) 名次加成恒为最高值 ——
    # 公众号条目被系统性高估，在几十条搜狗结果灌进来的跑批里直接把排序带偏。
    base_rank = (max(1, offset) - 1) * PAGE_SIZE + 1

    for i, block in enumerate(blocks):
        h3 = find(block, "h3")
        a = find(h3, "a") if h3 is not None else None
        title = text(a) if a is not None else text(h3)
        if not title:
            no_title += 1
            continue
        href = (a.attrs.get("href") if a is not None else "") or ""
        if not href:
            no_url += 1
            continue
        url = urljoin(BASE + "/", href)
        if url in seen:
            dup += 1
            continue
        seen.add(url)

        p = find(block, "p", "txt-info")
        snippet = text(p) if p is not None else ""

        account = ""
        for span in find_all(block, "span"):
            if span.has_class("all-time-y2"):
                account = text(span)
                break

        date_iso, full_iso = _time_of(block)
        if not date_iso:
            no_time += 1

        out.append(Candidate(
            url_raw=url,
            title=title,
            snippet=snippet,
            channel=channel_id,
            rank=base_rank + i,
            published_hint=date_iso or None,
            # 跨跑批去重键：中转 URL 每次请求都变，靠它才能认出"同一篇文章"
            dedupe_key=_dedupe_key(account, title, date_iso),
            extra={
                # 真实内容域，不是中转域名（见模块 docstring）
                "domain": "mp.weixin.qq.com",
                "via": "weixin.sogou.com",
                "account": account,
                "published_iso": full_iso,
                "serp_offset": offset,
            },
        ))

    detail_bits = [f"块 {len(blocks)} → 有效 {len(out)}"]
    for label, n in (("无标题", no_title), ("无链接", no_url), ("重复", dup),
                     ("无时间", no_time)):
        if n:
            detail_bits.append(f"{label} {n}")

    # 兜底页（结构判据，不看查询词）：搜狗对"搜不到"的词会返回挂着正常列表结构、
    # 实际是站点名/重复标题的兜底页。必须放在 check_relaxed 之前 —— 兜底页的
    # 提示语里原样写着查询词，语义体检会误判成"相关"（2026-10-23 实测 11 条假阳性）。
    if out:
        bp = check_boilerplate(out)
        if bp.is_boilerplate:
            logger.warning("sogou_weixin 兜底页（basis=%s）：%s", bp.basis, bp.detail)
            return ChannelResult(
                channel=channel_id, query="", candidates=[],
                raw_count=len(blocks),
                blocked=False, block_kind="boilerplate",
                detail=f"{bp.detail}；已解析到 {len(out)} 条但均非真实文章，整页丢弃",
            )

    if query:
        chk = check_relaxed(out, query)
        if chk.relaxed:
            logger.warning("sogou_weixin 查询被降级（basis=%s）：%s", chk.basis, chk.detail)
            return ChannelResult(
                channel=channel_id, query="", candidates=[],
                raw_count=len(blocks),
                blocked=False, block_kind="relaxed",
                detail=(f"{chk.detail}；已解析到 {len(out)} 条但无一相关，整页丢弃"),
            )

    return ChannelResult(
        channel=channel_id, query="", candidates=out, raw_count=len(blocks),
        blocked=False, detail="；".join(detail_bits),
    )


def _diagnose_empty(html: str, challenge_keywords: tuple[str, ...]) -> tuple[str, str]:
    low = html.lower()
    for kw in (*challenge_keywords, *_CHALLENGE_MARKERS):
        if kw and kw.lower() in low:
            return "challenge", f"命中风控词「{kw}」（页面 {len(html):,} 字符）"
    for marker in _NO_RESULT_MARKERS:
        if marker in low:
            return "empty", f"页面明示无结果（「{marker}」）"
    return "unknown_structure", (
        f"未命中风控词、也没有'无结果'提示，但解析不到含 txt-box 的 li"
        f"（页面 {len(html):,} 字符）—— 优先怀疑搜狗改版，"
        f"用 probe --save 存一份新样本再调 parse_serp"
    )


@register("sogou_weixin", parse=parse_serp)
def sogou_weixin(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    url = ctx.channel.build_url(keyword, offset)
    try:
        res = fetch_with_retry(ctx.fetcher, url, ctx.headers, ctx.policy, ctx.timeout)
    except FetchError as e:
        kind = {"blocked": "http_403", "timeout": "network", "conn": "network"}.get(
            e.kind, f"http_{e.status}" if e.status else "http")
        logger.warning("sogou_weixin 取页失败 offset=%s: %s", offset, e)
        return ChannelResult(
            channel=ctx.channel.id, query=keyword, candidates=[],
            blocked=True, block_kind=kind, detail=str(e),
        )

    result = parse_serp(res.body, offset, challenge_keywords=ctx.challenge_keywords,
                        query=keyword)
    result.channel = ctx.channel.id
    result.query = keyword
    result.elapsed_ms = res.elapsed_ms

    # 解链放在整页体检（兜底页/降级）之后：那一页本来就要整页丢弃，
    # 就别再为它多发 max_links 个请求、多担一份风控风险。
    if result.candidates:
        stats = resolve_candidates(
            result.candidates, ctx.fetcher, ctx.headers,
            referer=url, timeout=ctx.timeout,
            policy=ResolvePolicy.from_mapping(ctx.channel.resolve),
        )
        note = stats.note()
        if note:
            result.detail = "；".join(p for p in (result.detail, note) if p)

    if not result.candidates and result.block_kind == "challenge":
        result.detail += f"；URL={url}"
    return result
