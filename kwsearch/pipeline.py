"""编排层：把「一个关键词」跑成「一批入库文档」。**唯一同时碰网络与写库的地方。**

为什么单独一层而不是塞进 CLI：CLI 是给人手敲的，pipeline 是要能被测试、
被将来 watch 模式复用的。把"怎么跑"和"怎么显示"分开，测试才能不依赖终端输出。

执行顺序（严格串行，M1 不做并发）：

    建任务 → 逐通道采集 → 全局去重 → 脱敏/丢弃 → 打分排序
           → 抓正文（S3d）→ 用正文重排 → 单域配额 → 入库 → 转载归并（S5）→ 收尾

四条硬规则，每条都对应一个已知的失败模式：

  1. **通道故障只降级、不中断**。一个通道挂掉记 alert，其余继续。
     反例：因为 B站 412 就让整个任务失败，等于把"少一个来源"放大成"什么都没有"。
  2. **入库前脱敏**。手机号/证件号/邮箱在任何写库路径上都先过 Sanitizer，
     没有例外分支 —— 漏一处，整份报告的定性就变了。
     正文抓到之后还要**再洗一次**：那段文字来自第三方页面，没经过上面的清洗。
  3. **截断必须留痕**。超过 max_docs / 单域配额 / 时间上限时，task.truncated=1
     且写 alert。悄悄截断是最坏的：报告看起来完整，实际缺口没人知道。
  4. **排序在入库前**。先排序再截断，保证"被截掉的永远是分数最低的"，
     而不是"运气不好排在后面的"。

⚠️ S3d 的正文抓取是**可选且可失败**的：`extract.fetch_body: false` 就退回 S4 行为
   （摘要用搜索结果页原文，body_chars=0）。抓取整体起不来时也只记账、不失败任务 ——
   已经拿到的 SERP 结果仍然有价值。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from kwsearch.aggregate import host_of, relate
from kwsearch.aggregate.cluster import (
    DEFAULT_HAMMING,
    DEFAULT_MIN_BODY,
    UNTRUSTED_TIME_SOURCES,
    ClusterInput,
    cluster_docs,
)
from kwsearch.config import AppConfig, ChannelConfig
from kwsearch.discover import get_channel
from kwsearch.discover.base import (
    ChannelContext,
    build_context,
    check_assertions,
    page_signature,
)
from kwsearch.discover.quality import keep_related, needles_of
from kwsearch.enrich import EnrichReport, Enriched, enrich
from kwsearch.expand import expand_channel
from kwsearch.fetch import FetchError
from kwsearch.fetch.page import HostThrottle
from kwsearch.models import Candidate, Doc, Mention
from kwsearch.person import (
    TIER_CN,
    Judgment,
    PersonQuery,
    build_cards,
    judge,
    summarize,
)
from kwsearch.query import QueryVariant, expand_queries, probe_profile
from kwsearch.sanitize import Sanitizer
from kwsearch.storage import Repository, init_db

Progress = Callable[[str], None]

# 通道拒收的 block_kind 里，"empty" 是真没结果，不算故障，其余都要告警。
_BENIGN_BLOCK_KINDS = frozenset({"empty", ""})

# time_source 的取值。schema.sql 里注明了语义，这里集中定义避免各处手写字符串。
TS_JSONLD = "jsonld"                # 页面 JSON-LD 的 datePublished，最可靠
TS_META = "meta"                    # OpenGraph / article:published_time
TS_API = "api"                      # 结构化 API 字段（B站 pubdate / GDELT seendate）
TS_REGEX = "regex"                  # 正文里"发布于 2024-03-15"
TS_SERP = "serp"                    # 搜索引擎结果页给的绝对日期
TS_SERP_RELATIVE = "serp_relative"  # "5 天之前" 这类相对日期，按抓取日回推，精度只到天
TS_REGEX_UPDATED = "regex_updated"  # 只找到"最近更新"，多是站点构建时间，不是发布时间
TS_FALLBACK = "fallback"            # 完全没拿到日期

# 可信度排序：数字越小越可信。抓正文之后要在"页面自己声明的"和"通道给的"之间二选一，
# 靠这张表决定，而不是靠"谁后到"。
#   - api 排在 regex 之前：B站 pubdate 是精确时间戳，比正文正则稳
#   - regex_updated 排在 serp_relative 之后：相对日期语义没错（就是发布时间），
#     只是精度到天；而 regex_updated 语义本身就不对（那是更新时间）
_TS_RANK = {
    TS_JSONLD: 0,
    TS_META: 1,
    TS_API: 2,
    TS_REGEX: 3,
    TS_SERP: 4,
    TS_SERP_RELATIVE: 5,
    TS_REGEX_UPDATED: 6,
    TS_FALLBACK: 7,
}


def ts_rank(source: str) -> int:
    """时间来源的可信度名次。未知来源排最后，但不高于 fallback。"""
    return _TS_RANK.get(source, 7)


# 报告里要标"时间不可信"的来源
UNRELIABLE_TIME = frozenset({TS_SERP_RELATIVE, TS_REGEX_UPDATED, TS_FALLBACK})


@dataclass(slots=True)
class ChannelRun:
    """一条通道这一次的战果。报告里"覆盖率自评"那块就是它。"""

    channel: str
    type: str = ""
    queries: int = 0
    got: int = 0          # 通道解析出的候选总数（含跨查询重复、含随后被过滤的）
    unique: int = 0       # 本通道内去重后的条数
    filtered: int = 0     # 被关键词过滤丢掉的（只有 serp 会做这件事）
    duplicated: int = 0   # 跨查询重复的
    blocked: int = 0
    elapsed_ms: int = 0
    stopped_reason: str = ""
    error: str = ""       # 非空 = 这条通道这次失败了
    skipped: str = ""     # 非空 = 没跑（未注册 / 缺 key）


@dataclass(slots=True)
class RunResult:
    task_id: str
    keyword: str
    mode: str
    status: str = "ok"             # ok | partial | failed
    truncated: bool = False
    candidates: int = 0            # 通道解析出的候选总数（漏斗第一格）
    filtered: int = 0              # 关键词过滤丢掉的
    unique: int = 0                # 全局去重后
    kept: int = 0                  # 真正入库
    docs_new: int = 0              # 库里原本没有的
    dropped_url: int = 0           # 命中丢弃规则
    dropped_zero: int = 0          # 标题/摘要里完全没命中关键词（通道兜底给的噪声）
    dropped_cap: int = 0           # 命中单域配额
    pii_masked: int = 0
    alerts: int = 0
    elapsed_ms: int = 0
    # S3d 正文富化
    bodies_ok: int = 0             # 抓到可信正文的条数
    bodies_low: int = 0            # 抓到了但正文低置信（不覆盖摘要、body_chars=0）
    bodies_skipped: int = 0        # 库里已有正文，跳过重抓
    bodies_elapsed_ms: int = 0
    body_chars_avg: int = 0
    # S5 转载归并
    cluster_groups: int = 0        # 独立内容组数（= 簇数 + 没有指纹的那些）
    cluster_merged: int = 0        # 被并掉的条数
    # 没参与归并的两种情形必须分开记 —— 它们的"该怎么办"完全不同：
    #   pending：正文够长但缺指纹（老数据 / 本轮没轮到抓），重跑会自动补算
    #   short  ：正文短于 min_body_chars，本来就没有区分度，永远不参与
    # 混成一个"未判"数字，用户既不知道要不要处理，也不知道处理了有没有用。
    cluster_pending: int = 0
    cluster_short: int = 0
    # 人名模式（见 person.py）：卡片数与各档分布
    person_cards: int = 0
    person_tiers: dict[str, int] = field(default_factory=dict)
    per_channel: list[ChannelRun] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kept > 0


# --------------------------------------------------------------------- 通道执行


def _time_source_of(c: Candidate) -> str:
    """把通道给的日期线索映射成 time_source。没有日期就老实说没有。"""
    if not c.published_hint:
        return TS_FALLBACK
    kind = str(c.extra.get("hint_kind") or "")
    if kind == "relative":
        return TS_SERP_RELATIVE
    if kind == "absolute":
        return TS_SERP
    return TS_API


def _term_pattern(keyword: str) -> "re.Pattern[str] | None":
    """关键词过滤用的正则：**词元 OR**，不是整串包含。

    为什么不能整串：多词查询的整串（"红黑树 左旋 右旋"）永远不可能出现在任何
    标题或摘要里，用它做 require 会把这个变体的全部结果杀光 ——
    表现是"第 2 个变体起零新增，早停"，看起来像"这些后缀没用"，
    实际是我们自己把它们判死了（S4 实测的 10 条就是这么来的：
    3 次请求只留下基词的 10 条）。

    口径与 discover.quality.needles_of 完全一致，单页路径的 keep_related 用的是同一套。
    """
    needles = needles_of(keyword)
    if not needles:
        return None
    return re.compile("|".join(re.escape(n) for n in needles), re.I)


def _needles_hit(keyword: str, c: Candidate) -> bool:
    """与 serp 过滤（needles_of 词元口径）一致的"相关证据"判定。

    relate 的 token 口径是中文整串，对复合词（央行降准 → 标题只写
    "央行宣布降准"）恒判 0 命中；零命中丢弃必须用同一套词元口径复核，
    否则"require 放行、入库前再杀"自相矛盾。
    """
    text = f"{c.title} {c.snippet}".casefold()
    return any(n.casefold() in text for n in needles_of(keyword))


def _screen_single_page(
    cands: list[Candidate], keyword: str, require: str | None,
) -> tuple[list[Candidate], int]:
    """单页路径的逐条体检。

    serp 通道走变体矩阵时，过滤发生在 expand_channel 里；但变体未启用
    （max_queries=1 或 expand.enabled=false）时走的是 _run_paged，
    那条路径上**没有任何逐条校验** —— 一次查询的 10 条会不加判断地进池子，
    之后只剩入库前的 drop_zero_hit 兜底。那个开关可以被关掉，而且被它丢掉的
    条目在 query 表里不留痕迹，事后无法区分"这条通道没结果"和"结果全不相关"。

    --require 是用户显式给的正则，优先；否则用与 _term_pattern 同一套词元口径。
    """
    if not cands:
        return cands, 0
    if require:
        p = re.compile(require, re.I)
        kept = [c for c in cands if p.search(f"{c.title} {c.snippet}")]
        return kept, len(cands) - len(kept)
    return keep_related(cands, keyword)


def _run_serp(
    ch: ChannelConfig,
    ctx: ChannelContext,
    keyword: str,
    variants: list[QueryVariant],
    *,
    pattern,                       # re.Pattern | None
    zero_gain_streak: int,
    sleep: Callable[[float], None],
    plan_hook: Callable[[list[Candidate]], Sequence[QueryVariant]] | None = None,
) -> tuple[ChannelRun, list[Candidate], list[dict]]:
    """通用搜索通道：基词 + 变体矩阵，靠换说法扩量（它的翻页已废）。"""
    fn = get_channel(ch.parser)
    t0 = time.perf_counter()
    merged, stats = expand_channel(
        fn, ctx, variants,
        zero_gain_streak=zero_gain_streak,
        require=pattern,
        sleep=sleep,
        plan_hook=plan_hook,
    )
    logs = [
        {
            "seq": st.seq,
            "url": ch.build_url(st.q, ch.paging.start),
            "query_text": st.q,
            # new 是"本通道内新增"（expand 自己的口径），不是全局新增 ——
            # query 表的用途是判断"这个变体有没有用"，通道口径正好够。
            "got": st.got,
            "new": st.new,
            "blocked": st.blocked,
            "block_kind": st.block_kind or None,
            "elapsed_ms": st.elapsed_ms,
            "problems": [],
        }
        for st in stats.per_query
    ]
    run = ChannelRun(
        channel=ch.id, type=ch.type,
        queries=stats.queries, got=stats.raw, unique=stats.unique,
        filtered=stats.filtered, duplicated=stats.duplicated,
        blocked=sum(1 for st in stats.per_query if st.blocked),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        stopped_reason=stats.stopped_reason,
    )
    return run, merged, logs


def _run_paged(
    ch: ChannelConfig,
    ctx: ChannelContext,
    keyword: str,
    *,
    timespan: str | None,
    max_pages: int,
    sleep: Callable[[float], None],
) -> tuple[ChannelRun, list[Candidate], list[dict]]:
    """站点检索 / 公开 API：靠翻页扩量（它们的服务端匹配可靠，换词只会拿回重复）。"""
    fn = get_channel(ch.parser)
    t0 = time.perf_counter()
    offsets = ch.offsets()[:max(1, max_pages)]

    out: list[Candidate] = []
    logs: list[dict] = []
    run = ChannelRun(channel=ch.id, type=ch.type)
    prev_sig: frozenset[str] | None = None

    for i, off in enumerate(offsets, start=1):
        if i > 1:
            sleep(ch.throttle_min_interval_s)
        r = fn(keyword, off, ctx)
        run.queries += 1
        run.got += len(r.candidates)

        sig = page_signature(r)
        degenerate = prev_sig is not None and bool(sig) and sig == prev_sig
        prev_sig = sig or prev_sig

        logs.append({
            "seq": i,
            "url": ch.build_url(keyword, off, timespan),
            "query_text": keyword,
            "got": 0 if degenerate else len(r.candidates),
            "new": 0,
            "blocked": r.blocked,
            "block_kind": r.block_kind,
            "detail": r.detail or "",
            "elapsed_ms": r.elapsed_ms,
            # 断言按"每一页"判断：只有整批合并才判，会漏掉"某一页突然只剩 1 条"
            "problems": [] if (degenerate or r.blocked) else check_assertions(r, ch),
        })

        if r.blocked:
            run.blocked += 1
            run.stopped_reason = f"第 {i} 页被拦（{r.block_kind or '未知'}），立即停止"
            # 被拦之后继续打只会加重风控，而且拿回来的全是验证页
            break

        if degenerate:
            # 翻页参数失效时通道不会报错，只会把上一页原样再给一遍
            run.stopped_reason = f"第 {i} 页与上一页完全相同，判定分页失效，已停止"
            break

        if r.block_kind == "relaxed":
            # 请求成功、解析也成功，只是对方把查询降级成了更宽泛的词
            # （见 discover/quality.py）。这不是故障 —— 不设 run.error，
            # 但继续翻页/换页只会拿回同样的字面结果，就地停。
            run.stopped_reason = f"第 {i} 页查询被降级：{r.detail or '结果与查询无关'}"
            break

        if r.block_kind and r.block_kind not in _BENIGN_BLOCK_KINDS:
            run.stopped_reason = f"第 {i} 页结构异常（{r.block_kind}）"
            run.error = run.stopped_reason
            break

        out.extend(r.candidates)

    run.unique = len({c.url_raw for c in out})
    run.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if not run.stopped_reason:
        run.stopped_reason = f"已抓 {len(offsets)} 页"
    return run, out, logs


# ------------------------------------------------------------------------- 主流程


def _enrich_stage(
    cfg: AppConfig,
    repo: Repository,
    task_id: str,
    keyword: str,
    scored: list[tuple[float, Candidate, Mention]],
    *,
    say: Progress,
    sleep: Callable[[float], None],
    deadline: float,
    fetcher=None,  # noqa: ANN001 —— 只在测试里注入假实现，生产走 build_fetcher
    fetch_body: bool | None = None,
) -> tuple[list[tuple[float, Candidate, Mention]], dict[str, Enriched], EnrichReport | None]:
    """S3d：对"最可能入库"的那批抓正文，并用正文重算分数。

    为什么在排序之后才抓：抓正文的请求量是"条数"级的，对 200 条候选全抓等于
    自己把自己变成爬虫洪水。先按 SERP 证据排一次，只对前 `extract.max_fetch` 条
    花请求，抓完再重排 —— 被截掉的仍然永远是分数最低的。

    为什么要重算分数：SERP 摘要只有一句话，"这篇到底讲了什么"只有正文能回答。
    只抓正文却不重算，等于把新证据白拿。
    """
    ext = dict(getattr(cfg, "extract", None) or {})
    if fetch_body is False or not ext.get("fetch_body", True):
        return scored, {}, None
    if not scored:
        return scored, {}, None

    if time.perf_counter() > deadline:
        repo.add_alert(task_id, None, "limit_reached", "已达时间上限，正文抓取跳过")
        say("  ⏱ 到时间上限，跳过正文抓取")
        return scored, {}, None

    cap = int(ext.get("max_fetch") or 40)
    head = scored[:cap]
    keys = [c.url_canon or c.url_raw for _, c, _ in head]

    # 聚簇开着时，正文够长却没有指纹的行也算"没做完"（S5 之前抓的正文没有 simhash）。
    # 不补这一步，老数据会永远进不了聚簇：报告显示"归并 0 条"，看着像"这些文档都不相似"。
    cc = dict(getattr(cfg, "cluster", None) or {})
    need_sim = (int(cc.get("min_body_chars") or DEFAULT_MIN_BODY)
                if cc.get("enabled", True) else 0)
    done = repo.bodies_done(keys, need_simhash_above=need_sim)

    crawl = dict(getattr(cfg, "crawl", None) or {})
    # 0 = 不限速（显式写 0 才算），不能被 `or 1` 吃掉
    qps_raw = crawl.get("per_domain_qps", 1)
    qps = float(qps_raw if qps_raw is not None else 1)
    say(f"  📄 正文抓取：{len(head)} 条候选 · 单域 {qps:g} QPS"
        + (f" · 库里已有正文 {len(done)} 条跳过" if done else ""))

    try:
        rep = enrich(
            cfg, [c for _, c, _ in head],
            skip=done, limit=cap,
            fetcher=fetcher,
            progress=lambda m: say("    " + m),
            throttle=HostThrottle(qps, sleep=sleep),
        )
    except Exception as e:                                   # noqa: BLE001
        # 抓正文整体起不来（网络全断 / fetcher 装不上）不该让任务失败：
        # 已经拿到的 SERP 结果仍然有价值，照常入库，只记账。
        repo.add_alert(task_id, None, "fetch_fail",
                       f"正文抓取未执行：{type(e).__name__}: {e}")
        say(f"  ✗ 正文抓取未执行：{type(e).__name__}: {e}")
        return scored, {}, None

    say(f"  📄 {rep.summary_line()} · {rep.elapsed_ms} ms")

    if rep.failed:
        # 一类一条太吵（40 条候选可能有 8 种失败），合并成一条可读的汇总
        detail = "、".join(f"{k} {v}" for k, v in rep.failed.items())
        repo.add_alert(task_id, None, "fetch_fail", f"正文抓取失败：{detail}")
    if rep.skipped_robots:
        repo.add_alert(task_id, None, "robots_disallow",
                       f"{rep.skipped_robots} 条候选被 robots.txt 拒绝，未抓正文")

    rescored: list[tuple[float, Candidate, Mention]] = []
    for score, c, m in head:
        e = rep.items.get(c.url_canon or c.url_raw)
        if e is None:
            rescored.append((score, c, m))
            continue
        # 低置信正文（整页兜底，混着导航）不参与打分 —— 拿它算命中次数只会虚高
        body = e.text if e.confidence != "low" else (e.doc.summary or c.snippet)
        m2 = relate(
            keyword, e.doc.title or c.title, body,
            query_rank=int(c.extra.get("query_rank") or c.rank or 1),
            task_id=task_id, url_canon=c.url_canon or c.url_raw,
        )
        rescored.append((m2.score, c, m2))

    merged = rescored + scored[len(head):]
    merged.sort(key=lambda x: (-x[0], x[1].url_canon or ""))
    return merged, rep.items, rep


def _cluster_stage(
    cfg: AppConfig,
    repo: Repository,
    task_id: str,
    urls: list[str],
    *,
    say: Progress,
) -> tuple[int, int, int, int] | None:
    """S5：把本任务入库的文档按正文指纹归并成"独立内容组"。

    为什么放在入库**之后**：簇要引用 doc.url_canon，而重跑时大部分条目早就
    在库里了 —— 它们的指纹是从库里读的，不是这次抓的。放前面反而要为"这次没抓的"
    单独造一条路径。

    为什么不在入库前用已有的 Enriched：那条路径只能覆盖"这次抓到的"，
    重跑（全部跳过正文抓取）时会一条都聚不上，行为随运行次数变化。

    返回 (组数, 并入条数, 待补算指纹条数, 正文过短条数)；未启用时返回 None。
    后两个数分开返回：一个有救（重跑补算），一个没救（正文本来就不够长）。
    """
    cc = dict(getattr(cfg, "cluster", None) or {})
    if not cc.get("enabled", True):
        say("  🧩 转载归并：已关闭（cluster.enabled=false）")
        return None
    if not urls:
        return 0, 0, 0, 0

    rows = repo.doc_fingerprints(urls)
    items = [
        ClusterInput(
            url_canon=str(r["url_canon"]), simhash=r["simhash"],
            body_chars=int(r["body_chars"] or 0),
            body_core=int(r["body_core"] or 0),
            time_trusted=str(r["time_source"] or "") not in UNTRUSTED_TIME_SOURCES,
            title=str(r["title"] or ""), domain=str(r["domain"] or ""),
            published_at=r["published_at"],
        )
        for r in rows
    ]
    rep = cluster_docs(
        items,
        hamming=int(cc.get("hamming") or DEFAULT_HAMMING),
        min_body_chars=int(cc.get("min_body_chars") or DEFAULT_MIN_BODY),
    )
    repo.replace_clusters(task_id, rep.clusters)

    biggest = max((c.member_count for c in rep.clusters), default=0)
    extra = f" · 最大簇 {biggest} 篇" if biggest > 1 else ""
    say(f"  🧩 转载归并：{rep.summary_line()}{extra}")
    return rep.groups, rep.merged, rep.no_simhash, rep.too_short


def run_task(
    cfg: AppConfig,
    keyword: str,
    *,
    mode: str = "quick",
    timespan: str | None = None,
    db_path: str = "kwsearch.db",
    max_docs: int | None = None,
    expand: int | None = None,
    require: str | None = None,
    no_filter: bool = False,
    progress: Progress | None = None,
    sleep: Callable[[float], None] = time.sleep,
    fetcher=None,  # noqa: ANN001 —— 单测注入假实现；生产不传，由 build_fetcher 挑
    fetch_body: bool | None = None,
    person: PersonQuery | None = None,
) -> RunResult:
    """跑一次关键词任务。所有参数显式传入，不读全局状态。"""
    say = progress or (lambda _m: None)
    t_start = time.perf_counter()

    limits = cfg.limits or {}
    cap = int(max_docs or limits.get("max_docs", 1000))
    max_minutes = float(limits.get("max_minutes", 60) or 60)
    deadline = t_start + max_minutes * 60

    conn = init_db(db_path)
    repo = Repository(conn)
    task_id = repo.start_task(keyword, mode, timespan)
    sanitizer = Sanitizer(cfg.filter)

    spec = dict(cfg.expand or {})
    if expand and expand > 0:
        spec["max_queries"] = expand
    # 人名模式：查询侧只打裸姓名（"姓名 + 第二个词"必然触发 Bing 静默降级），
    # 收窄全部放到结果侧（person.judge）。所以强制 person 档、清掉后缀。
    if person is not None:
        if spec.get("profiles"):
            spec["profile"] = "person"
        spec["suffixes"] = []
        spec["extra_queries"] = []
    # profile=auto 时初始只生成基词：后续变体等第一个 serp 通道的基词结果
    # 出来、probe_profile 判完类型再补（plan_hook）。没有证据就不打无效后缀
    # —— 2026-10-18 "央行降准"实测：技术词后缀对时政词全是白打。
    variants = expand_queries(keyword, spec)
    zero_gain = int(spec.get("zero_gain_streak", 2))
    auto_probe = (person is None and bool(spec.get("profiles"))
                  and str(spec.get("profile", "auto")) == "auto")

    probed: list[QueryVariant] | None = None

    def _plan_hook(cands: list[Candidate]) -> list[QueryVariant]:
        """基词结果出来后判一次类型；结论全任务共用，不逐通道重复探测。"""
        nonlocal probed
        if probed is not None:
            return probed[1:]
        kind = probe_profile(cands) if cands else "generic"
        probed = expand_queries(keyword, spec, probe=cands)
        if len(probed) > 1:
            say(f"  ⿻ 探测：基词结果判为 {kind}，加 {len(probed) - 1} 个变体")
        else:
            say(f"  ⿻ 探测：基词结果判为 {kind}，只打基词")
        return probed[1:]

    result = RunResult(task_id=task_id, keyword=keyword, mode=mode)
    seen: dict[str, Candidate] = {}
    degraded = False

    if auto_probe:
        say(f"[{task_id}] {keyword!r}  mode={mode}  变体 基词先行，探测后定  上限 {cap} 条")
    else:
        say(f"[{task_id}] {keyword!r}  mode={mode}  变体 {len(variants)} 个  上限 {cap} 条")

    for ch in cfg.enabled_channels():
        if time.perf_counter() > deadline:
            result.truncated = True
            repo.add_alert(task_id, None, "limit_reached",
                           f"已达时间上限 {max_minutes:g} 分钟，剩余通道未执行")
            say(f"  ⏱ 到时间上限，{ch.id} 及之后的通道跳过")
            break
        if len(seen) >= cap:
            result.truncated = True
            repo.add_alert(task_id, None, "limit_reached",
                           f"已达文档上限 {cap} 条，{ch.id} 及之后的通道跳过")
            say(f"  ⏹ 已达上限 {cap} 条，剩余通道跳过")
            break

        # 未注册 / 未实现 / 缺 key：跳过并说明，不报错
        reason = ch.unavailable_reason()
        if reason is None:
            try:
                get_channel(ch.parser)
            except KeyError:
                reason = "parser 未注册"
        if reason:
            result.per_channel.append(ChannelRun(ch.id, ch.type, skipped=reason))
            say(f"  ○ {ch.id:18s} 跳过：{reason}")
            continue

        # 过滤只对 serp 生效：只有通用搜索引擎会把低频查询"放宽"成高频词。
        # 站点检索和公开 API 的每一条都是服务端认为相关的，再按标题筛会杀掉
        # 多语种改写标题（GDELT 实测 75 条里一条都不含原词）。
        pattern = None
        if ch.type == "serp" and not no_filter:
            if require:
                pattern = re.compile(require, re.I)
            elif spec.get("require_keyword", True):
                pattern = _term_pattern(keyword)

        ctx = build_context(cfg, ch.id)
        try:
            # auto_probe 时初始变体只有基词，但 serp 也必须走 _run_serp：
            # plan_hook 要在 expand_channel 内部（拿到过滤后的基词结果）补变体。
            if ch.type == "serp" and (len(variants) > 1 or auto_probe):
                run, cands, logs = _run_serp(
                    ch, ctx, keyword, variants,
                    pattern=pattern, zero_gain_streak=zero_gain, sleep=sleep,
                    plan_hook=_plan_hook if auto_probe else None,
                )
            else:
                run, cands, logs = _run_paged(
                    ch, ctx, keyword, timespan=timespan,
                    max_pages=int(ch.paging.max_pages), sleep=sleep,
                )
                if ch.type == "serp" and cands and not no_filter:
                    cands, dropped = _screen_single_page(cands, keyword, require)
                    if dropped:
                        run.filtered += dropped
                        run.unique = len({c.url_raw for c in cands})
        except FetchError as e:
            run = ChannelRun(ch.id, ch.type, error=f"{e.kind}: {e}")
            cands, logs = [], []
            repo.add_alert(task_id, ch.id, "blocked" if e.kind == "blocked" else "fetch_fail",
                           f"{e.kind}: {e}")
        except Exception as e:                                   # noqa: BLE001
            # 通道代码有 bug 也不能拖垮整个任务：留证据、继续下一条
            run = ChannelRun(ch.id, ch.type, error=f"{type(e).__name__}: {e}")
            cands, logs = [], []
            repo.add_alert(task_id, ch.id, "parse_fail", f"{type(e).__name__}: {e}")

        # 记录每次调用 + 被拦/结构异常的告警
        for lg in logs:
            repo.log_query(
                task_id, ch.id, lg["seq"], lg["url"],
                query_text=lg["query_text"], got=lg["got"], new=lg["new"],
                blocked=lg["blocked"], block_kind=lg["block_kind"],
                elapsed_ms=lg["elapsed_ms"],
            )
            # 被拦和"结构异常"只记一条：两者都写了 block_kind，
            # 分两条记会让告警计数翻倍，读者以为问题更严重
            if lg["blocked"]:
                repo.add_alert(task_id, ch.id, "blocked",
                               f"第 {lg['seq']} 次调用「{lg['query_text']}」被拦（{lg['block_kind']}）")
                degraded = True
            elif lg["block_kind"] and lg["block_kind"] not in _BENIGN_BLOCK_KINDS:
                # detail 只有单页路径有（含"结果只覆盖查询的 1/12 个片段"这类量化说明），
                # 拼进去才能分清"被降级"和"结构异常"是两回事。
                msg = f"第 {lg['seq']} 次调用「{lg['query_text']}」：{lg['block_kind']}"
                if lg.get("detail"):
                    msg += f"（{lg['detail']}）"
                repo.add_alert(task_id, ch.id, lg["block_kind"], msg)
                degraded = True
            for problem in lg.get("problems") or ():
                repo.add_alert(task_id, ch.id, "parse_fail", problem)
                degraded = True

        # 整条通道一条没解析出来：这既可能是"真没结果"，也可能是选择器失效。
        # 两者的区别在 block_kind 里已经写过（challenge/unknown_structure），
        # 这里只兜住"通道返回 0 条但也没报错"的情况。
        # relaxed 不算：它已经单独记过告警，再记一条"空结果"会让读者以为是两个问题。
        had_relaxed = any(lg.get("block_kind") == "relaxed" for lg in logs)
        if not cands and not had_relaxed and not run.error and not run.blocked and run.got == 0:
            repo.add_alert(task_id, ch.id, "empty_result",
                           run.stopped_reason or "通道未解析出任何条目")

        # serp 通道没有"每页"概念，必填字段在这里统一体检一次
        if ch.type == "serp":
            for problem in _field_problems(cands, ch):
                repo.add_alert(task_id, ch.id, "parse_fail", problem)
                degraded = True

        result.candidates += run.got or len(cands)
        result.filtered += run.filtered
        result.per_channel.append(run)

        # 全局去重：跨通道也会重叠（同一篇文章既在 SERP 也在 B站）
        added = 0
        for c in cands:
            key = c.url_canon or c.url_raw
            if not key or key in seen:
                continue
            c.url_canon = key
            seen[key] = c
            added += 1

        # 每条通道跑完就落一次：中途崩掉不至于把已花的请求全丢掉
        conn.commit()

        flag = "✗" if run.error else ("⚠" if run.blocked else "✓")
        say(f"  {flag} {ch.id:18s} 查 {run.queries:2d} 次 → 解析 {run.got:3d}"
            f" / 本通道去重 {run.unique:3d} / 全局新增 {added:3d}  {run.stopped_reason}")

    conn.commit()
    result.unique = len(seen)

    # ---------------------------------------------------- 过滤 → 脱敏 → 打分
    kept_cands: list[Candidate] = []
    for c in seen.values():
        domain = str(c.extra.get("domain") or host_of(c.url_raw))
        c.extra["domain"] = domain
        reason = sanitizer.check_url(c.url_raw, domain)
        if reason:
            result.dropped_url += 1
            repo.add_alert(task_id, c.channel, "dropped", f"{reason}: {c.url_raw[:120]}")
            continue
        c.title, n1 = sanitizer.scrub(c.title)
        c.snippet, n2 = sanitizer.scrub(c.snippet)
        if n1 + n2:
            result.pii_masked += n1 + n2
            repo.add_alert(task_id, c.channel, "pii_masked",
                           f"{c.url_raw[:120]} 内 {n1 + n2} 处联系方式/证件号已脱敏")
        kept_cands.append(c)

    scored: list[tuple[float, Candidate, Mention]] = []
    for c in kept_cands:
        rank = int(c.extra.get("query_rank") or c.rank or 1)
        m = relate(keyword, c.title, c.snippet, query_rank=rank,
                   task_id=task_id, url_canon=c.url_canon or c.url_raw)
        scored.append((m.score, c, m))
    # 先排序再截断：保证被截掉的永远是分数最低的
    scored.sort(key=lambda x: (-x[0], x[1].url_canon or ""))

    # ------------------------------------------------ S3d 正文抓取 + 重排
    enriched: dict[str, Enriched] = {}
    enrich_rep: EnrichReport | None = None
    if scored:
        scored, enriched, enrich_rep = _enrich_stage(
            cfg, repo, task_id, keyword, scored,
            say=say, sleep=sleep, deadline=deadline,
            fetcher=fetcher, fetch_body=fetch_body,
        )
        if enrich_rep is not None:
            result.bodies_ok = enrich_rep.ok
            result.bodies_low = enrich_rep.low
            result.bodies_skipped = enrich_rep.skipped_existing
            result.bodies_elapsed_ms = enrich_rep.elapsed_ms
            result.body_chars_avg = enrich_rep.body_chars_avg
            if enrich_rep.low:
                repo.add_alert(task_id, None, "parse_fail",
                               f"{enrich_rep.low} 条抓到了页面但抽不出可信正文"
                               f"（JS 站 / 推荐位 / 短页面），摘要仍用搜索结果原文")

    # -------------------------------------------------------------- 入库
    # 标题和摘要里都没出现关键词 = 通道自己给不出相关证据（B站对冷门词会
    # 拿热门课兜底、GDELT 会把无关新闻塞进 OR 查询）。
    # 这类条目的分数只剩名次加成（≤1），留着只会让报告的"总量"虚高。
    # structured_api 豁免：GDELT 的标题常是机器翻译的多语种，
    # 不含原词但内容确实相关，用关键词命中判它会把整条通道清空。
    drop_zero = bool((cfg.filter or {}).get("drop_zero_hit", False))

    per_domain: dict[str, int] = {}
    cap_per_domain = sanitizer.max_per_domain
    person_items: list[tuple[Candidate, Judgment]] = []
    kept_urls: list[str] = []
    for score, c, mention in scored:
        if result.kept >= cap:
            result.truncated = True
            repo.add_alert(task_id, None, "limit_reached",
                           f"候选 {len(scored)} 条，入库上限 {cap}，"
                           f"剩余 {len(scored) - result.kept} 条未入库")
            break

        if drop_zero and not mention.hit_title and not mention.hit_body:
            try:
                ch_type = cfg.channel(c.channel).type
            except KeyError:
                ch_type = ""
            # relate 的命中口径是"按空白切、中文整串"：对"央行降准"这类复合词，
            # 标题里几乎不会连着出现（实际标题是"央行宣布降准"），hit_title 恒 0。
            # 零命中丢弃若也用这套口径，会把 serp 过滤（needles_of 词元口径）
            # 刚放行的条目整批杀掉 —— 2026-10-18 实测 10/10 全灭、任务 failed。
            # 过滤口径必须全链路一致：require 放行的，这里不能再杀。
            if ch_type != "structured_api" and not _needles_hit(keyword, c):
                result.dropped_zero += 1
                continue

        domain = str(c.extra.get("domain") or "")
        if cap_per_domain and per_domain.get(domain, 0) >= cap_per_domain:
            result.dropped_cap += 1
            continue

        published = c.published_hint or None
        doc = Doc(
            url_canon=c.url_canon or c.url_raw,
            url_raw=c.url_raw,
            domain=domain,
            title=c.title,
            summary=(c.snippet or "")[:int(cfg.extract.get("summary_chars", 500))],
            # 没日期就是 None，不拿抓取时间冒充发布时间（first_seen 已经有抓取时间了）
            published_at=published,
            time_source=_time_source_of(c),
            channel=c.channel,
            body_chars=0,            # 抓正文失败时保持 0 = "没抓到"，语义明确
        )

        # 正文抓到了：用页面自己的事实覆盖 SERP 的转述
        e = enriched.get(c.url_canon or c.url_raw)
        if e is not None:
            page_doc = e.doc
            doc.title = page_doc.title or doc.title
            if page_doc.body_chars > 0 and page_doc.summary:
                # 正文摘要是文章自己的开头，比搜索引擎替我们写的那句话更接近原文
                doc.summary = page_doc.summary
            doc.body_chars = page_doc.body_chars
            # 有效正文长度也要一起搬：它是 S5 选代表的依据（剥掉转载模板的长度）。
            # 漏掉这一行，代表会退回按原始长度选，转载版挂着免责声明就继续冒充原文。
            doc.body_core = page_doc.body_core
            doc.content_type = page_doc.content_type or doc.content_type
            doc.http_status = page_doc.http_status
            # 指纹只能在这里传过去：它是在抓正文时用完整正文算的，
            # 后面聚簇要读它。漏了这一行，重跑会把每条都当成"没算过指纹"重新抓一遍。
            doc.simhash = page_doc.simhash
            doc.extra = dict(page_doc.extra)
            # 时间取更可信的一个：页面声明的 jsonld/meta 优先于通道转述的日期
            if ts_rank(page_doc.time_source) < ts_rank(doc.time_source):
                doc.published_at = page_doc.published_at
                doc.time_source = page_doc.time_source

            # ⚠️ 正文来自第三方页面，**必须重新脱敏**。
            # 上面那次 scrub 只洗过 SERP 的标题和摘要，没洗过页面正文。
            # "入库前脱敏，没有例外分支"这条规则在这里同样成立。
            doc.title, n3 = sanitizer.scrub(doc.title)
            doc.summary, n4 = sanitizer.scrub(doc.summary)
            if n3 + n4:
                result.pii_masked += n3 + n4
                repo.add_alert(task_id, c.channel, "pii_masked",
                               f"{doc.url_canon[:120]} 正文内 {n3 + n4} 处"
                               f"联系方式/证件号已脱敏")
        if repo.upsert_doc(doc):
            result.docs_new += 1
        repo.upsert_mention(mention)
        kept_urls.append(doc.url_canon)
        if person is not None:
            # 用洗过、且可能被正文替换过的标题/摘要做归因 —— 证据行要能直接给人看
            person_items.append((c, judge(doc.title, doc.summary, person)))
        per_domain[domain] = per_domain.get(domain, 0) + 1
        result.kept += 1

    if result.dropped_zero:
        repo.add_alert(task_id, None, "dropped",
                       f"零命中丢弃：{result.dropped_zero} 条候选的标题与摘要里"
                       f"完全没有关键词（多为通道兜底推荐）")

    if result.dropped_cap:
        repo.add_alert(task_id, None, "limit_reached",
                       f"单域配额 {cap_per_domain} 条：{result.dropped_cap} 条因所在域名"
                       f"已达配额未入库（防止单站垄断报告）")

    # ------------------------------------------------------- S5 转载归并
    clustered = _cluster_stage(cfg, repo, task_id, kept_urls, say=say)
    if clustered is not None:
        (result.cluster_groups, result.cluster_merged,
         result.cluster_pending, result.cluster_short) = clustered
        if result.cluster_pending:
            # 这一条是"有救的"：正文够长、只是缺指纹，换个次数重跑就会补上。
            # 告警里必须说清这一点，否则用户看到"未判"只会以为功能坏了。
            repo.add_alert(task_id, None, "no_fingerprint",
                           f"{result.cluster_pending} 条正文够长但没有指纹"
                           f"（老数据或本轮未轮到抓正文），未参与转载归并；"
                           f"重跑同一个关键词会自动补算")
        if result.cluster_short:
            # 这一条是"没救的"：正文短于阈值，指纹没有区分度，本来就不该参与。
            repo.add_alert(task_id, None, "body_too_short",
                           f"{result.cluster_short} 条正文短于阈值，"
                           f"指纹没有区分度，未参与转载归并（不是缺数据）")

    # --------------------------------------------------------- 人名模式归因
    if person is not None:
        cards = build_cards(person, person_items)
        repo.replace_person_cards(task_id, cards)
        result.person_cards = len(cards)
        result.person_tiers = summarize(cards)
        if not person.has_anchors:
            # 没有锚点不等于没有结果，但等于"无法归因" —— 必须在报告里说清楚
            repo.add_alert(task_id, None, "person_no_anchor",
                           "人名模式未提供机构/方向锚点，全部结果只能归入"
                           "「同名未归因」，不能当作候选人")
        tiers_txt = "、".join(
            f"{TIER_CN[t]} {n}" for t, n in result.person_tiers.items() if n) or "无"
        say(f"  人 候选人 {len(cards)} 张：{tiers_txt}")

    # ------------------------------------------------------------ 收尾
    result.elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    if result.kept == 0:
        result.status = "failed"
    elif degraded or result.truncated or any(r.error for r in result.per_channel):
        result.status = "partial"

    stats_payload = {
        "docs_new": result.docs_new,
        "docs_total": result.kept,
        "candidates": result.candidates,
        "filtered": result.filtered,
        "unique": result.unique,
        "dropped_url": result.dropped_url,
        "dropped_zero": result.dropped_zero,
        "dropped_cap": result.dropped_cap,
        "pii_masked": result.pii_masked,
        "max_per_domain": cap_per_domain,
        "channels": {r.channel: r.got for r in result.per_channel if not r.skipped},
        "bodies_ok": result.bodies_ok,
        "bodies_low": result.bodies_low,
        "bodies_skipped": result.bodies_skipped,
        "body_chars_avg": result.body_chars_avg,
        "bodies_elapsed_ms": result.bodies_elapsed_ms,
        "cluster_groups": result.cluster_groups,
        "cluster_merged": result.cluster_merged,
        "cluster_pending": result.cluster_pending,
        "cluster_short": result.cluster_short,
        "person_cards": result.person_cards,
        "person_tiers": result.person_tiers,
        "elapsed_ms": result.elapsed_ms,
    }
    repo.finish_task(task_id, result.status, result.truncated, stats_payload)

    result.alerts = conn.execute(
        "SELECT COUNT(*) FROM alert WHERE task_id=?", (task_id,)).fetchone()[0]
    conn.commit()
    conn.close()

    say(f"\n{result.status.upper()}  {result.kept} 条入库（新 {result.docs_new}）"
        f" / 去重后 {result.unique} / 原始 {result.candidates}"
        + (f" / 正文 {result.bodies_ok} 条均 {result.body_chars_avg} 字"
           if result.bodies_ok else " / 未取到正文")
        + (f" / 独立内容 {result.cluster_groups} 组"
           if result.cluster_groups else "")
        + f" / 告警 {result.alerts} / {result.elapsed_ms} ms")
    return result


def _field_problems(cands: list[Candidate], ch: ChannelConfig) -> list[str]:
    """必填字段体检。不做 min_items —— 条目少可能是被 require 过滤掉了，
    那不算解析失败（解析失败由 empty_result / unknown_structure 负责）。"""
    problems: list[str] = []
    for field in ch.assertions.required_fields:
        attr = "url_raw" if field == "url" else field
        missing = sum(1 for c in cands if not getattr(c, attr, None))
        if missing:
            problems.append(f"{missing}/{len(cands)} 条缺 {field}")
    return problems


__all__ = [
    "ChannelRun",
    "RunResult",
    "TS_API",
    "TS_FALLBACK",
    "TS_JSONLD",
    "TS_META",
    "TS_REGEX",
    "TS_REGEX_UPDATED",
    "TS_SERP",
    "TS_SERP_RELATIVE",
    "UNRELIABLE_TIME",
    "run_task",
    "ts_rank",
]
