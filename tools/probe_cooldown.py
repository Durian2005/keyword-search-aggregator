"""冷却窗口探针：验证「通道被风控之后多久恢复」。

和 `python -m kwsearch run` 的根本区别：
    - 只发 N 个请求（默认 1），**不建任务、不解析结果、不写 doc**
    - 但会把结果记进 `channel_state`（今日用量 / 是否被拦）：
      探测消耗的是同一份额度，不记账会让 Phase 1 的预算算不准。
      不想要就用 `--no-db`。
    - 判定只有一件事：这一发拿到的是真实结果页，还是风控页

为什么必须复用项目自己的 fetcher 和 channels.yaml 的头部：
    只有请求姿势与真实抓取完全一致，测出来的冷却时间才对外面的任务有意义。
    随手 curl 一发测出"X 分钟恢复"，换成 curl_cffi 未必是同一个数。

观测记录写在 %TEMP%/kwsearch_probe/<channel>.jsonl —— 跨次运行累积，
    仓库里不留观测数据，也不进 git。

用法（在项目根目录执行，需要 PYTHONPATH 找到 kwsearch 包）：
    ./.venv/Scripts/python.exe tools/probe_cooldown.py                       # 打一发
    ./.venv/Scripts/python.exe tools/probe_cooldown.py --rounds 5 --interval 60
    ./.venv/Scripts/python.exe tools/probe_cooldown.py --channel bilibili_search
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # tools/ 的上一层才是项目根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kwsearch.config import load_config          # noqa: E402
from kwsearch.fetch import build_fetcher, default_headers  # noqa: E402

TZ = timezone(timedelta(hours=8))


def log_path(cid: str) -> Path:
    base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    d = base / "kwsearch_probe"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{cid}.jsonl"


def judge(cid: str, res) -> tuple[str, dict]:
    """把响应收敛成「拿到了 / 被拦了 / 空壳」三态。

    只有 ok 才算恢复；challenge / blocked 才算被拦。
    unknown / unparsed 是"我们看不懂"，不能当成被拦，否则冷却时间会被高估。
    """
    body = res.body or ""
    info: dict = {"status": res.status, "chars": len(body), "bytes": res.size}
    if cid == "baidu_html":
        info["c_container"] = body.count("c-container")
        if "wappass" in body or "百度安全验证" in body:
            info["kind"] = "challenge"
            return "challenge", info
        if "STATUS OK" in body:
            return ("ok" if info["c_container"] >= 3 else "ok_empty"), info
        return "unknown", info
    if cid == "bilibili_search":
        try:
            d = json.loads(body)
        except Exception:  # noqa: BLE001
            info["kind"] = "unknown_format"
            return "unparsed", info
        code = d.get("code")
        info["code"] = code
        if code == 0:
            info["items"] = len(((d.get("data") or {}).get("result")) or [])
            return "ok", info
        # -352 是风控（challenge），-412/-799 是滚动配额（rate_limit）
        info["kind"] = "challenge" if code == -352 else "rate_limit"
        return "blocked", info
    return ("ok" if info["chars"] > 2000 else "empty"), info


def main() -> int:
    ap = argparse.ArgumentParser(description="冷却窗口探针")
    ap.add_argument("--channel", default="baidu_html")
    ap.add_argument("--keyword", default="红黑树")
    ap.add_argument("--rounds", type=int, default=1, help="连打几发")
    ap.add_argument("--interval", type=float, default=0.0, help="两发之间隔几秒")
    ap.add_argument("--db", default="kwsearch.db", help="探测结果记进这个库")
    ap.add_argument("--no-db", action="store_true", help="只探测，不记账")
    a = ap.parse_args()

    cfg = load_config()
    ch = cfg.channel(a.channel)
    headers = default_headers(cfg.defaults)
    headers.update(ch.headers or {})
    fetcher = build_fetcher()
    lp = log_path(a.channel)

    # 记账（2026-10-31 Phase 0）：探测本身也是真实请求，不记的话
    # "今日用量"会少算，而用量正是 Phase 1 用来卡预算的依据。
    repo = None
    if not a.no_db:
        from kwsearch.health import classify_block
        from kwsearch.storage import Repository, init_db
        repo = Repository(init_db(a.db))

    print(f"通道 {a.channel} · 词「{a.keyword}」· {a.rounds} 发 · 间隔 {a.interval:g}s")
    print(f"记录 {lp}" + ("" if repo else "（不记账）") + "\n")

    for i in range(1, a.rounds + 1):
        url = ch.build_url(a.keyword, offset=1)
        t0 = time.time()
        try:
            res = fetcher.fetch(url, headers, timeout=int(cfg.defaults.get("timeout", 20)))
        except Exception as e:  # noqa: BLE001 —— 网络异常也要留痕，否则"没数据"会被误读
            print(f"[{i}] 请求异常 {type(e).__name__}: {e}")
            fetcher.close()
            return 2
        verdict, info = judge(a.channel, res)
        rec = {
            "at": datetime.now(TZ).isoformat(timespec="seconds"),
            "channel": a.channel,
            "keyword": a.keyword,
            "round": i,
            "verdict": verdict,
            "ms": int((time.time() - t0) * 1000),
            **info,
        }
        with lp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{i}] {verdict:<10} {rec['ms']:>5}ms  "
              + json.dumps(info, ensure_ascii=False))

        if repo is not None:
            repo.add_channel_usage(a.channel, 1, rec["at"])
            kind = info.get("kind")
            if kind and classify_block(kind) == "cooldown":
                repo.record_channel_block(a.channel, kind, rec["at"])
            elif verdict in ("ok", "ok_empty"):
                repo.record_channel_ok(a.channel, rec["at"])
            repo.conn.commit()

        if i < a.rounds and a.interval > 0:
            time.sleep(a.interval)

    fetcher.close()
    if repo is not None:
        repo.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
