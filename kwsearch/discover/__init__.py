"""发现层：关键词 → 候选 URL。

注册表在 import 时自动填充（各通道模块末尾 import 触发）。新增通道的完整步骤：
    1) 在 channels.yaml 里加一段配置
    2) 在 discover/ 新建 xxx.py，用 @register("xxx", parse=parse_page) 装饰通道函数
    3) 在下面 _IMPL_MODULES 里登记模块名 —— 只有这一处需要动既有代码

通道函数签名统一为：
    fn(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult

parse 函数签名统一为（离线复跑用，不碰网络、不读配置）：
    parse(html: str, offset: int, *, challenge_keywords=(), channel_id=...) -> ChannelResult

为什么把 parse 单独登记：SERP 的选择器会随改版失效，排查时必须能用 probe --save
存下的旧样本反复复跑解析，不能每次都打一遍线上。
"""
from __future__ import annotations

import importlib
from typing import Callable

from kwsearch.models import ChannelResult

ChannelFn = Callable[..., ChannelResult]
ParseFn = Callable[..., ChannelResult]

_REGISTRY: dict[str, ChannelFn] = {}
_PARSE_REGISTRY: dict[str, ParseFn] = {}

# 已存在的实现模块。加通道就在这行加名字。
_IMPL_MODULES = [
    "kwsearch.discover.bing",
    "kwsearch.discover.bilibili",
    "kwsearch.discover.gdelt",
    "kwsearch.discover.searxng",
]


def register(name: str, *, parse: ParseFn | None = None) -> Callable[[ChannelFn], ChannelFn]:
    def deco(fn: ChannelFn) -> ChannelFn:
        if name in _REGISTRY:
            raise RuntimeError(f"发现通道注册名重复: {name}")
        _REGISTRY[name] = fn
        if parse is not None:
            _PARSE_REGISTRY[name] = parse
        return fn

    return deco


def _load() -> None:
    for mod in _IMPL_MODULES:
        try:
            importlib.import_module(mod)
        except NotImplementedError:  # 占位模块不该阻断其它通道加载
            continue


_load()


def get_channel(name: str) -> ChannelFn:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的发现通道: {name}（已注册: {sorted(_REGISTRY)}）")
    return _REGISTRY[name]


def get_parser(name: str) -> ParseFn | None:
    """离线解析器。None = 该通道还没提供（通常是还没实现，或本来就是纯 JSON 接口）。"""
    return _PARSE_REGISTRY.get(name)


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "register",
    "get_channel",
    "get_parser",
    "registered_names",
    "_REGISTRY",
    "_PARSE_REGISTRY",
]
