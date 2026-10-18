"""展示层：DB → report/<关键词>/index.html。**不碰网络**。

沿前作的看板思路：单文件 HTML（内联 CSS，无外部资源），双击能开。
报告必须包含"我们没抓到什么"，这是它和"搜索结果页截图"的区别：

    1  概览      入库数 / 覆盖域名 / 通道数 / 告警数 + 原始→入库的漏斗
    2  覆盖率自评  每通道：请求数 / 解析数 / 被拦数 / 停止原因
                  ⚠️ 这一块比"命中总数"更重要 —— 少了它，读者无法判断
                     "只有 12 条"到底是这个词冷门，还是我们被拦了
    3  来源分布  域名 Top N + 条形图（单域占比过高会被配额截断，报告里写明）
    4  时间分布  按天聚合；无日期与相对日期的文档单独计数，不混进时间线
    5  文档清单  标题 + 域名 + 时间来源 + 摘要 + 原文链接，按相关度排序
                 同文转载折叠在代表条目下（S5 正文指纹归并）
    6  查询明细  每个变体打了几条、新增几条（折叠），用来解释"为什么只打了 3 次"
    7  告警      被拦 / 结构变更 / 截断 / 脱敏，逐条列出

⚠️ 不渲染任何正文全文，只出标题 + 摘要 + 原文链接（版权与合规）。
   S4 阶段 doc 里还没有正文，摘要就是 SERP 给的原文，报告不额外补。

⚠️ 所有外部字符串都过 html.escape，所有 href 只放行 http/https。
   这两个不是洁癖：标题里带 `<script>` 的页面真实存在，报告是本机双击打开的
   文件，一旦被注入就等于执行本地 HTML 里的任意脚本。
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from kwsearch import __version__
from kwsearch.person import TIER_CN, UNMATCHED_LABEL
from kwsearch.storage import Repository

DOC_RENDER_LIMIT = 500

_STATUS_CN = {
    "running": "进行中",
    "ok": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "aborted": "已中止",
}

_TIME_SOURCE_CN = {
    "serp": "搜索结果页",
    "api": "接口字段",
    "serp_relative": "相对日期",
    "jsonld": "页面 JSON-LD",
    "meta": "页面 meta",
    "regex": "正文正则",
    "regex_updated": "仅更新时间",
    "fallback": "无日期",
}

_ALERT_CN = {
    "blocked": "被限流 / 拒绝",
    "challenge": "疑似风控页",
    "rate_limit": "触发频率限制",
    "captcha": "要求验证码",
    "unknown_structure": "页面结构变更（选择器可能失效）",
    "empty_result": "未解析出结果",
    "parse_fail": "解析异常",
    "fetch_fail": "请求失败",
    "limit_reached": "触发上限（结果不完整）",
    "dropped": "命中丢弃规则",
    "pii_masked": "已脱敏",
    "robots_disallow": "robots 禁止",
    "js_required": "需要 JS 渲染",
    "no_fingerprint": "无正文指纹（未参与归并）",
}

# regex_updated 也算不可信：它是"页面最近被改过"的时间，不是发布时间，
# 同一站所有文章常常共用同一个值（站点构建时间），当发布时间用会造出假高峰。
_UNRELIABLE_TIME = frozenset({"serp_relative", "regex_updated", "fallback"})

_HTTP_RE = re.compile(r"^https?://", re.I)
_BAD_PATH_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


# ------------------------------------------------------------------ 小工具


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _safe_url(url: Any) -> str:
    """只放行 http/https。别的 scheme（javascript: / data:）一律不给 href。"""
    s = str(url or "").strip()
    return html.escape(s, quote=True) if _HTTP_RE.match(s) else ""


def _short(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fmt_ts(iso: Any) -> str:
    s = str(iso or "").strip()
    if not s:
        return "-"
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s[:19].replace("T", " ")


def _fmt_day(value: Any) -> str:
    s = str(value or "").strip()
    return s[:10] if len(s) >= 10 else s or "-"


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def _bar(pct: float, cls: str = "") -> str:
    width = max(0.5, min(100.0, pct))
    return (f'<div class="bar {cls}"><span style="width:{width:.1f}%"></span></div>')


def safe_dirname(keyword: str, limit: int = 40) -> str:
    """关键词 → 合法目录名。中文保留（Windows 没问题），只处理真正的非法字符。"""
    name = _BAD_PATH_RE.sub("_", str(keyword or "")).strip(" .")
    name = name[:limit].strip(" .")
    if not name:
        return "untitled"
    if name.lower() in _WIN_RESERVED:
        return f"_{name}"
    return name


# ---------------------------------------------------------------------- CSS

_CSS = """
:root{
  --bg:#f5f6f8; --panel:#ffffff; --ink:#171a1f; --ink-2:#3d444d; --muted:#6c7480;
  --line:#e4e7ec; --line-2:#eef0f3;
  --accent:#0f766e; --accent-soft:#e6f4f1;
  --warn:#b45309; --warn-soft:#fdf3e3;
  --danger:#b42318; --danger-soft:#fdeceb;
  --chip:#f2f4f7;
  --radius:14px;
  --mono:ui-monospace,SFMono-Regular,"Cascadia Mono",Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
       "Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",sans-serif;
  font-feature-settings:"tnum" 1;
}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* ── 头部 ───────────────────────────────────────────── */
.top{background:#12161b;color:#f4f6f8;padding:34px 0 26px}
.kicker{margin:0 0 6px;font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#7f8b99}
.top h1{margin:0;font-size:clamp(26px,4vw,38px);line-height:1.2;letter-spacing:-.01em;word-break:break-word}
.top .meta{margin:10px 0 0;font-size:13px;color:#9aa5b1}
.top .meta code{font-family:var(--mono);color:#c8d2dc;background:#1c222a;padding:1px 6px;border-radius:5px}

/* ── 提示条 ─────────────────────────────────────────── */
.banner{margin:20px 0 0;padding:12px 16px;border-radius:12px;font-size:14px;display:flex;gap:10px;align-items:flex-start}
.banner.warn{background:var(--warn-soft);color:#7c3d06;border:1px solid #f3d9b0}
.banner.bad{background:var(--danger-soft);color:#7a1d13;border:1px solid #f6cdc8}
.banner b{white-space:nowrap}

/* ── 分区 ───────────────────────────────────────────── */
section{margin:34px 0 0}
h2{font-size:15px;margin:0 0 4px;letter-spacing:.02em}
h2 .sub{font-weight:400;color:var(--muted);font-size:13px;margin-left:8px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
       padding:6px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.hint{color:var(--muted);font-size:13px;margin:6px 0 12px}

/* ── 概览卡 ─────────────────────────────────────────── */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px}
.card .n{font-size:28px;font-weight:600;letter-spacing:-.02em;line-height:1.15}
.card .l{font-size:12px;color:var(--muted);margin-top:4px}
.card.alert .n{color:var(--warn)}
.card.zero .n{color:var(--muted)}

/* ── 漏斗 ───────────────────────────────────────────── */
.funnel{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px;
        font-size:13px;color:var(--ink-2)}
.funnel .step{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:4px 12px}
.funnel .step b{font-variant-numeric:tabular-nums}
.funnel .arrow{color:#aeb6c0}

/* ── 表格 ───────────────────────────────────────────── */
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:9px 16px;border-bottom:1px solid var(--line-2);vertical-align:top}
th{font-weight:500;color:var(--muted);font-size:12px;letter-spacing:.03em;white-space:nowrap}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.dim{color:var(--muted)}
.tag{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:999px;
     background:var(--chip);color:var(--ink-2);white-space:nowrap}
.tag.ok{background:var(--accent-soft);color:#0b5c56}
.tag.warn{background:var(--warn-soft);color:#8a4708}
.tag.bad{background:var(--danger-soft);color:#8f2419}

/* ── 条形图 ─────────────────────────────────────────── */
.bars{display:grid;grid-template-columns:minmax(120px,220px) 1fr 44px;gap:8px 14px;
      align-items:center;padding:14px 18px}
.bars .name{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars .cnt{font-size:12.5px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.bar{height:9px;background:var(--line-2);border-radius:999px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--accent);border-radius:999px}
.bar.muted>span{background:#c3cbd5}

/* ── 时间线 ─────────────────────────────────────────── */
.days{display:flex;align-items:stretch;gap:3px;height:140px;padding:14px 18px 26px;overflow-x:auto}
.days .d{position:relative;flex:1 1 0;min-width:5px;height:100%}
.days .d .w{height:100%;display:flex;align-items:flex-end}
.days .d .w i{display:block;width:100%;background:var(--accent);border-radius:3px 3px 0 0;min-height:2px}
/* 标签绝对定位在柱子下方：放进 flex 流里会因为列宽只有几像素而撑变布局 */
.days .d span{position:absolute;top:100%;left:50%;transform:translateX(-50%);
              margin-top:5px;font-size:9.5px;color:#9aa2ad;white-space:nowrap}

/* ── 候选人（人名模式） ─────────────────────────────── */
.pcard{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
       padding:14px 18px;margin-top:10px}
.phead{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.phead b{font-size:15px;letter-spacing:-.01em}
.pmeta{margin-left:auto;font-size:12px;color:var(--muted);font-family:var(--mono)}
.tier{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--chip);
      color:var(--ink-2);white-space:nowrap}
.tier.ok{background:#e7f6ec;color:#17663a}
.tier.warn{background:var(--warn-soft);color:#7c3d06}
.pev{list-style:none;margin:10px 0 0;padding:0}
.pev li{padding:7px 0;border-top:1px solid var(--line-2);font-size:13px;
        display:flex;gap:10px;align-items:baseline}
.pev li:first-child{border-top:none}
.pev a{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2)}
.pm{font-size:11.5px;color:var(--muted);white-space:nowrap}

/* ── 文档列表 ───────────────────────────────────────── */
.docs{padding:4px 0}
.doc{padding:16px 20px;border-bottom:1px solid var(--line-2)}
.doc:last-child{border-bottom:none}
.doc .head{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:5px}
.doc h3{margin:0 0 5px;font-size:15.5px;line-height:1.45;font-weight:600;word-break:break-word}
.doc h3 a{color:var(--ink)}
.doc h3 a:hover{color:var(--accent)}
.doc p{margin:0;color:var(--ink-2);font-size:13.5px;line-height:1.6}
.doc .src{color:var(--muted);font-size:12px;margin-top:6px;font-family:var(--mono);
          word-break:break-all}
.score{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;
       margin-left:auto;font-family:var(--mono)}
.empty{padding:26px 20px;color:var(--muted);font-size:14px}

/* ── 同文转载（折叠在代表条目下面） ─────────────────── */
.doc details{margin-top:10px;border:1px solid var(--line);border-radius:10px;
             background:#fafbfc;overflow:hidden}
.doc details>summary{padding:7px 12px;font-size:12.5px;color:var(--ink-2)}
.doc details[open]>summary{border-bottom:1px solid var(--line-2)}
.memlist{padding:2px 0}
.mem{display:flex;gap:12px;align-items:baseline;padding:7px 12px;font-size:12.5px;
     border-bottom:1px solid var(--line-2)}
.mem:last-child{border-bottom:none}
.mem a{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2)}
.mem span{color:var(--muted);font-size:11.5px;font-family:var(--mono);white-space:nowrap}

/* ── 折叠 ───────────────────────────────────────────── */
details{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
        padding:0;margin-top:12px;overflow:hidden}
details>summary{cursor:pointer;padding:13px 18px;font-size:13.5px;color:var(--ink-2);
                list-style:none;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸";display:inline-block;margin-right:8px;color:var(--muted);
                        transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}
details[open]>summary{border-bottom:1px solid var(--line-2)}

/* ── 页脚 ───────────────────────────────────────────── */
footer{margin:44px 0 0;padding:26px 0 40px;border-top:1px solid var(--line);
       color:var(--muted);font-size:12.5px;line-height:1.8}
footer ol{margin:8px 0 0;padding-left:20px}
footer .ver{font-family:var(--mono)}
@media (max-width:640px){
  .wrap{padding:0 16px}
  th,td{padding:8px 12px}
  .bars{grid-template-columns:minmax(80px,120px) 1fr 36px;padding:12px}
  .doc{padding:14px 16px}
}
"""


# ----------------------------------------------------------------- 各区块


def _header(task: Mapping[str, Any]) -> str:
    kw = task.get("keyword", "")
    status = _STATUS_CN.get(str(task.get("status")), task.get("status"))
    bits = [
        f"任务 <code>{_esc(task.get('task_id'))}</code>",
        _esc(task.get("mode")),
        f"时间窗 {_esc(task.get('timespan') or '默认')}",
        f"{_esc(_fmt_ts(task.get('started_at')))} 起",
        _esc(status),
    ]
    if task.get("finished_at"):
        bits.append(f"{_esc(_fmt_ts(task.get('finished_at')))} 止")
    return f"""
<header class="top">
  <div class="wrap">
    <p class="kicker">kwsearch · 指定搜索报告</p>
    <h1>{_esc(kw)}</h1>
    <p class="meta">{' · '.join(bits)}</p>
  </div>
</header>"""


def _banners(task: Mapping[str, Any], totals: Mapping[str, Any],
             channels: Sequence[Mapping[str, Any]]) -> str:
    out: list[str] = []
    if task.get("truncated"):
        out.append(
            '<div class="banner warn"><b>结果不完整</b>'
            "<span>本次触发了规模上限（文档数 / 单域配额 / 时间），被截掉的都是"
            "相关度最低的部分。具体数字见下方“告警”。</span></div>"
        )
    if str(task.get("status")) == "failed":
        out.append(
            '<div class="banner bad"><b>本次无结果</b>'
            "<span>所有通道都没有产出可用条目。先看“覆盖率自评”里的停止原因，"
            "多数情况是被限流或页面结构变了，而不是这个词真的没有内容。</span></div>"
        )
    elif channels and all(int(c.get("blocked") or 0) > 0 for c in channels):
        out.append(
            '<div class="banner warn"><b>全部通道均被拦</b>'
            "<span>每条通道都出现过限流/拒绝。这份结果只能说明“我们没拿到”，"
            "不能说明“网上没有”。</span></div>"
        )
    elif totals.get("no_time"):
        out.append(
            '<div class="banner warn"><b>时间不完整</b>'
            f"<span>{_num(totals.get('no_time'))} 条没有可核实的发布时间，"
            "未计入时间分布；清单里以“无日期 / 相对日期”标注。</span></div>"
        )
    return "".join(out)


def _overview(totals: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    tstats = task.get("stats") or {}
    cards = [
        (totals.get("docs", 0), "入库文档", "zero" if not totals.get("docs") else ""),
    ]
    # 「独立内容」只在真跑过归并时出现。没有这一格，读者会把 20 家门户的
    # 同一篇通稿当成 20 条独立证据 —— 那正是 S5 要纠正的误读。
    if totals.get("clusters"):
        cards.append((totals.get("groups", 0), "独立内容", ""))
    cards += [
        (totals.get("domains", 0), "覆盖域名", ""),
        (totals.get("with_time", 0), "有发布时间", ""),
        (totals.get("alerts", 0), "告警", "alert" if totals.get("alerts") else ""),
    ]
    card_html = "".join(
        f'<div class="card {cls}"><div class="n">{_num(n)}</div>'
        f'<div class="l">{_esc(label)}</div></div>'
        for n, label, cls in cards
    )

    # 漏斗：每一步都要能对上账，读者才知道"87 条怎么变成 45 条"
    steps = [
        ("原始候选", tstats.get("candidates")),
        ("关键词过滤", tstats.get("filtered")),
        ("去重后", tstats.get("unique")),
        ("零命中", tstats.get("dropped_zero")),
        ("规则丢弃", tstats.get("dropped_url")),
        ("单域配额", tstats.get("dropped_cap")),
        ("入库", totals.get("docs")),
    ]
    funnel = '<span class="arrow">→</span>'.join(
        f'<span class="step">{_esc(label)} <b>{_num(value)}</b></span>'
        for label, value in steps if value is not None
    )

    # 正文富化的自评：抓了几条、抽得出来几条、平均多长。
    # 用"本次抓取"作前缀，避免和上面"入库文档数"混淆 —— 这两个数字口径不同：
    # 重跑时大部分条目早已有正文（跳过），本次只抓了几条新出现的。
    body_bits = []
    if tstats.get("bodies_ok") is not None:
        body_bits.append(f'本次抓取 · 正文可信 <b>{_num(tstats.get("bodies_ok"))}</b>')
    if tstats.get("bodies_low"):
        body_bits.append(f'抽不出正文 <b>{_num(tstats.get("bodies_low"))}</b>')
    if tstats.get("bodies_skipped"):
        body_bits.append(f'已有正文跳过 <b>{_num(tstats.get("bodies_skipped"))}</b>')
    if tstats.get("body_chars_avg"):
        body_bits.append(f'正文均长 <b>{_num(tstats.get("body_chars_avg"))}</b> 字')
    body_line = '<span class="arrow">·</span>'.join(
        f'<span class="step">{b}</span>' for b in body_bits
    )
    body_html = f'<div class="funnel">{body_line}</div>' if body_line else ""

    # 归并对账：入库 N 条 → 独立 M 组。两个数字必须都出现，
    # 只写"独立 M 组"会让人以为抓少了，只写"入库 N 条"就把转载当成了独立证据。
    cluster_bits = []
    if totals.get("clusters"):
        cluster_bits.append(
            f'入库 <b>{_num(totals.get("docs"))}</b> 条 → 独立内容 '
            f'<b>{_num(totals.get("groups"))}</b> 组')
        if totals.get("merged"):
            cluster_bits.append(f'合并转载 <b>{_num(totals.get("merged"))}</b> 条')
        if totals.get("unfingerprinted"):
            # 有没参与判断的条目时不能断言"没有转载"，只能说"能比的都不同" ——
            # 那 5 条短正文里可能就藏着转载，报告不能替它们背书。
            cluster_bits.append(
                f'无指纹未判 <b>{_num(totals.get("unfingerprinted"))}</b> 条')
        if not totals.get("merged"):
            cluster_bits.append('可比对的条目里未发现同文转载')
    cluster_line = '<span class="arrow">·</span>'.join(
        f'<span class="step">{b}</span>' for b in cluster_bits
    )
    cluster_html = f'<div class="funnel">{cluster_line}</div>' if cluster_line else ""

    return f"""
<section>
  <h2>概览</h2>
  <div class="cards">{card_html}</div>
  <div class="funnel">{funnel}</div>
  {body_html}
  {cluster_html}
</section>"""


def _coverage(channels: Sequence[Mapping[str, Any]]) -> str:
    if not channels:
        return """
<section>
  <h2>覆盖率自评<span class="sub">每个通道各拿到多少、为什么停下</span></h2>
  <div class="panel"><div class="empty">本次没有任何通道完成请求。</div></div>
</section>"""

    rows: list[str] = []
    for c in channels:
        blocked = int(c.get("blocked") or 0)
        queries = int(c.get("queries") or 0)
        if blocked == 0:
            badge = '<span class="tag ok">正常</span>'
        elif blocked >= queries:
            badge = '<span class="tag bad">全部被拦</span>'
        else:
            badge = '<span class="tag warn">部分被拦</span>'
        kinds = ", ".join(
            k for k in str(c.get("block_kinds") or "").split(",")
            if k and k != "None"
        )
        kinds_html = f' <span class="tag">{_esc(kinds)}</span>' if kinds else ""
        rows.append(
            "<tr>"
            f'<td><code>{_esc(c.get("channel"))}</code></td>'
            f'<td class="num">{_num(queries)}</td>'
            f'<td class="num">{_num(c.get("got"))}</td>'
            f'<td class="num">{_num(c.get("new"))}</td>'
            f'<td class="num">{_num(blocked)}</td>'
            f"<td>{badge}{kinds_html}</td>"
            f'<td class="num dim">{_num(c.get("elapsed_ms"))} ms</td>'
            "</tr>"
        )
    return f"""
<section>
  <h2>覆盖率自评<span class="sub">每个通道各拿到多少、为什么停下</span></h2>
  <p class="hint">这一块比“共几条”更重要：结果少的时候，你得先知道是词太冷门，
  还是我们被拦了。“新增”是通道内去重后的净增量，用来判断这个变体有没有白打。</p>
  <div class="panel">
    <table>
      <thead><tr>
        <th>通道</th><th class="num">请求</th><th class="num">解析</th>
        <th class="num">新增</th><th class="num">被拦</th><th>状态</th>
        <th class="num">耗时</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>"""


def _queries(queries: Sequence[Mapping[str, Any]]) -> str:
    if not queries:
        return ""
    rows = []
    for q in queries:
        note = ""
        if q.get("blocked"):
            note = f'<span class="tag bad">{_esc(q.get("block_kind") or "被拦")}</span>'
        elif not int(q.get("new_count") or 0):
            note = '<span class="tag">无新增</span>'
        else:
            note = '<span class="tag ok">有效</span>'
        rows.append(
            "<tr>"
            f'<td><code>{_esc(q.get("channel"))}</code></td>'
            f'<td class="num dim">{_num(q.get("seq"))}</td>'
            f'<td>{_esc(q.get("query_text") or "-")}</td>'
            f'<td class="num">{_num(q.get("got_count"))}</td>'
            f'<td class="num">{_num(q.get("new_count"))}</td>'
            f"<td>{note}</td>"
            "</tr>"
        )
    return f"""
<details>
  <summary>查询明细（{len(queries)} 次调用）—— 用来解释“为什么只打了这么几次”</summary>
  <table>
    <thead><tr>
      <th>通道</th><th class="num">#</th><th>实际查询词</th>
      <th class="num">解析</th><th class="num">新增</th><th>判定</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</details>"""


def _domains(domains: Sequence[Mapping[str, Any]], max_per_domain: int = 0) -> str:
    if not domains:
        return ""
    top = list(domains)[:15]
    peak = max(int(d.get("n") or 0) for d in top) or 1
    rows = "".join(
        f'<div class="name" title="{_esc(d.get("domain"))}">{_esc(d.get("domain"))}</div>'
        f'{_bar(int(d.get("n") or 0) / peak * 100, "muted" if i else "")}'
        f'<div class="cnt">{_num(d.get("n"))}</div>'
        for i, d in enumerate(top)
    )
    more = ""
    if len(domains) > len(top):
        more = (f'<p class="hint" style="padding:0 18px 14px">'
                f'另有 {len(domains) - len(top)} 个域名各 1 条，未列出。</p>')
    quota = ""
    if max_per_domain:
        quota = (f'<p class="hint" style="padding:0 18px 14px">'
                 f"单域配额 {max_per_domain} 条：超过的部分已被丢弃，"
                 f"所以头部域名的真实数量可能更高。</p>")
    return f"""
<section>
  <h2>来源分布<span class="sub">{len(domains)} 个域名</span></h2>
  <div class="panel"><div class="bars">{rows}</div>{more}{quota}</div>
</section>"""


def _timeline(days: Sequence[Mapping[str, Any]]) -> str:
    if not days:
        return """
<section>
  <h2>时间分布</h2>
  <div class="panel"><div class="empty">没有任何可核实的发布时间，无法生成时间线。
  这不代表内容不存在，只代表各通道都没给出可靠日期。</div></div>
</section>"""

    rows = list(days)
    aggregated = ""
    if len(rows) > 45:      # 一年以上的词按天画会挤成一团，按月聚合
        buckets: dict[str, int] = {}
        for d in rows:
            key = str(d.get("date") or "")[:7]
            buckets[key] = buckets.get(key, 0) + int(d.get("n") or 0)
        rows = [{"date": k, "n": v} for k, v in sorted(buckets.items())]
        aggregated = "（按月份聚合）"

    peak = max(int(d.get("n") or 0) for d in rows) or 1
    # 日期标签不能每个都画：30 个竖排日期会挤成一团黑边。
    # 只标首、末、和峰值那几天，其余靠 hover 提示。
    peak_idx = max(range(len(rows)), key=lambda i: int(rows[i].get("n") or 0))
    label_every = max(1, len(rows) // 8)
    cells = []
    for i, d in enumerate(rows):
        n = int(d.get("n") or 0)
        show_label = i == 0 or i == len(rows) - 1 or i == peak_idx or i % label_every == 0
        label = (f'<span>{_esc(_fmt_day(d.get("date")))}</span>' if show_label else "<span></span>")
        cells.append(
            f'<div class="d" title="{_esc(_fmt_day(d.get("date")))}：{_num(n)} 条">'
            f'<div class="w"><i style="height:{max(2, n / peak * 100):.1f}%"></i></div>'
            f'{label}</div>'
        )
    bars = "".join(cells)
    return f"""
<section>
  <h2>时间分布<span class="sub">{len(rows)} 个时间段{aggregated}</span></h2>
  <p class="hint">只统计有真实发布日期的文档。相对日期（“5 天之前”）和抓取时间
  不参与统计 —— 那会把“我们的抓取节奏”画成“事件热度”。</p>
  <div class="panel"><div class="days">{bars}</div></div>
</section>"""


def _member_row(m: Mapping[str, Any]) -> str:
    url = _safe_url(m.get("url_canon"))
    title = _esc(_short(m.get("title") or m.get("url_canon") or "(无标题)", 70))
    link = (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
            if url else title)
    bits = [str(m.get("domain") or "-")]
    if m.get("published_at"):
        bits.append(_fmt_day(m.get("published_at")))
    bits.append(f"距离 {int(m.get('hamming') or 0)}")
    return f'<div class="mem">{link}<span>{_esc(" · ".join(bits))}</span></div>'


def _doc_card(
    d: Mapping[str, Any],
    cluster: Mapping[str, Any] | None = None,
    others: Sequence[Mapping[str, Any]] = (),
) -> str:
    url = _safe_url(d.get("url_raw") or d.get("url_canon"))
    title = _esc(d.get("title") or "(无标题)")
    title_html = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>' \
        if url else title

    chips = [f'<span class="tag">{_esc(d.get("domain"))}</span>']

    # 有没有抓到正文，决定了这条摘要的"成色"：正文摘要是文章自己的开头，
    # 没有正文时用的是搜索引擎替我们写的那句话。不标出来，读者会以为是同一回事。
    body_chars = int(d.get("body_chars") or 0)
    if body_chars > 0:
        chips.append(f'<span class="tag ok" title="已打开原页并抽到正文">'
                     f'正文 {_num(body_chars)} 字</span>')
    else:
        chips.append('<span class="tag" title="未取到可信正文，摘要是搜索结果页原文">'
                     '仅摘要</span>')

    ts = str(d.get("time_source") or "fallback")
    if ts in _UNRELIABLE_TIME:
        chips.append(f'<span class="tag warn">'
                     f'{_esc(_TIME_SOURCE_CN.get(ts, ts))}</span>')
    else:
        chips.append(f'<span class="tag ok">{_esc(_TIME_SOURCE_CN.get(ts, ts))}</span>')

    if d.get("exact_phrase"):
        chips.append('<span class="tag ok">精确短语</span>')
    if not int(d.get("hit_title") or 0) and not int(d.get("hit_body") or 0):
        chips.append('<span class="tag">仅渠道匹配</span>')

    members_html = ""
    if others:
        worst = int(cluster.get("max_hamming") or 0) if cluster else 0
        chips.append(
            f'<span class="tag ok" title="正文指纹汉明距离 ≤ {worst}/64，'
            f'保守相似度 {100 - worst * 100 // 64}%">'
            f'同文 {len(others) + 1} 篇</span>')
        members_html = (
            f'<details><summary>另有 {len(others)} 篇同文转载 —— '
            f'点开看是哪些站，判定依据是正文指纹而非标题</summary>'
            f'<div class="memlist">{"".join(_member_row(m) for m in others)}</div>'
            f'</details>')

    published = _fmt_day(d.get("published_at")) if d.get("published_at") else "无日期"
    summary = str(d.get("summary") or "").strip()
    score = d.get("score")
    score_txt = f"{float(score):.1f}" if score is not None else "-"
    tip = (f"标题命中 {int(d.get('hit_title') or 0)} 次 / "
           f"摘要命中 {int(d.get('hit_body') or 0)} 次")
    return f"""
<article class="doc">
  <div class="head">{''.join(chips)}
    <span class="score" title="{_esc(tip)}">{_esc(score_txt)}</span>
  </div>
  <h3>{title_html}</h3>
  <p>{_esc(_short(summary, 260)) or '<span style="color:#9aa2ad">（无摘要）</span>'}</p>
  <div class="src">{_esc(published)} · {_esc(_short(d.get("url_canon") or d.get("url_raw"), 120))}</div>
  {members_html}
</article>"""


_TIER_CLASS = {"confirmed": "ok", "likely": "", "suspect": "warn", "unrelated": ""}


def _evidence_rows(card: Mapping[str, Any]) -> str:
    rows = []
    for e in card.get("evidence") or []:
        url = _safe_url(e.get("url"))
        title = _esc(e.get("title") or "(无标题)")
        title_html = (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
                      if url else title)
        matched = " · ".join(_esc(m) for m in (e.get("matched") or []))
        rows.append(f'<li>{title_html}<span class="pm">{matched or "—"}</span></li>')
    return "".join(rows)


def _candidates(cards: Sequence[Mapping[str, Any]]) -> str:
    """人名模式：候选人卡片。

    只有命中机构的才当候选人展示；没有机构命中的折叠成"同名未归因"——
    它们不是没价值，而是**无法归因**，混进候选人列表就等于瞎猜。
    """
    if not cards:
        return ""
    named = [c for c in cards if c.get("label") != UNMATCHED_LABEL]
    others = [c for c in cards if c.get("label") == UNMATCHED_LABEL]

    body: list[str] = []
    if not named:
        body.append(
            '<div class="empty">没有任何结果命中你给的机构锚点。'
            "这不等于“没有这个人”，只说明这一轮的结果里没有能归因的证据 —— "
            "换更常用的机构全称、或补一个方向锚点再跑一次。</div>")
    for c in named:
        tier = str(c.get("tier") or "unrelated")
        negs = "、".join(str(x) for x in (c.get("negatives") or []))
        body.append(f"""<div class="pcard">
  <div class="phead">
    <span class="tier {_TIER_CLASS.get(tier, '')}">{_esc(TIER_CN.get(tier, tier))}</span>
    <b>{_esc(c.get("label"))}</b>
    {f'<span class="tier warn">同名疑点：{_esc(negs)}</span>' if negs else ''}
    <span class="pmeta">{_num(c.get("docs"))} 条证据 · 锚点 {_num(c.get("org_hits"))}+{_num(c.get("weak_hits"))}</span>
  </div>
  <ul class="pev">{_evidence_rows(c)}</ul>
</div>""")

    for c in others:
        body.append(
            f'<details><summary>另有 {_num(c.get("docs"))} 条同名结果'
            f"（标题与摘要里没有任何机构锚点，归入“未匹配机构”）</summary>"
            f'<ul class="pev">{_evidence_rows(c)}</ul></details>')

    return f"""
<section class="sec" id="people">
<h2>候选人<span class="sub">按命中的机构分组 · 不同机构绝不合并</span></h2>
<p class="hint">“确认”= 机构与方向/身份互相印证；“很可能”= 只有机构对上；
“疑似”= 只有方向对上。没有机构命中一律归入“未匹配机构”，不计入候选人。</p>
{"".join(body)}
</section>"""


def _docs(
    items: Sequence[Mapping[str, Any]],
    totals: Mapping[str, Any],
    clusters: Sequence[Mapping[str, Any]] = (),
) -> str:
    if not items:
        return """
<section>
  <h2>文档清单</h2>
  <div class="panel"><div class="empty">没有入库文档。</div></div>
</section>"""

    # 同文转载只保留代表那一条，其余折叠进代表条目的"另有 N 篇"。
    # 折叠的前提是**代表本身也在这次的渲染范围内**：代表排到 500 名之外时，
    # 把它的成员全部藏起来等于让这些链接凭空消失 —— 那种情况下宁可不折叠。
    by_rep: dict[str, Mapping[str, Any]] = {}
    member_to_rep: dict[str, str] = {}
    for c in clusters or ():
        if int(c.get("member_count") or 0) < 2:
            continue
        rep = str(c.get("rep_url") or "")
        by_rep[rep] = c
        for m in c.get("members") or ():
            member_to_rep[str(m.get("url_canon") or "")] = rep

    rendered = {str(d.get("url_canon") or "") for d in items}
    hidden = {u for u, rep in member_to_rep.items() if u != rep and rep in rendered}

    cards: list[str] = []
    for d in items:
        u = str(d.get("url_canon") or "")
        if u in hidden:
            continue
        cl = by_rep.get(u)
        others = ([m for m in (cl.get("members") or ())
                   if str(m.get("url_canon") or "") != u] if cl else [])
        cards.append(_doc_card(d, cl, others))

    shown = len(cards)
    total = int(totals.get("docs") or shown)
    note = ""
    if total > shown:
        note = (f'<p class="hint">共 {_num(total)} 条，按相关度只展示前 {_num(shown)} 条；'
                f"完整数据在数据库 mention 表里（或用 --json 导出）。</p>")
    merged_note = ""
    if totals.get("merged"):
        merged_note = (
            f'<p class="hint">同文转载已折叠：{_num(totals.get("merged"))} 条与'
            f'上列某一条正文高度重合，收在“另有 N 篇”里。'
            f'判定用的是正文指纹（字符 3-gram SimHash），不是标题相似度 —— '
            f'转载方改标题是常态。保留作代表的是簇内正文最完整的那一篇。</p>')
    return f"""
<section>
  <h2>文档清单<span class="sub">按相关度降序</span></h2>
  <p class="hint">分数 = 标题命中×3 + 摘要命中×1 + 精确短语×2 + 名次加成（≤1）。
  “仅渠道匹配”表示标题和摘要里都没直接出现关键词，只是通道认为相关 ——
  这部分通常在末尾，可视为待核实的弱信号。</p>
  {merged_note}
  {note}
  <div class="panel"><div class="docs">{''.join(cards)}</div></div>
</section>"""


def _alerts(alerts: Sequence[Mapping[str, Any]]) -> str:
    if not alerts:
        return """
<section>
  <h2>告警</h2>
  <div class="panel"><div class="empty">本次没有任何告警 —— 通道全部正常返回。</div></div>
</section>"""

    # 同类合并：60 条 "blocked" 逐行列出来只会淹没有用信息
    grouped: dict[tuple[str, str], list[str]] = {}
    for a in alerts:
        key = (str(a.get("channel") or "-"), str(a.get("kind") or ""))
        grouped.setdefault(key, []).append(str(a.get("detail") or ""))

    rows = []
    for (channel, kind), details in grouped.items():
        cls = "bad" if kind in {"blocked", "challenge", "captcha", "rate_limit",
                                "unknown_structure", "parse_fail", "fetch_fail"} \
            else ("warn" if kind in {"limit_reached", "dropped", "robots_disallow"} else "")
        shown = details[:3]
        more = (f'<div class="dim">…另有 {len(details) - len(shown)} 条同类</div>'
                if len(details) > len(shown) else "")
        body = "<br>".join(_esc(_short(x, 200)) for x in shown)
        rows.append(
            "<tr>"
            f'<td><code>{_esc(channel)}</code></td>'
            f'<td><span class="tag {cls}">{_esc(_ALERT_CN.get(kind, kind))}</span></td>'
            f'<td class="num">{len(details)}</td>'
            f"<td>{body}{more}</td>"
            "</tr>"
        )
    return f"""
<section>
  <h2>告警<span class="sub">{len(alerts)} 条，按通道与类型合并</span></h2>
  <p class="hint">这里记录的是“我们没拿到什么、为什么没拿到”。被拦和结构变更
  都会出现在这里，而不是被悄悄丢掉。</p>
  <div class="panel">
    <table>
      <thead><tr><th>通道</th><th>类型</th><th class="num">次数</th><th>详情</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>"""


def _footer(task: Mapping[str, Any]) -> str:
    return f"""
<footer>
  <div class="wrap">
    <p>本报告仅聚合公开可检索的标题与摘要，不存储、不分发原文全文；点击标题跳转原文。</p>
    <ol>
      <li>入库前已对手机号、身份证号、邮箱做脱敏，原始值不落库。</li>
      <li>不抓取需登录内容，不绕过验证码 / Cloudflare，不使用代理池。</li>
      <li>被限流、被拒绝、页面结构变更的站点，如实记录在“覆盖率自评”与“告警”中。</li>
      <li>“无日期 / 相对日期”的时间不可当作真实发布时间使用。</li>
    </ol>
    <p class="ver">kwsearch {_esc(__version__)} · 生成于 {_esc(_fmt_ts(datetime.now().isoformat(timespec="seconds")))}</p>
  </div>
</footer>"""


# ------------------------------------------------------------------- 入口


def render_html(data: Mapping[str, Any]) -> str:
    """纯函数：数据 → 完整 HTML 字符串。不碰文件、不碰数据库，便于测试。"""
    task = data.get("task") or {}
    totals = data.get("totals") or {}
    channels = data.get("channels") or []
    queries = data.get("queries") or []
    domains = data.get("domains") or []
    timeline = data.get("timeline") or []
    docs = data.get("docs") or []
    alerts = data.get("alerts") or []
    clusters = data.get("clusters") or []
    person_cards = data.get("person_cards") or []

    tstats = task.get("stats") or {}
    max_per_domain = int(tstats.get("max_per_domain") or 0)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(task.get("keyword"))} · kwsearch 报告</title>
<style>{_CSS}</style>
</head>
<body>
{_header(task)}
<div class="wrap">
{_banners(task, totals, channels)}
{_overview(totals, task)}
{_coverage(channels)}
{_queries(queries)}
{_domains(domains, max_per_domain)}
{_timeline(timeline)}
{_candidates(person_cards)}
{_docs(docs, totals, clusters)}
{_alerts(alerts)}
</div>
{_footer(task)}
</body>
</html>
"""


def render(conn, task_id: str, out_dir: str | Path = "report",  # noqa: ANN001
           *, write_json: bool = True) -> str:
    """把一个任务渲染成 report/<关键词>/index.html，返回绝对路径。

    同一个关键词跑多次会覆盖 index.html —— 这是有意的：报告目录要能双击就开，
    而不是让人在时间戳文件名里挑。历史数据在 SQLite 里，没有丢。
    """
    repo = Repository(conn)
    data = repo.stats(task_id, doc_limit=DOC_RENDER_LIMIT)

    folder = Path(out_dir) / safe_dirname(str(data["task"].get("keyword") or ""))
    folder.mkdir(parents=True, exist_ok=True)

    html_path = folder / "index.html"
    html_path.write_text(render_html(data), encoding="utf-8")

    if write_json:
        # 附一份原始数据：报告结论要能被复核，不能只给一张好看的页面
        (folder / "data.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return str(html_path.resolve())


__all__ = ["render", "render_html", "safe_dirname", "DOC_RENDER_LIMIT"]
