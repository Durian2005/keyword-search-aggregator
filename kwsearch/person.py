"""人名模式：裸姓名捞一次池子，结果内归因，输出候选人卡片。

**为什么查询侧只能裸姓名**（2026-10-18 实测，见 discover/quality.py）：
Bing 对"人名 + 任何第二个词"会静默降级成第一个字的结果
（"张军 某某大学" → 返回"张"的百科/字典/姓氏页），HTTP 200、无提示。
所以重名问题在查询层无解，收窄必须发生在结果侧 —— 也就是本模块。

判定用三层锚点：
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

P1 补的三件事（都是"真实姓名一跑就露馅"的）：

  1. **姓名必须真的出现**。机构名对得上、但整页没有这个姓名，多半是招生简章、
     实验室介绍、师资列表这类"机构页面"，不是这个人的证据。这类条目不进卡片，
     只计数（`name_missing`）—— 不记账的话，用户会以为"一条都没找到"。
  2. **多机构命中要选主机构，不能拼成一张卡**。一个人同时在"A 大学"和"B 研究院"
     出现，可能是兼职、跳槽、合作、同名，拼成 "A · B" 等于造出第三种身份。
     按"标题命中优先 → 命中次数多优先 → 用户给的顺序"选一个主机构，
     其余记为"还提到"，附在卡片上供人判断。
  3. **证据要带时间并按时间排**。只有标题的证据串看不出"这个人什么时候在哪"，
     日期即使只精确到天，也能把"2021 年在甲大学"和"2025 年在乙公司"分开。
"""
from __future__ import annotations

import re
import unicodedata
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

# 机构名末尾是"大学"时，紧跟"生"的多半是"大学生"而不是这所大学：
# "北京大学生就业指导中心"里的"北京大学"是假命中。只护这一种形态 ——
# 通用边界判断需要词典，宁可漏掉一个假命中，也不要把真机构名保护成筛子。
_ORG_TAIL_GUARD: dict[str, tuple[str, ...]] = {"大学": ("生",)}

# 太泛的锚点会命中一切结果，等于没给锚点。只提示，不阻止 —— 用户可能就想搜宽一点。
_GENERIC_ORGS = frozenset({
    "大学", "学院", "学校", "研究院", "研究所", "实验室", "公司", "集团", "医院", "中心",
})

_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    """比对用的归一化：NFKC（全角→半角）+ 去空白 + casefold。

    去空白是必需的：网页里的中文姓名常被写成"张 军"（排版换行、表格对齐），
    不去空白就永远匹配不上；"Zhang San" 这类英文名同理。
    """
    return _WS_RE.sub("", unicodedata.normalize("NFKC", s or "")).casefold()


def mentions_name(name: str, *texts: str) -> bool:
    """文本里有没有出现这个姓名（归一化后按子串判）。"""
    n = _norm(name)
    return bool(n) and any(n in _norm(t) for t in texts)


def org_hits(org: str, text: str) -> int:
    """机构名在文本里出现了几次（已排除"北京大学生"这类假命中）。"""
    o, t = _norm(org), _norm(text)
    if len(o) < 2 or not t:
        return 0
    guard: tuple[str, ...] = ()
    for suffix, bad in _ORG_TAIL_GUARD.items():
        if o.endswith(suffix):
            guard = bad
            break
    n, start = 0, 0
    while True:
        i = t.find(o, start)
        if i < 0:
            return n
        end = i + len(o)
        if not (guard and end < len(t) and t[end] in guard):
            n += 1
        start = end


def _plus(anchor: str, text: str) -> bool:
    a = _norm(anchor)
    return bool(a) and a in _norm(text)


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


