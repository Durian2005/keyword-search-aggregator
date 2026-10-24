"""入库前的最后一道闸：正文脱敏 + 丢弃判定。

为什么单独一个模块：这两件事一旦散落在 pipeline 里，就一定会漏。
前作的教训是"过滤器写在采集循环里，后来加了第二个通道就忘了套"。
这里做成一个对象，pipeline 只有一个入口 —— `sanitizer.scrub()` / `sanitizer.check_url()`。

三条规则来自 channels.yaml 的 filter 段：

  pii_patterns   命中即脱敏。手机号、身份证、邮箱原样入库会把报告变成
                 人肉搜索工具 —— 这是**报告质量**规则，2026-10-22 起不再是红线。
  drop_url_patterns  .zip/.exe/下载页 —— 不是"信息"，抓了也无用，还容易踩版权
  max_per_domain   单域配额。不加这条，一个知乎/CSDN 就能占满整份报告，
                   读者会误以为"全网都在讨论"，其实只是某个站的站内搜索排名好。
  block_host_suffixes  **项目唯一红线（2026-10-22 用户修订）：不抓政府网站**。
                   默认 [".gov.cn"]，命中即丢 + 抓取层同步硬拦（双保险）。
  列表页/站点首页  内置规则（不走配置）：栏目列表页点进去是一屏文章目录，
                   站点首页点进去是站门口，都不是"一条结果"。百度 site: 查询
                   尤其爱返回这些（列表页里链着目标文章，就被搜索引擎算相关）。

⚠️ 脱敏 vs 丢弃是两种不同处理，别混：
   脱敏 = 内容还有价值，只把敏感片段遮掉（如"联系 138****5678"）
   丢弃 = 内容本身就不该收（下载链接、拉黑站）
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

# 每类 PII 的遮罩函数。保留多少位是权衡过的：
#   手机号 保留前 3 后 4（能看出归属地段，不能拨通）
#   身份证 保留前 4 后 3（能看出省份年份，不能定位到人）
#   邮箱   保留 local 前 2 位 + 完整域名（能看出是哪家邮箱，不能发信）
_Masker = Callable[[re.Match[str]], str]


def _mask_phone(m: re.Match[str]) -> str:
    s = m.group(0)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def _mask_id_card(m: re.Match[str]) -> str:
    s = m.group(0)
    return s[:4] + "*" * (len(s) - 7) + s[-3:]


def _mask_email(m: re.Match[str]) -> str:
    s = m.group(0)
    local, _, domain = s.partition("@")
    # 取 local 的前 2 个"字"，跳过点/下划线 —— 否则 "a.b@x.com" 会遮成 "a.***@x.com"，
    # 看着像保留了半个词，实际什么也没遮住
    keep = re.sub(r"[^\w]", "", local)[:2]
    return f"{keep}***@{domain}"


# 顺序有意义：身份证先处理，避免 18 位里的一段被手机号规则先切走。
_BUILTIN_MASKERS: dict[str, _Masker] = {
    "id_card_cn": _mask_id_card,
    "phone_cn": _mask_phone,
    "email": _mask_email,
}

# 配置里没写、但实测很常见的一类：URL 里带手机号（如 ?tel=138...）
_FALLBACK_PATTERNS: dict[str, str] = {
    "phone_cn": r"\b1[3-9]\d{9}\b",
    "id_card_cn": r"\b\d{17}[\dXx]\b",
    "email": r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
}

# ---------------------------------------------------------------- 政府站红线
# 项目唯一红线（2026-10-22 用户修订）：**不抓政府网站**。
# 判定只看 host 后缀，宁可误杀不可漏放 —— .gov.cn 下的任何站点都不碰。
# 历史红线（登录态/代理池/存全文/恶意行为禁令）同日作废，见 fetch/robots.py 顶部。
DEFAULT_BLOCK_HOST_SUFFIXES: tuple[str, ...] = (".gov.cn",)


def is_blocked_host(host: str, suffixes: tuple[str, ...] | list[str] | None = None) -> bool:
    """host 是否命中红线后缀（政府网站）。大小写不敏感，允www. 前缀。

    双保险用法：Sanitizer.check_url 丢弃结果 + fetch 层拒绝发请求。
    """
    h = (host or "").strip().lower()
    if not h:
        return False
    if h.startswith("www."):
        h = h[4:]
    for suf in (suffixes if suffixes is not None else DEFAULT_BLOCK_HOST_SUFFIXES):
        s = str(suf).strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        bare = s.lstrip(".")
        if h == bare or h.endswith(s):
            return True
    return False

# ---------------------------------------------------------------- 列表页判定
# 栏目列表页与站点首页的 URL 形态。用户点开一条结果要看到的是正文，而这些页面
# 点进去是一屏文章目录（甚至就是站门口）。百度 `site:官网域` 查询尤其爱返回它们：
# 列表页里链着目标文章，搜索引擎就把整页算成相关。
#
# 判定只看**路径最后一段**：剥扩展名、剥尾部数字后查表 —— 整段精确匹配，
# 不做子串匹配（"my-list-of-tools" 不是列表页）。
# 真实样本（某某大学计算机学院官网，百度 site: 查询返回）：
#   /xsjs/list38.htm  /677/list3.htm  /_s2/677/list.psp  /55/list21.psp
#   /85/list1.htm    /53/list19.psp  /677/list50.psp
# 同站文章页形态是 /2021/0525/c282a3495/page.htm —— "page" 不在表里，不会误伤。
_LIST_WORDS = frozenset({
    "list", "column", "channel", "category", "cat", "topic", "tag", "tags",
})
_HOME_WORDS = frozenset({"index", "default", "home", "main"})


def _path_tail(url: str) -> str:
    """URL 路径最后一段：剥扩展名、剥尾部数字、转小写。"""
    seg = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if "." in seg:
        seg = seg.rsplit(".", 1)[0]
    return re.sub(r"\d+$", "", seg).lower()


def is_list_url(url: str) -> bool:
    """栏目列表页或站点首页 → True（这类页面没有"一条结果"的价值）。

    只认形态不猜内容：路径为空（https://a.com）或仅 "/" 视为站点首页；
    最后一段命中列表词/首页词视为栏目页。机构官网探测**不走这里**——
    那个场景恰恰需要首页来认域名。
    """
    if not url:
        return False
    if urlsplit(url).path in ("", "/"):
        return True
    tail = _path_tail(url)
    return tail in _LIST_WORDS or tail in _HOME_WORDS


class Sanitizer:
    """按 filter 配置做脱敏与丢弃判定。线程内复用，无状态（除配置）。"""

    __slots__ = ("_rules", "_drop_domains", "_drop_url_re", "max_per_domain",
                 "block_suffixes")

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        spec = spec or {}

        # 配置里给了 pii_patterns 就用配置的；没给的类别用内置兜底，保证"不会漏一类"
        configured = dict(spec.get("pii_patterns") or {})
        self._rules: list[tuple[str, re.Pattern[str], _Masker]] = []
        for name in ("id_card_cn", "phone_cn", "email"):
            masker = _BUILTIN_MASKERS[name]
            pattern = configured.get(name) or _FALLBACK_PATTERNS[name]
            try:
                self._rules.append((name, re.compile(pattern), masker))
            except re.error:
                # 配置写错正则不该让整个任务起不来：退到内置规则，宁可遮得粗一点
                self._rules.append((name, re.compile(_FALLBACK_PATTERNS[name]), masker))

        self._drop_domains = {
            str(d).strip().lower().lstrip(".") for d in (spec.get("drop_domains") or ())
            if str(d).strip()
        }
        self._drop_url_re = [
            re.compile(str(p), re.I) for p in (spec.get("drop_url_patterns") or ()) if str(p)
        ]
        # 政府网站红线后缀。配置写了用配置的，没写用内置默认（.gov.cn）。
        raw_suffixes = spec.get("block_host_suffixes")
        self.block_suffixes: tuple[str, ...] = tuple(
            str(s).strip() for s in (raw_suffixes if raw_suffixes is not None
                                    else DEFAULT_BLOCK_HOST_SUFFIXES) if str(s).strip()
        )
        # 0 / 负值 = 不限。默认 30 来自配置，不是硬编码。
        self.max_per_domain = max(0, int(spec.get("max_per_domain") or 0))

    # ---------------------------------------------------------------- 脱敏

    def scrub(self, text: str) -> tuple[str, int]:
        """返回 (脱敏后文本, 命中次数)。命中次数 > 0 时调用方应记一条 alert。

        没命中时原样返回**同一个对象**，避免给每篇文档都造一个新字符串。
        """
        if not text:
            return text, 0
        hits = 0
        out = text
        for _name, pattern, masker in self._rules:
            out, n = pattern.subn(masker, out)
            hits += n
        return (out, hits) if hits else (text, 0)

    # ---------------------------------------------------------------- 丢弃

    def check_url(self, url: str, domain: str = "") -> str | None:
        """返回丢弃原因；None 表示放行。

        只看 URL 本身和域名，不看内容 —— 内容级的判断（风控页、空壳页）属于抽取层。
        """
        d = (domain or "").strip().lower()
        if d.startswith("www."):
            d = d[4:]
        # 红线在最前面：政府网站连列表页/黑名单的讨论都轮不到，直接拒
        if is_blocked_host(d or urlsplit(url).netloc, self.block_suffixes):
            return "政府网站（红线：不抓）"
        if d:
            for bad in self._drop_domains:
                if d == bad or d.endswith("." + bad):
                    return f"域名在黑名单（{bad}）"
        if is_list_url(url):
            return "栏目列表页/站点首页（点开不是具体文章）"
        for pattern in self._drop_url_re:
            if pattern.search(url or ""):
                return f"URL 命中丢弃规则（{pattern.pattern}）"
        return None


__all__ = ["DEFAULT_BLOCK_HOST_SUFFIXES", "Sanitizer", "is_blocked_host", "is_list_url"]
