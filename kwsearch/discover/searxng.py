"""SearXNG 自建实例（适配器位，默认关闭）。

为什么默认关（实测 2026-10-18，别重复踩）：
    1) 本机没有 Docker：无 Docker Desktop 安装目录，PATH 里也没有 docker 命令
    2) WSL 被安全策略拦截，wsl.exe 无法启动
    3) 更关键：本机网络到不了 Google / DuckDuckGo（lite.duckduckgo.com 实测
       CONNECT tunnel 502），而 SearXNG 的主要价值就是转这两个源
    -> 剩下能用的只有 Bing（已直连，用 bing_html 更省事），
       外加一层"统一限流"的收益，不值得为它引一套容器环境

将来条件满足（装了 Docker 且网络可达）时：
    docker run -d --name searxng -p 8080:8080 searxng/searxng
    并把 channels.yaml 里 searxng.enabled 改成 true —— parser 名不用改。
    ⚠️ 记得在实例的 settings.yml 里打开 search.formats: [json]，
       否则 format=json 会返回 403。
"""
from __future__ import annotations

from kwsearch.discover import register
from kwsearch.discover.base import ChannelContext
from kwsearch.models import ChannelResult


@register("searxng")
def searxng(keyword: str, offset: int, ctx: ChannelContext) -> ChannelResult:
    raise NotImplementedError("SearXNG 未部署；开启前先确认 format=json 已放行")
