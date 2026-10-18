"""正文页面的字符编码判定。

为什么值得单独一个文件：中文站大量使用 GBK/GB2312，而响应头经常"根本没写 charset"
或"写了但写错"。`curl_cffi` 的 `resp.text` 在这种情况下会返回整页乱码，而乱码一旦
流进抽取层，下游全部静默失效 —— 正文长度算成 0、时间正则一个都匹配不上、SimHash
把同一篇文章算成两篇，而且没有任何一处会报错。

判定顺序（顺序本身是设计，不可以随意调换）：

    1. BOM               最硬的证据，有就直接用
    2. 响应头 charset     服务端明确声明，其次信任
    3. <meta charset>    页面自己声明，中文站常见（很多还会和响应头矛盾）
    4. utf-8 严格解码     无声明时的最佳猜测
    5. 回退表            默认 gb18030（GBK/GB2312 的超集）
    6. 全部失败           utf-8 + replace，并标记 ok=False

⚠️ 只在"严格解码成功"时才接受候选编码。gb18030 几乎能吃下任意字节序列，
   先拿它试会把 UTF-8 页面"成功"解成乱码还不报错，所以 utf-8 必须排在它前面。
"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

# <meta charset="gbk"> / <meta http-equiv=... content="text/html; charset=gb2312">
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I
)
_HEADER_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)

# 声明 gb2312/gbk 时直接用 gb18030 解：它是二者超集，能多解出生僻字，
# 而反过来（拿 gb2312 去解含 GBK 扩展字的页面）会整段失败。
_ALIAS = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "utf8": "utf-8",
    "unicode-1-1-utf-8": "utf-8",
}

# 只需要看前 4KB：meta charset 必须出现在文档头部，往后再找纯属浪费
_HEAD_BYTES = 4096


@dataclass(slots=True)
class Decoded:
    """一次解码的结果。ok=False 表示"能给你文字，但它不可信"。"""

    text: str
    encoding: str
    ok: bool
    source: str      # bom | header | meta | utf-8 | fallback | replace

    @property
    def garbled(self) -> bool:
        return not self.ok


def normalize_encoding(name: str | None) -> str | None:
    """把各种别名收敛成 Python 认得的编码名；认不出就返回 None。

    不直接用 codecs.lookup 的原因：服务端写 "gb2312" 是合法的，但拿它严格解码
    含扩展字的页面会失败。这里统一升到超集，是"更可能解对"而不是"更严格"。
    """
    if not name:
        return None
    key = name.strip().strip("\"'").lower()
    if not key:
        return None
    key = _ALIAS.get(key, key)
    try:
        return codecs.lookup(key).name
    except LookupError:
        return None


def charset_from_header(content_type: str) -> str | None:
    m = _HEADER_CHARSET_RE.search(content_type or "")
    return normalize_encoding(m.group(1)) if m else None


def charset_from_meta(raw: bytes) -> str | None:
    m = _META_CHARSET_RE.search(raw[:_HEAD_BYTES])
    return normalize_encoding(m.group(1).decode("ascii", "ignore")) if m else None


def _strict(raw: bytes, enc: str) -> str | None:
    """严格解码；失败返回 None（不抛异常，让调用方继续试下一个候选）。"""
    try:
        return raw.decode(enc, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return None


def decode_html(
    raw: bytes,
    content_type: str = "",
    *,
    fallbacks: tuple[str, ...] = ("utf-8", "gb18030"),
) -> Decoded:
    """按上面的优先级把字节解成文本。永远返回文本，绝不抛异常。"""
    if not raw:
        return Decoded("", "utf-8", True, "empty")

    # 1) BOM：比任何声明都可靠
    for bom, enc in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(bom):
            text = _strict(raw, enc)
            if text is not None:
                return Decoded(text, enc, True, "bom")

    # 2/3) 声明。注意两者可能互相矛盾，以响应头为准（HTTP 语义上它更权威）
    for source, enc in (
        ("header", charset_from_header(content_type)),
        ("meta", charset_from_meta(raw)),
    ):
        if enc:
            text = _strict(raw, enc)
            if text is not None:
                return Decoded(text, enc, True, source)

    # 4/5) 猜。utf-8 必须在 gb18030 之前，否则 UTF-8 页面会被"成功"解成乱码
    for i, enc in enumerate(fallbacks):
        norm = normalize_encoding(enc) or enc
        text = _strict(raw, norm)
        if text is not None:
            return Decoded(text, norm, True, "utf-8" if i == 0 else "fallback")

    # 6) 兜底：给文字，但明确标记不可信，让上层决定要不要入库
    return Decoded(raw.decode("utf-8", errors="replace"), "utf-8", False, "replace")


def garble_ratio(text: str) -> float:
    """替换字符占比。用于识别"解码没报错但结果仍是乱码"的兜底情况。"""
    if not text:
        return 0.0
    return text.count("\ufffd") / len(text)


__all__ = [
    "Decoded",
    "charset_from_header",
    "charset_from_meta",
    "decode_html",
    "garble_ratio",
    "normalize_encoding",
]