def anchor_warnings(person: PersonQuery) -> list[str]:
    """锚点质量提示。只提示不阻止 —— 用户可能就想搜得宽一点。

    这几句都是"跑完才发现结果没法用"的常见原因，跑之前说出来能省一轮。
    """
    out: list[str] = []
    for o in person.orgs:
        if len(o) < 2:
            out.append(f"机构锚点「{o}」只有 {len(o)} 个字，几乎会命中所有结果")
        elif o in _GENERIC_ORGS:
            out.append(f"机构锚点「{o}」太泛，任何单位的页面都会命中，建议写全称")
    if person.orgs and not person.fields and not person.roles:
        out.append("只给了机构锚点：卡片最高只能到「很可能」，"
                   "补 --field / --role 才能到「确认」")
    if not person.orgs and person.has_anchors:
        out.append("没给机构锚点：所有结果只能按方向判成「疑似」，"
                   "彼此之间无法区分是不是同一个人")
    return out


@dataclass(slots=True)
class Evidence:
    title: str
    url: str
    matched: tuple[str, ...]
    when: str | None = None         # 发布日期（YYYY-MM-DD），未知为 None


@dataclass(slots=True)
class CandidateCard:
    label: str                  # 主机构原文 / "未匹配机构"
    tier: str = TIER_UNRELATED
    score: int = 0
    org_hits: int = 0
    weak_hits: int = 0
    docs: int = 0
    negatives: tuple[str, ...] = ()
    also_orgs: tuple[str, ...] = ()     # 同一批证据里还提到了哪些机构（不合并，只标注）
    first_when: str | None = None       # 最早一条证据的日期 → 活跃起点
    last_when: str | None = None
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
    name_present: bool = True   # 页面上有没有这个人名；False = 机构页面，不是这个人的证据
    primary_org: str | None = None
    also_orgs: tuple[str, ...] = ()


def _pick_primary(
    person: PersonQuery,
    orgs: tuple[str, ...],
    t_hits: dict[str, int],
    s_hits: dict[str, int],
) -> str | None:
    """多机构命中时选一个"主机构"。

    优先序：标题里出现过 → 出现次数多的 → 用户给的顺序在前。
    标题优先，是因为标题描述的是"这个页面在讲谁"；摘要里出现机构常常只是
    页面底部的院系导航。用户顺序兜底，保证同一批输入结果可复现。
    """
    if not orgs:
        return None
    order = {o: i for i, o in enumerate(person.orgs)}
    return min(orgs, key=lambda o: (
        0 if t_hits.get(o) else 1, -t_hits.get(o, 0), -s_hits.get(o, 0), order.get(o, 0),
    ))


def judge(title: str, snippet: str, person: PersonQuery) -> Judgment:
    """给一条候选定档。纯字符串匹配 —— 不做实体消歧，也不猜。"""
    text = f"{title} {snippet}"
    name_present = mentions_name(person.name, title, snippet)

    t_hits = {o: org_hits(o, title) for o in person.orgs}
    s_hits = {o: org_hits(o, snippet) for o in person.orgs}
    orgs = tuple(o for o in person.orgs if t_hits[o] or s_hits[o])
    weak = tuple(a for a in person.weak_anchors if _plus(a, text))
    negatives = tuple(
        kind for kind, words in NEGATIVE_HINTS.items()
        if any(_plus(w, text) for w in words)
    )

    score = _W * len(orgs) + _W_WEAK * len(weak) - (_W_NEG * len(negatives) if not orgs else 0)
    if not name_present:
        # 姓名不在页面上：机构再对也只是"某个机构的页面"，不是这个人的证据。
        tier = TIER_UNRELATED
    elif orgs and weak:
        tier = TIER_CONFIRMED
    elif orgs:
        tier = TIER_LIKELY
    elif negatives:
        tier = TIER_UNRELATED
    elif weak:
        tier = TIER_SUSPECT
    else:
        tier = TIER_UNRELATED

    primary = _pick_primary(person, orgs, t_hits, s_hits)
    return Judgment(
        tier=tier, matches=orgs + weak, orgs=orgs, weak=weak,
        negatives=negatives, score=max(0, score), name_present=name_present,
        primary_org=primary,
        also_orgs=tuple(o for o in orgs if o != primary),
    )


