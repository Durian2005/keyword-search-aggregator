"""Bing 网页搜索（M1 主力通道）。

实测 2026-10-18（cn.bing.com）：
    GET /search?q=人工智能&count=20   -> 200 / ~104,000B / 10 条

⚠️ 分页在匿名访问下【完全失效】（2026-10-18 实测，覆盖面很广，不要再试一遍）：
    first=1 / 11 / 21、去掉 count、加 FORM=PERE、换 www.bing.com + mkt=en-US、
    同一 session 保持 Cookie、照抄页面自带的"下一页"链接（带 FPIG 令牌）——
    返回的都是同一批 10 条，重合度 10/10。中英文词结论一致。
    页面里也没有"相关搜索"容器，拿不到免费的查询扩展。
    ⇒ 一个词 = 10 条。扩量只能靠 query 变体矩阵（M2），不要指望翻页。
    这段结论同时写在 channels.yaml 的 bing_html 段里，两处必须一致。

解析要点（已按 2026-10-18 真实样本验证，样本见 .workbuddy/tmp/bing_serp.html）：
    结果块  : <li class="b_algo" data-id iid=SERP.5338>   ← 属性值不带引号，标准库能吞
    标题链接: 块内 <h2><a href="...">，标题里用 <strong> 高亮关键词（不是 <em>）
    真实域名: 块内 <cite>。注意它不是纯域名，而是面包屑形式：
             "https://blog.csdn.net › article › detail"  → 取第一个 › 之前的部分
             Bing 另有把结果包成 www.bing.com/ck/a?...&u=a1<base64url> 的中转，
             遇到必须解出真实 URL，不能直接落库
    摘要    : <p class="b_lineclamp2">（也有 b_lineclamp / b_lineclamp3 变体）
             摘要常以日期开头："2019年11月15日 · ..." 或 "5 天之前 · ..."，这是白送的线索
    日期    : 结果块本身没有独立的时间字段，只能从摘要前缀里取，且要标明可信度

⚠️ site: 运算符在本通道【未验证有效】，RSS 版明确无效。要做"限定站内搜索"，
   一律走 crawl 层（种子域 + CrawlSpider），别指望 SERP 的 site:。

⚠️ **静默降级**（2026-10-18 实测，比上面那条更阴险）：
   查询里一旦出现"人名 + 第二个词"，Bing 会把整条查询退回成**第一个词**的字面结果：

       张军 南京              → 10 条全是「张」的百科/字典/姓氏/家谱页
       网络安全张军            → 测速网、网速测试
       某某大学计算机学院张军   → 所在城市的旅游攻略
       某某研究院张军          → 同名机构的百科条目

   而 HTTP 200、`<title>` 仍是原查询、搜索框 value 也是原查询、没有风控提示。
   加空格 / 加引号 / site: / 换英文语序 全部无效。
   判据见 `discover/quality.py`：整页候选里没有一条真的提到查询里的人名。

结构诊断（blocks == 0 时）：这里刻意把"没结果""被挑战""选择器失效"分成三件事，
因为三者在报告里的含义完全不同 —— 前两个是覆盖缺口，第三个是我们自己坏了。
"降级"（block_kind=relaxed）是第四类：**解析到了，但解析出来的是别人**。
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from kwsearch.aggregate import host_of
from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.discover.quality import check_relaxed
from kwsearch.fetch import FetchError, fetch_with_retry
from kwsearch.htmlx import Node, find, find_all, iter_nodes, parse_html, text
from kwsearch.models import Candidate, ChannelResult

logger = logging.getLogger(__name__)

BASE = "https://cn.bing.com"
BLOCK_CLASS = "b_algo"

# 摘要前的日期：# 2019年11月15日 · / 2026-01-07 · / 2026/1/7
_DATE_ABS_RE = re.compile(r"^(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?")
# 相对日期：5 天之前 / 3 小时前 / 昨天
_DATE_REL_RE = re.compile(r"^(\d+)\s*(分钟|小时|天|周|个月)\s*(?:之)?前")
_DATE_REL_WORDS = {"刚刚": 0, "今天": 0, "昨天": 1, "前天": 2}
_REL_UNIT_DAYS = {"分钟": 0, "小时": 0, "天": 1, "周": 7, "个月": 30}

# 摘要前缀里紧跟在日期后的分隔符，去掉后摘要才是正文开头
_LEAD_SEP_RE = re.compile(r"^[\s·•∙|｜、,，。:：\-—–]+")
# 摘要末尾 Bing 追加的"阅读更多"链接文本（取文本时会拼进来）
_TAIL_MORE_RE = re.compile(r"\s*(?:阅读更多|Read more|Read more »)\s*$", re.I)
# 0 条结果时的判定
_NO_RESULT_MARKERS = ("没有与此相关的结果", "未找到与此相关的结果", "there are no results for")


# ---------------------------------------------------------------------------
# 小工具（纯函数，全部可单测）
# ---------------------------------------------------------------------------
def absolute(href: str, base: str = BASE) -> str:
    """补全协议/域名。Bing 的 href 基本上是绝对地址，但站内链接不能想当然。"""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if "://" in href:
        return href
    return urljoin(base + "/", href)


def unwrap_redirect(url: str) -> str | None:
    """解 Bing 中转链（/ck/a?...&u=a1<base64url>）。

    返回：
        真实 URL   —— 普通链接原样返回，中转链解出真实地址
        None      —— 看着像中转链但解不出来。调用方必须丢弃，绝不能拿中转链当正文 URL。
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    is_bing = host == "bing.com" or host.endswith(".bing.com")
    if not is_bing or not parts.path.startswith("/ck/"):
        return url

    raw = (parse_qs(parts.query).get("u") or [""])[0]
    if not raw:
        return None

    # 形态 A：u=https%3A%2F%2Fexample.com%2F...   （未 base64）
    if raw[:4].lower() in ("http", "%3a%"):
        cand = unquote(raw)
        return cand if _is_http(cand) else None

    # 形态 B：u=a1<base64url>   （"a1" 是 Bing 的版本前缀）
    blob = raw[2:] if raw.startswith("a1") else raw
    try:
        decoded = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    return decoded if _is_http(decoded) else None


