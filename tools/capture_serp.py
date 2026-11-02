"""抓一页 SERP，裁剪成测试用的 fixture（只留结果块，去掉脚本与样式）。

为什么需要这个工具：
    `tests/fixtures/` 里的样本会随时间过期 —— 站点一改版，fixture 里的结构就和线上
    对不上了，而单测**照样绿**（它只验证"能解析我手上这份旧的"）。这是最阴的一类假绿灯。
    所以样本必须能重新生成，而且生成过程要短到"看到线上解析失败时愿意跑一次"。

为什么裁剪而不是存整页：
    360 一页 568KB、搜狗 510KB，每个通道存两页就是 2MB 进仓库。裁剪后只剩结果块
    （20~40KB），结构不变、解析路径不变。代价是丢了"块外的噪声"——如果哪天要靠
    `<body>` 里的某个全局标记判断风控，fixture 就测不到，那种判据得另外手工加用例。

用法：
    ./.venv/Scripts/python.exe tools/capture_serp.py --channel so360 --keyword 红黑树
    ./.venv/Scripts/python.exe tools/capture_serp.py --channel sogou --keyword 红黑树 --pages 2

产物：tests/fixtures/<channel>_p<N>.html（最小 HTML 文档，只含结果块）
⚠️ 会真的发请求，注意配额。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kwsearch.config import load_config  # noqa: E402
from kwsearch.fetch import (  # noqa: E402
    RetryPolicy,
    build_fetcher,
    default_headers,
    fetch_with_retry,
)

# 每个通道：结果块的标签与 class。裁剪就是把这些块原样切出来拼在一起。
BLOCKS = {
    "so360": ("li", "res-list"),
    "sogou": ("div", "vrwrap"),
}

# 摘要/标题里的空白无关紧要，但 <script> 必须去掉：块内偶有统计脚本，几百 KB
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)


def slice_blocks(html: str, tag: str, cls: str) -> list[str]:
    """把所有 class 含 cls 的 tag 元素原样切出来。

    用同名标签计数找闭合，不依赖 `</li>` 一定存在 —— 但真的遇到没闭合的块时，
    截到最大长度就停，避免把整页塞进一个"块"。
    """
    out: list[str] = []
    pat = re.compile(rf'<{tag}\b[^>]*\bclass="[^"]*\b{re.escape(cls)}\b[^"]*"[^>]*>', re.I)
    close_pat = re.compile(rf"<(/?){tag}\b[^>]*?(/?)>", re.I)
    for m in pat.finditer(html):
        start = m.start()
        depth = 0
        end = min(len(html), start + 60_000)      # 单块上限，防病态页面
        for c in close_pat.finditer(html, start):
            if c.group(2):                         # 自闭合
                continue
            depth += -1 if c.group(1) else 1
            if depth == 0:
                end = c.end()
                break
        out.append(html[start:end])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="抓 SERP 并裁剪成测试 fixture")
    ap.add_argument("--channel", default="so360", choices=sorted(BLOCKS))
    ap.add_argument("--keyword", default="红黑树")
    ap.add_argument("--pages", type=int, default=1, help="抓前 N 页")
    ap.add_argument("--out", default=None, help="输出目录，默认 tests/fixtures")
    ap.add_argument("--interval", type=float, default=3.0, help="两页之间隔几秒")
    a = ap.parse_args()

    tag, cls = BLOCKS[a.channel]
    out_dir = Path(a.out) if a.out else ROOT / "tests" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    ch = cfg.channel(a.channel)
    # 与正式任务同一套请求头：UA 来自 defaults，通道自己的头覆盖（如 B 站 Referer）
    headers = default_headers(cfg.defaults)
    headers.update(ch.headers or {})
    fetcher = build_fetcher()
    policy = RetryPolicy(max_attempts=1, backoff_base=0.01)

    print(f"通道 {a.channel} · 词「{a.keyword}」· {a.pages} 页 → {out_dir}")
    for page in range(1, a.pages + 1):
        url = ch.build_url(a.keyword, page)
        try:
            res = fetch_with_retry(fetcher, url, headers, policy, 20)
        except Exception as e:  # noqa: BLE001
            print(f"  第 {page} 页失败：{type(e).__name__}: {e}")
            continue
        html = res.body or ""
        blocks = slice_blocks(html, tag, cls)
        kept = [b for b in (_SCRIPT_RE.sub("", x) for x in blocks)]
        kept = [_STYLE_RE.sub("", x) for x in kept]
        slim = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{a.channel} fixture p{page}</title></head><body>\n"
            f"<!-- 由 tools/capture_serp.py 从 {url} 裁剪生成；只保留 .{cls} 结果块 -->\n"
            + "\n".join(kept)
            + "\n</body></html>\n"
        )
        path = out_dir / f"{a.channel}_p{page}.html"
        # newline="\n" 是必须的：Windows 上默认会把 \n 写成 \r\n，
        # 仓库统一 LF，混进来一个 CRLF 文件会让 diff 看着像整文件重写
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(slim)
        print(f"  第 {page} 页：原 {len(html):,} 字符 → 裁出 {len(blocks)} 块 / "
              f"{len(slim):,} 字符  → {path.name}")
        if page < a.pages:
            time.sleep(a.interval)

    fetcher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
