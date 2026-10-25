"""层间契约。所有跨层传递的数据只能长这样。

为什么单独一个文件：前作的经验是，层与层之间一旦互相引用对方的内部结构，
改一处就全线动。这里只放"数据结构"，不放任何行为（除序列化辅助）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    """统一时间格式（带时区偏移），避免"本地时间 vs UTC"的歧义。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class Candidate:
    """发现层产出物：一条"可能相关、还没抓"的链接。

    此时只有搜索引擎给的信息（标题/摘要/链接），正文一律还没拿到。
    """

    url_raw: str                    # 通道原样给的链接（可能带 utm / 跳转中转）
    url_canon: str | None = None    # 规范化后的链接，聚合层填
    title: str = ""
    snippet: str = ""               # SERP 摘要，用于 0 成本相关度预筛
    channel: str = ""               # 来源通道 id
    rank: int = 0                   # 该通道内的出现序号（1 起）
    published_hint: str | None = None   # 通道直接给的日期（Bing RSS 的 pubDate / B站 pubdate）
    # 跨跑批业务去重键。URL 会轮换/带 token 的通道（如搜狗微信的中转链）用它：
    # 与 URL 无关、同一篇文章每次解析都相同。空 = 该通道不提供，退回按 URL 去重。
    dedupe_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        # slots=True 的 dataclass 没有 __dict__，必须走 asdict
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class Doc:
    """抽取层产出物：一篇已抓取、已结构化的文档。

    刻意不存正文全文：summary ≤ 500 字 + 全文另存 bodies/ 目录（默认关闭）。
    这既是库不膨胀，也是版权上更干净（不二次分发全文）。
    """

    url_canon: str
    url_raw: str = ""
    domain: str = ""
    title: str = ""
    summary: str = ""
    published_at: str | None = None
    # 时间来源可信度：jsonld | meta | regex | serp | fallback
    # ⚠️ 必须是 fallback 时，报告里要显式标注"时间不可信"，不许冒充真实发布时间
    time_source: str = "fallback"
    fetched_at: str = field(default_factory=utcnow_iso)
    channel: str = ""
    http_status: int | None = None
    content_type: str = ""
    body_chars: int = 0             # 正文长度：0 = 还没抓正文（S4），或空壳页/JS 站
    body_core: int = 0              # 剥掉首尾转载模板后的正文长度；0 = 未知（老数据）
                                    # ⚠️ 代表选取按它排，不按 body_chars：转载版挂一段
                                    #    免责声明就比原文长，按原始长度选会把二手站选成代表
    simhash: int | None = None      # 64bit，聚合层算
    cluster_id: int | None = None
    body_ref: str | None = None     # 全文落盘相对路径，None = 未存
    # 跨跑批业务去重键（见 schema.sql 该列注释）。空 = 该通道不提供。
    dedupe_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Mention:
    """关键词 × 文档 的一次命中记录。顶替 前作 里 snapshot 的位置。"""

    task_id: str
    url_canon: str
    hit_title: int = 0              # 标题命中次数
    hit_body: int = 0
    exact_phrase: bool = False      # 是否出现完整短语（区分"苹果手机"和"苹果 手机"）
    bm25: float = 0.0
    score: float = 0.0              # 最终排序分（已含标题权重/位置权重）


@dataclass(slots=True)
class ChannelResult:
    """一个通道一次查询的返回。既带数据，也带"这次到底发生了什么"。"""

    channel: str
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    raw_count: int = 0              # 通道声称返回了多少（如 B站的 numResults）
    blocked: bool = False           # 被限流/挑战页/需要登录
    block_kind: str | None = None   # http_403 | challenge | captcha | rate_limit | empty
    detail: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.candidates) and not self.blocked
