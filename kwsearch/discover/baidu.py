"""百度网页搜索。

实测 2026-10-21（www.baidu.com/s?wd=红黑树&rn=20）：
    200 / 1,390,231B / `<!--STATUS OK-->` / ~1.5s / 20 个 c-container 块

⚠️ **单次配额通道**：第 2 次请求就会返回 wappass 图形验证码（且 HTTP 仍是 200）。
   所以本通道在 channels.yaml 里 `variants: false` + `max_pages: 1` ——
   每个任务只打一次基词。变体矩阵对百度不是扩量，是自杀。

解析要点（按真实样本验证，样本结构见 tests/test_baidu.py 的合成 fixture）：
    结果块  : <div class="result c-container ..." mu="真实URL" tpl="www_index">
             20 个块里只有 tpl=www_index（自然结果）和 sg_kg_entity_san（百科卡片）
             是真内容；image_grid_san（图片聚合）/ recommend_list（推荐）/
             rel_base_realtime（相关新闻）/ new_baikan_index 的 mu 都指向
             baidu 自己的页面 → 按 mu 的域名拉黑，不按 tpl 白名单（tpl 会改版，
             黑名单只黑"百度自己"，改版时顶多少收、不会收错）。
    真实 URL: 块开标签上的 `mu` 属性就是落地页，**不需要解跳转链**
             （href 是 www.baidu.com/link?url=... 中转，没有 mu 的块直接丢弃，
             宁可少一条也不把中转链当正文 URL —— 与 bing 同一立场）
    标题    : h3 > a，关键词高亮是 <em>，htmlx.text() 直接出纯文本
    摘要    : class 带 `summary-text_` 前缀的元素（后缀是构建哈希，会变，
             所以按前缀匹配）；老版式兜底 `c-abstract` 前缀

⚠️ 页面里没有可用的发布日期线索（时间元素是 CSS 类名，内容由 JS 填）——
   本通道的候选 published_hint 一律为 None，时间靠正文层补，不猜。
"""
from __future__ import annotations

import logging
import re

from kwsearch.aggregate import host_of
from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.discover.quality import check_relaxed
from kwsearch.fetch import FetchError, fetch_with_retry
from kwsearch.htmlx import Node, find, iter_nodes, parse_html, text
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

BASE = "https://www.baidu.com"
BLOCK_CLASSES = ("result", "c-container")

# mu 指向 baidu 自己（百科除外）的块一律丢弃：图片聚合 / 相关新闻 / 推荐列表 /
# 百家号推荐的 mu 都在 *.baidu.com 下。按**注册域名**判而不是枚举子域 ——
# host_of 会把 www.baidu.com 归一成 baidu.com，枚举写法必然漏；
# 且聚合卡片的子域会随改版增加，黑名单追不上。
# baike.baidu.com（百科词条）是真实内容，唯一放行。
def _is_junk_mu(mu: str) -> bool:
    host = host_of(mu)
    if not host:
        return True
    if host == "baike.baidu.com":
        return False
    return host == "baidu.com" or host.endswith(".baidu.com")


# 摘要元素 class 前缀（后缀是构建哈希，如 summary-text_15QGa，会随发布变化）
_SUMMARY_PREFIXES = ("summary-text", "c-abstract")

# 0 块时的页面级判定
_CHALLENGE_MARKERS = ("百度安全验证", "wappass", "验证中心")
_NO_RESULT_MARKERS = ("没有找到", "抱歉，没有找到")


def _block_mu(block: Node) -> str:
    """结果块开标签上的 mu 属性（真实落地页）。"""
    mu = (block.attrs.get("mu") or "").strip()
    return mu


def _summary_of(block: Node) -> str:
    """按 class 前缀找摘要；找不到返回空串（不拿整块文本凑数）。"""
    for n in iter_nodes(block):
        if any(c.startswith(_SUMMARY_PREFIXES) for c in n.classes):
            t = text(n)
            if t:
                return t
    return ""


def _title_of(block: Node) -> str:
    h3 = find(block, "h3")
    if h3 is not None:
        t = text(h3)
        if t:
            return t
    # 老版式：title 直接在块首的 a 上
    for n in iter_nodes(block):
        if n.tag == "a":
            t = text(n)
            if t:
                return t
    return ""


