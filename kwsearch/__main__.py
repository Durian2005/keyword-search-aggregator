"""CLI 入口：python -m kwsearch <子命令>

    channels        列出所有通道 + 可用性自检（不联网）
    probe URL       单 URL 探针：真实请求一次，打印状态/耗时/大小/预览（S1）
    discover KW     跑一条通道的发现层，打印候选与断言结果；--offline/--expand 调试（S2）
    page [URL...]   真的打开候选链接，检查能不能解析：robots/编码/挑战页/空壳页（S3a）
    extract [URL...] 抓取并抽出正文与发布时间，看抽得干不干净（S3b）
    run --dry       打印"将要发哪些请求"的计划（不联网）
    run             执行 抓取 → 正文 → 入库 → 转载归并 → 报告（S4/S3d/S5）
    report [id]     由已有任务重新生成 HTML 报告（S6）
    serve           启动本地交互页面（浏览器里点着用，stdlib 服务，零新增依赖）
    db init         建库

只读的六个（channels / probe / discover / page / extract / run --dry）是调试脚手架：
任何一步坏了都先跑它们，而不是直接跑全链路。
"""
from __future__ import annotations

import argparse
import inspect
import logging
import sys
import time
import unicodedata
from pathlib import Path

from kwsearch import __version__
from kwsearch.config import AppConfig, ConfigError, load_config
from kwsearch.person import PersonQuery, parse_person

DB_DEFAULT = "kwsearch.db"


