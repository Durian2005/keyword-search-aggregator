"""B站视频搜索 —— 中文垂类最深、且分页真实有效的一路（20 条/页，最多 50 页）。

实测 2026-10-18（同一台机器，间隔 3s 连续打）：
    Referer: https://www.bilibili.com/        -> **412**（反爬直接拦）
    Referer: https://search.bilibili.com/     -> 200 / JSON
    ⇒ Referer 必须指向搜索页，不是首页。这一点换台机器也值得先试一次再说别的。
    page=1..4：page 2、4 拿到数据，page 1、3 随机 -412；间隔拉到 8s 仍然时好时坏
    ⇒ 这是**滚动配额**，不是固定间隔能绕开的。所以本通道只做"打到被拦就停"，
      绝不加重试去硬顶（顶了也会把 IP 关更久）。

返回结构：
    {"code":0,"data":{"page":1,"pagesize":20,"numResults":1000,"numPages":50,"result":[...]}}
    result[i] 关键字段：arcurl（干净真实链接）/ bvid / title（含 <em> 高亮）/ description
                        pubdate（unix 秒）/ author / play / danmaku / typename

注意点：
    - title 里带 <em class="keyword">，必须剥掉
    - pubdate 是 unix 秒 → 带时区 ISO，hint_kind="api"（本项目里时间最可信的一类）
    - numResults=1000 是**上限而非真实命中数**，报告里要写"≥1000"
    - code=-412 是限流、-352 是风控；两者都按 blocked 处理，不做任何规避

⚠️ 这是"公开搜索接口"，不是破解签名。哪天开始返回 -412/-352 且不恢复，
   正确的反应是关掉通道并告警，而不是去补 wbi 签名。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from kwsearch.aggregate import host_of
from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.fetch import FetchError, fetch_with_retry
from kwsearch.htmlx import strip_tags
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

CHANNEL_ID = "bilibili_search"
PAGE_SIZE = 20          # 接口固定 20，不跟 count 参数走

# 接口业务码 → 我们的 block_kind
_CODE_KIND = {-412: "rate_limit", -352: "challenge", -799: "rate_limit"}


def _pubdate_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts)).astimezone().isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


def parse_result(
    body: str,
    page: int = 1,
    *,
    challenge_keywords: tuple[str, ...] = (),
    channel_id: str = CHANNEL_ID,
) -> ChannelResult:
    """解析一页搜索结果。纯函数，可直接喂 probe --save 的样本。"""
    text = (body or "").strip()
    if not text or not text.startswith("{"):
        return ChannelResult(channel=channel_id, query="", blocked=True,
                             block_kind="unknown_format",
                             detail=f"非 JSON 响应（{len(text)} 字符）：{text[:100]}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        return ChannelResult(channel=channel_id, query="", blocked=True,
                             block_kind="unknown_format", detail=f"JSON 解析失败: {e}")

    code = payload.get("code")
    if code != 0:
        kind = _CODE_KIND.get(code, "api_error")
        return ChannelResult(
            channel=channel_id, query="", blocked=True, block_kind=kind,
            detail=f"code={code} msg={payload.get('message', '')}",
        )

    data = payload.get("data") or {}
    rows = data.get("result")
    if not isinstance(rows, list):
        # 搜不到东西时 result 会是 null 而不是 []，别当异常
        rows = []

    out: list[Candidate] = []
    no_url = no_title = 0
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        url = (r.get("arcurl") or "").strip()
        title = strip_tags(r.get("title") or "")
        if not url:
            no_url += 1
            continue
        if not title:
            no_title += 1
            continue
        iso = _pubdate_to_iso(r.get("pubdate"))
        out.append(Candidate(
            url_raw=url,
            title=title,
            snippet=strip_tags(r.get("description") or ""),
            channel=channel_id,
            rank=(max(1, page) - 1) * PAGE_SIZE + i + 1,
            published_hint=iso[:10] if iso else None,
            extra={
                "domain": host_of(url),
                "hint_kind": "api" if iso else "",
                "published_iso": iso,
                "bvid": r.get("bvid", ""),
                "author": r.get("author", ""),
                "play": r.get("play"),
                "danmaku": r.get("danmaku"),
                "typename": r.get("typename", ""),
                "page": page,
            },
        ))

    bits = [f"result {len(rows)} → 有效 {len(out)}"]
    for label, n in (("无链接", no_url), ("无标题", no_title)):
        if n:
            bits.append(f"{label} {n}")
    if data.get("numResults"):
        bits.append(f"numResults≈{data['numResults']}（上限，非真实命中）")

    return ChannelResult(
        channel=channel_id, query="", candidates=out,
        raw_count=int(data.get("numResults") or len(rows)),
        blocked=False,
        block_kind=None if out else "empty",
        detail="；".join(bits),
    )


@register("bilibili_search", parse=parse_result)
def bilibili_search(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    url = ctx.channel.build_url(keyword, offset)
    try:
        res = fetch_with_retry(ctx.fetcher, url, ctx.headers, ctx.policy, ctx.timeout)
    except FetchError as e:
        kind = {"blocked": "http_412", "timeout": "network", "conn": "network"}.get(
            e.kind, f"http_{e.status}" if e.status else "http")
        logger.warning("bilibili 取数失败 page=%s: %s", offset, e)
        return ChannelResult(channel=ctx.channel.id, query=keyword, blocked=True,
                             block_kind=kind, detail=str(e))

    result = parse_result(res.body, offset,
                          challenge_keywords=ctx.challenge_keywords,
                          channel_id=ctx.channel.id)
    result.channel = ctx.channel.id
    result.query = keyword
    result.elapsed_ms = res.elapsed_ms
    return result
