"""聚合层：URL 规范化 / 近重复去重 / 相关度打分。**不碰网络**。

这一层决定了报告的"可信度"。一个关键词搜出来 800 条，如果 500 条是同一篇
新闻稿的转载，那不叫热度，叫噪声 —— 报告必须先做归并再谈数量。

契约：
    canonicalize(url) -> str
        去掉 utm_* / spm 等跟踪参数、统一大小写域名、剥锚点、
        www. 与主站合并、去掉尾部多余斜杠、查询参数排序并重编码。
        ⚠️ 不做这一步，同一个页面会被算成多条（?utm_source=x / #comment / www. / 参数换序）。

    simhash64(text) -> int            （S5，实现在 cluster.py）
        正文 SimHash，汉明距离 ≤3 视为转载。
        中文按字符 3-gram 取词（不做分词：没有词典时分词比 3-gram 更差）。

    cluster_docs(items) -> ClusterReport
        S5 的归并。**贪心最近代表**，不是并查集 —— 见 cluster.py 顶部第 3 条。

    relate(keyword, title, body, *, query_rank) -> Mention
        打分 = 标题命中 × 3 + 正文命中 × 1 + 精确短语 × 2 + 名次加成
        S4 阶段 body 传的是 SERP 摘要，S3 之后换成真实正文，调用方不用改。

⚠️ 规范化只允许"不影响页面内容"的改动。下面这些**故意不动**：
    - 路径大小写（有些站区分大小写，改了就 404）
    - 微信文章的 __biz / mid / idx / sn、淘宝的 chksm —— 这些看着像追踪参数，
      实际是打开页面的必需标识，删了链接就废了
    - http → https 的升级（有的站只有一边能访问）
"""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from kwsearch.aggregate.cluster import (
    Cluster,
    ClusterInput,
    ClusterMember,
    ClusterReport,
    cluster_docs,
    from_signed64,
    hamming64,
    simhash64,
    to_signed64,
)
from kwsearch.models import Mention

# 纯追踪参数：删掉不改变页面内容。判断标准是"删了还能打开同一页"，拿不准的一律不删。
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "utm_reader", "utm_social", "utm_brand",
    "spm", "spm_id_from", "vd_source", "from_source", "from_spmid",
    "fbclid", "gclid", "yclid", "msclkid", "dclid",
    "share_token", "share_source", "share_medium", "share_plat", "share_session_id",
    "s_r", "refer", "referer",
})

