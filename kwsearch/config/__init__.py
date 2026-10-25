"""配置层：channels.yaml → 带校验的 dataclass。

只做三件事：
  1) 读完 YAML 立刻校验（缺字段、类型不对、parser 重名、id 重名）
  2) 把 URL 模板渲染成真实 URL（关键词 URL 编码在这里统一处理）
  3) 判断通道"当前是否可用"（requires=key 但没给 key → 跳过，不是报错）

刻意不在这里检查 parser 是否已实现 —— 那会让 config 反过来依赖 discover，
形成环。可用性检查放在 CLI 里做。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

DEFAULT_CONFIG = Path(__file__).with_name("channels.yaml")

VALID_TYPES = {"serp", "site_search", "structured_api"}
VALID_REQUIRES = {"none", "key"}


class ConfigError(Exception):
    pass


@dataclass(slots=True)
class PagingConfig:
    mode: str = "none"          # offset | none
    param: str = "page"
    start: int = 1
    step: int = 1
    max_pages: int = 1

    def offsets(self) -> list[int]:
        if self.mode == "none":
            return [0]
        return [self.start + i * self.step for i in range(max(1, self.max_pages))]


@dataclass(slots=True)
class AssertionConfig:
    min_items: int = 1
    required_fields: list[str] = field(default_factory=lambda: ["url", "title"])


@dataclass(slots=True)
class ChannelConfig:
    id: str
    name: str
    type: str
    enabled: bool = True
    requires: str = "none"
    parser: str = ""
    url_template: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)     # 模板里的额外占位符
    paging: PagingConfig = field(default_factory=PagingConfig)
    throttle_min_interval_s: float = 1.0
    assertions: AssertionConfig = field(default_factory=AssertionConfig)
    # 通道级抓取覆盖（timeout / retry）。空 dict = 完全沿用 defaults。
    # 存在的意义：GDELT 单次要 13s，全局 20s×3 次的"快速任务"被拖到 1 分钟；
    #   而全局收紧又会饿死慢站。超时是通道属性，不是全局属性。
    fetch_over: dict[str, Any] = field(default_factory=dict)
    # 是否参与查询变体矩阵。默认 true；false = 只打基词一次。
    # 为什么需要这个开关：百度第二次请求就出图形验证码（实测 2026-10-21），
    # 变体矩阵对它不是扩量而是自杀。这类"单次配额"通道必须显式关掉。
    variants: bool = True
    # 中转链解链参数（enabled / max_links / delay_s / stop_after_fail）。
    # 空 dict = 不解链，候选保留通道给的原始 URL。见 sogou_weixin 的同名配置。
    resolve: dict[str, Any] = field(default_factory=dict)
    robots_checked: str = ""
    robots_note: str = ""

    def offsets(self) -> list[int]:
        return self.paging.offsets()

    def build_url(self, keyword: str, offset: int = 0, timespan: str | None = None) -> str:
        """渲染 URL 模板。关键词在这里做 URL 编码（统一一次，各处不再手写 quote）。"""
        q = quote(keyword, safe="")
        tmpl = self.url_template
        if "{timespan}" in tmpl:
            tmpl = tmpl.replace("{timespan}", timespan or self.params.get("timespan", "30d"))
        for k, v in self.params.items():
            tmpl = tmpl.replace("{" + k + "}", v)
        return tmpl.replace("{q}", q).replace("{offset}", str(offset))

    def unavailable_reason(self, env: dict[str, str] | None = None) -> str | None:
        """返回 None = 可用；否则返回不可用原因（给 CLI 展示，不抛异常）。"""
        env = env if env is not None else os.environ
        if self.requires == "key":
            key_env = f"KWSEARCH_{self.id.upper()}_KEY"
            if not env.get(key_env):
                return f"缺少 API key（环境变量 {key_env}）"
        return None


@dataclass(slots=True)
class AppConfig:
    version: int
    defaults: dict[str, Any]
    channels: list[ChannelConfig]
    crawl: dict[str, Any]
    extract: dict[str, Any]
    filter: dict[str, Any]
    limits: dict[str, Any]
    expand: dict[str, Any] = field(default_factory=dict)   # 查询变体矩阵
    cluster: dict[str, Any] = field(default_factory=dict)  # S5 近重复聚簇

    def channel(self, cid: str) -> ChannelConfig:
        for c in self.channels:
            if c.id == cid:
                return c
        raise KeyError(f"未配置的通道: {cid}")

    def enabled_channels(self) -> list[ChannelConfig]:
        return [c for c in self.channels if c.enabled]


def _build_channel(raw: dict[str, Any], idx: int) -> ChannelConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"channels[{idx}] 不是映射")
    for must in ("id", "type", "parser", "url_template"):
        if not raw.get(must):
            raise ConfigError(f"channels[{idx}] 缺少必填字段 {must}")
    if raw["type"] not in VALID_TYPES:
        raise ConfigError(f"[{raw['id']}] type={raw['type']} 非法，可选 {sorted(VALID_TYPES)}")
    if raw.get("requires", "none") not in VALID_REQUIRES:
        raise ConfigError(f"[{raw['id']}] requires 非法，可选 {sorted(VALID_REQUIRES)}")

    pg_raw = raw.get("paging") or {}
    paging = PagingConfig(
        mode=pg_raw.get("mode", "none"),
        param=pg_raw.get("param", "page"),
        start=int(pg_raw.get("start", 1)),
        step=int(pg_raw.get("step", 1)),
        max_pages=int(pg_raw.get("max_pages", 1)),
    )
    if paging.mode not in ("offset", "none"):
        raise ConfigError(f"[{raw['id']}] paging.mode 只支持 offset/none")

    as_raw = raw.get("assertions") or {}
    assertions = AssertionConfig(
        min_items=int(as_raw.get("min_items", 1)),
        required_fields=list(as_raw.get("required_fields", ["url", "title"])),
    )

    throttle = raw.get("throttle") or {}
    fetch_over = raw.get("fetch") or {}
    if not isinstance(fetch_over, dict):
        raise ConfigError(f"[{raw['id']}] fetch 必须是映射（timeout / retry）")
    return ChannelConfig(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        type=raw["type"],
        enabled=bool(raw.get("enabled", True)),
        requires=raw.get("requires", "none"),
        parser=raw["parser"],
        url_template=raw["url_template"],
        headers=dict(raw.get("headers") or {}),
        params={k: str(v) for k, v in (raw.get("params") or {}).items()},
        paging=paging,
        throttle_min_interval_s=float(throttle.get("min_interval_s", 1.0)),
        assertions=assertions,
        fetch_over=dict(fetch_over),
        variants=bool(raw.get("variants", True)),
        resolve=dict(raw.get("resolve") or {}),
        robots_checked=str(raw.get("robots_checked", "")),
        robots_note=str(raw.get("robots_note", "")),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise ConfigError(f"配置文件不存在: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    raw_channels = data.get("channels") or []
    if not raw_channels:
        raise ConfigError("channels 不能为空")

    channels = [_build_channel(c, i) for i, c in enumerate(raw_channels)]

    seen_id: set[str] = set()
    for c in channels:
        if c.id in seen_id:
            raise ConfigError(f"通道 id 重复: {c.id}")
        seen_id.add(c.id)

    return AppConfig(
        version=int(data.get("version", 1)),
        defaults=dict(data.get("defaults") or {}),
        channels=channels,
        crawl=dict(data.get("crawl") or {}),
        extract=dict(data.get("extract") or {}),
        filter=dict(data.get("filter") or {}),
        limits=dict(data.get("limits") or {}),
        expand=dict(data.get("expand") or {}),
        cluster=dict(data.get("cluster") or {}),
    )
