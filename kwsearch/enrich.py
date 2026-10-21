"""S3d · 正文富化：把候选链接变成"带真实正文的 Doc"。

位置在 pipeline 与 fetch/extract 之间。它**只读网络、不写库**，所以能被单测完整覆盖：
注入一个假 fetcher，整条链路就能离线跑。

为什么单独一层，而不是塞进 pipeline：
    pipeline 已经同时在管通道调度、去重、脱敏、打分、配额、入库六件事。
    正文富化自己有一套完整的失败语义（robots 拒 / 404 / 挑战页 / JS 空壳 / 抽不出），
    混进去会让"任务失败"和"这一条没抓到"再也分不开。

三条必须守住的规则：

  1. **只抓将要入库的那批**。抓正文的请求量是"条数"级的，不能对全部候选抓。
     调用方传入的是已排序的前 N 条（pipeline 传 enrich.max_fetch）。
  2. **抓不到就是抓不到**。失败原因原样返回给 pipeline 记账，绝不拿 SERP 摘要
     冒充正文，也不重试硬顶（重试策略只处理 5xx/429，不会去撞 403）。
  3. **低置信正文不进摘要**。`extract_doc` 已经处理了这一条：整页兜底抽出来的
     东西混着导航和推荐位，比搜索引擎给的那句话更差，此时 body_chars 记 0。
"""
from __future__ import annotations

import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Callable

from kwsearch.aggregate.cluster import DEFAULT_MIN_BODY, simhash64
from kwsearch.config import AppConfig
from kwsearch.extract import extract_article, extract_doc
from kwsearch.fetch import BaseFetcher, build_fetcher, default_headers
from kwsearch.fetch.page import (
    HostThrottle,
    PageFetch,
    PagePolicy,
    fetch_pages,
)
from kwsearch.fetch.robots import RobotsCache
from kwsearch.models import Candidate, Doc

Progress = Callable[[str], None]

# 富化阶段的额外失败分类（其余沿用 fetch/page.py 的 F_*）
F_EXTRACT = "extract_fail"      # HTML 拿到了，抽取器自己抛异常
F_NO_CANDIDATE = "orphan"       # 抓回来了但对不上任何候选（理论上不该发生）


def fingerprint(cfg: AppConfig, text: str, body_chars: int) -> int | None:
    """算正文指纹（S5 聚簇用）。算不出来就返回 None，绝不返回 0。

    ⚠️ 为什么指纹必须在**这一层**算：只有这里同时握着"完整正文"和"配置"。
       正文不落库（版权红线），入库之后再也算不出来了 ——
       拿 500 字摘要算出的指纹，和拿 6000 字正文算出的指纹不在同一个尺度上，
       混在一起比会得到一堆假距离。所以要么在这里算对，要么永远不聚类。

    低置信正文（body_chars=0）不参与：那是整页兜底抽出来的导航+推荐位，
    全站长的都一样，拿来当指纹会把整个站的文章聚成一簇。
    """
    cc = dict(getattr(cfg, "cluster", None) or {})
    if not cc.get("enabled", True):
        return None
    min_chars = int(cc.get("min_body_chars") or DEFAULT_MIN_BODY)
    if body_chars < min_chars:
        return None
    return simhash64(text) or None


@dataclass(slots=True)
class Enriched:
    """一条候选 + 它的正文抽取结果。

    `text` 是正文全文，**只活在内存里**：pipeline 用它重算关键词命中次数，
    然后丢弃 —— 入库的仍然只有 ≤500 字摘要（`extract.store_full_body` 默认 false）。
    """

    url_canon: str
    doc: Doc
    text: str = ""
    confidence: str = "high"        # high | medium | low
    page: PageFetch | None = None


@dataclass(slots=True)
class EnrichReport:
    attempted: int = 0              # 真正发了请求的条数
    ok: int = 0                     # 正文可用
    low: int = 0                    # 抓到了但正文低置信（body_chars=0）
    skipped_existing: int = 0       # 库里已有正文，跳过
    skipped_robots: int = 0         # robots 不允许
    failed: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    items: dict[str, Enriched] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.ok / self.attempted if self.attempted else 0.0

    @property
    def body_chars_avg(self) -> int:
        if not self.ok:
            return 0
        return sum(e.doc.body_chars for e in self.items.values()) // self.ok

    def summary_line(self) -> str:
        bits = [f"抓 {self.attempted} 条", f"正文可用 {self.ok}"]
        if self.low:
            bits.append(f"低置信 {self.low}")
        if self.skipped_existing:
            bits.append(f"已有正文跳过 {self.skipped_existing}")
        if self.failed:
            top = "、".join(f"{k} {v}" for k, v in list(self.failed.items())[:4])
            bits.append(f"失败 {top}")
        return " · ".join(bits)


