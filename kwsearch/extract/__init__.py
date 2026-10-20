"""抽取层：HTML → Doc。**不碰网络**。

这是从前作继承下来的关键解耦：解析逻辑不认识"字节是怎么来的"，
所以将来把 HTTP 直取换成浏览器渲染，这一层一行都不用改。
正因如此，S3a 的正文页抓取（robots / 限速 / 编码 / 挑战页识别）**放在 fetch 层**，
不能放这里 —— 一放进来，"不碰网络"这条契约就废了，以后很难再补。

现状：
    S3a `fetch/page.py`        抓取 + 页面体检（robots / 编码 / challenge / 空壳）
    S3b `extract/body.py`      容器打分 → 正文
        `extract/dates.py`     时间四级降级 → published_at + time_source
        `extract_doc()`        组装成 models.Doc
    S3d `enrich.py`            对候选抓正文 → extract_doc → 用正文重算分数（pipeline 调用）

两条不可退让的规则：
    - 时间抽不到就老实标 `time_source="fallback"`，报告里必须显示"时间不可信"，
      绝不拿抓取时间冒充发布时间 —— 一条 2015 年的文章被标成今天，时间线就全废了
    - 正文只用于算摘要，不落盘、不二次分发（`extract.store_full_body` 默认 false）
"""
from __future__ import annotations

from urllib.parse import urlsplit

from kwsearch.extract.body import (
    Article,
    core_chars,
    extract_article,
    extract_title,
)
from kwsearch.extract.dates import DateResult, normalize_date, pick_date
from kwsearch.fetch.page import (  # noqa: F401 —— 再导出，保持调用方名字稳定
    ChallengeVerdict,
    is_challenge,
    page_text,
    page_title,
)
from kwsearch.models import Doc

DEFAULT_DATE_SOURCES = ("jsonld", "meta", "regex", "serp", "fallback")


def extract_doc(
    url_canon: str,
    html: str,
    content_type: str = "",
    cfg=None,  # noqa: ANN001 —— AppConfig 或 extract 段的 dict，两种都接受
    *,
    url_raw: str = "",
    channel: str = "",
    serp_title: str = "",
    serp_snippet: str = "",
    serp_date: str | None = None,
    fetched_at: str | None = None,
    article: Article | None = None,
) -> Doc:
    """把一份已经抓到的 HTML 组装成 Doc。

    `cfg` 允许传 None / dict / AppConfig：pipeline 传整个配置，
    单测传 `{"summary_chars": 200}` 就够了，不必为测一个摘要长度去拼 YAML。

    `article` 是已经抽好的正文（调用方往往已经抽过一次用于别的目的，比如算关键词
    命中次数）。不传就现抽 —— 同一份 HTML 抽两遍没有意义，所以留了这个口子。

    低置信正文（整页兜底）**不覆盖 SERP 摘要**：那种正文里混着导航和推荐位，
    拿它当摘要比搜索引擎给的那句话更差。此时 summary 退回 snippet，
    body_chars 记 0 —— 表示"没有可信正文"，而不是"正文有 850 字"。

    ⚠️ `fetched_at` 只影响 `time_source="fallback"` 时的 published_at。
       pipeline 不传它：schema 要求 fallback 时 published_at 为 NULL，
       抓取时间已经在 doc.first_seen 里，再塞进发布时间会污染时间线。
    """
    ext: dict = {}
    if cfg is not None:
        ext = dict(getattr(cfg, "extract", None) or cfg or {})

    limit = int(ext.get("summary_chars", 500))
    art: Article = article or extract_article(html, title_hint=serp_title)
    dr: DateResult = pick_date(
        html,
        serp_hint=serp_date,
        text=art.text,
        sources=tuple(ext.get("date_sources") or DEFAULT_DATE_SOURCES),
        fetched_at=fetched_at,
    )

    summary = art.summary(limit) if art.confidence != "low" else ""
    if not summary:
        summary = (serp_snippet or "")[:limit]

    return Doc(
        url_canon=url_canon,
        url_raw=url_raw or url_canon,
        domain=urlsplit(url_canon).netloc.lower(),
        title=art.title or serp_title,
        summary=summary,
        published_at=dr.published_at,
        time_source=dr.time_source,
        channel=channel,
        content_type=content_type,
        body_chars=art.chars if art.confidence != "low" else 0,
        # 有效长度：剥掉首尾的转载模板段。转载稿的免责声明会让 body_chars 变大，
        # 而 S5 的代表按长度选 —— 不区分"模板"和"正文"，二手站就会当上代表。
        body_core=core_chars(art.text) if art.confidence != "low" else 0,
        extra={
            "container": art.container,
            "paragraphs": art.paragraphs,
            "link_ratio": art.link_ratio,
            "date_raw": dr.raw,
            "extract_confidence": art.confidence,
            "raw_chars": art.chars,
        },
    )


__all__ = [
    "DEFAULT_DATE_SOURCES",
    "Article",
    "ChallengeVerdict",
    "DateResult",
    "extract_article",
    "extract_doc",
    "extract_title",
    "is_challenge",
    "normalize_date",
    "page_text",
    "page_title",
    "pick_date",
]
