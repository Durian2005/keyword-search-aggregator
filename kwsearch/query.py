"""查询变体：一个关键词 → 一小组查询。

为什么需要它：Bing 的匿名访问只给第一页 10 条，且翻页参数失效（见 discover/bing.py），
一个词就 10 条，报告会单薄得没有意义。唯一的扩量手段是换查询词。

但"加后缀"不是万灵药。2026-10-18 实测"红黑树"（每次 2s 间隔，共 13 次请求）：

    红黑树                    新增 10   ← 基词
    红黑树 左旋 右旋            新增 10   ← 具体技术动作，最好用
    红黑树 实现                新增 10
    red black tree            新增  9   ← 跨语言术语（需人工给，见下）
    红黑树 java                新增  8   ← 技术栈
    红黑树 原理                新增  5
    红黑树 图解                新增  0   ← 这三类"泛化后缀"实测完全无效
    红黑树 时间复杂度            新增  0
    红黑树 与 AVL 的区别         新增  0
    红黑树 教程 / 面试 / 优缺点 / 应用场景 / 是什么   全部新增 0

结论：**变体要按实测增益排序**，并且必须支持早停 —— 连续两个变体零新增就停，
否则会白白打 5 次请求换回同一批结果。这正是 expand.py 的做法。

为什么不做自动跨语言扩展：那需要翻译，翻译错了会引入大量无关结果。
英文术语走 extra_queries 人工补，宁少不乱。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class QueryVariant:
    q: str
    strategy: str          # base | suffix | extra
    priority: int          # 0 = 基词，越大越靠后

    @property
    def is_base(self) -> bool:
        return self.strategy == "base"


def query_key(q: str) -> str:
    """判断"是不是同一个查询"用的归一：折叠空白 + 忽略大小写。

    刻意不做同义词/繁简归并：那属于语义层，做错了会把不同查询悄悄合并。
    """
    return _WS_RE.sub(" ", (q or "").strip()).casefold()


def expand_queries(base: str, spec: Mapping[str, Any] | None = None) -> list[QueryVariant]:
    """生成查询变体列表。第一个永远是基词。

    spec（来自 channels.yaml 的 expand 段）：
        enabled        false 时只返回基词
        max_queries    含基词在内的总上限，默认 8
        separator      基词与后缀的连接符，默认一个空格
        suffixes       修饰词列表，按实测增益降序排列
        extra_queries  人工补充的整条查询（如英文术语）
    """
    spec = spec or {}
    base = (base or "").strip()
    if not base:
        return []

    first = QueryVariant(q=base, strategy="base", priority=0)
    if not spec.get("enabled", True):
        return [first]

    sep = str(spec.get("separator", " "))
    max_queries = max(1, int(spec.get("max_queries", 8)))

    seen = {query_key(base)}
    out = [first]

    def _add(q: str, strategy: str) -> None:
        q = _WS_RE.sub(" ", q.strip())
        key = query_key(q)
        if not q or key in seen or len(out) >= max_queries:
            return
        seen.add(key)
        out.append(QueryVariant(q=q, strategy=strategy, priority=len(out)))

    for suffix in _as_list(spec.get("suffixes")):
        _add(base + sep + suffix, "suffix")
    for q in _as_list(spec.get("extra_queries")):
        _add(q, "extra")

    return out


def _as_list(v: Any) -> Iterable[str]:
    if not v:
        return ()
    if isinstance(v, str):
        return (v,)
    return [str(x) for x in v if str(x).strip()]
