"""正文抽取：从整页 HTML 里找出"这篇文章"的那一块。

不引 trafilatura / readability 这类成品库（避免为一段正文抽取背上一整棵依赖树，
也避免它们对中文站点的启发式完全不可控）。算法是 Readability 的简化版，只依赖两条判据：

    段落是最强的内容信号 —— 正文由若干 <p> 组成，导航由若干 <a> 组成
    链接密度是最强的噪声信号 —— 导航、推荐位、标签云的文字几乎都在 <a> 里

做法：先给每个文本块（p/pre/blockquote/td）打分，把分累加到父容器（满分）和
祖父容器（半分），再按链接密度与 class 语义调整，取最高分。

    block_score = 1 + 逗号数 + min(长度 / 100, 3)
    容器最终分 = Σblock × (1 - 链接密度 × 0.8) × class 系数

为什么不能"取最长文本块"：整页 <body> 永远最长。中文站常见的
`<div class="article"><div class="inner"><p>…</p></div></div>` 必须靠打分
才能挑到 inner —— body 只拿到一半分，inner 拿满分。

⚠️ 这一层的产出**只是候选正文**。它不是"版权上可以随便转的全文"，
   入库仍然只存摘要（`extract.summary_chars`），全文不落盘。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from kwsearch.htmlx import Node, find, iter_nodes, normalize, parse_html, text

# 参与打分的"文本块"。刻意不含 li：列表页/导航的 li 会把分全灌给菜单容器。
_TEXT_BLOCK_TAGS = frozenset({"p", "pre", "blockquote", "td", "dd"})

# 行内元素不能当正文容器：`<a>` 是链接、`<span>` 是片段。
# 实测 bilibili 视频页的正文是 `<span class="desc-info-text">`，它自己不配当容器，
# 但它的**父 div** 配 —— 所以它的分要传给父级，而不是把它选出来。
_INLINE_TAGS = frozenset({
    "a", "span", "strong", "em", "b", "i", "u", "s", "font", "small", "big",
    "label", "code", "mark", "sup", "sub", "abbr", "time", "cite", "q", "kbd",
})

# 标题标签同样不能当正文容器：它们的文本要计入父级，但自己不是"文章正文"。
# 实测 bilibili 短视频页：一旦允许 h1 参评，它会靠"标题 43 字"赢过所有真实内容。
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "legend"})
_NOT_CONTAINER = _INLINE_TAGS | _HEADING_TAGS

# 一定不是正文：脚本、导航、页脚、表单、嵌入控件。
# head/title 也要排除：它们的裸文本会满足"裸文本块"的条件，混进候选榜。
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "nav", "aside", "footer", "header", "form", "button", "select", "textarea",
    "audio", "video", "object", "embed", "head", "title", "meta", "link",
})

# 需要换行的块级标签（决定段落边界）
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
    "section", "article", "blockquote", "pre", "dd", "dt", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr",
})

# class / id 里出现这些 token（或以此开头）的节点视为噪声
_NOISE_TOKENS = frozenset({
    "comment", "comments", "recommend", "recommends", "related", "share",
    "sidebar", "banner", "advert", "advertisement", "ad", "ads", "copyright",
    "footer", "foot", "nav", "navbar", "menu", "breadcrumb", "crumb",
    "pagination", "pager", "tags", "tag", "toolbar", "popup", "modal",
    "subscribe", "newsletter", "hot", "rank", "ranking", "cookie",
    # "title" 是"推荐位的标题"最常见的位置（实测 bilibili 的 div.title-txt
    # 被当成正文抽出 71 字）。标题属于内容，但它不该是正文容器。
    "title", "headline",
})

# class / id 里出现这些 token 的节点更可能是正文容器
_CONTENT_TOKENS = frozenset({
    "article", "content", "post", "entry", "main", "text", "body",
    "rich_media", "markdown", "story", "detail",
})

_MIN_PARA = 25          # 短于此的 <p> 不计分（版权声明、导航提示）
_MIN_BARE = 40          # 裸文本块（div/span 里直接放的文字）的最小长度
_PAGE_SHARE_MAX = 0.7   # 容器文本超过整页的 70% → 它是"整页壳"，不是正文
_MAX_LINK_RATIO = 0.6   # 链接密度超过这个值直接淘汰：正文不会有一半以上的字在 <a> 里
_MAX_CANDIDATES = 80    # 只对分数最高的这些容器算链接密度，避免 O(n²)
_SCORE_THRESHOLD = 3.0  # 低于此分认为"没找到正文"，退到最高分容器


@dataclass(slots=True)
class Article:
    """一次抽取的结果。container 是给人看的调试线索，不参与业务判断。"""

    text: str
    title: str = ""
    container: str = ""
    paragraphs: int = 0
    score: float = 0.0
    link_ratio: float = 0.0
    # high   找到了过阈值的容器，且链接密度正常
    # medium 退到了最高分容器（分数不足，可能是推荐位）
    # low    整页兜底
    confidence: str = "high"

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def ok(self) -> bool:
        return self.chars >= 50

    def summary(self, limit: int = 500) -> str:
        """把正文压成一段摘要（保留段落感，不用省略号硬切词）。

        开头的目录块 / 分隔线 / 标题式短行要先跳过：不少技术站的第一段是目录
        （`目录 / 一、红黑树简介 / 二、为什么需要红黑树？…`），直接取前 500 字
        会得到一串章节名 —— 实测 CSDN 就是这样，摘要变成了目录，
        比搜索引擎给的那句话还不如。
        """
        paras = [p.strip() for p in self.text.split("\n") if p.strip()]
        start = summary_start(paras)
        flat = re.sub(r"\s*\n\s*", " ", "\n".join(paras[start:])).strip()
        return flat[:limit]


# ---------------------------------------------------------------------------
#  摘要预处理
# ---------------------------------------------------------------------------
# 目录条目：`一、红黑树简介` `二. 特性` `4.1 旋转`（只认编号本身，不认后面的字）
_TOC_MARK_RE = re.compile(r"(?:[一二三四五六七八九十]{1,3}[、.．]|\d{1,2}[、.．])")
# 整段只有分隔符：`=====` `-----` `*****` `/////`
_DECOR_ONLY_RE = re.compile(r"^[\s/\-_=*#~·.。,，、—|]+$")
# 目录的标题行：`目录` / `本文目录` / `Contents`
_TOC_TITLE_RE = re.compile(
    r"^(?:目录|本文目录|文章目录|contents|table of contents|toc)\s*[:：]?$", re.I)
# 句子标点。开头一行没有这些，基本就是标题而不是正文
_SENTENCE_PUNCT_RE = re.compile(r"[，。！？；：,.!?;:]")


def _toc_block_len(paras: list[str], max_items: int = 40) -> int:
    """开头是不是一整块目录；是就返回它之后的下标，不是返回 0。

    必须按"连续多段"判，不能按单段判：实测 CSDN 的目录是**一行一段**
    （`目录` / `一、红黑树简介` / `二、为什么需要红黑树？` …），
    每段只有 2~11 个字，单独看任何一段都不像目录。
    """
    n = len(paras)
    i = 0
    while i < n and _DECOR_ONLY_RE.match(paras[i]):
        i += 1
    had_title = False
    if i < n and _TOC_TITLE_RE.match(paras[i]):
        i += 1
        had_title = True

    j = i
    run = 0
    while j < n and run < max_items:
        p = paras[j]
        if len(p) <= 40 and _TOC_MARK_RE.search(p):
            run += 1
            j += 1
        else:
            break

    if run >= 3:
        return j
    if had_title and run == 0:
        return i        # 光一个"目录"两个字，跳掉它本身就够了
    return 0


def _is_heading_like(para: str) -> bool:
    """开头的短行且没有句读 → 多半是混进正文的 h1/h2，不是正文第一句。"""
    return len(para) <= 14 and not _SENTENCE_PUNCT_RE.search(para)


def summary_start(paras: list[str]) -> int:
    """摘要从第几段开始。宁可不跳，也不要把真正的第一句跳掉。"""
    if not paras:
        return 0
    start = _toc_block_len(paras)
    if start:
        return start if start < len(paras) else 0

    j = 0
    limit = min(len(paras), 5)
    while j < limit and _is_heading_like(paras[j]):
        j += 1
    return j if j < len(paras) else 0


# ---------------------------------------------------------------------------
#  转载模板剥离
# ---------------------------------------------------------------------------
# 转载站往首尾加的那几段话：免责声明、来源署名、编辑署名、公众号引导。
# 只在**首尾**、**够短**的段落上认 —— 正文中间提到"版权""删除"一律不算。
#
# 特征词刻意写窄：单独一个"删除"会命中"红黑树支持插入和删除"，
# 单独一个"编辑"会命中"编辑器"。模板话术是用整句出现的，就按整句认。
_BOILER_HINT_RE = re.compile(
    r"免责声明|版权声明|版权所有|侵删|请联系(?:我们)?(?:删除|处理)"
    r"|本文(?:仅代表|来源|转载|转自|系|由)|原文(?:链接|地址|出处)"
    r"|责任编辑|责编[:：]|编辑[:：]|来源[:：]|作者[:：]"
    r"|公众号|扫码|二维码|关注我们|长按识别"
    r"|未经授权|如需转载|转载请注明|转载自|有删改|文中观点"
)
_BOILER_MAX_LINE = 200      # 段落超过这个长度就不判模板（那是正文）
_BOILER_MAX_TAIL = 6        # 尾部最多剥 6 段
_BOILER_MAX_HEAD = 3        # 头部最多剥 3 段
_BOILER_MAX_CHARS = 800     # 首尾合计最多剥这么多字


def _is_boiler(para: str) -> bool:
    return len(para) <= _BOILER_MAX_LINE and bool(_BOILER_HINT_RE.search(para))


def trim_boilerplate(text: str) -> str:
    """剥掉首尾的转载模板段，返回剩下的正文。

    为什么要在意这件事：转载稿几乎都会在末尾挂一段免责声明/引导语（100~300 字），
    于是它的正文比原文**更长**。S5 的代表原先按正文长度选，结果转载版顶替原文
    成了代表 —— 报告里那条"原文链接"其实指向某个二手站点。

    三条自保规则：
      - 只动首尾，且只动"短段 + 强特征词"；正文中段一律不碰。
      - 总剥离量封顶（首 3 段、尾 6 段、合计 800 字）。
      - 剥完只剩模板、或者什么都不剩，就认为判错了，原样返回 ——
        宁可代表选错，也不能把一篇文章删成一句话。
        ⚠️ 不按"剥掉超过一半就退回"来保护：短讯 + 长免责声明是真实存在的形态，
           按比例保护会把那种转载版重新算得比原文更长，正好放跑要修的问题。
    """
    raw = text or ""
    paras = raw.split("\n")
    idx = [i for i, p in enumerate(paras) if p.strip()]
    if len(idx) <= 1:
        return raw

    budget = _BOILER_MAX_CHARS
    head = 0
    while head < len(idx) - 1 and head < _BOILER_MAX_HEAD:
        p = paras[idx[head]].strip()
        if not _is_boiler(p) or len(p) > budget:
            break
        budget -= len(p)
        head += 1

    tail = 0
    while tail < len(idx) - head - 1 and tail < _BOILER_MAX_TAIL:
        p = paras[idx[len(idx) - 1 - tail]].strip()
        if not _is_boiler(p) or len(p) > budget:
            break
        budget -= len(p)
        tail += 1

    if not head and not tail:
        return raw
    keep = paras[idx[head]:idx[len(idx) - 1 - tail] + 1]
    if not keep or all(_is_boiler(p.strip()) for p in keep):
        return raw
    return "\n".join(keep)


def core_chars(text: str) -> int:
    """正文的"有效长度"（剥掉转载模板之后）。代表选取用它，而不是 body_chars。

    ⚠️ 返回 0 只可能来自空文本；调用方拿到 0 必须退回 body_chars，
       否则老数据（没有 body_core 的）会被当成"全文都是模板"。
    """
    return len(trim_boilerplate(text))


# ---------------------------------------------------------------------------
#  结构工具
# ---------------------------------------------------------------------------
def _index(root: Node) -> tuple[dict[int, Node], dict[int, Node]]:
    """一次遍历建 parent 索引。Node 没有 parent 指针，而打分必须往上传分。

    不用"递归时携带祖先 tuple"的写法：节点上万时每次 path + (node,) 都要复制，
    复杂度一样但常数大得多。
    """
    nodes: dict[int, Node] = {}
    parent: dict[int, Node] = {}
    stack = [root]
    while stack:
        cur = stack.pop()
        nodes[id(cur)] = cur
        for c in cur.children:
            if isinstance(c, Node) and c.tag not in _SKIP_TAGS:
                parent[id(c)] = cur
                stack.append(c)
    return nodes, parent


def _tokens(node: Node) -> set[str]:
    raw = f"{node.attrs.get('class', '')} {node.attrs.get('id', '')}".lower()
    return {t for t in re.split(r"[\s\-_]+", raw) if t}


def _has_token(node: Node, vocab: frozenset[str]) -> bool:
    """token 级精确匹配，不是子串匹配 —— 否则 class="canvas" 会被 'nav' 误伤。

    不需要 startswith：`_tokens` 已经把 `comment-list` 拆成 {comment, list}。
    直接查属性更快，因为 collect_text 会对每个节点调它，上万节点时省下的很可观。
    """
    raw = node.attrs.get("class") or node.attrs.get("id") or ""
    if not raw:
        return False
    return any(t in vocab for t in re.split(r"[\s\-_]+", raw.lower()) if t)


def _link_chars(node: Node) -> int:
    """节点内链接文本的总长度。

    节点自己就是 <a> 时也要算进去：否则"拿 a 当容器"这种情况链接密度恒为 0，
    导航/推荐位就完全没有惩罚了。
    """
    total = len(text(node)) if node.tag == "a" else 0
    for n in iter_nodes(node):
        if n.tag == "a":
            total += len(text(n))
    return total


def _bare_text(node: Node) -> str:
    """直接挂在这个元素上的文字（不递归子元素）。

    中文站有大量"没有 <p>"的正文：`<div class="desc">一段话<br>又一段</div>`，
    或者 bilibili 那种 `<span class="desc-info-text">222 字</span>`。
    只认 <p> 的算法对这类页面一无所获。
    """
    return normalize("".join(s for s in node.children if isinstance(s, str)))


def _punctuation(s: str) -> int:
    return (s.count("，") + s.count("。") + s.count("；") + s.count("：")
            + s.count(",") + s.count(".") + s.count(";") + s.count("?")
            + s.count("！") + s.count("？"))


def _block_score(t: str) -> float:
    return 1.0 + _punctuation(t) + min(len(t) / 100.0, 3.0)


def _class_factor(node: Node) -> float:
    """class/id 语义只是乘数，不直接决定胜负：命名不可靠，但也不该完全无视。"""
    if _has_token(node, _NOISE_TOKENS):
        return 0.25
    if _has_token(node, _CONTENT_TOKENS):
        return 1.3
    return 1.0


def _describe(node: Node) -> str:
    cls = node.attrs.get("class", "").split()
    nid = node.attrs.get("id", "")
    if cls:
        return f"{node.tag}.{cls[0]}"
    if nid:
        return f"{node.tag}#{nid}"
    return node.tag


# ---------------------------------------------------------------------------
#  打分
# ---------------------------------------------------------------------------
def score_containers(root: Node) -> dict[int, tuple[float, Node]]:
    """返回 {id(容器): (分数, 容器节点)}。纯计算，不改树。

    两类文本块分别计分，共同点是"分数向上传给容器，不留给文本块自己"：

        结构化段落  <p>/<pre>/<blockquote>/<td>/<dd> 且 ≥25 字 → 父满分割，祖父半分
        裸文本块    元素里**直接**挂着 ≥60 字文字（没有 <p> 包裹）→ 自己 1.0，父 0.6

    裸文本块之所以要给自己计分：`<div class="content">200 字</div>` 这种结构里，
    理想容器就是 div 本身，全给父级会把 body 选出来。
    """
    nodes, parent = _index(root)
    page_chars = len(collect_text(root)) or 1
    raw: dict[int, float] = {}

    def add(nid: int | None, amount: float) -> None:
        if nid is not None:
            raw[nid] = raw.get(nid, 0.0) + amount

    for nid, node in nodes.items():
        if node.tag in _TEXT_BLOCK_TAGS:
            t = text(node)
            if len(t) >= _MIN_PARA:
                s = _block_score(t)
                p1 = parent.get(nid)
                add(id(p1) if p1 is not None else None, s)
                p2 = parent.get(id(p1)) if p1 is not None else None
                add(id(p2) if p2 is not None else None, s * 0.5)
            continue

        bare = _bare_text(node)
        if len(bare) >= _MIN_BARE:
            s = _block_score(bare)
            if node.tag not in _NOT_CONTAINER:
                add(nid, s)
            p1 = parent.get(nid)
            add(id(p1) if p1 is not None else None, s * 0.6)

    if not raw:
        return {}

    top = sorted(raw.items(), key=lambda kv: -kv[1])[:_MAX_CANDIDATES]
    out: dict[int, tuple[float, Node]] = {}
    for nid, s in top:
        node = nodes[nid]
        if node.tag in _NOT_CONTAINER:
            continue
        t = collect_text(node)          # 用 collect_text 而不是 text：script 不能算进正文
        if not t:
            continue
        lr = min(_link_chars(node) / len(t), 1.0)
        if lr > _MAX_LINK_RATIO:
            continue          # 推荐位 / 导航 / 标签云：文字一半以上在链接里，不是正文
        score = s * (1.0 - lr * 0.8) * _class_factor(node)
        # 占整页七成以上的容器是"整页壳"（body、视频页的外层 div），不是正文。
        # 实测 bilibili：外层 div 几乎覆盖整页，靠这条把它压下去，否则它靠累计分稳赢。
        if len(t) / page_chars > _PAGE_SHARE_MAX:
            score *= 0.3
        out[nid] = (score, node)
    return out


def best_container(root: Node) -> tuple[Node | None, float, float]:
    """返回 (容器, 分数, 链接密度)。找不到可信容器时 Node 为 None。"""
    scored = score_containers(root)
    if not scored:
        return None, 0.0, 0.0
    nid, (score, node) = max(scored.items(), key=lambda kv: kv[1][0])
    if score < _SCORE_THRESHOLD:
        return None, score, 0.0
    t = collect_text(node)
    lr = min(_link_chars(node) / len(t), 1.0) if t else 0.0
    return node, score, lr


# ---------------------------------------------------------------------------
#  文本收集
# ---------------------------------------------------------------------------
def _tidy(s: str) -> str:
    lines = [normalize(x) for x in s.split("\n")]
    return "\n".join(x for x in lines if x)


def collect_text(node: Node, *, skip_noise: bool = True) -> str:
    """按文档顺序取文本，块级元素之间断行，跳过脚本/导航/噪声 class。"""
    parts: list[str] = []
    stack: list[Node | str | None] = [node]
    while stack:
        cur = stack.pop()
        if cur is None:
            parts.append("\n")
            continue
        if isinstance(cur, str):
            parts.append(cur)
            continue
        if cur.tag in _SKIP_TAGS:
            continue
        if skip_noise and cur is not node and _has_token(cur, _NOISE_TOKENS):
            continue
        if cur.tag in _BLOCK_TAGS:
            parts.append("\n")
            stack.append(None)          # 块结束标记：先入后出，对应块后换行
        stack.extend(reversed(cur.children))
    return _tidy("".join(parts))


# ---------------------------------------------------------------------------
#  标题
# ---------------------------------------------------------------------------
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
# "标题_站点名" / "标题 - 站点名" / "标题 | 站点名" 三种最常见的拼接
_TITLE_SPLIT_RE = re.compile(r"\s*[_|｜]\s*|\s+[-–—]\s+|\s+::\s+")
_MIN_TITLE, _MAX_TITLE = 4, 150


def _title_tag(html: str) -> str:
    m = _TITLE_RE.search(html[:30_000])
    if not m:
        return ""
    return normalize(re.sub(r"<[^<>]{0,200}>", "", m.group(1)))


def extract_title(html: str, root: Node | None = None) -> str:
    """优先 <h1>，否则从 <title> 里切出最像正文标题的那一段。

    默认取**第一段**而不是最长段：站点名几乎总在后面，而最长段经常是
    `红黑树详解_哔哩哔哩_bilibili` 里的 "bilibili"。只有当第一段短得不像标题
    （< 4 字，说明站点名被放在了前面）才退到最长段。
    """
    root = root if root is not None else parse_html(html)
    h1 = find(root, "h1")
    if h1 is not None:
        t = text(h1)
        if _MIN_TITLE <= len(t) <= _MAX_TITLE:
            return t

    raw = _title_tag(html)
    if not raw:
        return ""
    parts = [p.strip() for p in _TITLE_SPLIT_RE.split(raw) if p.strip()]
    if not parts:
        return raw[:_MAX_TITLE]
    first = parts[0]
    # 全大写纯英文（CSDN / BLOG / NEWS）基本一定是站点名，不是文章标题
    is_site_name = first.isascii() and first.isupper()
    if len(first) >= _MIN_TITLE and not is_site_name:
        return first[:_MAX_TITLE]
    return max(parts, key=len)[:_MAX_TITLE]


# ---------------------------------------------------------------------------
#  主入口
# ---------------------------------------------------------------------------
def extract_article(
    html: str,
    *,
    root: Node | None = None,
    title_hint: str = "",
) -> Article:
    """整页 HTML → 正文文本 + 标题 + 调试线索。

    title_hint 是 SERP 给的标题：只在页面自己抽不出标题时使用，
    反过来（页面标题优先）是刻意的 —— SERP 标题常被搜索引擎改写。
    """
    if not html:
        return Article(text="", title=title_hint)

    root = root if root is not None else parse_html(html)
    title = extract_title(html, root) or title_hint

    node, score, lr = best_container(root)
    confidence = "high"
    if node is None:
        # 没过阈值不代表没有局部最优点。退一步用"分数最高的那一块"，
        # 它通常仍是最像正文的区域，比整页 body 干净得多
        # （实测 bilibili 短视频页：直接兜底 body 会把"首页/番剧/直播/会员购"全带进来）。
        # 但此时**必须降级置信度**：这一块很可能是推荐位而不是正文。
        confidence = "medium"
        scored = score_containers(root)
        if scored:
            score, node = max(scored.values(), key=lambda v: v[0])
            t0 = collect_text(node)
            lr = min(_link_chars(node) / len(t0), 1.0) if t0 else 0.0
        else:
            body = find(root, "body") or root
            btext = collect_text(body)
            blr = min(_link_chars(body) / len(btext), 1.0) if btext else 0.0
            return Article(
                text=btext,
                title=title,
                container="body(兜底)",
                paragraphs=btext.count("\n") + 1 if btext else 0,
                score=score,
                link_ratio=round(blr, 2),
                confidence="low",
            )

    body_text = collect_text(node)
    return Article(
        text=body_text,
        title=title,
        container=_describe(node),
        paragraphs=body_text.count("\n") + 1 if body_text else 0,
        score=round(score, 1),
        link_ratio=round(lr, 2),
        confidence=confidence,
    )


__all__ = [
    "Article",
    "best_container",
    "collect_text",
    "core_chars",
    "extract_article",
    "extract_title",
    "score_containers",
    "trim_boilerplate",
]
