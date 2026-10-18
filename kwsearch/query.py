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

同日实测"央行降准"：技术词后缀（"左旋 右旋"）对时政词完全无效 —— 两个变体
全被 Bing 降级，白打 2 次请求，告警里还出现"没有任何一条含「左旋」「右旋」"
这种荒谬话。结论有两层：
  1. **变体要按实测增益排序**，并且必须支持早停 —— 连续两个变体零新增就停，
     否则会白白打 5 次请求换回同一批结果（expand.py 的做法）。
  2. **后缀必须按查询类型选**，不能一套后缀打天下。可"红黑树"和"央行降准"
     都是纯中文短词，从字面上根本分不出来 —— 所以类型判定不用猜，用基词
     首批结果的域名构成来探测（probe_profile）：结果里 GitHub/CSDN/博客园
     扎堆 → 技术词，值得加后缀；新闻站扎堆或判不出来 → 只打基词。
     人名同理：任何"人名 + 第二个词"的组合都会触发 Bing 静默降级（见
     discover/quality.py），人名永远只打基词、收窄靠结果内归因。

为什么不做自动跨语言扩展：那需要翻译，翻译错了会引入大量无关结果。
英文术语走 extra_queries 人工补，宁少不乱。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

_WS_RE = re.compile(r"\s+")

# 探测用的域名信号。匹配规则：host == h 或 host 以 "."+h 结尾
# （blog.csdn.net 命中 csdn.net；notcsdn.net 不会）。
#
# 为什么 tech 不能只靠域名投票（2026-10-18 "红黑树" 实测的教训）：
#   bing 对技术词的结果经常落在 zhihu / 百家号 / B站 / 百度文库 这些
#   "什么词都收"的站上，纯技术域名（csdn/cnblogs/github）可能只有 1~2 票。
#   所以 tech 额外认标题信号 —— 技术词的标题里几乎必然出现
#   数据结构/算法/源码/复杂度 这类词，而时政词的标题里不会。
_TECH_HOSTS = (
    "csdn.net", "cnblogs.com", "juejin.cn", "github.com",
    "segmentfault.com", "oschina.net", "stackoverflow.com", "v2ex.com",
    "51cto.com", "infoq.cn", "jianshu.com",
)
_TECH_TITLE_RE = re.compile(
    r"数据结构|算法|源码|代码|编程|程序|手撕|刷题|LeetCode|面试题"
    r"|Java|Python|C\+\+|Golang|Rust|JavaScript|前端|后端|架构"
    r"|时间复杂度|空间复杂度|左旋|右旋|插入|删除|旋转|遍历|指针|数组|链表|哈希|二叉"
    , re.I,
)
_NEWS_HOSTS = (
    "people.com.cn", "xinhuanet.com", "news.cn", "cctv.com", "cnr.cn",
    "chinanews.com", "chinanews.com.cn", "thepaper.cn", "caixin.com",
    "ce.cn", "gmw.cn", "chinadaily.com.cn", "yicai.com", "21jingji.com",
    "eastmoney.com", "stcn.com", "cnstock.com", "nbd.com.cn",
)
# 人名信号：招聘/简历站 + 高校域名（师资个人主页几乎都挂在 .edu.cn 下）。
# 命中率要求放低（≥2 即可），因为把人名误判成 generic 的代价是零
# （两者都只打基词），反过来（把普通词判成 tech）才会白打请求。
_PERSON_HOSTS = (
    "zhaopin.com", "51job.com", "liepin.com", "kanzhun.com", "jobui.com",
)
_PERSON_TITLE_RE = re.compile(r"师资|教师|教授|导师|个人主页|faculty|博导|硕导")

PROBE_MIN_TECH = 3      # 候选级命中数（域名或标题信号）
PROBE_MIN_NEWS = 2      # 不同的新闻域名数
PROBE_MIN_PERSON = 2


def _host_matches(host: str, suffix: str) -> bool:
    return bool(host) and (host == suffix or host.endswith("." + suffix))