def _when_of(cand: Any) -> str | None:
    """证据日期：优先页面声明的 published_at，其次通道给的 published_hint。

    只取日期部分：报告是给人看的，精确到天足够；而且同一批里
    "2026-01-01" 和 "2026-01-01T08:00:00+08:00" 混着比大小会出怪结果。
    """
    for attr in ("published_at", "published_hint"):
        v = getattr(cand, attr, None)
        if v:
            return str(v)[:10]
    return None


def build_cards(
    person: PersonQuery,
    items: Sequence[tuple[Any, Judgment]],
    *,
    max_evidence: int = 5,
    stats: dict[str, int] | None = None,
) -> list[CandidateCard]:
    """把候选按"主机构"分组，产出候选人卡片。

    items：[(候选, judgment)] —— 候选要有 .title、.url_canon/.url_raw，
           以及 .published_at 或 .published_hint（有就带上，用于时间线）。
    stats：可选出参，填 {"total", "attributed", "name_missing"}。
           没出现姓名的那批必须能被数出来：它们不是"没找到"，
           而是"找到了、但归不到这个人头上"。

    没有机构命中的候选归入"未匹配机构"一张卡（tier 保持无关/疑似），
    报告里折叠 —— 它们仍可能是有价值的线索，但不能算"确认的候选人"。
    """
    buckets: dict[str, CandidateCard] = {}
    rows: dict[str, list[Evidence]] = {}
    name_missing = 0

    for cand, j in items:
        if not j.name_present:
            name_missing += 1
            continue
        label = j.primary_org or UNMATCHED_LABEL
        card = buckets.get(label)
        if card is None:
            card = buckets[label] = CandidateCard(label=label, tier=TIER_UNRELATED)
            rows[label] = []
        if TIER_RANK[j.tier] > TIER_RANK[card.tier]:
            card.tier = j.tier
        card.score += j.score
        card.org_hits += len(j.orgs)
        card.weak_hits += len(j.weak)
        card.docs += 1
        if j.negatives and not card.negatives:
            card.negatives = j.negatives
        if j.also_orgs:
            card.also_orgs = card.also_orgs + tuple(
                o for o in j.also_orgs if o not in card.also_orgs)

        when = _when_of(cand)
        if when:
            card.first_when = min(card.first_when or when, when)
            card.last_when = max(card.last_when or when, when)
        rows[label].append(Evidence(
            title=str(getattr(cand, "title", "") or ""),
            url=str(getattr(cand, "url_canon", "") or getattr(cand, "url_raw", "") or ""),
            matched=j.matches,
            when=when,
        ))

    for label, card in buckets.items():
        allrows = rows[label]
        # 有日期的按时间倒序（新的在前），没日期的垫底。
        # 这个顺序本身就是最朴素的"轨迹"：能看出他先在哪、后来去了哪。
        dated = sorted((e for e in allrows if e.when),
                       key=lambda e: (e.when or "", e.title), reverse=True)
        undated = [e for e in allrows if not e.when]
        card.evidence = (dated + undated)[:max_evidence]

    cards = list(buckets.values())
    cards.sort(key=lambda c: (
        -TIER_RANK.get(c.tier, 0), -c.docs, -c.score, c.label,
    ))
    if stats is not None:
        stats["total"] = len(items)
        stats["name_missing"] = name_missing
        stats["attributed"] = len(items) - name_missing
    return cards


def summarize(cards: Sequence[CandidateCard]) -> dict[str, int]:
    """各档卡片数，给 CLI 与报告的概览用。"""
    out = {tier: 0 for tier in TIER_RANK}
    for c in cards:
        if c.label == UNMATCHED_LABEL:
            continue
        out[c.tier] = out.get(c.tier, 0) + 1
    return out
