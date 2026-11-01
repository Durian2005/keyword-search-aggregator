"""通道健康度 · 纯函数层。

分三步落地（2026-10-31）：
    Phase 0 —— pipeline 只写不读：先把"谁被拦过、今天打了几发"记准，
               用一轮真实任务核对无误，再让判定真的拦请求。
    Phase 1 —— decide() 接进 pipeline 的通道循环：冷却中 / 额度用尽就跳过，
               一个请求都不发，并写 channel_cooldown 告警。
    Phase 2 —— plan() 多出第三态 probe：冷却刚解除时先打一发探针（只打基词、
               只打一页），拿到真实结果才恢复正常配额，写 channel_probe 告警。
    回滚点很干净：把 plan() 换回 decide() 就回到 Phase 1，数据不用动。

为什么单独成模块而不是塞进 pipeline：
    冷却 6 小时 / 翻倍 / 封顶 24 小时的逻辑一旦直接写在 run_task 里，
    就只能靠手工改系统时间才能测。这里每个函数都接受 now 参数，
    单测可以"假设现在是某时刻"，不需要真的等。

判定口径（2026-10-31 实测，数据见 README 的「风控实测」）：
    - 百度不是频控：间隔 9 秒仍放行，调 min_interval_s 无效
    - 百度是额度池：见底时先缩量（5→3→1）再断供
    - 不是永久封禁：94 小时后回来是满血（108 个结果块）
    - 恢复上界观测到 25.3 小时（10-24 21:16 被拦 → 10-25 22:37 恢复）
    ⇒ 冷却按 6h 起步、连续被拦翻倍、24h 封顶。下界尚未测到，所以取保守值。

⚠️ 最容易错的一件事：**不是所有 blocked 都算被风控**。
    gdelt 在库里 20 次全是 blocked，但 kind=network（本机到不了它的服务器）。
    如果把这种也计冷却，它会永久卡在冷却里 —— 而且等再久也不会好。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

COOLDOWN_BASE_H = 6.0          # 首次被拦冷却
COOLDOWN_FACTOR = 2.0          # 连续被拦的倍数
COOLDOWN_MAX_H = 24.0          # 封顶（实测恢复上界 25.3h）

# 对方明确在拒绝我们 —— 只有这些计冷却
BLOCK_COOLDOWN = frozenset({
    "challenge",      # 验证码 / 风控页（百度 wappass、搜狗 antispider）
    "captcha",
    "http_403",
    "http_412",       # B站 -412 的 HTTP 形态
    "rate_limit",     # B站 code=-412 / -799
})

# 不算被拦：网络故障、我方看不懂、对方降级、真空结果
#   network/timeout/conn —— 等再久也不会好，计冷却只会把通道永久封死
#   relaxed/boilerplate  —— 请求成功，只是内容不可信
#   empty                —— 真的没结果
#   unknown_format       —— 版式变了，是我们的问题
#   api_error/parse_fail —— 未知错误码 / 解析异常，证据不足，保守不计
BLOCK_NEUTRAL = frozenset({
    "network", "timeout", "conn", "relaxed", "boilerplate", "empty",
    "unknown_format", "api_error", "parse_fail", "relaxed_page",
})

DAY_FMT = "%Y-%m-%d"


# ---------------------------------------------------------------------------
#  基础
# ---------------------------------------------------------------------------
def parse_iso(text: str | None) -> datetime | None:
    """ISO 字符串 → aware datetime。解析不了就返回 None（当"没有记录"处理）。

    不能抛：老数据/手改过的行不该让整个任务崩掉。
    """
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def local_day(moment: datetime) -> str:
    """本地日期（系统时区，本机即 UTC+8）。跨天清零按它算，不按 UTC。

    用 UTC 的话，北京时间早上 8 点前会算成"昨天"，额度会提前半天刷新。
    """
    return moment.astimezone().strftime(DAY_FMT)


def local_day_of_iso(text: str | None) -> str:
    """ISO 字符串 → 本地日期。解析不了返回空串（= 不匹配任何一天）。

    storage 记账时用它：把 when 和 day 一次算好，避免别处再用 when[:10] 猜 ——
    带时区偏移的 ISO 前 10 位恰好是本地日期，但那是巧合，不该依赖。
    """
    dt = parse_iso(text)
    return local_day(dt) if dt else ""


def human_delta(delta: timedelta) -> str:
    """时长 → "5h12m" / "43m" / "刚刚"。只给展示用。"""
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "刚刚"
    hours, rest = divmod(secs, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


# ---------------------------------------------------------------------------
#  分类与曲线
# ---------------------------------------------------------------------------
def classify_block(kind: str | None) -> str:
    """block_kind → "cooldown" | "neutral"。

    未知 kind 一律 neutral：看不懂就当没被拦，宁可少冷却也不要误封通道。
    """
    k = (kind or "").strip().lower()
    return "cooldown" if k in BLOCK_COOLDOWN else "neutral"


def cooldown_hours(streak: int, policy: "CooldownPolicy | None" = None) -> float:
    """连续被拦 streak 次之后的冷却时长。streak<=0 表示不冷却。

    1 → 6h  2 → 12h  3 → 24h  4+ → 24h（封顶）
    """
    if streak <= 0:
        return 0.0
    p = policy or CooldownPolicy()
    return min(p.base_hours * (p.factor ** (streak - 1)), p.max_hours)


# ---------------------------------------------------------------------------
#  策略：把"多久冷却一次、每个通道一天几发"变成可配置项
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class CooldownPolicy:
    """冷却曲线参数。默认值就是 2026-10-31 实测推出来的那组。"""

    base_hours: float = COOLDOWN_BASE_H
    factor: float = COOLDOWN_FACTOR
    max_hours: float = COOLDOWN_MAX_H

    @classmethod
    def from_mapping(cls, d: Mapping | None) -> CooldownPolicy:
        d = d or {}
        base = float(d.get("base_hours", COOLDOWN_BASE_H))
        factor = float(d.get("factor", COOLDOWN_FACTOR))
        cap = float(d.get("max_hours", COOLDOWN_MAX_H))
        # 配置写坏了也不能让通道永不冷却：factor < 1 会让曲线往下走，
        # max < base 会让第一次冷却就被截断成 0，两种都按默认值兜住。
        if factor < 1.0:
            factor = COOLDOWN_FACTOR
        if cap < base:
            cap = base
        return cls(base_hours=max(0.0, base), factor=factor, max_hours=max(0.0, cap))


@dataclass(slots=True, frozen=True)
class ProbePolicy:
    """冷却解除后的探针：先打一发，确认真的通了再恢复全量。

    为什么需要它：额度池见底时是"先缩量再断供"（实测 5→3→1→0），
    冷却一解除就按满额打，等于又把刚回满的池子一次打空 —— 结果是
    "一恢复就再挂"，永远拿不到完整数据。

    pages 是探针允许的翻页数。默认 1：翻页对额度池的消耗与换词同阶，
    探针的目的只是"问一句还能不能打"，不是一个请求。所以不翻页。
    """

    enabled: bool = True
    pages: int = 1

    @classmethod
    def from_mapping(cls, d: Mapping | None) -> ProbePolicy:
        d = d or {}
        try:
            pages = int(d.get("pages", 1))
        except (TypeError, ValueError):
            pages = 1
        # pages < 1 会让"探针"变成"不发请求"，那它就不是探针而是跳过。
        # 真要跳过有冷却和预算两道闸，不靠这里，所以钳到 1。
        return cls(enabled=bool(d.get("enabled", True)), pages=max(1, pages))


@dataclass(slots=True, frozen=True)
class HealthPolicy:
    """一个任务里所有与"还能不能打"相关的开关。

    quota 为空 + default_quota 为 None = 完全不限（Phase 0 的行为）。
    这也是默认值：不配就等同于"只管冷却、不管预算"。
    """

    cooldown: CooldownPolicy = CooldownPolicy()
    quota: Mapping[str, int] = field(default_factory=dict)
    default_quota: int | None = None
    probe: ProbePolicy = ProbePolicy()

    @classmethod
    def from_mapping(cls, d: Mapping | None) -> HealthPolicy:
        d = d or {}
        q = d.get("quota") or {}
        per = q.get("per_channel") or {}
        quota: dict[str, int] = {}
        for k, v in per.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue          # 写错的预算项直接跳过，不要因此让任务起不来
            if n > 0:
                quota[str(k)] = n
        default = q.get("default")
        try:
            default_n = int(default) if default is not None else None
        except (TypeError, ValueError):
            default_n = None
        return cls(
            cooldown=CooldownPolicy.from_mapping(d.get("cooldown")),
            quota=quota,
            default_quota=default_n if (default_n or 0) > 0 else None,
            probe=ProbePolicy.from_mapping(d.get("probe")),
        )

    def quota_for(self, channel: str) -> int | None:
        """该通道今日预算。None = 不限。"""
        return self.quota.get(channel, self.default_quota)


def decide(h: ChannelHealth, now: datetime,
           policy: HealthPolicy | None = None) -> tuple[bool, str]:
    """(是否跳过这个通道, 原因)。原因空串 = 放行。

    顺序有意义：先看冷却（硬约束，跟"今天打了几发"无关），再看预算。
    冷却中却先去查预算，会给出"额度还有 3 发"这种自相矛盾的说明。
    """
    policy = policy or HealthPolicy()
    cool, why = h.cooling(now, policy.cooldown)
    if cool:
        return True, why
    done, why = h.exhausted(now, policy.quota_for(h.channel))
    if done:
        return True, why
    return False, ""


# 三种放行程度。用字符串而不是 Enum：它只在本模块与 pipeline 之间传，
# 而字符串在日志里直接可读（`skip` / `probe` / `normal`），省一层转换。
MODE_SKIP = "skip"       # 一个请求都不发
MODE_PROBE = "probe"     # 只发一发探针
MODE_NORMAL = "normal"   # 按配置全量


def needs_probe(h: ChannelHealth, now: datetime,
                policy: HealthPolicy | None = None) -> bool:
    """现在该不该先打一发探针？

    判据是 `block_streak > 0` 且冷却已过 —— 因为 record_channel_ok() 会把 streak
    清零，所以 streak 大于 0 只可能代表"最后一次事件是被拦、之后还没成功过"，
    也就是"池子按模型算该回满了，但没被证实"。

    额外挡一道"成功时间晚于被拦时间"：正常流程走不到这里（成功就清零了），
    但历史回填或手改过的行可能有这种自相矛盾的记录，那种情况按已验证处理，
    不要把全量任务降级成探针。

    注意它**不判断冷却**：调用方（plan / pipeline）已经先过了 decide()。
    单独调用时若仍在冷却中，返回 True 也无害 —— 冷却优先，探针不会被发出。
    """
    policy = policy or HealthPolicy()
    if not policy.probe.enabled or h.block_streak <= 0:
        return False
    ok, blk = parse_iso(h.last_ok_at), parse_iso(h.last_block_at)
    if ok and blk and ok >= blk:
        return False
    return True


def plan(h: ChannelHealth, now: datetime,
         policy: HealthPolicy | None = None) -> tuple[str, str]:
    """(mode, 原因)，mode ∈ {skip, probe, normal}。原因空串 = 正常放行。

    这是 pipeline 通道循环唯一该调的函数 —— 它在 decide() 外面只多做一件事：
    把"冷却刚解除"从"正常"里挑出来，降级成探针。

    顺序仍然重要：冷却 → 预算 → 探针。冷却中就说"还能打几发"是自相矛盾的，
    预算用尽时说"先打一发探针"同样自相矛盾（那一发也是要花额度的）。
    """
    policy = policy or HealthPolicy()
    skip, why = decide(h, now, policy)
    if skip:
        return MODE_SKIP, why
    if needs_probe(h, now, policy):
        return MODE_PROBE, (
            f"冷却已解除（连续被拦 {h.block_streak} 次），"
            f"本次只打基词 {policy.probe.pages} 页当探针，通了下次才恢复全量")
    return MODE_NORMAL, ""


# ---------------------------------------------------------------------------
#  一个通道的完整状态
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class ChannelHealth:
    """channel_state 表的一行。字段全部可缺省 = "从没跑过/没记录"。"""

    channel: str
    last_ok_at: str | None = None
    last_block_at: str | None = None
    last_block_kind: str | None = None
    block_streak: int = 0
    day: str | None = None            # 上次记账的本地日期
    used_today: int = 0               # 那一天的请求数
    note: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping) -> ChannelHealth:
        """从 sqlite Row（或 dict）构造。缺列按默认值走，不 KeyError。"""
        get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k])
        return cls(
            channel=str(get("channel") or ""),
            last_ok_at=get("last_ok_at"),
            last_block_at=get("last_block_at"),
            last_block_kind=get("last_block_kind"),
            block_streak=int(get("block_streak") or 0),
            day=get("day"),
            used_today=int(get("used_today") or 0),
            note=get("note"),
        )

    # ---- 冷却 ----
    @property
    def cooldown_h(self) -> float:
        return cooldown_hours(self.block_streak)

    def allowed_at(self, now: datetime,
                   policy: CooldownPolicy | None = None) -> datetime | None:
        """冷却结束的时刻；None = 当前不在冷却状态。"""
        if self.block_streak <= 0:
            return None
        start = parse_iso(self.last_block_at)
        if start is None:
            return None
        return start + timedelta(hours=cooldown_hours(self.block_streak, policy))

    def cooling(self, now: datetime,
                policy: CooldownPolicy | None = None) -> tuple[bool, str]:
        """(是否冷却中, 给人看的原因)。Phase 1 用它决定要不要跳过通道。"""
        at = self.allowed_at(now, policy)
        if at is None or now >= at:
            return False, ""
        when = (self.last_block_at or "")[:16].replace("T", " ")
        return True, (f"冷却中（剩余 {human_delta(at - now)}，"
                      f"上次被拦 {when}，连续 {self.block_streak} 次）")

    # ---- 用量 ----
    def used(self, now: datetime) -> int:
        """今日已用请求数。不是今天记的账一律算 0 —— 跨天自动清零，不需要定时任务。"""
        return self.used_today if self.day == local_day(now) else 0

    def quota_left(self, now: datetime, quota: int | None) -> int | None:
        """今日剩余额度。None = 没设预算（不限）。

        quota 必须是正数才有意义；0 与负数按"不限"处理，避免把通道锁死。
        """
        if not quota or quota <= 0:
            return None
        return max(0, quota - self.used(now))

    def exhausted(self, now: datetime, quota: int | None) -> tuple[bool, str]:
        """(今日额度是否用尽, 原因)。Phase 1 的第二道闸。"""
        left = self.quota_left(now, quota)
        if left is None or left > 0:
            return False, ""
        return True, f"今日额度已用完（{self.used(now)}/{quota} 发）"

    def summary(self, now: datetime, quota: int | None = None) -> str:
        """一行摘要，给 CLI / 报告表格用。"""
        bits: list[str] = []
        cool, why = self.cooling(now)
        if cool:
            bits.append(why)
        left = self.quota_left(now, quota)
        bits.append(f"今日 {self.used(now)} 发" + (f" / {quota}" if left is not None else ""))
        if self.last_ok_at:
            bits.append("上次成功 " + self.last_ok_at[:16].replace("T", " "))
        if self.block_streak:
            bits.append(f"连续被拦 {self.block_streak}")
        return " · ".join(bits)


__all__ = [
    "BLOCK_COOLDOWN",
    "BLOCK_NEUTRAL",
    "COOLDOWN_BASE_H",
    "COOLDOWN_FACTOR",
    "COOLDOWN_MAX_H",
    "MODE_NORMAL",
    "MODE_PROBE",
    "MODE_SKIP",
    "ChannelHealth",
    "CooldownPolicy",
    "HealthPolicy",
    "ProbePolicy",
    "classify_block",
    "cooldown_hours",
    "decide",
    "human_delta",
    "local_day",
    "local_day_of_iso",
    "needs_probe",
    "parse_iso",
    "plan",
]
