"""发布时间抽取：jsonld → meta → 正文正则 → SERP 线索 → fallback。

顺序来自配置 `extract.date_sources`，**顺序本身是可信度排序**，不可以随便调：
    jsonld  页面主动声明的结构化数据，最可靠
    meta    OpenGraph / article:published_time，次之
    regex   正文里的"发布于 2024-03-15"
            —— 内部再分三档：publish → `regex`；只找到"最近更新" → `regex_updated`；
               什么标记都没有 → 仍是 `regex`（最弱，但仍是页面自己写的日期）
    serp    搜索引擎给的线索（可能是相对时间"3 天前"，也可能根本没有）
    fallback 都没有 → 存抓取时间，**必须标 fallback**，报告里不许冒充真实发布时间

`regex_updated` 单独成一个来源，是因为 `最近更新: 2026/7/21` 往往是**站点构建时间**：
同一站的每篇文章都会得到同一个日期，全塞进时间线就会凭空冒出一根高峰。

反过来的代价：一条 2015 年的文章被标成今天，时间线就全废了，而且看不出是坏的。
所以宁可返回 None 让报告显示"时间未知"，也不拿抓取时间去填 —— 只有最后一级才填，
并且一定带 time_source="fallback"。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Sequence

from kwsearch.htmlx import Node, find_all, parse_html

_YEAR_MIN, _YEAR_MAX = 1990, 2100
_LOCAL_OFFSET_H = 8          # 项目只服务中文场景；UTC 时间按北京时间还原

# 支持 2024-03-15 / 2024/3/5 / 2024.03.15 / 2024年3月15日，可带时分秒
_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
    r"(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?(?!\d)"
)

# 日期后面紧跟的时区标记（允许中间有毫秒，bilibili 给的是 `...T04:09:58.000Z`）。
# UTC 必须换算成本地时间：实测 bilibili 的 jsonld 是 04:09:58Z，页面显示 12:09:58，
# 不换算的话每一条时间都差 8 小时，报告里看着像数据错。
_TZ_TAIL_RE = re.compile(r"^\s*(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})", re.I)
_UTC_MARKS = frozenset({"Z", "+00:00", "+0000", "-00:00", "-0000"})

# 带上下文的日期优先于全文第一个日期：正文里常有"参考 2019 年的数据"这类干扰。
#
# 但"发布"和"更新"必须分开，**它们不是同一件事**：
#   `最近更新: 2026/7/21` 是站点构建 / 模板重刷的时间，几乎所有文章共用同一个值。
#   拿它当发布时间，时间线上会凭空多出一根"当天所有人一起发文"的柱子，
#   而且看不出来是错的 —— 所以只能标成 regex_updated，不许冒充发布。
_PUBLISH_CTX_RE = re.compile(
    r"(?:发布(?:时间|于|日期)?|发表(?:时间|于)?|上传(?:时间|于)?|刊发|"
    r"posted|published|publish\s*date)\s*[:：]?\s*$",
    re.I,
)
_UPDATE_CTX_RE = re.compile(
    r"(?:最近更新|最后更新|更新(?:时间|于|日期)?|修改(?:时间|于)?|修订(?:时间|于)?|"
    r"last\s*updated|updated|modified)\s*[:：]?\s*$",
    re.I,
)
# 日期后置写法：`2026/7/21 更新`
_PUBLISH_SUFFIX_RE = re.compile(r"^\s*[:：]?\s*(?:发布|发表|posted|published)", re.I)
_UPDATE_SUFFIX_RE = re.compile(r"^\s*[:：]?\s*(?:最近更新|最后更新|更新|修改|updated)", re.I)

_JSONLD_KEYS = ("datePublished", "dateCreated", "uploadDate", "releaseDate")
_JSONLD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script\s*>',
    re.I | re.S,
)

# 越靠前越可信。'date' 放最后：它太泛，不少站用它表示"抓取时间"甚至无关字段。
_META_KEYS = (
    "article:published_time",
    "og:published_time",
    "og:release_date",
    "datepublished",
    "publishdate",
    "publish_date",
    "published_time",
    "pubdate",
    "dc.date.issued",
    "dc.date",
    "sailthru.date",
    "date",
)


@dataclass(slots=True)
class DateHit:
    """一个正文里的日期，以及它旁边的上下文属于哪一类。

    publish  旁边写着"发布于"→ 可以当发布时间
    update   旁边写着"最近更新"→ 只代表页面最近被改过，可能是站点构建时间
    loose    旁边什么都没有 → 全文第一个日期，最弱的一档
    """
    raw: str
    kind: str = "loose"       # publish | update | loose


@dataclass(slots=True)
class DateResult:
    published_at: str | None
    time_source: str          # jsonld | meta | regex | regex_updated | serp | fallback
    raw: str = ""             # 命中的原始字符串，排查"时间怎么来的"时唯一线索

    @property
    def trusted(self) -> bool:
        return self.time_source not in ("fallback", "regex_updated")


def normalize_date(text: str) -> str | None:
    """把各种写法收敛成 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`；认不出返回 None。

    校验用 datetime.date 而不是手写范围判断：后者会把 2 月 31 日放过去。
    """
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (_YEAR_MIN <= y <= _YEAR_MAX):
        return None
    try:
        date(y, mo, d)          # 闰年、月份天数由标准库校验
    except ValueError:
        return None
    hh, mm, ss = m.group(4), m.group(5), m.group(6)
    if hh is None:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    h, mi = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    sec = int(ss) if ss is not None else 0

    # UTC → 本地：只认明确的 UTC 标记。带 +08:00 的原样保留，不做二次换算
    tail = _TZ_TAIL_RE.match(text[m.end():])
    if tail and tail.group(1).upper() in _UTC_MARKS:
        local = datetime(y, mo, d, h, mi, sec) + timedelta(hours=_LOCAL_OFFSET_H)
        return local.strftime("%Y-%m-%d %H:%M:%S")

    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{sec:02d}"


# ---------------------------------------------------------------------------
#  各来源
# ---------------------------------------------------------------------------
def _walk_jsonld(obj: object) -> Iterator[str]:
    """递归找日期字段。ld+json 可能是对象、数组，或带 @graph 的嵌套结构。"""
    if isinstance(obj, dict):
        for key in _JSONLD_KEYS:
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                yield v
        for v in obj.values():
            yield from _walk_jsonld(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_jsonld(v)


def jsonld_dates(html: str) -> list[str]:
    out: list[str] = []
    for m in _JSONLD_RE.finditer(html):
        blob = m.group(1).strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue          # 坏 JSON 很常见（尾逗号、注释），跳过即可
        out.extend(_walk_jsonld(data))
    return out


def meta_dates(root: Node) -> list[str]:
    """按 _META_KEYS 的可信度顺序返回，而不是文档里的先后顺序。"""
    found: dict[str, str] = {}
    for n in find_all(root, "meta"):
        key = (n.attrs.get("property") or n.attrs.get("name")
               or n.attrs.get("itemprop") or "").strip().lower()
        if key in _META_KEYS and key not in found:
            val = n.attrs.get("content") or n.attrs.get("datetime") or ""
            if val.strip():
                found[key] = val.strip()
    return [found[k] for k in _META_KEYS if k in found]


def context_kind(text: str, start: int, end: int) -> str:
    """判断 `text[start:end]` 这个日期的上下文说的是"发布"还是"更新"。

    前置看 start 之前 24 字符（中文既有写法都是前置），后置看 end 之后 12 字符
    （`2026/7/21 更新`）。**后置必须从 end 起算** —— 从 start 起算看到的还是日期本身。
    前置优先：`发布于 2026-07-21 更新于…` 这种连写按"发布"算，发布时间更具体。
    """
    before = text[max(0, start - 24):start]
    if _PUBLISH_CTX_RE.search(before):
        return "publish"
    if _UPDATE_CTX_RE.search(before):
        return "update"
    after = text[end:end + 12]
    if _PUBLISH_SUFFIX_RE.match(after):
        return "publish"
    if _UPDATE_SUFFIX_RE.match(after):
        return "update"
    return "loose"


def regex_dates_tagged(text: str) -> list[DateHit]:
    """正文里所有日期 + 各自的上下文类别，按出现顺序。"""
    return [
        DateHit(m.group(0), context_kind(text, m.start(), m.end()))
        for m in _DATE_RE.finditer(text or "")
    ]


def regex_dates(text: str, *, context_only: bool = False) -> list[str]:
    """正文里的日期。context_only=True 时只收"发布于/更新于…"这种带标记的。"""
    hits = regex_dates_tagged(text)
    if context_only:
        return [h.raw for h in hits if h.kind in ("publish", "update")]
    return [h.raw for h in hits]


# ---------------------------------------------------------------------------
#  主入口
# ---------------------------------------------------------------------------
def pick_date(
    html: str,
    root: Node | None = None,
    *,
    serp_hint: str | None = None,
    text: str = "",
    sources: Sequence[str] = ("jsonld", "meta", "regex", "serp", "fallback"),
    fetched_at: str | None = None,
) -> DateResult:
    """按 sources 顺序取第一个可信来源。`text` 是已抽好的正文，用于 regex 级。"""
    root = root if root is not None else parse_html(html)

    for src in sources:
        if src == "jsonld":
            for raw in jsonld_dates(html):
                if (norm := normalize_date(raw)):
                    return DateResult(norm, "jsonld", raw)

        elif src == "meta":
            for raw in meta_dates(root):
                if (norm := normalize_date(raw)):
                    return DateResult(norm, "meta", raw)

        elif src == "regex":
            # 正文取不到时退到整页文本：很多站的日期在标题下方，不在正文容器里。
            # 两处合成一个池子再按"上下文类别"分档 —— 不能让正文里的一句"最近更新"
            # 压过页头明写的"发布时间"（那种情况在 VitePress / Hugo 站上很常见）。
            pools = [h for h in (regex_dates_tagged(text), regex_dates_tagged(_plain(html))) if h]
            for kind, source, limit in (
                ("publish", "regex", None),          # 明确写了"发布于"
                ("update", "regex_updated", None),   # 只剩"最近更新"，只当更新时间
                ("loose", "regex", 5),               # 什么都没写，最多看前 5 个
            ):
                picked = [h for hits in pools for h in hits if h.kind == kind]
                for h in (picked[:limit] if limit else picked):
                    if (norm := normalize_date(h.raw)):
                        return DateResult(norm, source, h.raw)

        elif src == "serp":
            if serp_hint and (norm := normalize_date(serp_hint)):
                return DateResult(norm, "serp", serp_hint)

        elif src == "fallback":
            if fetched_at:
                return DateResult(normalize_date(fetched_at) or fetched_at, "fallback", fetched_at)

    return DateResult(None, "fallback", "")


def _plain(html: str) -> str:
    """整页去标签文本（只在 regex 级兜底时用，不追求干净）。"""
    from kwsearch.htmlx import strip_tags

    body = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html or "", flags=re.I | re.S)
    return strip_tags(body)


__all__ = [
    "DateHit",
    "DateResult",
    "context_kind",
    "jsonld_dates",
    "meta_dates",
    "normalize_date",
    "pick_date",
    "regex_dates",
    "regex_dates_tagged",
]
