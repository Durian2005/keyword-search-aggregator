"""把一个关键词在一条通道上"打透"：基词 + 变体，边打边去重、边判定早停。

这个模块是"指定搜索"和"热词搜索"的分水岭：
    热词搜索 = 5 个固定接口各打一次，拿到什么算什么
    指定搜索 = 一个词打 N 次（换说法），每次都要判断"这次有没有带来新东西"

三条硬规则：
  1. **早停**：连续 zero_gain_streak 个变体零新增就停。实测"红黑树"有 5/7 个后缀
     完全无效，不早停就是白打 5 次请求（每次 2s 间隔 + 100KB 流量）。
  2. **按 url_canon 全局去重**：不同查询会大量重叠，重复条目必须在这里就压掉，
     否则下游 doc 表会膨胀、报告里的"总量"是虚的。
  3. **被拦立即停**：任一变体返回 blocked，立刻收手。继续打只会加重风控，
     而且拿回来的全是验证页 —— 这是前作"403 不重试"那条经验的延伸。

限速由调用方通过 throttle_s 传入（CLI / Pipeline 决定），本模块只管"打一次要等多久"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Pattern, Sequence

from kwsearch.aggregate import canonicalize, host_of
from kwsearch.discover.base import ChannelContext
from kwsearch.models import Candidate, ChannelResult
from kwsearch.query import QueryVariant

ChannelFn = Callable[[str, int, ChannelContext], ChannelResult]


@dataclass(slots=True)
class QueryStat:
    """单次查询的战果。落 query 表、也用于报告里的"哪个变体有用"。"""

    seq: int
    q: str
    strategy: str
    got: int = 0          # 通道解析出的条数
    new: int = 0          # 去重后真正新增的条数
    duplicated: int = 0   # 与已有结果重复的条数
    filtered: int = 0     # 被 --require 过滤掉的
    blocked: bool = False
    block_kind: str = ""
    elapsed_ms: int = 0

    @property
    def useful(self) -> bool:
        return self.new > 0


@dataclass(slots=True)
class ExpansionStats:
    queries: int = 0
    raw: int = 0
    unique: int = 0
    duplicated: int = 0
    filtered: int = 0
    blocked: bool = False
    stopped_reason: str = ""
    elapsed_ms: int = 0
    per_query: list[QueryStat] = field(default_factory=list)


def expand_channel(
    fn: ChannelFn,
    ctx: ChannelContext,
    variants: Sequence[QueryVariant],
    *,
    zero_gain_streak: int = 2,
    require: Pattern[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    plan_hook: Callable[[list[Candidate]], Sequence[QueryVariant]] | None = None,
) -> tuple[list[Candidate], ExpansionStats]:
    """逐变体调用通道，返回 (去重后的候选, 统计)。

    require：正则。只保留 标题+摘要 命中的候选，用来压"红黑树 → 红黑彩票"这类歧义噪声。
             命中判断在去重之后、入库之前，被过滤的计入 filtered，不算 zero-gain。

    plan_hook：变体可以"边打边定"。基词打完后调用一次，传入基词的去重结果，
             返回后续变体（典型用法：用结果域名构成探测查询类型，技术词才加
             后缀 —— 见 query.probe_profile）。返回空列表 = 只打基词。
             刻意只在 i==0 后调用一次：探测证据来自基词，与后续变体无关。
    """
    stats = ExpansionStats()
    out: list[Candidate] = []
    seen: dict[str, str] = {}          # url_canon -> 首次发现它的查询
    t0 = time.perf_counter()
    streak = 0
    offset = ctx.channel.paging.start
    plan: list[QueryVariant] = list(variants)

    for i, v in enumerate(plan):
        if i and ctx.channel.throttle_min_interval_s > 0:
            sleep(ctx.channel.throttle_min_interval_s)

        result = fn(v.q, offset, ctx)
        stats.queries += 1
        stats.raw += len(result.candidates)

        stat = QueryStat(
            seq=i + 1, q=v.q, strategy=v.strategy,
            got=len(result.candidates),
            blocked=result.blocked,
            block_kind=result.block_kind or "",
            elapsed_ms=result.elapsed_ms,
        )

        if result.blocked:
            stats.blocked = True
            stats.per_query.append(stat)
            stats.stopped_reason = f"「{v.q}」被拦（{result.block_kind or '未知'}），立即停止"
            break

        for c in result.candidates:
            key = canonicalize(c.url_raw) or c.url_raw
            if key in seen:
                stats.duplicated += 1
                stat.duplicated += 1
                continue
            if require is not None and not require.search(f"{c.title} {c.snippet}"):
                stats.filtered += 1
                stat.filtered += 1
                continue
            c.url_canon = key
            c.extra.update({
                "found_by": v.q,             # 哪个查询先发现了它（报告里要能追溯）
                "strategy": v.strategy,
                "query_rank": c.rank,        # 原查询内的名次，加权用
                "domain": c.extra.get("domain") or host_of(c.url_raw),
            })
            seen[key] = v.q
            out.append(c)
            stat.new += 1

        stats.per_query.append(stat)
        if i == 0 and plan_hook is not None:
            # 基词已见分晓：让调用方看一眼结果，决定还要不要打后续查询。
            # 被去重/过滤剩下的 out 才是探测证据 —— 拿原始 cands 会把风控页当信号。
            plan.extend(plan_hook(out))
        streak = 0 if stat.new > 0 else streak + 1
        if streak >= zero_gain_streak:
            stats.stopped_reason = f"连续 {streak} 个变体零新增，提前收手"
            break

    stats.unique = len(out)
    stats.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if not stats.stopped_reason:
        stats.stopped_reason = "变体已全部执行"
    return out, stats


def domain_distribution(cands: Sequence[Candidate]) -> list[tuple[str, int]]:
    """按域名计数，降序。报告里"来源分布"和"是否被某个大站垄断"都靠它。"""
    counts: dict[str, int] = {}
    for c in cands:
        d = c.extra.get("domain") or host_of(c.url_raw)
        counts[d] = counts.get(d, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
