"""栏目列表页 → 文章链接。**不碰网络**（契约与 extract 其它模块相同）。

为什么单独一个模块：百度 `姓名 site:官网域` 会返回栏目列表页 —— 列表里链着
目标文章，搜索引擎就把整页算成相关。列表页本身不是"一条结果"（点开是一屏目录），
但**它链着的文章是**。所以处理方式不是丢页，而是把页里相关的文章捞出来当结果：
链接从此直指正文（2026-10-21 用户原话：「我只是增加要求尽量能够跳转到原文章」）。

判定"相关"用与 serp 过滤（discover.quality.needles_of）同一套词元口径：
锚文本里含任一 needle。只做一层：列表页链着另一个列表页时不跟进
（由调用方用 reject 谓词挡掉），否则「院系风采 → 学术动态 → …」会没完没了。
"""
from __future__ import annotations

import re
from typing import Callable, Sequence
from urllib.parse import urldefrag, urljoin

from kwsearch.htmlx import iter_nodes, parse_html, text

# 锚文本上限：超过这个长度通常是整块导航/页脚被误当成一个链接
_MAX_LABEL = 120

_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "sms:", "data:")


def article_links(
    html: str,
    needles: Sequence[str],
    base_url: str = "",
    *,
    limit: int = 20,
    reject: Callable[[str], bool] | None = None,
) -> list[tuple[str, str]]:
    """从列表页 HTML 抽 `(文章URL, 标题)`，按文档顺序、去重、限量。

    needles  与 needles_of 同口径的词元表；空表直接返回空（不猜"全都相关"）
    base_url 列表页自己的 URL，用于把 `/2021/0525/xxx/page.htm` 这类相对链接补齐
    reject   可选谓词：对解析出的文章 URL 返回 True 则跳过（调用方用来挡列表页自身）
    """
    if not html or not needles:
        return []
    pat = re.compile("|".join(re.escape(n) for n in needles if n), re.I)
    if not pat.pattern:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in iter_nodes(parse_html(html)):
        if node.tag != "a":
            continue
        href = (node.attrs.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        low = href.lower()
        if low.startswith(_SKIP_SCHEMES):
            continue
        label = " ".join((text(node) or "").split())
        if not label or len(label) > _MAX_LABEL:
            continue
        if not pat.search(label):
            continue
        url = urljoin(base_url, href) if base_url else href
        url = urldefrag(url).url                 # 去掉 #锚点；查询串保留（?id= 有时是文章标识）
        if not url.lower().startswith(("http://", "https://")):
            continue
        if reject is not None and reject(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, label))
        if len(out) >= limit:
            break
    return out


__all__ = ["article_links"]
