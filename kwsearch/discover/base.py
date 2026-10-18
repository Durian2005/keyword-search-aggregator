"""发现层契约。

约定（比前作的 parser 约定更严，因为这里是"整个任务会不会一无所获"的关键）：
  - 只允许返回 Candidate，不允许顺手抓正文
  - 必须如实报告 blocked / block_kind / raw_count，不允许把"被拦了"伪装成"没有结果"
    （这两件事在报告里的含义完全相反：前者是覆盖缺口，后者是文档缺失）
  - 不允许在通道内部做限速与任务级重试：那属于驱动层（CLI / Pipeline）
  - 通道必须可离线：解析逻辑放在独立的 parse_*(html, ...) 里，
    register 时一并登记，这样没有网络也能复跑同一份样本

为什么要 ChannelContext 而不是直接传 AppConfig：
    通道真正需要的只有 4 样东西（自己的配置、默认头、重试策略、fetcher），
    把整个 AppConfig 传进去会让通道有能力去读 offsets/limits 之类不该它管的东西，
    也会让人误以为通道可以自己决定翻几页。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kwsearch.config import AppConfig, ChannelConfig
from kwsearch.fetch import BaseFetcher, RetryPolicy, build_fetcher, default_headers
from kwsearch.models import ChannelResult

# 断言里的字段名 → Candidate 的真实属性名
_FIELD_ALIAS = {"url": "url_raw", "date": "published_hint"}


@dataclass(slots=True)
class ChannelContext:
    """一次通道调用需要的全部外部条件。显式传参，不搞全局单例。"""

    channel: ChannelConfig
    defaults: dict
    extract: dict
    fetcher: BaseFetcher
    policy: RetryPolicy
    timeout: int = 20

    @property
    def headers(self) -> dict[str, str]:
        """默认头 + 通道自己的头。通道指定的头优先级更高（如 B站 的 Referer）。"""
        h = default_headers(self.defaults)
        h.update(self.channel.headers)
        return h

    @property
    def challenge_keywords(self) -> tuple[str, ...]:
        return tuple(k for k in (self.extract.get("challenge_keywords") or ()) if k)


def build_context(cfg: AppConfig, channel_id: str, *,
                  fetcher: BaseFetcher | None = None) -> ChannelContext:
    ch = cfg.channel(channel_id)
    return ChannelContext(
        channel=ch,
        defaults=dict(cfg.defaults),
        extract=dict(cfg.extract),
        fetcher=fetcher or build_fetcher(),
        policy=RetryPolicy.from_mapping(cfg.defaults.get("retry")),
        timeout=int(cfg.defaults.get("timeout", 20)),
    )


def check_assertions(result: ChannelResult, ch: ChannelConfig) -> list[str]:
    """返回断言违例清单；空列表 = 通过。

    只报告、不抛异常：一条通道的结构变了，不该把整个任务炸掉 ——
    报告里"这条通道这次没拿到东西"本身就是有效信息。
    """
    problems: list[str] = []
    n = len(result.candidates)
    if n < ch.assertions.min_items:
        problems.append(f"条目不足：{n} < min_items={ch.assertions.min_items}")
    for field in ch.assertions.required_fields:
        attr = _FIELD_ALIAS.get(field, field)
        missing = sum(1 for c in result.candidates if not getattr(c, attr, None))
        if missing:
            problems.append(f"{missing} 条缺 {field}")
    return problems


def page_signature(result: ChannelResult) -> frozenset[str]:
    """一页结果的指纹（全部 url_raw）。

    驱动层用它识别"翻页退化成同一页"：Bing 的分页参数失效时不会报错，
    只是把第 1 页原样再给一遍 —— 不去比对指纹，就会白打 N 次请求还以为翻到了新内容。
    """
    return frozenset(c.url_raw for c in result.candidates)


@runtime_checkable
class Channel(Protocol):
    """一个发现通道 = 一个可调用对象。"""

    id: str

    def __call__(self, keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:  # noqa: ANN001
        ...