def probe_profile(cands: Sequence[Any]) -> str:
    """用基词首批结果的域名/标题构成，猜这个词属于哪一类。

    cands：Candidate 或任何有 .title / .extra["domain"]（或 .url_raw）的对象。
           传 duck-typing 而不是具体类型，是为了让单测不用 import models。
    返回："tech" | "person" | "news" | "generic"。

    设计取向（宁可漏判，不可错判）：
      - generic 是兜底，而 generic 和 person/news 的行为一样（只打基词），
        所以"判不出来"永远安全；唯一需要主动确认的是 tech，
        因此 tech 的票数门槛最高。
      - 各类的计票单位不同：
          tech   → 按条计（技术域名 或 标题带技术词，命中一条算一票）
          news   → 按不同域名计（新闻站垄断下十条同域只是 1 票）
          person → 按不同域名/标题计
    """
    tech_hits = 0
    news_hosts: set[str] = set()
    person_keys: set[str] = set()
    for c in cands or ():
        domain = ""
        extra = getattr(c, "extra", None)
        if isinstance(extra, dict):
            domain = str(extra.get("domain") or "")
        if not domain:
            url = getattr(c, "url_raw", "") or ""
            m = re.search(r"(?:^|//)([^/?#]+)", url)
            domain = m.group(1) if m else ""
        title = getattr(c, "title", "") or ""
        host = domain.lower()

        if (any(_host_matches(host, h) for h in _TECH_HOSTS)
                or _TECH_TITLE_RE.search(title)):
            tech_hits += 1
        if any(_host_matches(host, h) for h in _NEWS_HOSTS):
            news_hosts.add(host)
        if (_PERSON_TITLE_RE.search(title)
                or any(_host_matches(host, h) for h in _PERSON_HOSTS)
                or host.endswith(".edu.cn")):
            person_keys.add(host or title)

    n_person, n_news = len(person_keys), len(news_hosts)
    best = max(tech_hits, n_person, n_news)
    if best == 0:
        return "generic"
    # 严格最多才下结论；并列 = 信号矛盾，退回 generic（只打基词，安全）
    if best == tech_hits and tech_hits >= PROBE_MIN_TECH:
        return "tech"
    if best == n_news and n_news >= PROBE_MIN_NEWS:
        return "news"
    if best == n_person and n_person >= PROBE_MIN_PERSON:
        return "person"
    return "generic"


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


def expand_queries(
    base: str,
    spec: Mapping[str, Any] | None = None,
    probe: Sequence[Any] | None = None,
) -> list[QueryVariant]:
    """生成查询变体列表。第一个永远是基词。

    spec（来自 channels.yaml 的 expand 段）：
        enabled        false 时只返回基词
        max_queries    含基词在内的总上限，默认 8
        separator      基词与后缀的连接符，默认一个空格
        suffixes       修饰词列表（旧式：一套后缀打所有词）
        profiles       新式：按查询类型给后缀，如 {tech: [...], person: [], news: [], generic: []}
        profile        tech/person/news/generic/auto；auto 时用 probe 探测（见下）
        extra_queries  人工补充的整条查询（如英文术语）

    probe：基词的首批结果（Candidate 列表）。profile=auto 且 probe 非空时，
    用 probe_profile() 从结果域名构成判定类型；probe 为空（离线调试、
    或调用方还没打过基词）时退回 generic —— 即"没有证据就别加后缀"，
    宁可少扩量，不打无效请求。
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

    profiles = spec.get("profiles") or None
    if profiles:
        sel = str(spec.get("profile", "auto"))
        if sel == "auto":
            sel = probe_profile(probe) if probe else "generic"
        suffixes = _as_list(profiles.get(sel))
    else:
        suffixes = _as_list(spec.get("suffixes"))

    seen = {query_key(base)}
    out = [first]

    def _add(q: str, strategy: str) -> None:
        q = _WS_RE.sub(" ", q.strip())
        key = query_key(q)
        if not q or key in seen or len(out) >= max_queries:
            return
        seen.add(key)
        out.append(QueryVariant(q=q, strategy=strategy, priority=len(out)))

    for suffix in suffixes:
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
