"""结果与查询的相关性体检 —— 专治"搜索引擎静默降级"。

为什么需要这个模块（2026-10-18 实测，**不要再试一遍**）：

    cn.bing.com 对"人名 + 任何第二个词"会退回成"第一个词"的字面结果：

        张军 南京                → 10 条全是「张」的百科/字典/姓氏/家谱页
        网络安全张军              → 测速网、网速测试、计算机网络知识点
        某某大学计算机学院张军     → 所在城市的旅游攻略、政务网站
        某某研究院张军            → 同名机构的百科条目

    而 HTTP 是 200、`<title>` 仍是原查询、搜索框 value 也是原查询、**没有任何风控提示**。
    唯一可辨的特征：**10 条结果里没有一条真的提到查询里的人名**。

    加空格、加引号、`site:` 全部无效；换成英文（`Zhang Jun Nanjing University`）
    也一样退化 —— 这不是我们编码错了，是对方的行为。

危险之处：这种页面**看起来是成功的**。上游拿到 10 条候选、断言通过、
`run.got=10`，报告里"覆盖 10 个来源"，实际全是噪声。
所以判定必须发生在通道层（拿到候选的第一时间），而不是等下游按分数筛。

三条判据，按查询形态选一条（都基于"命中"= 某条候选的 标题+摘要 含该串）：

    多片段      查询开头的片段被 ≥ 半数候选提到 → 正常；
                否则再看后面的片段，一个都没命中才判降级
    单片段长串   该片段的 2-gram 覆盖率 ≥ min_bigram_ratio **且末尾二字片段命中**
    单片段短串   整串被命中

**为什么多片段先看开头**：降级的本质是"搜索引擎只用了查询的开头"，所以真正要问的是
"开头那个词到底被用上了没有"。两个真实样本在"后面的词全不命中"上完全一样：

    `张军 南京`   退化页 0/10 条含「张军」   → 连主体都没命中 → 判降级
    `红黑树 实现` 正常页 10/10 条含「红黑树」 → 正常（只是标题里没"实现"二字）

唯一的分水岭就是开头被用上了多少。**反过来要求"后面的词必须命中"是错的**：
实测 `红黑树 实现` 会被误判成降级、整页 10 条好结果全丢。

**为什么长串还要看末尾**：只看覆盖率会被宽泛的前半段吃满。
实测 `网络安全张军` 有一次返回的全是"计算机网络/互联网百科"，"网络""络安""安全"
三个片段都命中（60%，过线），但"张军"一次都没出现 —— 那依然是"只用了开头"。

判据刻意保持"形式可解释"，不做语义猜测、不引分词库：
一旦判错就会整页丢弃，宁可漏判也不能误杀。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from kwsearch.models import Candidate

# 片段分隔：空白（含全角空格）+ 常见中英文标点
_SPLIT_RE = re.compile(r"[\s\u3000,，、;；:：|/\\()（）\[\]【】<>《》\"'“”‘’!！?？~～\-—+]+")

# 只挡"几乎不含信息"的虚词。刻意不挡"网络/安全/大学"这类领域词 ——
# 它们在某些查询里是关键词，在另一个查询里是泛词，机械区分不了，不如不区分。
_STOP = frozenset({
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "个", "我", "你", "他",
    "我们", "什么", "怎么", "如何", "哪些", "关于", "以及",
})

_MIN_BIGRAM_RATIO = 0.5      # 单片段长串：query 的 2-gram 至少半数要出现
_MIN_HEAD_RATIO = 0.5        # 多片段：查询开头的词至少要被半数候选提到
_MIN_DOCS = 5                # 样本太少不做判定（宁可放过，不可误杀）


@dataclass(frozen=True, slots=True)
class RelaxCheck:
    """一次降级体检的结果。detail 直接进 ChannelResult.detail / 告警文案。"""

    relaxed: bool
    basis: str = ""          # tail | bigram | phrase | sample_too_small | no_query
    matched: int = 0         # 命中的片段/gram 数
    total: int = 0           # 参与判定的片段/gram 总数
    detail: str = ""

    @property
    def ratio(self) -> float:
        return self.matched / self.total if self.total else 1.0


def query_segments(query: str) -> list[str]:
    """把查询切成"信息片段"。中文不分词（没有分词库，且切错比不切更糟）。

    长度 < 2 的片段、纯虚词直接丢：它们无法承载"这条结果说的是不是这件事"。
    """
    out: list[str] = []
    for raw in _SPLIT_RE.split((query or "").casefold()):
        s = raw.strip()
        if len(s) < 2 or s in _STOP or s in out:
            continue
        out.append(s)
    return out


def bigrams(s: str) -> list[str]:
    """相邻 2 字滑窗，去重保序。

    中文长串没有空格（"某某大学信息学院张军"），整串匹配永远失败，
    2-gram 是零依赖条件下唯一还能反映"这段文字被用上了多少"的东西。
    """
    if len(s) < 2:
        return []
    seen: dict[str, None] = {}
    for i in range(len(s) - 1):
        seen.setdefault(s[i:i + 2], None)
    return list(seen)


def _texts(candidates: Sequence[Candidate]) -> list[str]:
    return [f"{c.title}\n{c.snippet}".casefold() for c in candidates]


def _hits(texts: Sequence[str], needle: str) -> int:
    """有多少条候选提到了 needle。用"条数"而不是"次数"：
    堆词标题里出现 10 次和 1 次对"这件事是否被提到"没有区别。"""
    return sum(1 for t in texts if needle in t)


def check_relaxed(
    candidates: Sequence[Candidate],
    query: str,
    *,
    min_docs: int = _MIN_DOCS,
    min_bigram_ratio: float = _MIN_BIGRAM_RATIO,
    min_head_ratio: float = _MIN_HEAD_RATIO,
) -> RelaxCheck:
    """判断"这一页结果是不是查询被降级后的产物"。

    返回 relaxed=True 时，调用方应当**整页丢弃**：这些候选不是结果，
    只是"搜索引擎没听懂"的证据。
    """
    segs = query_segments(query)
    if not segs:
        return RelaxCheck(False, "no_query")

    n = len(candidates)
    if n == 0:
        # 0 条不归这里管：那是 empty / unknown_structure 的职责
        return RelaxCheck(False, "sample_too_small", detail="无候选")
    if n < min_docs:
        return RelaxCheck(
            False, "sample_too_small", total=1,
            detail=f"仅 {n} 条候选，样本太小，不做降级判定",
        )

    texts = _texts(candidates)

    # ---- 单片段 ------------------------------------------------------------
    if len(segs) == 1:
        s = segs[0]
        if len(s) <= 3:
            hit = _hits(texts, s)
            if hit:
                return RelaxCheck(False, "phrase", 1, 1)
            return RelaxCheck(
                True, "phrase", 0, 1,
                detail=f"{n} 条候选没有一条含「{s}」",
            )

        grams = bigrams(s)
        hit_grams = [g for g in grams if _hits(texts, g)]
        ratio = len(hit_grams) / len(grams) if grams else 1.0

        # 双重条件：整体覆盖过半 **且** 查询末尾的二字片段命中。
        # 只看覆盖率会被宽泛的前半段吃满 —— 实测 "网络安全张军" 有一次返回的全是
        # "计算机网络/互联网百科"，"网络""络安""安全" 三个片段都命中（60%），
        # 但"张军"一次没出现。归根到底还是"只用了查询的开头"，必须判降级。
        tail_gram = grams[-1]
        if ratio >= min_bigram_ratio and tail_gram in hit_grams:
            return RelaxCheck(False, "bigram", len(hit_grams), len(grams))

        if tail_gram not in hit_grams:
            detail = (f"{n} 条候选没有一条含「{tail_gram}」（查询末尾的二字片段），"
                      f"只覆盖了 {len(hit_grams)}/{len(grams)} 个片段")
        else:
            missing = "".join(g for g in grams if g not in hit_grams)[:12]
            detail = (f"{n} 条候选只覆盖查询的 {len(hit_grams)}/{len(grams)} 个二字片段"
                      f"（缺「{missing}」等）")
        return RelaxCheck(True, "bigram", len(hit_grams), len(grams), detail=detail)

    # ---- 多片段：先看开头的词被用上了没有 ---------------------------------
    # 关键区别（实测逼出来的）：
    #   "张军 南京"   → 退化页 0/10 条含"张军"，连查询主体都没命中 → 降级
    #   "红黑树 实现" → 正常页 10/10 条含"红黑树"，只是标题里没"实现"二字 → 正常
    # 两者在"tail 全不命中"上长得一模一样，唯一的区别是**开头那个词被用上了多少**。
    # 所以先看 head_ratio；只有连开头都没命中时才继续看 tail。
    # ⚠️ 不能反过来要求"tail 必须命中"：那会把 "红黑树 实现" 这 10 条好结果整页丢掉。
    head = segs[0]
    head_hits = _hits(texts, head)
    if head_hits / n >= min_head_ratio:
        return RelaxCheck(False, "head", head_hits, n)

    tail = segs[1:]
    hit = [s for s in tail if _hits(texts, s)]
    if hit:
        return RelaxCheck(False, "tail", len(hit), len(tail))
    return RelaxCheck(
        True, "tail", 0, len(tail),
        detail=(f"{n} 条候选里含「{head}」的只有 {head_hits} 条，"
                f"也没有任何一条含「{'」「'.join(tail)}」"),
    )


def needles_of(query: str) -> list[str]:
    """把查询展开成"任一条结果该含什么"的判定原语。

    规则：每个片段本身 + 长片段（>3 字）的全部 2-gram，去重保序。
    这是 `keep_related` 与 pipeline 的 `require` 正则**共用的同一口径** ——
    两处各写一套的话，"变体路径"和"单页路径"会筛出不同的集合，排查时无从对比。
    """
    out: list[str] = []
    seen: set[str] = set()
    for seg in query_segments(query):
        # 只有长片段才展开 2-gram。短片段（"张军""红黑树"）本身就是完整的词，
        # 再拆成"张军"→"张军"、"红黑树"→"红黑/黑树" 会让"红黑人口库"这种
        # 只沾两个字的页面也被判为相关 —— 那正是我们想挡掉的东西。
        parts = [seg]
        if len(seg) > 3:
            parts.extend(bigrams(seg))
        for nd in parts:
            if nd not in seen:
                seen.add(nd)
                out.append(nd)
    return out


def keep_related(
    candidates: Sequence[Candidate],
    query: str,
) -> tuple[list[Candidate], int]:
    """把"整页没退化、但个别条目跑偏"的那几条剔掉。

    口径与 check_relaxed 一致：只要候选的 标题+摘要 命中查询里的**任一**原语
    （片段本身，或长片段的任一二字片段），就算相关。
    这是"逐条体检"，用在单页路径上 —— 变体路径由 expand 的 require 做同一件事。

    返回 (保留下来的候选, 被剔掉的条数)。
    """
    needles = needles_of(query)
    if not needles:
        return list(candidates), 0

    kept: list[Candidate] = []
    dropped = 0
    for c in candidates:
        text = f"{c.title}\n{c.snippet}".casefold()
        if any(nd in text for nd in needles):
            kept.append(c)
        else:
            dropped += 1
    return kept, dropped


__all__ = [
    "RelaxCheck",
    "bigrams",
    "check_relaxed",
    "keep_related",
    "needles_of",
    "query_segments",
]