def _is_http(s: str) -> bool:
    return s[:8].lower().startswith("https://") or s[:7].lower().startswith("http://")


def parse_date_hint(snippet: str, today: date | None = None) -> tuple[str | None, str, str]:
    """从摘要前缀抽日期线索。

    返回 (published_hint, 去掉日期后的摘要, hint_kind)：
        hint_kind = "absolute" | "relative" | ""
    入参必须是**已解码的纯文本**（即 htmlx.text() 的输出，&ensp; / &#0183; 都已还原）。
    相对日期（"5 天之前"）按抓取日回推成日期 —— 精度只到天，所以必须打上 relative 标记，
    由抽取层决定要不要采信；绝不允许它冒充页面真实发布时间。
    """
    if not snippet:
        return None, snippet, ""

    m = _DATE_ABS_RE.match(snippet)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                iso = date(y, mo, d).isoformat()
            except ValueError:
                return None, snippet, ""
            return iso, _LEAD_SEP_RE.sub("", snippet[m.end():]), "absolute"

    for word, days in _DATE_REL_WORDS.items():
        if snippet.startswith(word):
            base = today or date.today()
            return (base - timedelta(days=days)).isoformat(), \
                   _LEAD_SEP_RE.sub("", snippet[len(word):]), "relative"

    m = _DATE_REL_RE.match(snippet)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        base = today or date.today()
        return (base - timedelta(days=n * _REL_UNIT_DAYS[unit])).isoformat(), \
               _LEAD_SEP_RE.sub("", snippet[m.end():]), "relative"

    return None, snippet, ""


def _clean_snippet(s: str) -> str:
    s = _TAIL_MORE_RE.sub("", s or "")
    return _LEAD_SEP_RE.sub("", s).strip()


def _cite_domain(block: Node) -> str:
    cite = find(block, "cite")
    if cite is None:
        return ""
    raw = text(cite)
    if not raw:
        return ""
    # 面包屑："https://blog.csdn.net › article › detail"
    head = re.split(r"[›»]", raw, maxsplit=1)[0].strip()
    return host_of(head)


def _snippet_of(block: Node) -> str:
    # 首选 b_lineclamp*（正文摘要），b_caption 兜底（会把"阅读更多"也带进来）
    for p in find_all(block, "p"):
        if any(c.startswith("b_lineclamp") for c in p.classes):
            return _clean_snippet(text(p))
    cap = find(block, "div", "b_caption")
    return _clean_snippet(text(cap)) if cap is not None else ""


def _title_and_href(block: Node) -> tuple[str, str]:
    h2 = find(block, "h2")
    if h2 is not None:
        a = find(h2, "a")
        if a is not None and a.attrs.get("href"):
            return text(a) or text(h2), a.attrs["href"]
        if text(h2):
            return text(h2), ""      # 少数"直接答案"卡片没有链接
    a = find(block, "a", "tilk")      # 老版式兜底：站点图标链
    if a is not None and a.attrs.get("href"):
        return text(block)[:120], a.attrs["href"]
    return "", ""