def parse_serp(
    html: str,
    offset: int = 1,
    *,
    challenge_keywords: tuple[str, ...] = (),
    channel_id: str = "baidu_html",
    query: str = "",
) -> ChannelResult:
    """把一页百度 SERP 解析成 ChannelResult。

    与 bing 同一套 block_kind 口径：challenge / empty / unknown_structure / relaxed。
    relaxed（查询被静默降级）同样整页丢弃 —— 百度对"人名 + 第二个词"也会放宽，
    而宽松页的分数只是名次加成，放进报告就是噪声。
    """
    root = parse_html(html)
    blocks = [
        n for n in iter_nodes(root)
        if n.tag == "div" and all(c in n.classes for c in BLOCK_CLASSES)
    ]

    if not blocks:
        kind, detail = _diagnose_empty(html, challenge_keywords)
        logger.warning("baidu_html 0 块（%s）", kind)
        return ChannelResult(
            channel=channel_id, query="", candidates=[], raw_count=0,
            blocked=kind == "challenge", block_kind=kind, detail=detail,
        )

    base_rank = offset if offset >= 1 else 1
    seen: set[str] = set()
    out: list[Candidate] = []
    junk = no_mu = no_title = dup = 0

    for i, block in enumerate(blocks):
        mu = _block_mu(block)
        if not mu:
            no_mu += 1
            continue
        if not mu.lower().startswith(("http://", "https://")) or _is_junk_mu(mu):
            junk += 1
            continue
        title = _title_of(block)
        if not title:
            no_title += 1
            continue
        if mu in seen:
            dup += 1
            continue
        seen.add(mu)

        out.append(Candidate(
            url_raw=mu,
            title=title,
            snippet=_summary_of(block),
            channel=channel_id,
            rank=base_rank + i,
            published_hint=None,          # 百度 SERP 没有可信时间线索，不猜
            extra={
                "domain": host_of(mu),
                "serp_offset": offset,
            },
        ))

    detail_bits = [f"块 {len(blocks)} → 有效 {len(out)}"]
    for label, n in (("聚合卡片", junk), ("无直链", no_mu), ("无标题", no_title), ("重复", dup)):
        if n:
            detail_bits.append(f"{label} {n}")

    if query:
        chk = check_relaxed(out, query)
        if chk.relaxed:
            logger.warning("baidu_html 查询被降级（basis=%s）：%s", chk.basis, chk.detail)
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
    """0 结果块时判断是哪一类失败。顺序：先风控，再"确实没结果"，最后才是结构变更。"""
    low = html.lower()
    for kw in (*challenge_keywords, *_CHALLENGE_MARKERS):
        if kw and kw.lower() in low:
            return "challenge", f"命中风控词「{kw}」（页面 {len(html):,} 字符）"
    for marker in _NO_RESULT_MARKERS:
        if marker in low:
            return "empty", f"页面明示无结果（「{marker}」）"
    return "unknown_structure", (
        f"未命中风控词、也没有'无结果'提示，但解析不到 .result.c-container"
        f"（页面 {len(html):,} 字符）—— 优先怀疑百度改版，"
        f"用 probe --save 存一份新样本再调 parse_serp"
    )


@register("baidu_html", parse=parse_serp)
def baidu_html(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    url = ctx.channel.build_url(keyword, offset)
    try:
        res = fetch_with_retry(ctx.fetcher, url, ctx.headers, ctx.policy, ctx.timeout)
    except FetchError as e:
        kind = {"blocked": "http_403", "timeout": "network", "conn": "network"}.get(
            e.kind, f"http_{e.status}" if e.status else "http")
        logger.warning("baidu_html 取页失败 offset=%s: %s", offset, e)
        return ChannelResult(
            channel=ctx.channel.id, query=keyword, candidates=[],
            blocked=True, block_kind=kind, detail=str(e),
        )

    result = parse_serp(res.body, offset, challenge_keywords=ctx.challenge_keywords,
                        query=keyword)
    result.channel = ctx.channel.id
    result.query = keyword
    result.elapsed_ms = res.elapsed_ms
    if not result.candidates and result.block_kind == "challenge":
        result.detail += f"；URL={url}"
    return result
