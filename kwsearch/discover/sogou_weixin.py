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
    链接    : href 是 `/link?url=...` 搜狗中转。不逐条解跳转（那等于每结果多一次
              请求，正中风控下怀）：URL 保留搜狗链（浏览器打开会 302 到
              mp.weixin.qq.com），但 extra.domain 记**真实内容域**
              mp.weixin.qq.com —— 否则"来源分布"会被单一中转域名垄断，
              单域配额也会错杀。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.discover.quality import check_boilerplate, check_relaxed
from kwsearch.fetch import FetchError, fetch_with_retry
from kwsearch.htmlx import Node, find, find_all, parse_html, raw_text, text
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

BASE = "https://weixin.sogou.com"
TZ_CN = timezone(timedelta(hours=8))          # 显式 +8，不依赖机器时区

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
            rank=offset if offset >= 1 else 1,
            published_hint=date_iso or None,
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
    if not result.candidates and result.block_kind == "challenge":
        result.detail += f"；URL={url}"
    return result
