"""近重复聚簇：SimHash64 + 贪心最近代表归并。**不碰网络，也不碰数据库**。

为什么必须有这一层：中文互联网的"多来源"绝大部分是同一篇稿子的转载。
一条消息被 20 家门户转载，报告里就会出现 20 行，看着像"20 条独立证据" ——
那是把噪声当热度。归并之后才谈得上"多少个独立信息源"。

四条设计决定，每条都对应一个会出错的具体情形：

1. **指纹只用正文，且长度口径必须一致**。
   SimHash 的汉明距离只有在"两边用同一套取词规则、同一量级的文本"时才有可比性。
   拿 500 字摘要算出的指纹去和 6000 字正文算出的指纹比，距离天然偏大；
   反过来，两条短摘要又天然彼此接近。所以：**只用正文算，正文不足
   `min_body_chars` 的一律不参与**（宁可不聚类，也不乱聚类）。

2. **取词用字符 3-gram，不用分词**。
   转载稿的差异常常是标点全半角、空格、`【】`→`[]`、站点前后加引导语。
   3-gram 对这些不敏感（同一个三元组还是同一个三元组），
   而分词会因为我们没装词典、把"红黑树"切成"红黑/树"而引入噪声。
   归一化时把标点空白全部剥掉，正是为了让这些差异不算差异。

3. **贪心最近代表，不用并查集**。
   并查集求的是传递闭包：A~B、B~C 就把 A 和 C 连成一簇，而 A 与 C 可能毫无关系。
   转载稿的"共同模板"（站点页头、版权声明、公众号引导语）最容易造出这种链。
   贪心法逐个与**代表**比，天然保证"簇与簇的代表之间互不相似"这条不变量。

4. **代表按"有效正文"选，不按原始长度选**。
   正文越长指纹越稳，这条仍然成立 —— 但"长"必须量的是**内容**，不是字数。
   转载稿几乎都会在末尾挂一段免责声明/公众号引导（100~300 字），
   于是它的 body_chars 比原文更大。按原始长度选，代表就会是二手站点，
   报告里那条"原文链接"其实指向一个转载页。
   所以排序用的是 `body_core`（剥掉首尾模板后的长度）。长度相同表示
   "内容一样多、只是有人多带了模板"，此时取原始长度**更短**的那个；
   再相同就看发布更早的（转载一般晚于原文），最后用 URL 兜底 ——
   保证同一批输入每次跑出来的分组完全一致。
   ⚠️ 老数据没有 `body_core`（补列后为 NULL），退回 `body_chars` 参与排序：
      代表仍然能选出来，只是享受不到这条改进。
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# 这些 time_source 的 published_at 不参与"谁更早"的排序：
#   regex_updated  站点构建时间（VitePress/Hugo 页脚"最近更新"，全站同一个值）
#   serp_relative  "5 天之前"回推，精度只到天
#   fallback       根本没有
# 拿它们比先后，等于让"站点最近重建过"决定谁是原文。
UNTRUSTED_TIME_SOURCES = frozenset({"regex_updated", "serp_relative", "fallback", ""})

# 汉明距离阈值。**10 不是拍脑袋，也不是照抄"业界常用 3"。**
#
# 实测（tests/test_cluster.py 里固化成回归用例）：
#     同文                                  0
#     同文 + 全角/半角 + 站名前置              3
#     同文 + 前置编者按                        1
#     同文 + 追加 180 字免责声明/公众号引导语      8   ← 转载最常见的形态
#     同文被截断到 60%                         12
#     同文被截断到 40%                         17
#     同模板不同事件的通报（换人名换事由）        15~20
#     同主题不同文章                          26~33
#     完全无关                              23~35
#
# 阈值 3（大多数 simhash 教程的默认值）会把"转载时加了一段免责声明"全部漏掉 ——
# 而那恰恰是中文门户转载最标准的做法。10 能覆盖到"加模板"，同时与无关文本
# 至少留 13 位余量；再往上调就会开始吃进"同模板不同事件"的通报类文本。
#
# ⚠️ 代价是"被截断的预览页"（只露出前一半）不会与全文归并。这是刻意的：
#    12 与 17 已经贴近 20 以下的误并区，宁可漏并，也不要把两篇不同的文章说成一篇。
DEFAULT_HAMMING = 10

# 正文短于此值不参与聚类。200 字≈200 个 3-gram，指纹才开始有区分度。
DEFAULT_MIN_BODY = 200

# 只取正文前 N 个字符算指纹。
# 一是控制成本（每个 3-gram 要做 64 次位累加），二是**前缀截断不会破坏比对**：
# 3-gram 是按内容取的，转载方在前面加 200 字引导语只会多出 200 个新 3-gram，
# 原文的那些三元组仍然在集合里，重合度基本不掉。
_MAX_TEXT = 3000

# 少于这个长度算不出有意义的指纹（1~2 个字没有 3-gram）。
_MIN_TEXT = 12

_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """归一化到"只剩内容字符"：NFKC（全角→半角）、小写、去掉标点空白。

    剥掉标点是刻意的：转载站最爱改的就是标点（`，`→`,`、`——`→`-`、加空格）。
    这些差异对"是不是同一篇文章"毫无信息量，留着只会把距离推高。
    """
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return _NON_WORD_RE.sub("", t)


def _hash64(s: str) -> int:
    """稳定的 64 位哈希。

    ⚠️ 绝对不能用内置 `hash()`：CPython 对 str 的哈希每个进程加盐，
    同一篇文章换一次进程就变成另一个指纹，聚簇结果无法复现。
    """
    return int.from_bytes(
        hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def _shingles(text: str, n: int = 3) -> Iterable[str]:
    if len(text) <= n:
        yield text
        return
    for i in range(len(text) - n + 1):
        yield text[i:i + n]


def simhash64(text: str) -> int:
    """64 位 SimHash。文本太短（<12 字）或全为标点时返回 0。

    ⚠️ 0 表示"算不出指纹"，不是一个"和谁都很像"的值 ——
    调用方必须把 0 当作 None 处理，否则所有空文本会聚成一簇。
    """
    t = normalize_text(text)
    if len(t) < _MIN_TEXT:
        return 0
    t = t[:_MAX_TEXT]

    counts: dict[str, int] = {}
    for gram in _shingles(t):
        counts[gram] = counts.get(gram, 0) + 1

    weights = [0.0] * 64
    for gram, n in counts.items():
        h = _hash64(gram)
        # 频次取对数：站点模板（版权声明、导航语）重复几十次，
        # 线性加权会让它压过正文的语义，这正是"所有文章都像"的来源。
        w = 1.0 + math.log2(n) if n > 1 else 1.0
        for bit in range(64):
            weights[bit] += w if (h >> bit) & 1 else -w

    out = 0
    for bit, total in enumerate(weights):
        if total > 0:
            out |= 1 << bit
    return out


def hamming64(a: int, b: int) -> int:
    """两个指纹的汉明距离（0~64）。"""
    return (a ^ b).bit_count()


_MASK64 = (1 << 64) - 1
_SIGN64 = 1 << 63


def to_signed64(value: int) -> int:
    """无符号指纹 → SQLite 能存的**有符号** 64 位。

    必须做这一步：SQLite 的 INTEGER 是 int64，而 SimHash 的最高位可能为 1，
    直接绑 `2**63` 以上的值会抛 OverflowError（实测把整条入库打断）。
    按补码转成负数存，读出来再用 from_signed64 还原 —— 64 位一位不丢。
    """
    v = int(value) & _MASK64
    return v - (1 << 64) if v >= _SIGN64 else v


def from_signed64(value: int) -> int:
    """库里的有符号值 → 无符号指纹。

    ⚠️ 负数在 Python 里是无限位补码，`(-1).bit_count() == 1` 而不是 64 ——
    不还原就送去算汉明距离，负的那些文档会被判成"和谁都几乎一样"。
    """
    return int(value) & _MASK64


# ------------------------------------------------------------------ 数据结构


@dataclass(slots=True)
class ClusterInput:
    """参与聚类的一条文档。刻意不直接用 models.Doc：
    这一层只关心"指纹、长度、时间"这几个字段，不该被上游模型的变化牵着走。"""

    url_canon: str
    simhash: int | None = None
    body_chars: int = 0
    body_core: int = 0          # 剥掉转载模板后的正文长度；0/None = 老数据未算，退回 body_chars
    time_trusted: bool = True   # published_at 是否可用于比较先后（见 UNTRUSTED_TIME_SOURCES）
    title: str = ""
    domain: str = ""
    published_at: str | None = None

    @property
    def effective_chars(self) -> int:
        """用于代表排序的"内容长度"：有有效长度就用它。"""
        return self.body_core if self.body_core and self.body_core > 0 else self.body_chars


@dataclass(slots=True)
class ClusterMember:
    url_canon: str
    hamming: int = 0            # 与代表的距离；代表自己是 0
    title: str = ""
    domain: str = ""
    published_at: str | None = None
    body_chars: int = 0


@dataclass(slots=True)
class Cluster:
    cluster_id: int
    rep_url: str
    members: list[ClusterMember] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def rep(self) -> ClusterMember | None:
        return self.members[0] if self.members else None

    @property
    def max_hamming(self) -> int:
        """簇内最差的一条与代表的距离。报告用它换算"最低相似度"。"""
        return max((m.hamming for m in self.members), default=0)

    @property
    def similarity(self) -> float:
        """保守相似度：按簇内最差的那条算，不拿平均值粉饰。"""
        return 1.0 - self.max_hamming / 64.0


@dataclass(slots=True)
class ClusterReport:
    clusters: list[Cluster] = field(default_factory=list)
    no_simhash: int = 0         # 有正文但没有指纹（老数据 / 算不出）
    too_short: int = 0          # 正文不足 min_body_chars，不参与判断

    @property
    def member_total(self) -> int:
        """参与聚类的文档数（含单条成簇的）。"""
        return sum(c.member_count for c in self.clusters)

    @property
    def groups(self) -> int:
        """独立内容数 = 簇数 + 完全没指纹的那些（各自算一组）。"""
        return len(self.clusters) + self.no_simhash + self.too_short

    @property
    def merged(self) -> int:
        """被归并掉的条数（非代表成员的数量）。"""
        return sum(c.member_count - 1 for c in self.clusters)

    @property
    def multi(self) -> list[Cluster]:
        return [c for c in self.clusters if c.member_count > 1]

    def summary_line(self) -> str:
        bits = [f"{self.groups} 组独立内容", f"并入 {self.merged} 条"]
        if self.no_simhash:
            # "待补算"而不是"无指纹"：这批是有正文、只差指纹的，重跑一次就能补上。
            bits.append(f"待补算指纹 {self.no_simhash}")
        if self.too_short:
            bits.append(f"正文过短 {self.too_short}")
        return " · ".join(bits)


# ---------------------------------------------------------------------- 聚类


def _order_key(item: ClusterInput) -> tuple:
    """代表优先序：内容长度 → 原始长度（短的更像原文）→ 发布时间 → URL。

    第二位的 `+body_chars` 是**升序**，也就是同等内容量时选更短的那个：
    多出来的那几百字如果是模板，我们已经在 body_core 里扣掉了，扣完还更长的
    才是真的多内容；扣完一样长、原始却更长，那多出来的就是没认出来的模板。

    URL 只用来兜底，保证"同一批输入结果完全一致"（否则两份同样长的稿子
    谁当代表取决于输入顺序，报告会随抓取顺序抖动）。
    """
    when = item.published_at if item.time_trusted else None
    return (-item.effective_chars, item.body_chars, when or "9999-99-99", item.url_canon)


def cluster_docs(
    items: Sequence[ClusterInput],
    *,
    hamming: int = DEFAULT_HAMMING,
    min_body_chars: int = DEFAULT_MIN_BODY,
) -> ClusterReport:
    """把一批文档按正文指纹归并成簇。纯函数，不读写任何外部状态。

    每个"能算出指纹且正文够长"的文档一定落在某个簇里（最差是单条成簇），
    所以 `member_total + no_simhash + too_short == len(items)` —— 这个恒等式
    是报告里"入库 N 条 / 独立 M 组"能对上账的前提。
    """
    report = ClusterReport()
    eligible: list[ClusterInput] = []

    for it in items:
        if not it.url_canon:
            continue
        if it.body_chars < min_body_chars:
            report.too_short += 1
            continue
        if not it.simhash:
            # 0 / None 都是"没有指纹"。不能当普通值参与比较：
            # 空文本的指纹是 0，两条空文本会算出距离 0 而被错误地并在一起。
            report.no_simhash += 1
            continue
        eligible.append(it)

    eligible.sort(key=_order_key)

    # reps[i] = 该簇当前的成员列表，[0] 是代表
    reps: list[list[ClusterMember]] = []
    rep_simhashes: list[int] = []

    for it in eligible:
        best_idx = -1
        best_d = hamming + 1
        for idx, sh in enumerate(rep_simhashes):
            d = hamming64(it.simhash, sh)
            if d <= hamming and d < best_d:
                best_idx, best_d = idx, d
        member = ClusterMember(
            url_canon=it.url_canon, hamming=0 if best_idx < 0 else best_d,
            title=it.title, domain=it.domain,
            published_at=it.published_at, body_chars=it.body_chars,
        )
        if best_idx < 0:
            reps.append([member])
            rep_simhashes.append(int(it.simhash))
        else:
            reps[best_idx].append(member)

    # 簇号按"条数降序、代表 URL"排，保证同一批输入的簇号稳定可读
    ordered = sorted(reps, key=lambda ms: (-len(ms), ms[0].url_canon))
    report.clusters = [
        Cluster(cluster_id=i, rep_url=ms[0].url_canon, members=ms)
        for i, ms in enumerate(ordered, start=1)
    ]
    return report


__all__ = [
    "Cluster",
    "ClusterInput",
    "ClusterMember",
    "ClusterReport",
    "DEFAULT_HAMMING",
    "DEFAULT_MIN_BODY",
    "UNTRUSTED_TIME_SOURCES",
    "cluster_docs",
    "from_signed64",
    "hamming64",
    "normalize_text",
    "simhash64",
    "to_signed64",
]