def _w(s: str) -> int:
    """终端显示宽度：CJK 全角字符算 2 列，否则对齐会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    s = str(s)
    return s + " " * max(0, width - _w(s))


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(_w(h), *(_w(r[i]) for r in rows)) if rows else _w(h)
              for i, h in enumerate(headers)]
    out = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        out.append("  ".join(_pad(c, widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def cmd_channels(cfg: AppConfig) -> int:
    from kwsearch.discover import registered_names

    have = set(registered_names())
    rows: list[list[str]] = []
    ready = 0
    for c in cfg.channels:
        if c.parser not in have:
            state = "✗ 未注册"
        else:
            reason = c.unavailable_reason()
            if reason:
                state = f"✗ {reason}"
            elif _is_stub(c.parser):
                state = "○ 未实现"
            else:
                state = "✓ 就绪"
                ready += 1
        rows.append([
            c.id,
            c.type,
            "on" if c.enabled else "off",
            c.parser,
            f"{len(c.offsets())} 次/任务",
            f"{c.throttle_min_interval_s:g}s",
            state,
        ])

    print(f"kwsearch {__version__}  配置: {len(cfg.channels)} 个通道，"
          f"启用 {len(cfg.enabled_channels())} 个，就绪 {ready} 个\n")
    print(_table(rows, ["id", "类型", "启用", "parser", "翻页", "间隔", "状态"]))
    print("\n提示：'未实现' = parser 已注册但函数体还是 NotImplementedError（M1 待写）")
    return 0


def _is_stub(parser_name: str) -> bool:
    """占位实现的判据：函数体里直接 raise NotImplementedError。"""
    from kwsearch.discover import _REGISTRY

    fn = _REGISTRY.get(parser_name)
    if fn is None:
        return True
    code = getattr(fn, "__code__", None)
    return bool(code and "NotImplementedError" in code.co_names)


def cmd_run(
    cfg: AppConfig,
    keyword: str,
    mode: str,
    timespan: str | None,
    dry: bool,
    *,
    db_path: str = DB_DEFAULT,
    max_docs: int | None = None,
    expand: int | None = None,
    require: str | None = None,
    no_filter: bool = False,
    out_dir: str = "report",
    want_report: bool = True,
    fetch_body: bool | None = None,
    stealth: bool = False,
    person: PersonQuery | None = None,
    robots_mode: str | None = None,
    robots_on_failure: str | None = None,
) -> int:
    from kwsearch.discover import registered_names
    from kwsearch.person import anchor_warnings
    from kwsearch.query import expand_queries
    have = set(registered_names())
    if person is not None:
        # 锚点质量提示放最前面：跑完 50 秒才发现"机构名太泛"是最冤的一种浪费
        for w in anchor_warnings(person):
            print(f"  ! {w}")
    variants = expand_queries(keyword, cfg.expand)
    plan: list[list[str]] = []
    for c in cfg.enabled_channels():
        if c.parser not in have:
            plan.append([c.id, "-", "跳过", "parser 未注册"])
            continue
        reason = c.unavailable_reason()
        if reason:
            plan.append([c.id, "-", "跳过", reason])
            continue
        # 计划表必须与 pipeline 的三条真实分支一一对应，否则 --dry 会骗人：
        #   serp        —— 变体矩阵，每个说法只打一次（翻页对它们已失效）
        #   site_search —— 基词翻满页 + 变体各只打第一页（_run_paged_multi）
        #   其它        —— 只打基词，靠翻页扩量
        # ch.variants=false 的通道（如百度：第二发就是验证码）不论哪一类都只打基词。
        # 曾经这里只认 serp 吃变体，site_search 支持变体之后表格仍只列基词，
        # 与实际请求数差出一倍多 —— "计划最多 N 个请求"那句话直接失真。
        targets: list[tuple[str, int]] = []
        if c.type == "serp" and c.variants and len(variants) > 1:
            targets = [(v.q, c.paging.start) for v in variants]
        elif (c.type == "site_search" and c.variants
              and len(variants) > 1 and c.paging.mode != "none"):
            offsets = c.offsets()
            targets = [(variants[0].q, off) for off in offsets]
            targets += [(v.q, offsets[0]) for v in variants[1:]]
        else:
            targets = [(keyword, off) for off in c.offsets()]
        for q, off in targets:
            plan.append([c.id, _clip(q, 20), "待发", c.build_url(q, off, timespan)])

    total = sum(1 for r in plan if r[2] == "待发")
    if dry:
        print(f"[{mode}] 关键词 = {keyword!r}   变体 {len(variants)} 个   "
              f"时间窗 = {timespan or '默认'}\n")
        print(_table(plan, ["通道", "查询", "动作", "URL"]))
        print(f"\n共 {len(plan)} 行，其中 {total} 个请求待发"
              f"（单域 {cfg.crawl.get('per_domain_qps', 1)} QPS，"
              f"全局并发 {cfg.crawl.get('concurrency', 4)}）")
        print("注意：实际执行时会按 zero_gain_streak 早停、被拦即停，"
              f"所以真实请求数通常低于 {total}")
        # profile=auto 时上面只列得出基词：变体要等基词结果出来、探测完类型才定。
        # 不说明的话，"共 N 个请求待发"会被当成上界，而它只是下界。
        profiles = dict(cfg.expand or {}).get("profiles") or {}
        n_var = max((len(v or []) for v in profiles.values()), default=0)
        if (str((cfg.expand or {}).get("profile", "auto")) == "auto"
                and person is None and n_var):
            print(f"       profile=auto 会先打基词、按结果类型决定是否追加变体"
                  f"（最多 +{n_var} 个查询），所以这也是个下界。")
        print("\n--dry：只渲染 URL，未发任何请求。")
        return 0

    # 真实运行时只报"将要做什么"，不铺整张表 —— 执行过程的逐通道结果更有用
    print(f"[{mode}] 关键词 = {keyword!r}  变体 {len(variants)} 个  "
          f"计划最多 {total} 个请求  上限 {max_docs or cfg.limits.get('max_docs', 1000)} 条")
    if robots_mode:
        print(f"robots 档位 = {robots_mode}"
              + (f" · 失败语义 = {robots_on_failure}" if robots_on_failure else "")
              + ("（⚠ 将无视 robots.txt 规则抓取，逐条留痕进报告）"
                 if robots_mode in ("record", "off") else ""))
    if mode != "quick":
        print(f"⚠ mode={mode} 尚未实现（deep 需要种子域遍历，M3），本次按 quick 执行。")

    from kwsearch.pipeline import run_task

    print()
    try:
        result = run_task(
            cfg, keyword, mode=mode, timespan=timespan, db_path=db_path,
            max_docs=max_docs, expand=expand, require=require,
            no_filter=no_filter, progress=print, fetch_body=fetch_body,
            stealth=stealth, person=person,
            robots_mode=robots_mode, robots_on_failure=robots_on_failure,
        )
    except KeyboardInterrupt:
        # 中断也要能解释：已入库的部分留在库里，报告可以正常生成
        print("\n已中断。已完成的通道数据已落库，可稍后用 report 生成报告。")
        return 130

    print(_run_summary(result))

    if want_report and result.kept:
        cc = dict(getattr(cfg, "cluster", None) or {})
        return _write_report(db_path, result.task_id, out_dir,
                             min_body_chars=int(cc.get("min_body_chars") or 200))
    return 0 if result.kept else 1


def _run_summary(result) -> str:  # noqa: ANN001
    """把 RunResult 压成一张能扫的收尾表。"""
    rows = [
        [r.channel, r.type or "-",
         "跳过" if r.skipped else str(r.queries),
         "-" if r.skipped else str(r.got),
         "-" if r.skipped else str(r.unique),
         r.skipped or r.error or (f"被拦 {r.blocked}" if r.blocked else "正常"),
         r.stopped_reason or "-"]
        for r in result.per_channel
    ]
    head = (
        f"\n任务 {result.task_id}  {result.status}"
        + ("  [结果不完整]" if result.truncated else "")
        + f"\n入库 {result.kept}（新 {result.docs_new}） · 去重后 {result.unique}"
          f" · 原始 {result.candidates} · 丢弃 {result.dropped_url}"
          f" · 配额丢 {result.dropped_cap} · 脱敏 {result.pii_masked}"
          + (f" · 同文换链 {result.dropped_dup}" if result.dropped_dup else "")
          + (f" · 列表页解析 {result.resolved_list} 篇"
             if result.resolved_list else "")
          + f" · 告警 {result.alerts} · {result.elapsed_ms} ms"
    )
    if result.person_cards or result.person_name_missing:
        from kwsearch.person import TIER_CN
        tiers = "、".join(f"{TIER_CN[t]} {n}" for t, n in result.person_tiers.items() if n)
        head += f"\n候选人 {result.person_cards} 张：{tiers}"
        if result.person_name_missing:
            # 这句很重要：没有它，"结果很少"会被误读成"这个人没上网"
            head += (f"（另有 {result.person_name_missing} 条结果里没出现该姓名，"
                     f"多为机构页面，未参与归因）")
    # getattr 兜底：web 层的假 result（SimpleNamespace）不带 robots/gov 字段
    if getattr(result, "robots_mode", "") not in ("", "obey"):
        head += f"\nrobots 档位 {result.robots_mode}"
        if getattr(result, "robots_forced", 0):
            head += f" · ⚠ 强制抓取 {result.robots_forced} 条（已留痕进报告）"
    if getattr(result, "gov_blocked", 0):
        head += f"\n政府站红线拦截 {result.gov_blocked} 条（请求未发）"
    return head + "\n\n" + _table(rows, ["通道", "类型", "请求", "解析", "去重", "状态", "停止原因"])


def _write_report(db_path: str, task_id: str, out_dir: str,
                  *, min_body_chars: int = 200) -> int:
    from kwsearch.report import render
    from kwsearch.storage import connect

    conn = connect(db_path)
    try:
        path = render(conn, task_id, out_dir, min_body_chars=min_body_chars)
    except KeyError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"\n报告：{path}")
    return 0


def cmd_report(cfg: AppConfig, task_id: str | None, out_dir: str, db_path: str) -> int:
    from kwsearch.report import render, status_cn
    from kwsearch.storage import connect, recent_tasks

    if not Path(db_path).exists():
        print(f"数据库不存在: {Path(db_path).resolve()}（先跑一次 run 或 db init）",
              file=sys.stderr)
        return 2

    conn = connect(db_path)
    try:
        if not task_id:
            rows = recent_tasks(conn, 10)
            if not rows:
                print("库里还没有任务记录，先跑一次 run。", file=sys.stderr)
                return 2
            table = _table(
                [[r["task_id"], _clip(r["keyword"], 24), r["mode"],
                  r["started_at"][:16].replace("T", " "), status_cn(r["status"]),
                  "截断" if r["truncated"] else ""]
                 for r in rows],
                ["task_id", "关键词", "模式", "开始时间", "状态", ""],
            )
            print("最近的任务：\n" + table + "\n")
            task_id = str(rows[0]["task_id"])
            print(f"未指定 task_id，按最近一次 {task_id} 生成。\n")
        try:
            cc = dict(getattr(cfg, "cluster", None) or {})
            path = render(conn, task_id, out_dir,
                          min_body_chars=int(cc.get("min_body_chars") or 200))
        except KeyError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    print(f"报告：{path}")
    return 0


def cmd_serve(
    cfg: AppConfig,
    host: str,
    port: int,
    out_dir: str,
    no_browser: bool,
    *,
    db_path: str = DB_DEFAULT,
) -> int:
    """本地交互页面：把 run/report 包进一个浏览器页面，命令照旧可用。

    与 CLI 的关系是「同一套内核，两个入口」：页面发的每个请求最终都走
    run_task + render，和命令行没有第二份实现。默认只绑 127.0.0.1。
    """
    from kwsearch.web import serve as web_serve

    return web_serve(
        host, port, cfg=cfg, db_path=db_path, out_dir=out_dir,
        open_browser=not no_browser,
    )


def cmd_probe(
    cfg: AppConfig,
    url: str,
    raw_headers: list[str],
    timeout: int,
    preview: int,
    save: str | None,
    which: str,
) -> int:
    """单 URL 探针。通道上线前先用它确认"能拿到什么"，比直接跑全链路快得多。"""
    from kwsearch.fetch import (
        FetchError,
        RetryPolicy,
        build_fetcher,
        default_headers,
        fetch_with_retry,
    )

    headers = default_headers(cfg.defaults)
    for h in raw_headers:
        sep = ":" if ":" in h else ("=" if "=" in h else None)
        if not sep:
            print(f"请求头格式应为 'K: V'，收到: {h!r}", file=sys.stderr)
            return 2
        k, v = h.split(sep, 1)
        headers[k.strip()] = v.strip()

    try:
        fetcher = build_fetcher(which)
    except RuntimeError as e:
        print(f"无可用 Fetcher：{e}", file=sys.stderr)
        return 2

    policy = RetryPolicy.from_mapping(cfg.defaults.get("retry"))
    print(f"fetcher = {fetcher.name}    timeout = {timeout}s    最多 {policy.max_attempts} 次")
    print(f"url     = {url}")
    if raw_headers:
        print(f"headers = {', '.join(raw_headers)}")

    try:
        res = fetch_with_retry(fetcher, url, headers, policy, timeout)
    except FetchError as e:
        extra = f"  (status={e.status})" if e.status else ""
        print(f"\n✗ [{e.kind}] {e}{extra}")
        print("  conn/timeout → 网络不可达或超时；blocked → 401/403，已判定不放行；"
              "http → 其他状态码")
        return 4

    print(f"\n✓ status  = {res.status}")
    print(f"  final   = {res.final_url}")
    print(f"  size    = {res.size:,} B（{res.chars:,} 字符）")
    print(f"  type    = {res.content_type or '-'}")
    print(f"  time    = {res.elapsed_ms} ms")
    if save:
        p = Path(save)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(res.body, encoding="utf-8")
        print(f"  saved   = {p.resolve()}")
    if preview and res.body:
        head = res.body[:preview].replace("\r", " ").replace("\n", " ")
        print(f"  preview = {head}")
    return 0


def _resolve_targets(
    db_path: str, urls: list[str], task: str | None, limit: int | None,
) -> tuple[list[str], str]:
    """命令行给的 URL 优先；否则从库里的任务取。返回 (URL 列表, 错误信息)。

    错误信息用空串表示"没问题"，这样调用方只有一处需要判断。
    """
    targets = [u.strip() for u in urls if u.strip()]
    if targets:
        return targets, ""

    from kwsearch.storage import Repository, connect, recent_tasks

    path = Path(db_path)
    if not path.exists():
        return [], f"数据库不存在: {path.resolve()}；给几个 URL 或先跑 run"
    conn = connect(db_path)
    try:
        tid = task
        if tid in (None, "latest"):
            rows = recent_tasks(conn, 1)
            if not rows:
                return [], "库里还没有任务记录；给几个 URL，或先跑 run"
            tid = str(rows[0]["task_id"])
        docs = Repository(conn).stats(tid, doc_limit=limit or 50)["docs"]
    finally:
        conn.close()

    if not docs:
        return [], f"任务 {tid} 里没有可抓的文档"
    print(f"从任务 {tid} 取 {len(docs)} 条 URL\n")
    return [d["url_canon"] for d in docs], ""


def cmd_page(
    cfg: AppConfig,
    urls: list[str],
    *,
    db_path: str = DB_DEFAULT,
    task: str | None = None,
    limit: int | None = None,
    show: int = 0,
    no_robots: bool = False,
    robots_mode: str | None = None,
    robots_on_failure: str | None = None,
    per_domain: int | None = None,
    which: str = "auto",
) -> int:
    """S3a 验收入口：真正打开候选链接，回答"能拿到多少份可解析的 HTML"。

    与 probe 的分工：probe 只看**一条** URL 的传输结果（状态/大小/预览），
    这里是**一批**，而且看的是抽取前置条件 —— robots、编码、挑战页、空壳页。
    """
    from kwsearch.fetch import build_fetcher, default_headers
    from kwsearch.fetch.page import PagePolicy, fetch_pages, page_text

    targets, err = _resolve_targets(db_path, urls, task, limit)
    if err:
        print(err, file=sys.stderr)
        return 2

    policy = PagePolicy.from_config(
        cfg, limit=limit,
        robots_mode=robots_mode or ("off" if no_robots else None),
        robots_on_failure=robots_on_failure)
    if per_domain:
        policy.max_per_domain = per_domain

    try:
        fetcher = build_fetcher(which)
    except RuntimeError as e:
        print(f"无可用 Fetcher：{e}", file=sys.stderr)
        return 2

    print(f"[抓正文] {len(targets)} 条 · 单域 {policy.per_domain_qps:g} QPS · "
          f"单域上限 {policy.max_per_domain} · "
          f"robots {policy.robots_mode}"
          + (f"/{policy.robots_on_failure}" if policy.robots_mode != "off" else "")
          + f" · fetcher={fetcher.name} · 超时 {policy.timeout}s\n")

    headers = default_headers(cfg.defaults)
    robots = None
    if policy.robots_mode != "off":
        from kwsearch.fetch.robots import RobotsCache

        # 隐身档的 robots.txt 走轻量抓取器：为一行规则启动浏览器是纯浪费
        robots_fetcher = getattr(fetcher, "robots_fetcher", None) or fetcher
        robots = RobotsCache(robots_fetcher, headers.get("User-Agent", "*"), timeout=10,
                            mode=policy.robots_mode,
                            on_failure=policy.robots_on_failure)

    batch = fetch_pages(
        targets, policy,
        fetcher=fetcher,
        headers=headers,
        progress=print,
        robots=robots,
    )

    rows = [[
        str(i),
        _clip(p.host or "-", 22),
        str(p.status or "-"),
        f"{p.bytes:,}",
        str(p.text_chars),
        p.encoding or "-",
        p.enc_source or "-",
        "✓" if p.ok else f"✗ {p.failure}",
        _clip(p.title, 24) or "-",
    ] for i, p in enumerate(batch.pages, start=1)]

    if rows:
        print()
        # 注意列名是"文本量"不是"正文量"：这里是整页去标签后的字数（含导航/推荐位），
        # 只用于判 challenge 与 JS 站。真正的正文长度要等 S3b 抽出主容器才有。
        print(_table(rows, ["#", "域名", "状态", "字节", "文本量", "编码", "来源", "判定", "标题"]))

    print(f"\n成功 {batch.ok_count}/{batch.total}（{batch.success_rate:.0%}）"
          f" · 硬失败 {batch.hard_failures()} · 配额跳过 {batch.skipped}"
          f" · 限速等待 {batch.slept_s:.1f}s · 总耗时 {batch.elapsed_ms / 1000:.1f}s")

    if robots is not None and robots.stats:
        st = robots.stats
        forced = st.get("forced", 0)
        print(f"robots：{st.get('hosts', 0)} 个站点"
              f"（有规则 {st.get('with_rules', 0)} · 无规则 {st.get('no_rules', 0)}"
              f" · 不可达 {st.get('unreachable', 0)}）"
              f" · 拒绝请求 {st.get('denied', 0)} 次"
              + (f" · ⚠ 强制抓取 {forced} 次（无视规则，已留痕）" if forced else ""))

    fails = batch.failures()
    if fails:
        print("失败分布：" + "  ".join(f"{k}({v})" for k, v in fails.items()))
        print("  硬失败 = 被挡（robots/403/挑战页/超时）；软失败 = 打开了但交不出正文；"
              "gov_blocked = 政府站红线，请求未发")

    if show:
        for p in batch.pages:
            if not p.ok:
                continue
            # 这是整页文本（含导航/推荐位），只用来确认"抓到了什么"。
            # 真正的正文要等 S3b 抽出主容器，这里看到的长尾噪声是已知的。
            body = page_text(p.html)[:show].replace("\n", " ")
            print(f"\n── {p.title or p.url}\n   {p.url}\n   {body}")

    return 0 if batch.ok_count else 1


def cmd_extract(
    cfg: AppConfig,
    urls: list[str],
    *,
    db_path: str = DB_DEFAULT,
    task: str | None = None,
    limit: int | None = None,
    show: int = 400,
    no_robots: bool = False,
    robots_mode: str | None = None,
    robots_on_failure: str | None = None,
    which: str = "auto",
) -> int:
    """S3b 验收入口：抓一页 → 抽正文 → 抽时间，三件事的结果并排看。

    与 page 的分工：page 只回答"能不能拿到可解析的 HTML"，
    这里回答"拿到之后文章本身抽出来了吗、时间可不可信"。
    """
    from kwsearch.extract import extract_article, pick_date
    from kwsearch.fetch import build_fetcher, default_headers
    from kwsearch.fetch.page import PagePolicy, fetch_pages
    from kwsearch.models import utcnow_iso

    targets, err = _resolve_targets(db_path, urls, task, limit)
    if err:
        print(err, file=sys.stderr)
        return 2

    policy = PagePolicy.from_config(
        cfg, limit=limit,
        robots_mode=robots_mode or ("off" if no_robots else None),
        robots_on_failure=robots_on_failure)
    try:
        fetcher = build_fetcher(which)
    except RuntimeError as e:
        print(f"无可用 Fetcher：{e}", file=sys.stderr)
        return 2

    sources = tuple(cfg.extract.get("date_sources")
                    or ("jsonld", "meta", "regex", "serp", "fallback"))
    print(f"[抽正文] {len(targets)} 条 · 时间优先级 {' → '.join(sources)}"
          f" · fetcher={fetcher.name}\n")

    batch = fetch_pages(targets, policy, fetcher=fetcher,
                        headers=default_headers(cfg.defaults), progress=print)

    rows: list[list[str]] = []
    samples: list[tuple[str, str, str, str]] = []
    time_counts: dict[str, int] = {}
    good = 0
    high = 0
    total_chars = 0

    for i, p in enumerate(batch.pages, start=1):
        if not p.ok:
            rows.append([str(i), _clip(p.host or "-", 20), "-", "-", "-", "-", "-",
                         f"✗ {p.failure}"])
            continue

        art = extract_article(p.html, title_hint=p.title)
        # 不传 fetched_at：fallback 时 published_at 必须是 NULL（见 schema.sql），
        # 抓取时间已经在 doc.first_seen 里，塞进发布时间只会污染时间线。
        dr = pick_date(p.html, serp_hint=None, text=art.text, sources=sources)
        if art.ok:
            good += 1
            total_chars += art.chars
            if art.confidence == "high":
                high += 1
        time_counts[dr.time_source] = time_counts.get(dr.time_source, 0) + 1

        # 低置信必须显式标出来：S3d 入库时按它决定要不要覆盖 SERP 摘要
        verdict = {"high": "✓", "medium": "△ 低置信", "low": "△ 整页"}.get(art.confidence, "✓")
        if not art.ok:
            verdict = "✗ 太短"

        rows.append([
            str(i), _clip(p.host or "-", 20), str(art.chars), str(art.paragraphs),
            _clip(art.container, 22),
            (dr.published_at or "-")[:19],
            dr.time_source + ("!" if dr.time_source in ("fallback", "regex_updated") else ""),
            verdict,
        ])
        samples.append((
            art.title or p.title or p.url, p.url,
            f"{art.container} · {art.chars} 字 · {art.paragraphs} 段"
            f" · 链接密度 {art.link_ratio:.2f} · 置信 {art.confidence}",
            art.text,
        ))

    print()
    print(_table(rows, ["#", "域名", "正文", "段落", "容器", "时间", "来源", "判定"]))
    print(f"\n可用 {good}/{batch.total}（高置信 {high}）"
          f" · 正文平均 {total_chars // max(good, 1)} 字"
          f" · 时间来源 " + (" / ".join(f"{k} {v}" for k, v in sorted(time_counts.items()))
                            or "-"))
    print("  低置信 = 没过打分阈值，可能抽到推荐位；整页 = 退到 body；"
          "fallback! = 没抽到日期（published_at 留空）；"
          "regex_updated! = 只有「最近更新」（多为站点构建时间）")

    if show:
        for title, url, meta, body in samples:
            if not body:
                continue
            print(f"\n── {title}\n   {url}\n   [{meta}]")
            for line in body[:show].split("\n"):
                print(f"   {line}")

    return 0 if good else 1


def _clip(s: str, width: int) -> str:
    """按终端显示宽度截断，超长补省略号（中文按 2 列算，否则表格会歪）。"""
    s = str(s)
    if _w(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def cmd_discover(
    cfg: AppConfig,
    keyword: str,
    channel_id: str,
    pages: int,
    offline: str | None,
    as_json: bool,
    limit: int,
    expand: int | None = None,
    require: str | None = None,
    no_filter: bool = False,
) -> int:
    """发现层单通道调试。这是 S2 的验收入口。

    三个用途分开：
      联网       —— 验证"真实请求还能不能拿到结果块"
      --offline  —— 只用本地存下的 HTML 复跑解析，改选择器时不必反复打线上
      --expand   —— 基词 + 变体矩阵，看"打几次能把量提上来"（早停 + 去重）
    """
    import json as _json
    import re
    from dataclasses import asdict

    from kwsearch.discover import get_channel, get_parser
    from kwsearch.discover.base import build_context, check_assertions, page_signature
    from kwsearch.expand import domain_distribution, expand_channel
    from kwsearch.query import expand_queries, probe_profile
    try:
        ch = cfg.channel(channel_id)
    except KeyError:
        ids = ", ".join(c.id for c in cfg.channels)
        print(f"配置里没有通道 {channel_id!r}（可选: {ids}）", file=sys.stderr)
        return 2

    if not ch.enabled:
        print(f"⚠ {ch.id} 在配置里 enabled=false，仍按调试请求执行\n", file=sys.stderr)

    challenge_kw = tuple(k for k in (cfg.extract.get("challenge_keywords") or ()) if k)
    results = []
    stats = None
    merged = None

    if offline:
        if expand is not None:
            print("⚠ --offline 只解析一份样本，忽略 --expand", file=sys.stderr)
        parser = get_parser(ch.parser)
        if parser is None:
            print(f"{ch.id} 没有登记离线解析器（parser={ch.parser}），只能联网跑", file=sys.stderr)
            return 2
        path = Path(offline)
        if not path.exists():
            print(f"样本文件不存在: {path}", file=sys.stderr)
            return 2
        html = path.read_text(encoding="utf-8", errors="replace")
        # 只有需要"降级体检"的解析器（bing_html）才接受 query；
        # 结构化通道（B站/GDELT 的 JSON）是服务端匹配，不需要这个参数。
        kwargs: dict = {"challenge_keywords": challenge_kw, "channel_id": ch.id}
        if "query" in inspect.signature(parser).parameters:
            kwargs["query"] = keyword
        r = parser(html, ch.paging.start, **kwargs)
        r.query = keyword
        results.append(r)
    else:
        try:
            fn = get_channel(ch.parser)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
        ctx = build_context(cfg, ch.id)

        if expand is not None:
            spec = dict(cfg.expand or {})
            if expand > 0:
                spec["max_queries"] = expand
            variants = expand_queries(keyword, spec)
            auto_probe = bool(spec.get("profiles")) and str(spec.get("profile", "auto")) == "auto"

            def _probe_hook(cands):
                # 与 pipeline 同一套探测：基词结果出来后判类型，再补变体
                rest = expand_queries(keyword, spec, probe=cands)[1:]
                if rest:
                    print(f"⿻ 探测：基词结果判为 {probe_profile(cands)}，加 {len(rest)} 个变体")
                else:
                    print(f"⿻ 探测：基词结果判为 {probe_profile(cands)}，只打基词")
                return rest

            pattern = None
            if not no_filter:
                # 过滤只对 serp 类生效：只有通用搜索引擎会"放宽"查询。
                # GDELT 是服务端短语检索、B站是站内检索，它们返回的每一条都是服务端
                # 认为相关的，再按"标题含基词"筛一遍会把多语种/改写标题的好结果全杀掉
                # （实测：GDELT 的热门词结果标题是土耳其语等机器翻译，一个都不含原词）。
                if require:
                    pattern = re.compile(require, re.I)
                elif spec.get("require_keyword", True) and ch.type == "serp":
                    pattern = re.compile(re.escape(keyword), re.I)
            merged, stats = expand_channel(
                fn, ctx, variants,
                zero_gain_streak=int(spec.get("zero_gain_streak", 2)),
                require=pattern,
                plan_hook=_probe_hook if auto_probe else None,
            )
            results = []          # 扩展模式下不再有"每页"概念，明细全在 stats 里
        else:
            offsets = ch.offsets()
            if pages > len(offsets):
                print(f"⚠ 配置里 {ch.id} 只允许 {len(offsets)} 页，"
                      f"--pages {pages} 按 {len(offsets)} 页执行", file=sys.stderr)
            for i, off in enumerate(offsets[:max(1, pages)]):
                if i:
                    time.sleep(ch.throttle_min_interval_s)   # 限速在驱动层，不在通道里
                r = fn(keyword, off, ctx)
                sig = page_signature(r)
                if results and sig and sig == page_signature(results[-1]):
                    # 分页参数失效时通道不会报错，只会把同一页再给一遍 —— 必须自己发现
                    print(f"⚠ offset={off} 与本页之前的请求结果完全相同，"
                          f"判定分页失效，已停止翻页（省下后续请求）", file=sys.stderr)
                    break
                results.append(r)

    cands = merged if merged is not None else [c for r in results for c in r.candidates]
    total = len(cands)
    # 被拦的那一次不该再报"条目不足"：它已经用 [http_412]/[challenge] 单独说明了
    problems_all = [p for r in results if not r.blocked for p in check_assertions(r, ch)]

    if as_json:
        payload: dict = {"candidates": [asdict(c) for c in cands]}
        if stats is not None:
            payload["stats"] = asdict(stats)
        else:
            payload["results"] = [asdict(r) for r in results]
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode_desc = (f"变体 {stats.queries} 个（上限 {int((cfg.expand or {}).get('max_queries', 8))}）"
                     if stats else (f"离线样本 {offline}" if offline else f"{len(results)} 次请求"))
        print(f"[{ch.id}] 关键词 = {keyword!r}   {mode_desc}\n")

        if stats is not None:
            rows = [[str(s.seq), _clip(s.q, 22), s.strategy,
                     str(s.got), str(s.new), str(s.duplicated), str(s.filtered),
                     "被拦" if s.blocked else ("有效" if s.useful else "无新增")]
                    for s in stats.per_query]
            print(_table(rows, ["#", "查询", "来源", "解析", "新增", "重复", "过滤", "判定"]))
            print(f"\n合并：{stats.raw} 条原始 → 去重 {stats.unique} 条"
                  f"（重复 {stats.duplicated}，过滤 {stats.filtered}）"
                  f"　用时 {stats.elapsed_ms} ms")
            print(f"停止：{stats.stopped_reason}")
            dist = domain_distribution(cands)
            if dist:
                head = "  ".join(f"{d}({n})" for d, n in dist[:8])
                more = f" …共 {len(dist)} 个域名" if len(dist) > 8 else ""
                print(f"域名：{head}{more}")
            print()

        rows = []
        for i, c in enumerate(cands[:limit], start=1):
            rows.append([
                str(i),
                c.extra.get("domain", ""),
                _clip(c.title, 32),
                c.published_hint or "-",
                _clip(c.url_raw, 55),
            ])
        if rows:
            print(_table(rows, ["#", "域名", "标题", "日期线索", "URL"]))
        else:
            print("（无候选）")

        print()
        for r in results:
            if r.blocked:
                state = "被拦"
            elif r.block_kind == "empty":
                state = "无结果"
            elif r.block_kind == "relaxed":
                state = "查询被降级"
            elif r.block_kind == "boilerplate":
                state = "无结果兜底页"
            elif r.block_kind:
                state = "结构异常"
            else:
                state = "正常"
            print(f"  {r.channel}  {r.detail or '-'}   → {len(r.candidates)} 条"
                  f" / raw {r.raw_count} / {r.elapsed_ms}ms / {state}"
                  + (f" [{r.block_kind}]" if r.block_kind else ""))

        if problems_all:
            print("\n断言未通过：")
            for p in dict.fromkeys(problems_all):     # 多页重复的同一个问题只报一次
                print(f"  ✗ {p}")
        elif results:
            print(f"\n断言通过：min_items={ch.assertions.min_items}，"
                  f"必填 {', '.join(ch.assertions.required_fields)}")

    if total == 0:
        return 6
    if stats is not None:
        if stats.blocked:
            return 6
        if total < ch.assertions.min_items:
            return 5
        return 0
    return 5 if problems_all else 0


def cmd_db(cfg: AppConfig, action: str, db_path: str, assume_yes: bool = False) -> int:
    from kwsearch.storage import Repository, init_db, recent_tasks

    if action == "init":
        conn = init_db(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        conn.close()
        print(f"已初始化 {Path(db_path).resolve()}")
        print("表: " + ", ".join(tables))
        return 0

    if action != "clear":
        print(f"未知 db 动作: {action}（可选 init / clear）", file=sys.stderr)
        return 2

    # ---- 清空所有历史数据 ----
    # 破坏性操作，两道闸：先打印将删什么（不看数就删等于盲删），
    # 再要求 --yes 明示确认。页面上同样两步（预览 + 确认框）。
    if not Path(db_path).exists():
        print(f"数据库不存在，没什么可清: {Path(db_path).resolve()}")
        return 0
    conn = init_db(db_path)
    repo = Repository(conn)
    preview = repo.wipe_preview()
    total = sum(preview.values())
    print(f"数据库: {Path(db_path).resolve()}")
    if total == 0:
        print("已经是空库（所有表 0 行）。")
        conn.close()
        return 0
    print("将删除以下全部数据：")
    tasks = recent_tasks(conn, 10)
    if tasks:
        print("  任务（最近 10 个）:")
        for r in tasks:
            kw = (r["keyword"] or "")[:20]
            when = (r["started_at"] or "")[:16].replace("T", " ")
            print(f"    {kw:22s} {when}  {r['status']}")
    for t, n in preview.items():
        if n:
            print(f"  {t:14s} {n:>6d} 行")
    print(f"合计 {total} 行。报告 HTML 文件不受影响，仍在报告目录里。")
    sys.stdout.flush()   # 管道里 stdout 是块缓冲，不让"将删什么"排在闸门提示后面
    if not assume_yes:
        print("未加 --yes，什么也没删。确认请执行: db clear --yes", file=sys.stderr)
        conn.close()
        return 2
    deleted = repo.wipe()
    gone = sum(deleted.values())
    print(f"已清空 {gone} 行，库恢复到初始状态。")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认可能是 GBK，中文/emoji 会在 print 时直接抛 UnicodeEncodeError。
    # 改成 utf-8 + replace，宁可显示成问号也不要整个命令崩掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        prog="kwsearch",
        description="指定关键词 → 多通道公开信息聚合爬虫",
    )
    p.add_argument("--config", help="channels.yaml 路径（默认用包内配置）")
    p.add_argument("--db", default=DB_DEFAULT, help=f"SQLite 路径（默认 {DB_DEFAULT}）")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="打印各层的调试日志（通道失败、重试等）")
    p.add_argument("-V", "--version", action="version", version=f"kwsearch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("channels", help="列出通道并做可用性自检（不联网）")

    pr = sub.add_parser("run", help="执行一次关键词搜索，并生成 HTML 报告")
    pr.add_argument("keyword")
    pr.add_argument("--mode", choices=["quick", "deep", "watch"], default="quick")
    pr.add_argument("--timespan", default=None, help="GDELT 时间窗，如 7d / 30d")
    pr.add_argument("--dry", action="store_true", help="只打印请求计划，不联网")
    pr.add_argument("--max-docs", type=int, default=None, metavar="N",
                    help="入库上限，默认取配置 limits.max_docs（触顶会标 truncated）")
    pr.add_argument("--expand", nargs="?", type=int, const=-1, default=None, metavar="N",
                    help="查询变体上限（含基词）；不带 N 用配置里的 max_queries")
    pr.add_argument("--require", metavar="REGEX",
                    help="只保留 标题+摘要 命中该正则的候选（默认按基词过滤，仅 serp 通道）")
    pr.add_argument("--no-filter", action="store_true", help="关闭关键词过滤")
    pr.add_argument("--no-body", action="store_true",
                    help="不抓正文（摘要用搜索结果原文，快但信息量小）")
    pr.add_argument("--out", default="report", help="报告输出目录（默认 report/）")
    pr.add_argument("--no-report", action="store_true", help="只入库，不生成报告")
    pr.add_argument("--stealth", action="store_true",
                    help="正文抓取改用 Scrapling 隐身浏览器（可过 Cloudflare 挑战；"
                         "需 pip install 'scrapling[fetchers]' && scrapling install；"
                         "默认关闭，反爬能力为可选项）")
    pr.add_argument("--robots-mode", default=None, choices=["obey", "record", "off"],
                    help="robots.txt 档位：obey=遵守（默认）| record=查但不拦（留痕）"
                         "| off=完全不查。默认听配置 crawl.robots_mode")
    pr.add_argument("--no-robots", action="store_true",
                    help="等价 --robots-mode off：不检查 robots.txt")
    pr.add_argument("--robots-on-failure", default=None, choices=["deny", "allow"],
                    help="robots.txt 拉不到时：deny=保守禁止（默认）| allow=放行（留痕）")
    # ---- 人名模式（见 person.py）：查询侧只打裸姓名，收窄在结果侧做 ----
    pr.add_argument("--person", action="store_true",
                    help="人名模式：只打裸姓名，按机构/方向锚点在结果内归因，产出候选人卡片")
    pr.add_argument("--org", action="append", default=[], metavar="机构",
                    help="强锚点，可重复或逗号分隔，如 --org 某某大学")
    pr.add_argument("--field", action="append", default=[], metavar="方向",
                    help="弱锚点（研究方向/业务领域），可重复，如 --field 网络安全")
    pr.add_argument("--role", action="append", default=[], metavar="身份",
                    help="弱锚点（身份/职称），可重复，如 --role 教授 --role 博导")

    ps = sub.add_parser("serve", help="启动本地交互页面（浏览器操作，零新增依赖）")
    ps.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1，仅本机；改 0.0.0.0 会暴露给局域网）")
    ps.add_argument("--port", type=int, default=8787,
                    help="起始端口，被占用则顺序往上试（默认 8787）")
    ps.add_argument("--out", default="report", help="报告输出目录（默认 report/）")
    ps.add_argument("--no-browser", action="store_true", help="只起服务，不自动打开浏览器")

    pp = sub.add_parser("probe", help="单 URL 探针：状态/耗时/大小/预览，通道上线前先跑它")
    pp.add_argument("url")
    pp.add_argument("--header", action="append", default=[], metavar="K:V",
                    help="附加请求头，可重复，如 --header 'Referer: https://www.bilibili.com/'")
    pp.add_argument("--timeout", type=int, default=None, help="秒，默认取配置里的 20")
    pp.add_argument("--preview", type=int, default=200, help="预览字符数，0 = 不预览")
    pp.add_argument("--save", help="把响应体写到文件，便于离线调试抽取器")
    pp.add_argument("--fetcher", default="auto",
                    choices=["auto", "curl_cffi", "httpx", "scrapling"],
                    help="scrapling = 隐身浏览器档（需 scrapling[fetchers]）")

    pd = sub.add_parser("discover", help="跑一条通道的发现层，打印候选与断言；--offline/--expand 两种调试法")
    pd.add_argument("keyword")
    pd.add_argument("--channel", default="bing_html", help="通道 id（默认 bing_html）")
    pd.add_argument("--pages", type=int, default=1, help="翻几页（上限由配置里的 max_pages 决定）")
    pd.add_argument("--expand", nargs="?", type=int, const=-1, default=None, metavar="N",
                    help="启用查询变体（基词+修饰词矩阵）；不带 N 用配置里的 max_queries")
    pd.add_argument("--require", metavar="REGEX",
                    help="只保留 标题+摘要 命中该正则的候选；不给则默认按基词过滤")
    pd.add_argument("--no-filter", action="store_true",
                    help="完全关闭关键词过滤（连默认的基词过滤也关）")
    pd.add_argument("--offline", metavar="HTML", help="不联网，直接解析本地保存的样本文件")
    pd.add_argument("--json", action="store_true", help="输出 JSON（便于管道/对比）")
    pd.add_argument("--limit", type=int, default=20, help="表格最多显示多少行")

    prep = sub.add_parser("report", help="由已有任务生成 HTML 报告（不给 task_id 则取最近一次）")
    prep.add_argument("task_id", nargs="?", default=None)
    prep.add_argument("--out", default="report", help="报告输出目录（默认 report/）")

    ppage = sub.add_parser(
        "page",
        help="S3a：真的打开候选链接，检查能否解析（robots/编码/挑战页/空壳页）",
    )
    ppage.add_argument("urls", nargs="*", default=[], metavar="URL",
                       help="要抓的 URL；不给则从库里的任务取")
    ppage.add_argument("--task", default=None, metavar="ID|latest",
                       help="从这个任务的文档取 URL（缺省取最近一次）")
    ppage.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多抓几条")
    ppage.add_argument("--per-domain", type=int, default=None, metavar="N",
                       help="单域上限，默认取 filter.max_per_domain")
    ppage.add_argument("--show", type=int, default=0, metavar="N",
                       help="额外打印每条正文前 N 字（看抽取质量用）")
    ppage.add_argument("--no-robots", action="store_true",
                       help="等价 --robots-mode off（默认遵守）")
    ppage.add_argument("--robots-mode", default=None, choices=["obey", "record", "off"],
                       help="robots.txt 档位：obey|record|off（默认听配置）")
    ppage.add_argument("--robots-on-failure", default=None, choices=["deny", "allow"],
                       help="robots.txt 拉不到时：deny（默认）| allow")
    ppage.add_argument("--fetcher", default="auto",
                       choices=["auto", "curl_cffi", "httpx", "scrapling"],
                       help="scrapling = 隐身浏览器档（需 scrapling[fetchers]）")

    pext = sub.add_parser(
        "extract",
        help="S3b：抓取并抽取正文与发布时间，看抽得干不干净",
    )
    pext.add_argument("urls", nargs="*", default=[], metavar="URL",
                      help="要抽的 URL；不给则从库里的任务取")
    pext.add_argument("--task", default=None, metavar="ID|latest",
                      help="从这个任务的文档取 URL（缺省取最近一次）")
    pext.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多抽几条")
    pext.add_argument("--show", type=int, default=400, metavar="N",
                      help="每条打印正文前 N 字，0 = 只出表格")
    pext.add_argument("--no-robots", action="store_true", help="等价 --robots-mode off")
    pext.add_argument("--robots-mode", default=None, choices=["obey", "record", "off"],
                      help="robots.txt 档位：obey|record|off（默认听配置）")
    pext.add_argument("--robots-on-failure", default=None, choices=["deny", "allow"],
                      help="robots.txt 拉不到时：deny（默认）| allow")
    pext.add_argument("--fetcher", default="auto",
                      choices=["auto", "curl_cffi", "httpx", "scrapling"],
                      help="scrapling = 隐身浏览器档（需 scrapling[fetchers]）")

    pd = sub.add_parser("db", help="数据库维护")
    pd.add_argument("action", choices=["init", "clear"])
    pd.add_argument("--yes", action="store_true",
                    help="确认执行破坏性操作（仅 clear 需要；不加只预览不删）")

    args = p.parse_args(argv)

    # 各层的 logger.warning 默认会由 lastResort 直接甩到 stderr，格式难看且和
    # 主输出混在一起。这里统一成以 "·" 开头的一行，一眼能区分"层内告警"和"结果"。
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="  · %(message)s",
        stream=sys.stderr,
        force=True,
    )

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "channels":
            return cmd_channels(cfg)
        if args.cmd == "run":
            robots_mode = "off" if args.no_robots else args.robots_mode
            return cmd_run(
                cfg, args.keyword, args.mode, args.timespan, args.dry,
                db_path=args.db, max_docs=args.max_docs, expand=args.expand,
                require=args.require, no_filter=args.no_filter,
                out_dir=args.out, want_report=not args.no_report,
                fetch_body=False if args.no_body else None,
                stealth=args.stealth,
                person=(parse_person(args.keyword, args.org, args.field, args.role)
                        if args.person else None),
                robots_mode=robots_mode,
                robots_on_failure=args.robots_on_failure,
            )
        if args.cmd == "db":
            return cmd_db(cfg, args.action, args.db, assume_yes=args.yes)
        if args.cmd == "probe":
            timeout = args.timeout or int(cfg.defaults.get("timeout", 20))
            return cmd_probe(cfg, args.url, args.header, timeout,
                             args.preview, args.save, args.fetcher)
        if args.cmd == "discover":
            return cmd_discover(cfg, args.keyword, args.channel, args.pages,
                                args.offline, args.json, args.limit,
                                args.expand, args.require, args.no_filter)
        if args.cmd == "page":
            return cmd_page(
                cfg, args.urls, db_path=args.db, task=args.task,
                limit=args.limit, show=args.show, no_robots=args.no_robots,
                robots_mode=args.robots_mode,
                robots_on_failure=args.robots_on_failure,
                per_domain=args.per_domain, which=args.fetcher,
            )
        if args.cmd == "extract":
            return cmd_extract(
                cfg, args.urls, db_path=args.db, task=args.task,
                limit=args.limit, show=args.show, no_robots=args.no_robots,
                robots_mode=args.robots_mode,
                robots_on_failure=args.robots_on_failure,
                which=args.fetcher,
            )
        if args.cmd == "report":
            return cmd_report(cfg, args.task_id, args.out, args.db)
        if args.cmd == "serve":
            return cmd_serve(cfg, args.host, args.port, args.out,
                             args.no_browser, db_path=args.db)
    except NotImplementedError as e:
        print(f"未实现: {e}", file=sys.stderr)
        return 3

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
