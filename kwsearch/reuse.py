"""同词增量复用：TTL 内已经跑过的词，不再重复花额度。

为什么需要这一层：
    额度池是按**请求次数**消耗的（实测与判定见 health.py），而同一个词反复全量
    重跑结果几乎一样 —— 那是拿配额换零新增。库里某词连跑 5 次，`docs_new` 从 38
    掉到 0，每一轮却都把全部通道重新打了一遍。

为什么粒度是「整个任务」而不是「单个通道」：
    mention 表只有 (task_id, url_canon)，**没有 channel 列**。按通道跳过意味着本次
    报告里那部分结果凭空消失 —— 用户看到的是"少了一块数据"，而不是"省了请求"。
    要做通道级复用，得先在 mention 上记来源通道，那是另一件事。
    整任务复用没有这个问题：它直接指向上一轮的报告，数据完整、只是不重新抓。

默认关闭，因为**它改变语义**：这次没有真的去搜。开法有两个：
    - CLI `--reuse`（本次开启）
    - channels.yaml 里 `reuse.enabled: true`（长期开启，此时用 `--refresh` 强制重跑）

判据里最容易错的一条是 status：库里 22 个任务**全是 partial**（GDELT 恒 network、
robots 拦正文，任务几乎不可能"全绿"），所以不能拿 status == "ok" 当门槛，
否则这个功能永远不触发。只排除 aborted（崩了，数据不全）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from kwsearch.health import human_delta, parse_iso
from kwsearch.query import query_key

TTL_HOURS = 24.0        # 默认 24 小时：与"当天额度"同量级
MIN_DOCS = 1            # 上一轮至少入库 N 条才值得复用


@dataclass(slots=True, frozen=True)
class ReusePolicy:
    """复用策略。默认全关 —— 不配就等同于 Phase 3 之前的行为。"""

    enabled: bool = False
    ttl_hours: float = TTL_HOURS
    min_docs: int = MIN_DOCS

    @classmethod
    def from_mapping(cls, d: Mapping | None) -> ReusePolicy:
        d = d or {}
        try:
            ttl = float(d.get("ttl_hours", TTL_HOURS))
        except (TypeError, ValueError):
            ttl = TTL_HOURS
        try:
            docs = int(d.get("min_docs", MIN_DOCS))
        except (TypeError, ValueError):
            docs = MIN_DOCS
        # ttl <= 0 会让"TTL 内"永远不成立 —— 功能静默失效比报错更难查，
        # 所以按默认值兜住；min_docs < 1 等于"0 条也算覆盖"，同样钳到 1。
        return cls(
            enabled=bool(d.get("enabled", False)),
            ttl_hours=ttl if ttl > 0 else TTL_HOURS,
            min_docs=max(1, docs),
        )


    @classmethod
    def resolve(cls, d: Mapping | None, override: bool | None = None) -> ReusePolicy:
        """配置 + 命令行开关 → 本次生效的策略。

        override 三态：None 听配置 / True 本次开启（--reuse）/ False 强制重跑（--refresh）。
        把这条合并在一个地方，CLI 与 pipeline 才不会各写一遍、然后哪天只改了一边。
        """
        p = cls.from_mapping(d)
        if override is None:
            return p
        return cls(enabled=override, ttl_hours=p.ttl_hours, min_docs=p.min_docs)


def _docs_of(task: Mapping[str, Any]) -> int:
    """上一轮入库条数。老任务的 stats_json 可能缺键，缺了按 0 处理（= 不值得复用）。"""
    stats = task.get("stats") or {}
    if not isinstance(stats, Mapping):
        return 0
    try:
        return int(stats.get("docs_total") or 0)
    except (TypeError, ValueError):
        return 0


def find_reusable(
    tasks: Sequence[Mapping[str, Any]],
    *,
    keyword: str,
    mode: str | None,
    now: datetime,
    policy: ReusePolicy,
) -> dict[str, Any] | None:
    """从候选任务里挑出可复用的那一个；没有就返回 None。

    `tasks` 由调用方按 `started_at` **降序**给（storage.task_history 就是这么排的），
    这里取第一条命中的，也就是最近的那次。

    返回 {"task_id", "started_at", "docs", "age"}，age 是刚算好的时长字符串，
    只用于展示 —— 别让调用方再解析一遍时间。
    """
    if not policy.enabled:
        return None
    want = query_key(keyword)
    horizon = now - timedelta(hours=policy.ttl_hours)

    for t in tasks:
        tid = str(t.get("task_id") or "")
        if not tid:
            continue
        # aborted = 任务中途崩了，query/doc 可能写了一半，不能当"已有结果"
        if str(t.get("status") or "") in ("aborted", "running"):
            continue
        # 模式不同结果不可比：quick 上限 1000 条、deep 会遍历种子域，
        # 拿 quick 的结果冒充 deep 等于骗自己
        if (t.get("mode") or "") != (mode or ""):
            continue
        if query_key(str(t.get("keyword") or "")) != want:
            continue
        started = parse_iso(t.get("started_at"))
        if started is None or started < horizon:
            continue
        docs = _docs_of(t)
        if docs < policy.min_docs:
            continue
        return {
            "task_id": tid,
            "started_at": t.get("started_at"),
            "docs": docs,
            "age": human_delta(now - started),
        }
    return None


__all__ = ["MIN_DOCS", "TTL_HOURS", "ReusePolicy", "find_reusable"]