# ---------------------------------------------------------------------------
# 纯解析（不联网）：离线调试、单测、离线复跑都靠它
# ---------------------------------------------------------------------------
def parse_serp(
    html: str,
    offset: int = 1,
    *,
    challenge_keywords: tuple[str, ...] = (),
    channel_id: str = "bing_html",
    query: str = "",
) -> ChannelResult:
    """把一页 SERP HTML 解析成 ChannelResult。

    offset 是 Bing 的 first 参数（1 起），用来算全局 rank：first=11 时 rank 从 11 开始。

    query 传"本页对应的查询词"时，会额外做一次**降级体检**（见 quality.check_relaxed）：
    整页候选没有一条真的提到查询内容 → 判 `block_kind="relaxed"` 并整页丢弃。
    不传 query 就跳过这项检查（离线只关心结构时的用法）。

    blocked 的口径是"这条通道这次为什么没产出数据"，不是只有"被封"才算：
        challenge      命中风控词
        empty          正常返回但确实 0 条结果（冷门词，属正常）
        unknown_structure 既没风控词也没"无结果"提示，却解析不到结果块
                       → 最危险的一种：Bing 改版了，选择器失效
        relaxed        解析到了结果块，但内容与查询无关 —— 查询被静默降级了
                       → **不算 blocked**：请求本身成功，是被"降智"了
    """
    root = parse_html(html)
    # 不写死 li：Bing 改版时 div.b_algo 也出现过，按 class 找更耐改
    blocks = [n for n in iter_nodes(root) if n.has_class(BLOCK_CLASS)]

    if not blocks:
        kind, detail = _diagnose_empty(html, challenge_keywords)
        logger.warning("bing_html 0 块（%s）", kind)
        return ChannelResult(
            channel=channel_id, query="", candidates=[], raw_count=0,
            blocked=kind == "challenge", block_kind=kind, detail=detail,
        )

    base_rank = offset if offset >= 1 else 1
    seen: set[str] = set()
    out: list[Candidate] = []
    undecodable = no_title = no_domain = dup = 0

    for i, block in enumerate(blocks):
        title, href = _title_and_href(block)
        if not title:
            no_title += 1
            continue
        url = unwrap_redirect(absolute(href))
        if not url:
            # 宁可少一条也不写错链接：中转链解不出来就丢弃，只记数
            undecodable += 1
            continue
        if url in seen:
            dup += 1
            continue
        seen.add(url)

        snippet = _snippet_of(block)
        hint, snippet, hint_kind = parse_date_hint(snippet)
        domain = _cite_domain(block) or host_of(url)
        if not domain:
            no_domain += 1

        out.append(Candidate(
            url_raw=url,
            title=title,
            snippet=snippet,
            channel=channel_id,
            rank=base_rank + i,
            published_hint=hint,
            extra={
                "domain": domain,
                "hint_kind": hint_kind,          # absolute | relative | ""
                "serp_offset": offset,
            },
        ))

    detail_bits = [f"块 {len(blocks)} → 有效 {len(out)}"]
    for label, n in (("中转未解", undecodable), ("无标题", no_title), ("重复", dup), ("无域名", no_domain)):
        if n:
            detail_bits.append(f"{label} {n}")

    # 降级体检必须在这里做：等到下游按分数筛就晚了 —— 退化页的分数是"名次加成"，
    # 而 pipeline 的 drop_zero_hit 只丢"标题+摘要都不含关键词"的条目，
    # 一旦有人关了那个开关（或查询本身很短），这 10 条会一路进报告。
    if query:
        chk = check_relaxed(out, query)
        if chk.relaxed:
            logger.warning("bing_html 查询被降级（basis=%s）：%s", chk.basis, chk.detail)
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
    """0 结果块时判断到底是哪一类失败。顺序有意义：先排风控，再看"确实没结果"。"""
    low = html.lower()
    for kw in challenge_keywords:
        if kw and kw.lower() in low:
            return "challenge", f"命中风控词「{kw}」（页面 {len(html):,} 字符）"
    for marker in _NO_RESULT_MARKERS:
        if marker in low:
            return "empty", f"页面明示无结果（「{marker}」）"
    return "unknown_structure", (
        f"未命中风控词、也没有'无结果'提示，但解析不到 .{BLOCK_CLASS}"
        f"（页面 {len(html):,} 字符）—— 优先怀疑 Bing 改版，"
        f"用 probe --save 存一份新样本再调 parse_serp"
    )


# ---------------------------------------------------------------------------
# 通道入口：负责取字节，解析交给 parse_serp
# ---------------------------------------------------------------------------
@register("bing_html", parse=parse_serp)
def bing_html(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    url = ctx.channel.build_url(keyword, offset)
    try:
        res = fetch_with_retry(ctx.fetcher, url, ctx.headers, ctx.policy, ctx.timeout)
    except FetchError as e:
        kind = {"blocked": "http_403", "timeout": "network", "conn": "network"}.get(
            e.kind, f"http_{e.status}" if e.status else "http")
        logger.warning("bing_html 取页失败 offset=%s: %s", offset, e)
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
