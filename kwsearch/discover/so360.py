"""360 搜索（so.com）结果页解析 —— 百度的替补之一。

实测 2026-11-02（关键词「红黑树」，curl_cffi chrome 指纹）：

    pn=1  -> 200 / 568KB / normal 结果 4 条（另有 richsum 1 条）
    pn=2  -> 200 / 382KB / normal 结果 4~5 条，与 pn=1 的标题交集为 0

三个结论，都直接影响这里的写法：

1. **每页只有 4~5 条 normal 结果**，不是 Bing 那种 10 条。
   所以 channels.yaml 的 `assertions.min_items` 必须从 5 降到 3 ——
   照抄 5 会让每页都被判"结构异常"，而且看起来像"360 改版了"。

2. **翻页真的有效**（这点比 Bing 强）：pn=1 与 pn=2 标题交集 0。
   Bing 是配了页数也只返回同一页，所以那边的 max_pages 才压到 1；这里可以留 2。

3. **真实地址白送**：`<a href="https://www.so.com/link?m=..." data-mdurl="https://真站点/...">`
   `data-mdurl` 就是目标地址，**不需要再发一次还原请求**。
   这点很关键 —— 实测过不带 Referer 去请求 `link?m=`，会 302 到 `hao.360.com`
   导航页（250 字节）。如果这里没发现 data-mdurl、改成"发请求还原"，
   既多花 4~5 个请求，还可能把 360 导航页当成正文存进库。
   ⇒ 所以：拿不到 data-mdurl 且 href 是 `/link?` 中转的，**直接丢弃**，不落库。

解析要点（2026-11-02 真实样本）：
    结果块  : <li class="res-list" data-result-type="normal|richsum" data-result-pos="3">
              ⚠️ **不要按 data-result-type 过滤**（我第一版就是这么写错的）：
              pn=1 是 4 个 normal + 1 个 richsum，pn=2 却是 **7 个全是 richsum**。
              按 "只要 normal" 筛，第二页会整页丢弃，报出来是 unknown_structure ——
              看起来像"360 改版了"，其实是自己筛错了。
              正确的判据是"块里有没有 <h3 class="res-title"><a>" + 能不能拿到链接。
    标题    : 块内 <h3 class="res-title"><a ...>标题</a>，关键词用 <em> 高亮（不是 <strong>）
    真实地址: <a data-mdurl="...">（见上）；少数块没有 data-mdurl 但 href 本身是直连，
              那种也收 —— 只有 href 是 `/link?m=` 中转时才无解
    摘要    : <p class="res-desc">，常以「2024年1月23日 - 」开头，日期剥出来单独记
    显示域名: <cite><a>cloud.tencent.com</a></cite>（纯域名，不是 Bing 那种面包屑）

必须剔掉的一类：`ai.so.com/search/...`。它是 360 自家的 AI 聚合页，
被标成 normal、也有 res-title，但点进去是对话界面而不是一篇网页 ——
混进候选会让"抓正文"抓到一坨和查询无关的 AI 回答。

刻意没做的事：
- **不套 check_relaxed**（Bing 用来抓"多词查询被静默降级"的那道体检）。
  那个现象在 Bing 上实测确认过，360 没有对应证据；贸然套用会把
  "用同义词表述的相关结果"误杀。兜底交给 pipeline 的 drop_zero_hit。
- **不做 site: 支持**：未验证，别假装能用。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from kwsearch.aggregate import host_of
from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.fetch import FetchError, fetch_with_retry
from kwsearch.htmlx import Node, find, iter_nodes, parse_html, text
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

BASE = "https://www.so.com"
BLOCK_TAG = "li"
BLOCK_CLASS = "res-list"
# 360 自家的 AI 聚合页混在正常结果里（标成 normal、也有 res-title），
# 点进去是对话界面而不是一篇网页 —— 必须剔掉，否则"抓正文"会抓回一坨 AI 回答。
_AI_HOSTS = ("ai.so.com",)

# 摘要前缀的绝对日期：2024年1月23日 - / 2024-01-23 / 2024/1/23
_DATE_ABS_RE = re.compile(r"^(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?\s*[-–—·]?\s*")

_NO_RESULT_MARKERS = ("没有找到相关结果", "抱歉，没有找到", "未找到相关结果",
                      "没有找到与", "相关结果为空")


def _clean_domain(s: str) -> str:
    return (s or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]


def _is_relay(url: str) -> bool:
    """是不是 360 自己的中转链（拿不到目标地址时它没有任何用处）。"""
    return "/link?" in url or "/link/" in url


def _is_ai_card(url: str) -> bool:
    """360 的 AI 聚合页 —— 不是一篇可抓的网页，混进候选只会污染正文。"""
    host = host_of(url) or ""
    return any(host == h or host.endswith("." + h) for h in _AI_HOSTS)


def _title_and_url(block: Node) -> tuple[str, str, str]:
    """(标题, 真实 URL, 是否中转未解)。URL 解不出来时返回空串，调用方丢弃该条。"""
    h3 = find(block, "h3")
    a = find(h3, "a") if h3 is not None else None
    if a is None:
        return "", "", ""
    title = text(a)
    real = (a.attrs.get("data-mdurl") or "").strip()
    href = (a.attrs.get("href") or "").strip()
    if real.startswith("http"):
        return title, real, ""
    url = urljoin(BASE, href) if href else ""
    if not url.startswith("http"):
        return title, "", ""
    # 没有 data-mdurl 又是个中转链：解不出目标，宁可少一条也不写错链接
    if _is_relay(url):
        return title, "", url
    return title, url, ""


def _snippet_and_date(block: Node) -> tuple[str, str]:
    """摘要 + 从摘要前缀里剥出来的日期（ISO）。日期拿不到就返回空串。"""
    p = next((n for n in iter_nodes(block)
              if n.tag == "p" and n.has_class("res-desc")), None)
    raw = text(p) if p is not None else ""
    if not raw:
        return "", ""
    m = _DATE_ABS_RE.match(raw)
    if not m:
        return raw, ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return raw, ""
    return raw[m.end():].strip(), f"{y:04d}-{mo:02d}-{d:02d}"


def _cite_domain(block: Node) -> str:
    cite = next((n for n in iter_nodes(block) if n.tag == "cite"), None)
    return _clean_domain(text(cite)) if cite is not None else ""


def parse_serp(
    html: str,
    offset: int = 1,
    *,
    challenge_keywords: tuple[str, ...] = (),
    channel_id: str = "so360",
    query: str = "",
) -> ChannelResult:
    """把一页 360 SERP HTML 解析成 ChannelResult。纯函数：不发请求、不读配置。

    offset 是 pn 参数（**页码**，1 起，不是条数偏移）。Bing 的 first=11 是条数偏移，
    所以那边可以 `rank = offset`；这里不能 —— pn=2 只有 7 条，按 offset 算会让
    第二页第一条和第一页第一条抢同一个名次加成。按"一页约 10 条"折算成全局名次。
    """
    root = parse_html(html)
    # 不按 data-result-type 筛（理由见模块 docstring：pn=2 全是 richsum）
    blocks = [n for n in iter_nodes(root)
              if n.tag == BLOCK_TAG and n.has_class(BLOCK_CLASS)]

    if not blocks:
        kind, detail = _diagnose_empty(html, challenge_keywords)
        logger.warning("so360 0 块（%s）", kind)
        return ChannelResult(
            channel=channel_id, query="", candidates=[], raw_count=0,
            blocked=kind == "challenge", block_kind=kind, detail=detail,
        )

    # pn 是页码：折算成全局名次（一页按 10 条估，实际只有 7 条左右，余量无妨）
    base_rank = (max(1, offset) - 1) * 10 + 1
    seen: set[str] = set()
    out: list[Candidate] = []
    undecodable = no_title = relay_failed = dup = no_domain = ai_card = 0

    for i, block in enumerate(blocks):
        title, url, relay = _title_and_url(block)
        if not title:
            no_title += 1
            continue
        if not url:
            # 区分两种"没 URL"：中转解不出（relay）和压根没 href
            if relay:
                relay_failed += 1
            else:
                undecodable += 1
            continue
        if _is_ai_card(url):
            ai_card += 1
            continue
        if url in seen:
            dup += 1
            continue
        seen.add(url)

        snippet, date_iso = _snippet_and_date(block)
        domain = _cite_domain(block) or (host_of(url) or "")
        if not domain:
            no_domain += 1
        pos = (block.attrs.get("data-result-pos") or "").strip()

        out.append(Candidate(
            url_raw=url,
            title=title,
            snippet=snippet,
            channel=channel_id,
            rank=base_rank + i,
            published_hint=date_iso,
            extra={
                "domain": domain,
                "hint_kind": "absolute" if date_iso else "",
                "serp_offset": offset,
                # 360 自己给的站内名次，备查；真正参与排序的是上面的 rank
                "serp_pos": pos,
            },
        ))

    detail_bits = [f"块 {len(blocks)} → 有效 {len(out)}"]
    for label, n in (("AI 卡片", ai_card), ("中转未解", relay_failed),
                     ("无链接", undecodable), ("无标题", no_title),
                     ("重复", dup), ("无域名", no_domain)):
        if n:
            detail_bits.append(f"{label} {n}")

    return ChannelResult(
        channel=channel_id, query="", candidates=out, raw_count=len(blocks),
        blocked=False, detail="；".join(detail_bits),
    )


def _diagnose_empty(html: str, challenge_keywords: tuple[str, ...]) -> tuple[str, str]:
    """0 结果块时判断是哪一类失败。顺序有意：先排风控，再看"确实没结果"。"""
    low = html.lower()
    for kw in challenge_keywords:
        if kw and kw.lower() in low:
            return "challenge", f"命中风控词「{kw}」（页面 {len(html):,} 字符）"
    for marker in _NO_RESULT_MARKERS:
        if marker in html:
            return "empty", f"页面明示无结果（「{marker}」）"
    return "unknown_structure", (
        f"未命中风控词、也没有'无结果'提示，但解析不到任何一个 "
        f".{BLOCK_CLASS} 块（页面 {len(html):,} 字符）"
        f"—— 优先怀疑 360 改版，跑 "
        f"`tools/capture_serp.py --channel so360` 存一份新样本再调 parse_serp"
    )


@register("so360", parse=parse_serp)
def so360(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    url = ctx.channel.build_url(keyword, offset)
    try:
        res = fetch_with_retry(ctx.fetcher, url, ctx.headers, ctx.policy, ctx.timeout)
    except FetchError as e:
        kind = {"blocked": "http_403", "timeout": "network", "conn": "network"}.get(
            e.kind, f"http_{e.status}" if e.status else "http")
        logger.warning("so360 取页失败 offset=%s: %s", offset, e)
        return ChannelResult(
            channel=ctx.channel.id, query=keyword, candidates=[],
            blocked=True, block_kind=kind, detail=str(e),
        )
    return parse_serp(res.body, offset,
                      challenge_keywords=ctx.challenge_keywords,
                      channel_id=ctx.channel.id, query=keyword)
