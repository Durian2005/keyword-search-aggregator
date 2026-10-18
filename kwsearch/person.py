"""人名模式：裸姓名捞一次池子，结果内归因，输出候选人卡片。

**为什么查询侧只能裸姓名**（2026-10-18 实测，见 discover/quality.py）：
Bing 对"人名 + 任何第二个词"会静默降级成第一个词的字面结果
（"张军 某某大学" → 返回"张"的百科/字典/姓氏页），HTTP 200、无提示。
所以重名问题在查询层无解，收窄必须发生在结果侧 —— 也就是本模块。

三层判定：
  1) 强锚点（机构）：标题/摘要里出现用户给的机构 → 这个人大概率属于该单位
  2) 弱锚点（方向 / 身份）：网络安全、教授、博导 …… 只能说明"像这个人"
  3) 负锚点：演员/歌手、招聘、判决书 —— 同名不同人的常见来源

负锚点只在**没有强锚点**时否决：机构对上之后，页面里出现"判决书"
可能是同一人的案子，不能因为一个法学名词就把真目标踢掉。

四档：
    confirmed  强锚点 + 至少一个弱锚点（机构与身份互相印证）
    likely     只有强锚点（机构对了，身份没说清）
    suspect    只有弱锚点（同方向但机构没对上 —— 同一领域里还有别人）
    unrelated  都没有，或负锚点且无强锚点

**红线：不同机构绝不合并。** 卡片按命中的机构分组，没有任何机构命中的
归入"未匹配机构"，报告里折叠为"同名未归因"。把两个单位的人合成一张卡，
比漏掉一个人更糟 —— 那是凭空造出一个不存在的身份。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

TIER_CONFIRMED = "confirmed"
TIER_LIKELY = "likely"
TIER_SUSPECT = "suspect"
TIER_UNRELATED = "unrelated"

TIER_CN = {
    TIER_CONFIRMED: "确认",
    TIER_LIKELY: "很可能",
    TIER_SUSPECT: "疑似",
    TIER_UNRELATED: "同名不相关",
}
TIER_RANK = {TIER_CONFIRMED: 3, TIER_LIKELY: 2, TIER_SUSPECT: 1, TIER_UNRELATED: 0}

# 负锚点：同名不同人的常见来源。
# ⚠️ 只在"没有强锚点"时作为否决依据（判 unrelated），不能一票否决有机命中。
NEGATIVE_HINTS: dict[str, tuple[str, ...]] = {
    "演艺": ("演员", "歌手", "导演", "编剧", "主持人", "艺人", "偶像", "主演", "演员表"),
    "招聘": ("招聘", "求职", "简历模板", "岗位职责", "薪资", "猎头", "实习生"),
    "法律": ("判决书", "裁判文书", "失信被执行", "开庭公告"),
}

UNMATCHED_LABEL = "未匹配机构"

_W = 3      # 强锚点权重
_W_WEAK = 1
_W_NEG = 2


@dataclass(frozen=True, slots=True)
class PersonQuery:
    """一次人名任务的输入。orgs/fields/roles 只用于结果归因，绝不拼进查询。"""

    name: str
    orgs: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()

    @property
    def weak_anchors(self) -> tuple[str, ...]:
        return self.fields + self.roles

    @property
    def has_anchors(self) -> bool:
        return bool(self.orgs or self.fields or self.roles)


def parse_person(
    name: str,
    orgs: Sequence[str] = (),
    fields: Sequence[str] = (),
    roles: Sequence[str] = (),
) -> PersonQuery:
    """构造 PersonQuery。支持重复传参与逗号（中英文）分隔两种写法。"""

    def _flat(vals: Sequence[str]) -> tuple[str, ...]:
        out: list[str] = []
        for v in vals or ():
            for part in re.split(r"[,，]", str(v)):
                part = part.strip()
                if part and part not in out:
                    out.append(part)
        return tuple(out)

    return PersonQuery(
        name=(name or "").strip(),
        orgs=_flat(orgs),
        fields=_flat(fields),
        roles=_flat(roles),
    )


@dataclass(slots=True)
class Evidence:
    title: str
    url: str
    matched: tuple[str, ...]


@dataclass(slots=True)
class CandidateCard:
    label: str                  # 命中的机构原文 / "未匹配机构"
    tier: str = TIER_UNRELATED
    score: int = 0
    org_hits: int = 0
    weak_hits: int = 0
    docs: int = 0
    negatives: tuple[str, ...] = ()
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Judgment:
    """单条候选的归因结论。"""

    tier: str
    matches: tuple[str, ...]    # 命中的锚点原文（机构在前）
    orgs: tuple[str, ...]
    weak: tuple[str, ...]
    negatives: tuple[str, ...]
    score: int


def judge(title: str, snippet: str, person: PersonQuery) -> Judgment:
    """给一条候选定档。纯字符串匹配 —— 不做实体消歧，也不猜。"""
    text = f"{title} {snippet}"
    orgs = tuple(o for o in person.orgs if o and o in text)
    weak = tuple(a for a in person.weak_anchors if a and a in text)
    negatives = tuple(
        kind for kind, words in NEGATIVE_HINTS.items()
        if any(w in text for w in words)
    )

    score = _W * len(orgs) + _W_WEAK * len(weak) - (_W_NEG * len(negatives) if not orgs else 0)
    if orgs and weak:
        tier = TIER_CONFIRMED
    elif orgs:
        tier = TIER_LIKELY
    elif negatives:
        tier = TIER_UNRELATED
    elif weak:
        tier = TIER_SUSPECT
    else:
        tier = TIER_UNRELATED
    return Judgment(
        tier=tier, matches=orgs + weak, orgs=orgs, weak=weak,
        negatives=negatives, score=max(0, score),
    )


def build_cards(
    person: PersonQuery,
    items: Sequence[tuple[Any, Judgment]],
    *,
    max_evidence: int = 5,
) -> list[CandidateCard]:
    """把候选按命中的机构分组，产出候选人卡片。

    items：[(candidate, judgment)] —— candidate 需要有 .title / .url_raw 或 .url_canon。
    没有机构命中的候选归入"未匹配机构"一张卡（tier 保持无关/疑似），
    报告里折叠 —— 它们仍可能是有价值的线索，但不能算"确认的候选人"。
    """
    buckets: dict[str, CandidateCard] = {}
    for cand, j in items:
        label = " · ".join(j.orgs) if j.orgs else UNMATCHED_LABEL
        card = buckets.get(label)
        if card is None:
            card = buckets[label] = CandidateCard(label=label, tier=TIER_UNRELATED)
        if TIER_RANK[j.tier] > TIER_RANK[card.tier]:
            card.tier = j.tier
        card.score += j.score
        card.org_hits += len(j.orgs)
        card.weak_hits += len(j.weak)
        card.docs += 1
        if j.negatives and not card.negatives:
            card.negatives = j.negatives
        if len(card.evidence) < max_evidence:
            card.evidence.append(Evidence(
                title=str(getattr(cand, "title", "") or ""),
                url=str(getattr(cand, "url_canon", "") or getattr(cand, "url_raw", "") or ""),
                matched=j.matches,
            ))

    cards = list(buckets.values())
    cards.sort(key=lambda c: (
        -TIER_RANK.get(c.tier, 0), -c.docs, -c.score, c.label,
    ))
    return cards


def summarize(cards: Sequence[CandidateCard]) -> dict[str, int]:
    """各档卡片数，给 CLI 与报告的概览用。"""
    out = {tier: 0 for tier in TIER_RANK}
    for c in cards:
        if c.label == UNMATCHED_LABEL:
            continue
        out[c.tier] = out.get(c.tier, 0) + 1
    return out
