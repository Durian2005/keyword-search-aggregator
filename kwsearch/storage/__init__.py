"""存储层：只认 models 里的 dataclass，不认 HTTP。

两个部分：
    init_db()     建库/补表/补列（纯 DDL，幂等）
    Repository    全部读写操作，一个任务一个实例

⚠️ 唯一约束一律带 task_id（见 schema.sql 顶部说明）。

为什么 Repository 不做连接池/线程局部：M1 全程串行 + 单连接，天生没有并发问题。
到 M2 上并发时必须改成"每次操作新建连接"，那时候这块要重写 —— 所以现在不预留
半成品抽象，免得让人误以为它能扛并发。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

from kwsearch.aggregate import from_signed64, host_of, to_signed64
from kwsearch.models import Candidate, Doc, Mention, utcnow_iso

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 增量迁移：只支持"加列"。
# CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，所以给老库补列必须显式 ALTER。
# 需要改类型/删列/改主键时，说明是破坏性变更 —— 那时候删库重建，别硬迁移。
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "query": {"query_text": "TEXT", "new_count": "INTEGER NOT NULL DEFAULT 0"},
    # S5 之前建的库没有这两列。cluster_member 由 CREATE TABLE IF NOT EXISTS 自动补。
    "cluster": {"max_hamming": "INTEGER", "method": "TEXT"},
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接。row_factory 设成 Row，取字段比下标可读得多。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """给已存在的表补缺失列，返回补了哪些（便于 CLI 打印"迁移了什么"）。"""
    added: list[str] = []
    for table, columns in _ADDITIVE_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:          # 表还不存在，交给 schema.sql 建
            continue
        for col, ddl in columns.items():
            if col in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            added.append(f"{table}.{col}")
    return added


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """建库/补表/补列。幂等 —— 可反复调用。"""
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    added = _ensure_columns(conn)
    conn.commit()
    if added:
        logger.info("已给老库补列: %s", ", ".join(added))
    return conn


class Repository:
    """一个任务的全部读写。**不是线程安全的**，M1 串行执行。"""

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ task

    def start_task(
        self,
        keyword: str,
        mode: str = "quick",
        timespan: str | None = None,
        *,
        task_id: str | None = None,
    ) -> str:
        """开一个任务，返回 task_id。

        为什么不是自增主键：报告要做成"关键词/时间"可读的目录名，
        12 位 hex 既够唯一，也能直接当文件名。
        """
        tid = task_id or uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO task(task_id, keyword, mode, timespan, started_at, status)"
            " VALUES (?, ?, ?, ?, ?, 'running')",
            (tid, keyword, mode, timespan, utcnow_iso()),
        )
        self.conn.commit()
        return tid

    def finish_task(
        self,
        task_id: str,
        status: str = "ok",
        truncated: bool = False,
        stats: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE task SET finished_at=?, status=?, truncated=?, stats_json=?"
            " WHERE task_id=?",
            (utcnow_iso(), status, 1 if truncated else 0,
             json.dumps(stats, ensure_ascii=False) if stats else None, task_id),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- query

    def log_query(
        self,
        task_id: str,
        channel: str,
        seq: int,
        url: str,
        *,
        query_text: str | None = None,
        http_status: int | None = None,
        raw_count: int | None = None,
        got: int = 0,
        new: int = 0,
        blocked: bool = False,
        block_kind: str | None = None,
        elapsed_ms: int = 0,
    ) -> None:
        """记一次通道调用。**这是报告"覆盖率自评"的唯一数据源**，不能省。

        用 ON CONFLICT 而不是 INSERT OR IGNORE：重试同一个 seq 时要覆盖旧记录，
        而 INSERT OR IGNORE 会静默吞掉（前作踩过的坑 9）。
        """
        self.conn.execute(
            "INSERT INTO query(task_id, channel, seq, url, query_text, http_status,"
            " raw_count, got_count, new_count, blocked, block_kind, elapsed_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(task_id, channel, seq) DO UPDATE SET"
            "   url=excluded.url, query_text=excluded.query_text,"
            "   http_status=excluded.http_status, raw_count=excluded.raw_count,"
            "   got_count=excluded.got_count, new_count=excluded.new_count,"
            "   blocked=excluded.blocked, block_kind=excluded.block_kind,"
            "   elapsed_ms=excluded.elapsed_ms, created_at=excluded.created_at",
            (task_id, channel, seq, url, query_text, http_status, raw_count,
             got, new, 1 if blocked else 0, block_kind, elapsed_ms, utcnow_iso()),
        )

    # ------------------------------------------------------------------- doc

    def upsert_doc(self, doc: Doc) -> bool:
        """写入/更新一篇文档。返回 True = 库里原本没有（本任务的新发现）。

        "新"的语义很重要：报告里的"共 N 条"必须是本任务发现的，
        而不是全库累积 —— 否则同一个词跑三次，数字会翻三倍，看起来像热度。
        """
        url_canon = doc.url_canon
        if not url_canon:
            raise ValueError("upsert_doc 需要 url_canon")

        old = self.conn.execute(
            "SELECT title, summary, published_at, time_source, channel, body_chars"
            " FROM doc WHERE url_canon=?",
            (url_canon,),
        ).fetchone()
        now = utcnow_iso()
        # SimHash 是无符号 64 位，SQLite 只认 int64，必须按补码转存
        sim = to_signed64(doc.simhash) if doc.simhash else None

        if old is None:
            self.conn.execute(
                "INSERT INTO doc(url_canon, url_raw, domain, title, summary, published_at,"
                " time_source, channel, http_status, content_type, body_chars, simhash,"
                " cluster_id, body_ref, first_seen, last_seen, extra_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    url_canon, doc.url_raw or url_canon,
                    doc.domain or host_of(doc.url_raw or url_canon),
                    doc.title, doc.summary, doc.published_at, doc.time_source,
                    doc.channel, doc.http_status, doc.content_type, doc.body_chars,
                    sim, doc.cluster_id, doc.body_ref,
                    doc.fetched_at, now,
                    json.dumps(doc.extra, ensure_ascii=False) if doc.extra else None,
                ),
            )
            return True

        # 已存在：只补空字段，不覆盖已有内容。
        # 覆盖是有害的 —— 后到的通道（如 B站）标题带 "#标签"，会污染 SERP 的干净标题。
        title = old["title"] or doc.title
        # 摘要有一条例外：**从"没有正文"变成"有正文"时，允许用正文摘要替换**。
        # 正文摘要就是文章自己的开头，比搜索引擎替我们写的那句话更接近原文，
        # 这正是 S3d 的收益所在 —— 不放行，重跑一次等于什么都没发生。
        # 但不能反过来：已有正文的条目再被 SERP 摘要覆盖回去。
        got_body = doc.body_chars > 0 and int(old["body_chars"] or 0) <= 0
        summary = doc.summary if (got_body and doc.summary) else (old["summary"] or doc.summary)
        # 时间只允许"从无到有"，且必须连 time_source 一起接受。
        # 已经有了就整对保留：拿新值的 published_at 配旧的 time_source（或反之）
        # 会让报告把"兜底时间"当真实发布时间展示，这正是坑。
        if not old["published_at"] and doc.published_at:
            published_at, time_source = doc.published_at, doc.time_source
        else:
            published_at, time_source = old["published_at"], old["time_source"]
        self.conn.execute(
            "UPDATE doc SET last_seen=?, title=?, summary=?, published_at=?, time_source=?,"
            " http_status=COALESCE(http_status, ?), content_type=COALESCE(content_type, ?),"
            " body_chars=MAX(body_chars, ?), simhash=COALESCE(simhash, ?)"
            " WHERE url_canon=?",
            (now, title, summary, published_at, time_source,
             doc.http_status, doc.content_type, doc.body_chars, sim, url_canon),
        )
        return False

    def bodies_done(
        self, url_canons: Iterable[str] | None, *, need_simhash_above: int = 0,
    ) -> set[str]:
        """这些 URL 里，库里已经有可信正文的那些。

        用途有两个：
        1) **重跑同一个关键词时不重复打站点**。正文抓取的请求量是"条数"级的，
           同一个站被来回打三遍，礼貌上也说不过去。body_chars=0 的（抓过但抽不出 /
           从没抓过）不算"已有"，该重试还得重试。
        2) `need_simhash_above > 0` 时，正文够长却没有指纹的行**也算"没做完"**。
           S5 之前抓下来的正文没有 simhash，不补这一步它们会永远进不了聚簇 ——
           表现是"重跑一次，转载归并显示 0 条"，而报告看起来一切正常。
           代价是老数据各重抓一次，之后 simhash 落库就不再重抓。
        """
        keys = [u for u in dict.fromkeys(url_canons or ()) if u]
        if not keys:
            return set()
        marks = ",".join("?" * len(keys))
        sql = (f"SELECT url_canon FROM doc WHERE body_chars > 0 AND url_canon IN ({marks})")
        params: list[Any] = list(keys)
        if need_simhash_above > 0:
            sql += " AND (simhash IS NOT NULL OR body_chars < ?)"
            params.append(need_simhash_above)
        return {str(r[0]) for r in self.conn.execute(sql, params)}

    # -------------------------------------------------------------- cluster

    def doc_fingerprints(self, url_canons: Iterable[str] | None) -> list[dict[str, Any]]:
        """取这批 URL 的正文指纹与聚类需要的字段。

        为什么从库里读而不是直接用内存里的 `Enriched`：重跑时命中的是
        "库里早就抓过正文"的条目，那次没有发请求、内存里没有正文，
        但它们的指纹上次已经算过并落库了。两条来源合成一份，才不会有条目凭空消失。

        ⚠️ 返回的 simhash 已经还原成**无符号**（库里存的是补码负数），
        直接拿去算汉明距离即可，不要再自己取反。
        """
        keys = [u for u in dict.fromkeys(url_canons or ()) if u]
        if not keys:
            return []
        out: list[dict[str, Any]] = []
        # SQLite 的变量上限是 999，分批查（max_docs 默认 1000，正好会踩线）
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                "SELECT url_canon, simhash, body_chars, title, domain, published_at"
                f" FROM doc WHERE url_canon IN ({marks})", chunk,
            ):
                row = dict(r)
                if row.get("simhash") is not None:
                    row["simhash"] = from_signed64(row["simhash"])
                out.append(row)
        return out

    def replace_clusters(
        self, task_id: str, clusters: Iterable[Any], *, method: str = "simhash",
    ) -> int:
        """重写一个任务的簇。返回写入了多少个簇。

        先删后插而不是 upsert：簇号是每任务重新编的（按条数降序），
        重跑之后"3 号簇"很可能是另一批文章 —— 增量更新只会留下上一轮的幽灵成员。
        """
        self.conn.execute("DELETE FROM cluster_member WHERE task_id=?", (task_id,))
        self.conn.execute("DELETE FROM cluster WHERE task_id=?", (task_id,))
        now = utcnow_iso()
        n = 0
        for c in clusters:
            members = list(getattr(c, "members", []) or [])
            if not members:
                continue
            dates = [m.published_at for m in members if m.published_at]
            cur = self.conn.execute(
                "INSERT INTO cluster(task_id, rep_url, member_count, max_hamming, method,"
                " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, c.rep_url, len(members), int(getattr(c, "max_hamming", 0) or 0),
                 method, min(dates) if dates else now, max(dates) if dates else now),
            )
            cid = int(cur.lastrowid or 0)
            self.conn.executemany(
                "INSERT OR IGNORE INTO cluster_member(task_id, cluster_id, url_canon,"
                " hamming, is_rep) VALUES (?,?,?,?,?)",
                [(task_id, cid, m.url_canon, int(m.hamming or 0),
                  1 if m.url_canon == c.rep_url else 0) for m in members],
            )
            n += 1
        self.conn.commit()
        return n

    # --------------------------------------------------------- 人名模式卡片

    def replace_person_cards(self, task_id: str, cards: Iterable[Any]) -> int:
        """重写一个任务的候选人卡片，返回写入张数。

        与簇一样先删后插：卡片是派生结果，重跑后"某某大学"那张卡的内容
        可能完全不同，增量更新只会留下上一轮的幽灵证据。
        """
        self.conn.execute("DELETE FROM person_card WHERE task_id=?", (task_id,))
        n = 0
        for c in cards:
            evidence = [
                {"title": e.title, "url": e.url, "matched": list(e.matched)}
                for e in getattr(c, "evidence", []) or []
            ]
            self.conn.execute(
                "INSERT INTO person_card(task_id, label, tier, score, org_hits,"
                " weak_hits, docs, negatives, evidence_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, c.label, c.tier, int(c.score or 0), int(c.org_hits or 0),
                 int(c.weak_hits or 0), int(c.docs or 0),
                 ",".join(getattr(c, "negatives", ()) or ()) or None,
                 json.dumps(evidence, ensure_ascii=False)),
            )
            n += 1
        self.conn.commit()
        return n

    def upsert_mention(self, m: Mention) -> None:
        """同一任务内一篇文档只留一行，分数取最大值（多通道发现同一页时）。"""
        self.conn.execute(
            "INSERT INTO mention(task_id, url_canon, hit_title, hit_body, exact_phrase,"
            " bm25, score, created_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(task_id, url_canon) DO UPDATE SET"
            "   hit_title=MAX(hit_title, excluded.hit_title),"
            "   hit_body=MAX(hit_body, excluded.hit_body),"
            "   exact_phrase=MAX(exact_phrase, excluded.exact_phrase),"
            "   score=MAX(score, excluded.score)",
            (m.task_id, m.url_canon, m.hit_title, m.hit_body, 1 if m.exact_phrase else 0,
             m.bm25, m.score, utcnow_iso()),
        )

    # ----------------------------------------------------------------- queue

    def enqueue(self, candidates: Iterable[Candidate], task_id: str,
                channel: str | None = None, depth: int = 0,
                priority: int = 0) -> int:
        """把候选放进待抓队列，返回真正新增的条数。

        跨通道统一排队：Scrapling 的 crawldir 只管它自己那份，
        发现层从三个通道拿到的候选必须由我们自己排。
        """
        now = utcnow_iso()
        before = self.conn.total_changes
        for c in candidates:
            self.conn.execute(
                "INSERT OR IGNORE INTO queue(url_raw, url_canon, task_id, channel, depth,"
                " priority, state, created_at, updated_at) VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
                (c.url_raw, c.url_canon, task_id, channel or c.channel, depth,
                 priority, now, now),
            )
        return self.conn.total_changes - before

    def pick_batch(self, task_id: str, n: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM queue WHERE task_id=? AND state='pending'"
            " ORDER BY priority DESC, depth, created_at LIMIT ?",
            (task_id, n),
        ))

    def mark_queue(self, url_raw: str, state: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE queue SET state=?, attempts=attempts+CASE WHEN ?='failed' THEN 1 ELSE 0 END,"
            " last_error=?, updated_at=? WHERE url_raw=?",
            (state, state, error, utcnow_iso(), url_raw),
        )

    # ---------------------------------------------------------------- robots

    def get_robots(self, domain: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM robots WHERE domain=?", (domain,)).fetchone()

    def put_robots(
        self,
        domain: str,
        *,
        status: int | None = None,
        disallow: str = "",
        crawl_delay: float | None = None,
        allowed: bool = True,
        note: str = "",
    ) -> None:
        """留档 robots 探测结论。这是合规证据，报告里要能逐域列出来。"""
        self.conn.execute(
            "INSERT INTO robots(domain, checked_at, status, disallow, crawl_delay, allowed, note)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(domain) DO UPDATE SET checked_at=excluded.checked_at,"
            "   status=excluded.status, disallow=excluded.disallow,"
            "   crawl_delay=excluded.crawl_delay, allowed=excluded.allowed, note=excluded.note",
            (domain, utcnow_iso(), status, disallow or None, crawl_delay,
             1 if allowed else 0, note or None),
        )

    # ----------------------------------------------------------------- alerts

    def add_alert(self, task_id: str | None, channel: str | None,
                  kind: str, detail: str) -> None:
        """记一条告警。**任何"少拿到数据"都必须走这里**，不许静默吞。"""
        self.conn.execute(
            "INSERT INTO alert(task_id, channel, kind, detail, created_at) VALUES (?,?,?,?,?)",
            (task_id, channel, kind, detail, utcnow_iso()),
        )

    # ------------------------------------------------------------------ stats

    def stats(self, task_id: str, *, doc_limit: int = 500) -> dict[str, Any]:
        """报告层的唯一数据入口。一次查全，不让报告层自己拼 SQL。

        doc_limit 是渲染上限，不是数据上限 —— 排名 500 以后的文档对报告没有信息量，
        但库里一条不少（`--json` 或直接查库都能拿到）。
        """
        task = self.conn.execute(
            "SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(f"没有这个任务: {task_id}")

        channels = [dict(r) for r in self.conn.execute(
            "SELECT channel, COUNT(*) AS queries,"
            "       COALESCE(SUM(got_count),0) AS got,"
            "       COALESCE(SUM(new_count),0) AS new,"
            "       COALESCE(SUM(blocked),0) AS blocked,"
            "       COALESCE(SUM(elapsed_ms),0) AS elapsed_ms,"
            "       GROUP_CONCAT(DISTINCT block_kind) AS block_kinds,"
            "       MIN(created_at) AS first_at, MAX(created_at) AS last_at"
            " FROM query WHERE task_id=? GROUP BY channel"
            " ORDER BY got DESC, channel", (task_id,))]

        docs = [dict(r) for r in self.conn.execute(
            "SELECT d.url_canon, d.url_raw, d.domain, d.title, d.summary,"
            "       d.published_at, d.time_source, d.channel, d.body_chars,"
            "       m.score, m.hit_title, m.hit_body, m.exact_phrase"
            " FROM mention m JOIN doc d ON d.url_canon = m.url_canon"
            " WHERE m.task_id=?"
            " ORDER BY m.score DESC, d.published_at DESC, d.domain"
            " LIMIT ?", (task_id, doc_limit))]

        domains = [dict(r) for r in self.conn.execute(
            "SELECT d.domain, COUNT(*) AS n FROM mention m"
            " JOIN doc d ON d.url_canon = m.url_canon"
            " WHERE m.task_id=? GROUP BY d.domain ORDER BY n DESC, d.domain",
            (task_id,))]

        timeline = [dict(r) for r in self.conn.execute(
            "SELECT substr(d.published_at, 1, 10) AS date, COUNT(*) AS n"
            " FROM mention m JOIN doc d ON d.url_canon = m.url_canon"
            " WHERE m.task_id=? AND d.published_at IS NOT NULL"
            "   AND length(d.published_at) >= 10"
            " GROUP BY date ORDER BY date", (task_id,))]

        time_sources = [dict(r) for r in self.conn.execute(
            "SELECT d.time_source AS source, COUNT(*) AS n FROM mention m"
            " JOIN doc d ON d.url_canon = m.url_canon"
            " WHERE m.task_id=? GROUP BY source ORDER BY n DESC", (task_id,))]

        alerts = [dict(r) for r in self.conn.execute(
            "SELECT id, channel, kind, detail, created_at FROM alert"
            " WHERE task_id=? ORDER BY id", (task_id,))]

        queries = [dict(r) for r in self.conn.execute(
            "SELECT channel, seq, query_text, got_count, new_count, blocked, block_kind,"
            "       elapsed_ms FROM query WHERE task_id=? ORDER BY channel, seq",
            (task_id,))]

        clusters = [dict(r) for r in self.conn.execute(
            "SELECT c.cluster_id, c.rep_url, c.member_count, c.max_hamming, c.method,"
            "       d.title AS rep_title, d.domain AS rep_domain,"
            "       d.published_at AS rep_published"
            " FROM cluster c LEFT JOIN doc d ON d.url_canon = c.rep_url"
            " WHERE c.task_id=?"
            " ORDER BY c.member_count DESC, c.cluster_id", (task_id,))]

        members: dict[int, list[dict[str, Any]]] = {}
        for r in self.conn.execute(
            "SELECT m.cluster_id, m.url_canon, m.hamming, m.is_rep,"
            "       d.title, d.domain, d.published_at, d.body_chars"
            " FROM cluster_member m LEFT JOIN doc d ON d.url_canon = m.url_canon"
            " WHERE m.task_id=?"
            " ORDER BY m.cluster_id, m.hamming, m.url_canon", (task_id,),
        ):
            members.setdefault(int(r["cluster_id"]), []).append(dict(r))
        for c in clusters:
            c["members"] = members.get(int(c["cluster_id"]), [])


        doc_count = self.conn.execute(
            "SELECT COUNT(*) FROM mention WHERE task_id=?", (task_id,)).fetchone()[0]
        with_time = self.conn.execute(
            "SELECT COUNT(*) FROM mention m JOIN doc d ON d.url_canon=m.url_canon"
            " WHERE m.task_id=? AND d.published_at IS NOT NULL", (task_id,)).fetchone()[0]
        new_docs = self.conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0]

        # 人名模式的候选人卡片（普通任务里为空表，报告据此决定要不要出这一块）
        person_cards = [dict(r) for r in self.conn.execute(
            "SELECT label, tier, score, org_hits, weak_hits, docs, negatives,"
            "       evidence_json FROM person_card WHERE task_id=?"
            " ORDER BY CASE tier WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1"
            "   WHEN 'suspect' THEN 2 ELSE 3 END, docs DESC, score DESC",
            (task_id,))]
        for c in person_cards:
            try:
                c["evidence"] = json.loads(c.pop("evidence_json") or "[]")
            except (ValueError, TypeError):
                c["evidence"] = []
            c["negatives"] = [x for x in (c.get("negatives") or "").split(",") if x]

        stats_json: dict[str, Any] = {}
        if task["stats_json"]:
            try:
                stats_json = json.loads(task["stats_json"])
            except (ValueError, TypeError):
                stats_json = {}

        t = dict(task)
        t["stats"] = stats_json

        # 聚簇账目：入库 N 条 → 独立内容 M 组。
        # 没跑聚簇时（clusters 为空）M 必须等于 N，否则"归并了 0 条"会被误读成
        # "这些文档都不相似"，而真相是"这一步没执行"。
        fingerprinted = sum(int(c.get("member_count") or 0) for c in clusters)
        unfingerprinted = max(0, doc_count - fingerprinted)
        groups = len(clusters) + unfingerprinted

        return {
            "task": t,
            "totals": {
                "docs": doc_count,
                "docs_shown": len(docs),
                "domains": len(domains),
                "channels": len(channels),
                "alerts": len(alerts),
                "with_time": with_time,
                "no_time": doc_count - with_time,
                "new_docs": int(stats_json.get("docs_new", 0)),
                "db_docs": new_docs,
                "clusters": len(clusters),
                "groups": groups,
                "merged": doc_count - groups,
                "fingerprinted": fingerprinted,
                "unfingerprinted": unfingerprinted,
            },
            "channels": channels,
            "docs": docs,
            "domains": domains,
            "timeline": timeline,
            "time_sources": time_sources,
            "alerts": alerts,
            "queries": queries,
            "clusters": clusters,
            "person_cards": person_cards,
        }


def recent_tasks(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """最近的任务列表。`report` 不带参数时用它提示"该报哪一个"。"""
    return list(conn.execute(
        "SELECT task_id, keyword, mode, started_at, finished_at, status, truncated"
        " FROM task ORDER BY started_at DESC LIMIT ?", (limit,)))


__all__ = ["Repository", "connect", "init_db", "recent_tasks", "SCHEMA_PATH"]