# 排序/分页类参数：保留，但规范化时不参与"是否同一页"的判断会在 S5 处理；
# 现在先原样保留 —— 把 ?page=2 归一到 ?page=1 会把第 2 页吞掉。

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def host_of(url: str) -> str:
    """取主机名：小写、去 www.、去端口。接受 "example.com" 与 "https://a.example.com/x" 两种形态。

    注意：这只做展示/统计用的粗粒度归并，不处理 co.uk / com.cn 这类公共后缀 ——
    报告里按域名分组够用，真要按站点归并得引 tldextract，M1 不引。
    """
    s = (url or "").strip()
    if not s:
        return ""
    host = urlsplit(s).netloc if "://" in s else s.split("/")[0]
    host = host.rsplit("@", 1)[-1].split(":")[0].lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def canonicalize(url: str) -> str:
    """把"看起来不同、其实是同一页"的 URL 归一成一个字符串。

    幂等：canonicalize(canonicalize(u)) == canonicalize(u)。
    解析失败时原样返回（宁可少归并一次，也不要造出一个打不开的链接）。
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.netloc:
        return raw                      # 相对链接：调用方应该先补全，这里不猜

    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.rsplit("@", 1)[-1].lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        h, _, port = host.rpartition(":")
        if port.isdigit() and _DEFAULT_PORTS.get(scheme) == port:
            host = h

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    kept.sort()
    query = urlencode(kept, doseq=True)

    # 锚点整段丢弃：/#comment、/#respond 这类都是页内定位，不影响内容
    return urlunsplit((scheme, host, path, query, ""))


# ---------------------------------------------------------------- 相关度打分
_WS_RE = re.compile(r"\s+")

# 权重不是拍脑袋：标题是作者对全文的"人工摘要"，命中的信息量远大于正文里
# 顺带提一句；精确短语加成用来区分"苹果手机"（一个产品）和"苹果 手机"
# （两个词恰好在同一页出现）。
#
# ⚠️ 标题命中封顶 2 次而不是 3 次以上：实测 B站/公众号标题习惯把关键词重复
# 三遍（"红黑树…红黑树…手写红黑树"），按次数线性加分会让"堆词标题"霸榜。
# 对相关性来说，标题里出现 1 次和 3 次没有区别 —— 出现就算命中。
_W_TITLE = 3.0
_W_BODY = 0.5          # 摘要/正文里的命中只作佐证，权重必须明显低于标题
_W_EXACT = 2.0
_CAP_TITLE = 2
_CAP_BODY = 4


def normalize_space(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip())


def tokens_of(keyword: str) -> list[str]:
    """查询词切分。中文不分词 —— 分词器会把"红黑树"切成"红黑/树"，
    反而让精确短语判断失效。这里只按空白切，中文整串当一个 token。"""
    return [t for t in _WS_RE.split((keyword or "").strip()) if t]


def count_occurrences(text: str, phrase: str) -> int:
    """非重叠计数，忽略大小写。空输入返回 0。"""
    if not text or not phrase:
        return 0
    return text.casefold().count(phrase.casefold())


def relate(
    keyword: str,
    title: str,
    body: str,
    *,
    query_rank: int = 1,
    task_id: str = "",
    url_canon: str = "",
) -> Mention:
    """两级相关度：标题命中 / 正文命中 + 精确短语加成 + 名次加成。

    score = min(标题命中,2)*3 + min(正文命中,4)*0.5 + 精确短语*2
            + 1/(1+log2(1+名次))

    为什么名次也要参与：同一个词在 SERP 第 1 位和第 9 位的可信度不同，
    但它的权重（≤1）必须小于任何一次标题命中（3），否则就会被"渠道排名"
    盖过"内容是否真的提到这个词" —— 那正是搜索引擎的结果页，不是我们的结论。

    ⚠️ hit_title == hit_body == 0 时，score 只剩名次加成（≤1）。
       这种条目是"通道认为相关但内容里根本没这个词"，pipeline 默认会按
       filter.drop_zero_hit 丢掉 —— 实测 B站 对冷门词会拿热门课兜底。

    body 在 S4 阶段是 SERP 摘要（还没抓正文），S3 之后换成真实正文，
    函数签名不变。
    """
    toks = tokens_of(keyword)
    title_n = sum(count_occurrences(title, t) for t in toks)
    body_n = sum(count_occurrences(body, t) for t in toks)

    # 精确短语用"折叠空白后的整串"判断，这样"苹果手机"与"苹果 手机"判定不同
    kw_norm = normalize_space(keyword).casefold()
    exact = bool(kw_norm) and (
        kw_norm in normalize_space(title).casefold()
        or kw_norm in normalize_space(body).casefold()
    )

    rank = max(1, int(query_rank or 1))
    score = (
        min(title_n, _CAP_TITLE) * _W_TITLE
        + min(body_n, _CAP_BODY) * _W_BODY
        + (_W_EXACT if exact else 0.0)
        + 1.0 / (1.0 + math.log2(1.0 + rank))
    )
    return Mention(
        task_id=task_id,
        url_canon=url_canon,
        hit_title=title_n,
        hit_body=body_n,
        exact_phrase=exact,
        bm25=0.0,                     # M2 才做 BM25，这里显式留 0，不留空
        score=round(score, 4),
    )


__all__ = [
    "Cluster",
    "ClusterInput",
    "ClusterMember",
    "ClusterReport",
    "canonicalize",
    "cluster_docs",
    "count_occurrences",
    "hamming64",
    "host_of",
    "normalize_space",
    "relate",
    "simhash64",
    "tokens_of",
]
