"""GDELT DOC 2.0 —— 全球新闻，本项目里"最诚实的全网"，也是单次量最大的一路。

为什么它是量最大的：
    mode=artlist + maxrecords=75，一次请求就能拿 75 条，零风控、零 key、结果可复现。
    代价是慢（单次 10s+）且中文召回一般（覆盖外媒与英文新闻为主）。

实测 2026-10-18：
    第 1 次 200；紧接着第 2 次 429，正文原话：
      "Please limit requests to one every 5 seconds or contact ... "
    单次耗时 13.4s（服务器端本来就慢，别误判成网络问题）

约束（实现时照做）：
    - min_interval_s ≥ 5，配置里给了 6（这是本项目最慢的通道）
    - timespan 必填，否则默认窗口很短；默认用 30d
    - 冷门词返回 0 条是正常结果，不是失败
    - articles 字段：url / title / seendate / domain / language / sourcecountry
      seendate 形如 20261018T120000Z（UTC、无分隔符），必须自己拆
    - 中文召回一般：中文覆盖靠 bing_html + bilibili，
      报告里要把"各语种条数"分开列，不能混成一个"总量"

⚠️ 429 必须当作"限速"而不是"被封"：只做退避重试，不做任何规避。
⚠️ 超限时 GDELT 有时返回 200 + 纯文本，不一定是 429 —— 解析层也要兜住。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from kwsearch.aggregate import host_of
from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.fetch import FetchError, fetch_with_retry
from kwsearch.htmlx import strip_tags
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

CHANNEL_ID = "gdelt"


def parse_seendate(raw: str) -> str | None:
    """"20261018T120000Z" → "2026-10-18T20:00:00+08:00"（本地时区）。

    解不出来返回 None —— 时间宁可缺，不能瞎给。
    """
    s = (raw or "").strip()
    if len(s) < 15:
        return None
    try:
        dt = datetime.strptime(s[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.astimezone().isoformat(timespec="seconds")


def parse_articles(
    body: str,
    offset: int = 0,
    *,
    challenge_keywords: tuple[str, ...] = (),
    channel_id: str = CHANNEL_ID,
) -> ChannelResult:
    """解析 GDELT 返回体。纯函数，可直接喂 probe --save 存下的样本。

    空结果（articles 为空）标记为 block_kind="empty" 但 blocked=False：
    冷门词没新闻是正常结论，不是"我们被拦了"。
    """
    text = (body or "").strip()
    if not text:
        return ChannelResult(channel=channel_id, query="", blocked=True,
                             block_kind="empty", detail="响应体为空")

    # 超限时可能是 200 + 纯文本，别直接 json.loads 崩掉
    if not text.startswith("{"):
        low = text.lower()
        kind = "rate_limit" if ("limit" in low or "too many" in low) else "unknown_format"
        return ChannelResult(channel=channel_id, query="", blocked=True, block_kind=kind,
                             detail=f"非 JSON 响应（{len(text)} 字符）：{text[:120]}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        return ChannelResult(channel=channel_id, query="", blocked=True,
                             block_kind="unknown_format", detail=f"JSON 解析失败: {e}")

    articles = payload.get("articles") or []
    if not isinstance(articles, list):
        articles = []

    out: list[Candidate] = []
    seen: set[str] = set()
    no_url = no_title = dup = 0

    for a in articles:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or "").strip()
        title = strip_tags(a.get("title") or "")
        if not url:
            no_url += 1
            continue
        if not title:
            no_title += 1
            continue
        if url in seen:
            dup += 1
            continue
        seen.add(url)

        iso = parse_seendate(a.get("seendate", ""))
        out.append(Candidate(
            url_raw=url,
            title=title,
            snippet="",                       # artlist 不带摘要，正文靠抽取层
            channel=channel_id,
            rank=len(out) + 1,
            published_hint=iso[:10] if iso else None,
            extra={
                "domain": host_of(a.get("domain") or url),
                "hint_kind": "api" if iso else "",   # api = 服务端给的时间，最可信
                "published_iso": iso,
                "language": a.get("language", ""),
                "country": a.get("sourcecountry", ""),
            },
        ))

    bits = [f"articles {len(articles)} → 有效 {len(out)}"]
    for label, n in (("无链接", no_url), ("无标题", no_title), ("重复", dup)):
        if n:
            bits.append(f"{label} {n}")

    return ChannelResult(
        channel=channel_id, query="", candidates=out, raw_count=len(articles),
        blocked=False,
        block_kind=None if out else "empty",
        detail="；".join(bits),
    )


@register("gdelt", parse=parse_articles)
def gdelt(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    url = ctx.channel.build_url(keyword, offset)
    try:
        res = fetch_with_retry(ctx.fetcher, url, ctx.headers, ctx.policy, ctx.timeout)
    except FetchError as e:
        kind = {"blocked": "http_403", "timeout": "network", "conn": "network"}.get(
            e.kind, f"http_{e.status}" if e.status else "http")
        logger.warning("gdelt 取数失败: %s", e)
        return ChannelResult(channel=ctx.channel.id, query=keyword, blocked=True,
                             block_kind=kind, detail=str(e))

    result = parse_articles(res.body, offset,
                            challenge_keywords=ctx.challenge_keywords,
                            channel_id=ctx.channel.id)
    result.channel = ctx.channel.id
    result.query = keyword
    result.elapsed_ms = res.elapsed_ms
    return result