def enrich(
    cfg: AppConfig,
    cands: Sequence[Candidate],
    *,
    fetcher: BaseFetcher | None = None,
    stealth: bool | dict | None = None,
    skip: Collection[str] = (),
    limit: int | None = None,
    progress: Progress | None = None,
    throttle: HostThrottle | None = None,
    robots: RobotsCache | None = None,
) -> EnrichReport:
    """对一批候选抓正文并抽取。**不抛异常**：单条失败只记进 failed。

    `skip` 是"库里已经有正文"的 url_canon，用来避免每次重跑都重新打一遍站点。
    `limit` 不传时受 `extract.max_fetch` 限制 —— 没有上限等于允许一次打几百个站。
    """
    say = progress or (lambda _m: None)
    t0 = time.perf_counter()
    report = EnrichReport()

    ext = dict(getattr(cfg, "extract", None) or {})
    cap = int(limit if limit is not None else (ext.get("max_fetch") or 40))

    skip_set = set(skip)
    picked: list[Candidate] = []
    for c in cands:
        key = c.url_canon or c.url_raw
        if not key:
            continue
        if key in skip_set:
            report.skipped_existing += 1
            continue
        picked.append(c)
        if len(picked) >= cap:
            break

    if not picked:
        report.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return report

    by_url: dict[str, Candidate] = {
        (c.url_canon or c.url_raw): c for c in picked
    }
    report.attempted = len(picked)

    policy = PagePolicy.from_config(cfg, limit=len(picked))
    fetcher = fetcher or build_fetcher(str(cfg.defaults.get("fetcher", "auto")),
                                       stealth=stealth)
    batch = fetch_pages(
        list(by_url),
        policy,
        fetcher=fetcher,
        headers=default_headers(cfg.defaults),
        progress=say,
        throttle=throttle,
        robots=robots,
    )
    report.skipped_robots = sum(1 for p in batch.pages if p.robots == "disallow")

    for page in batch.pages:
        if not page.ok:
            key = page.failure or "unknown"
            report.failed[key] = report.failed.get(key, 0) + 1
            continue

        c = by_url.get(page.url)
        if c is None:
            report.failed[F_NO_CANDIDATE] = report.failed.get(F_NO_CANDIDATE, 0) + 1
            continue

        try:
            art = extract_article(page.html, title_hint=c.title)
            doc = extract_doc(
                url_canon=page.final_url or (c.url_canon or c.url_raw),
                html=page.html,
                content_type=page.content_type,
                cfg=cfg,
                url_raw=c.url_raw,
                channel=c.channel,
                serp_title=c.title,
                serp_snippet=c.snippet,
                serp_date=c.published_hint,
                article=art,
                # 不传 fetched_at：schema 明确要求 fallback 时 published_at 必须为 NULL。
                # 抓取时间在 doc.first_seen 里已经有了，塞进 published_at 只会污染时间线。
            )
        except Exception as e:  # noqa: BLE001 —— 抽取器坏在一条页面上不该炸掉整批
            report.failed[F_EXTRACT] = report.failed.get(F_EXTRACT, 0) + 1
            say(f"  ! 抽取失败 {page.host}: {type(e).__name__}: {e}")
            continue

        if doc.http_status is None:
            doc.http_status = page.status
        doc.simhash = fingerprint(cfg, art.text, doc.body_chars)
        conf = str(doc.extra.get("extract_confidence") or "high")
        report.items[c.url_canon or c.url_raw] = Enriched(
            url_canon=doc.url_canon, doc=doc, text=art.text, confidence=conf, page=page,
        )
        if doc.body_chars > 0:
            report.ok += 1
        else:
            report.low += 1

    report.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return report


__all__ = [
    "F_EXTRACT",
    "EnrichReport",
    "Enriched",
    "enrich",
]
