"""容错 DOM（标准库 html.parser 实现），发现层与抽取层共用。

只提供四个能力，够用即止：
    parse_html(html) -> Node
    find(node, tag, cls) / find_all(node, tag, cls)
    text(node)                子树叶全部文本，折叠空白
    node.attrs / node.classes 取属性

为什么不用 lxml / BeautifulSoup：
  1) 项目已定"抽取规则自写、不引正文抽取库"，链路里不想再出现第二个解析器；
  2) 这里要的东西极少（找容器、取属性、取文本），llibxml2 的容错优势在本项目
     的目标页面上换不来相应收益，还要多背一个 C 扩展依赖；
  3) 标准库 HTMLParser 对"属性不加引号、标签不闭合"的容忍度足够 ——
     Bing 的 `<li class="b_algo" data-id iid=SERP.5338>` 正是无引号属性。

代价（明确写出来，避免以后误用）：
  - 不实现命名空间、不支持 CSS 选择器、不保证与浏览器 DOM 完全一致；
  - 只用于"找容器 + 取文本"，不用于需要精确 DOM 结构的场景。

遍历一律用显式栈、不用递归：真实页面嵌套几百层很常见，递归会撞 py 的 1000 层上限。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape as _unescape
from html.parser import HTMLParser

# 自闭合标签：不压栈
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# 可省略闭合标签：新开同类时自动关掉上一个（只处理最常见的一层，够用）
_AUTO_CLOSE: dict[str, frozenset[str]] = {
    "li": frozenset({"li"}),
    "p": frozenset({"p"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "tr": frozenset({"tr"}),
    "option": frozenset({"option"}),
}

# 空白类字符：普通空白 + nbsp + 各类间隔符 + 零宽。\u2002 是 HTML 的 &ensp;（Bing 摘要里大量使用）
_WS_RE = re.compile(r"[ \t\r\n\f\v\u00a0\u1680\u2000-\u200d\u2028\u2029\u202f\u205f\u3000]+")


@dataclass(slots=True)
class Node:
    """一个元素节点。children 里混放子元素和文本片段，顺序即文档顺序。"""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(self.attrs.get("class", "").split())

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def __repr__(self) -> str:  # 调试时别把整棵树打进日志
        cls = f".{'.'.join(self.classes)}" if self.classes else ""
        return f"<{self.tag}{cls} children={len(self.children)}>"


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # 实体(&amp; &#0183; &ensp;)在解析期就还原
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(Node(tag, _attr_dict(attrs)))

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        closable = _AUTO_CLOSE.get(tag)
        if closable and len(self._stack) > 1 and self._stack[-1].tag in closable:
            self._stack.pop()
        node = Node(tag, _attr_dict(attrs))
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        # 从栈顶往下找最近的同名节点；找不到就当作孤儿闭合标签丢掉（不抛错）
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _attr_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {k: (v if v is not None else "") for k, v in attrs}


def parse_html(html: str) -> Node:
    """解析成树。永不抛异常：坏标签由 HTMLParser 自己吞掉。"""
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:  # noqa: BLE001 —— 极端畸形页面：返回已解析的部分，好过整个任务挂掉
        pass
    return builder.root


def iter_nodes(root: Node):
    """先序遍历（文档顺序），显式栈实现。"""
    stack = [c for c in reversed(root.children) if isinstance(c, Node)]
    while stack:
        cur = stack.pop()
        yield cur
        kids = [c for c in cur.children if isinstance(c, Node)]
        stack.extend(reversed(kids))


def find_all(node: Node, tag: str | None = None, cls: str | None = None) -> list[Node]:
    """按标签名 + class 精确 token 匹配（匹配 class 列表里的整词，不是子串）。"""
    return [
        n for n in iter_nodes(node)
        if (tag is None or n.tag == tag) and (cls is None or n.has_class(cls))
    ]


def find(node: Node, tag: str | None = None, cls: str | None = None) -> Node | None:
    for n in iter_nodes(node):
        if (tag is None or n.tag == tag) and (cls is None or n.has_class(cls)):
            return n
    return None


def text(node: Node | None) -> str:
    """子树全部文本，按文档顺序拼接，空白折叠成单空格。

    刻意不在块级元素之间插空格：中文标题 "读懂<strong>红黑树</strong>（图解）"
    插空格会变成 "读懂 红黑树 （图解）"，反而失真。
    """
    if node is None:
        return ""
    out: list[str] = []
    stack: list[Node | str] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
        else:
            stack.extend(reversed(cur.children))
    return normalize("".join(out))


def normalize(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


# 只剥"看起来真是标签"的片段：`<` 后面必须紧跟字母或 `/字母`。
# 若写成 `<[^<>]*>`，正文里的 "若 a < b 且 b > c 则" 会被整段吃掉（实测踩过）。
_TAG_RE = re.compile(r"</?[a-zA-Z][^<>]{0,199}/?>")


def strip_tags(fragment: str) -> str:
    """处理"字段本身是 HTML 片段"的情况，如 B站 title 里的 <em class="keyword">。

    不要用 parse_html 再取 text：那会把 `a < b` 里的 `< b ...` 当成标签吞掉。
    这里先按标签正则剥、再还原实体 —— 顺序反过来会把 `&lt;script&gt;` 变成真标签再被剥掉。
    """
    return normalize(_unescape(_TAG_RE.sub("", fragment or "")))


def raw_text(node: Node | None) -> str:
    """不折叠空白的版本：<script type=json-ld> 之类要原样取。"""
    if node is None:
        return ""
    out: list[str] = []
    stack: list[Node | str] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
        else:
            stack.extend(reversed(cur.children))
    return "".join(out)


__all__ = ["Node", "find", "find_all", "iter_nodes", "normalize", "parse_html",
           "raw_text", "strip_tags", "text"]
