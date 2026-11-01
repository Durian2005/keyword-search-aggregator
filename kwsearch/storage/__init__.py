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
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from kwsearch.aggregate import from_signed64, host_of, to_signed64
from kwsearch.aggregate.cluster import DEFAULT_MIN_BODY
from kwsearch.health import (
    COOLDOWN_MAX_H,
    classify_block,
    local_day_of_iso,
    parse_iso,
)
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
    # body_core 是"剥掉转载模板后的正文长度"。老库补列后为 NULL，
    # 聚类时退回 body_chars（代表照旧能选，只是享受不到"不选二手站"这条改进）。
    "doc": {"body_core": "INTEGER",
            # 跨跑批业务去重键（2026-10-24）。搜狗微信的 URL 是 token 轮换的中转链，
            # 靠 url_canon 去重完全失效 —— 详见 schema.sql 里该列的注释。
            "dedupe_key": "TEXT"},
    # P1 的候选人卡片多了三列：次要机构、活跃时间范围。老库补列后为 NULL。
    "person_card": {
        "also_orgs": "TEXT", "first_when": "TEXT", "last_when": "TEXT",
    },
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接。row_factory 设成 Row，取字段比下标可读得多。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 默认 busytimeout 是 0：别的连接（典型：交互页面服务还开着）持锁时立刻
    # 报 "database is locked"。等 5 秒再失败，本地单人工具足够宽容，
    # 又不至于把一个真死锁挂成无限等待。
    conn.execute("PRAGMA busy_timeout = 5000")
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
    _ensure_indexes(conn)
    conn.commit()
    if added:
        logger.info("已给老库补列: %s", ", ".join(added))
    return conn


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """依赖"补列之后才存在的列"的索引，统一在这里建（幂等）。

    不能写进 schema.sql：老库跑脚本时列还没补上，CREATE INDEX 会报 no such column。
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_dedupe ON doc(dedupe_key)"
        " WHERE dedupe_key IS NOT NULL"
    )


# ── 清空所有历史数据 ────────────────────────────────────────────────────────

# 删除顺序：**子表先**。schema 里唯一一条外键是 mention.url_canon → doc，
# 而连接开着 foreign_keys=ON（立即检查），先删 doc 会直接抛 FOREIGN KEY 失败。
# 以后新增带外键的表，把它挪到被引用表前面。
_WIPE_ORDER: tuple[str, ...] = (
    "mention",          # → doc
    "query", "cluster_member", "cluster", "person_card",
    "queue", "alert",
    "doc", "task",
    "robots",           # 礼貌缓存，不是用户数据，但留着会让人以为"没清干净"
)

# 清空时刻意**保留**的表（2026-10-31）。
# channel_state 记的是出口 IP 的健康度，不是任务数据：清了它，下一次任务会
# 立刻对刚封过我们的通道再打一遍，等于用用户的数据做赌注。
# 想连它一起清属于另一个动作（"--all"），语义不同，不要混进 wipe()。
KEEP_ON_WIPE: tuple[str, ...] = ("channel_state",)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """每张表现存行数。表不存在（老库 / 空库）记 0，不抛。"""
    counts: dict[str, int] = {}
    for t in _WIPE_ORDER:
        try:
            counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except sqlite3.OperationalError:
            counts[t] = 0
    return counts


class _WipeMixin:
    """清空相关操作。混在 Repository 上，保持 conn 单入口。"""

    # 必须显式写空 __slots__：只要基类没声明它，子类的 __slots__ 就完全失效
    # （实例照样带 __dict__）。Repository.__slots__ 原意是"只允许 conn 一个属性、
    # 拼错属性名立刻 AttributeError"，基类这一行缺失会让那条保证悄悄消失。
    __slots__ = ()

    conn: sqlite3.Connection

    def wipe_preview(self) -> dict[str, int]:
        """清空前先看数：每张表有多少行。**不删任何东西**，可反复调用。"""
        return table_counts(self.conn)

    def wipe(self) -> dict[str, int]:
        """清空所有历史数据，返回每张表删掉的行数。

        一个事务里做完：中途失败（磁盘满 / 库被锁）整体回滚，绝不留下"删了一半"的库 ——
        那种库比不清更危险，报告会把残缺的历史当完整历史展示。

        DELETE 之后 SQLite 不会把文件变小，也不重置 AUTOINCREMENT 记数器
        （cluster_id 会从旧最大值继续往上）。两者都补上：VACUUM 收缩文件，
        清 sqlite_sequence 让簇编号重新从 1 开始 —— 用户视角"这就是一个空库"。
        VACUUM 不能在事务里跑，所以放在提交之后；它失败（库被别的连接占着）
        不影响"数据已清"这个事实，只留个日志。
        """
        before = table_counts(self.conn)
        with self.conn:                      # 退出时提交；异常自动回滚
            for t in _WIPE_ORDER:
                self.conn.execute(f"DELETE FROM {t}")
            # sqlite_sequence 是 SQLite 内部表，只在有 AUTOINCREMENT 列时才存在
            try:
                self.conn.execute("DELETE FROM sqlite_sequence")
            except sqlite3.OperationalError:
                pass
        try:
            self.conn.execute("VACUUM")
        except sqlite3.Error as e:           # 具体异常，不是 blind except
            logger.warning("已清空数据，但 VACUUM 收缩文件失败: %s", e)
        return before


class Repository(_WipeMixin):
    """一个任务的全部读写。**不是线程安全的**，M1 串行执行。"""

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        # 顺手补列，而不是只在 init_db() 里补：`report` / `db` 这些子命令走的是
        # connect()，但它们同样会读到新列（比如 doc_fingerprints 的 body_core）。
        # 少补一次的表现是 "no such column"，而老库恰恰是最常见的情形。
        # _ensure_columns 是幂等的，表不存在时直接跳过。
        _ensure_columns(conn)
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

    def abandon_task(self, task_id: str, exc: BaseException) -> None:
        """把因异常中断的任务标成 aborted（**不是** failed）。

        为什么不用 failed：report 把 failed 渲染成「本次无结果 · 所有通道都没有产出
        可用条目」，那是"跑完了但没东西"；这里是"没跑完"。混用会让读者把一次崩溃
        读成一次限流，然后去调通道配置，调错方向。

        `AND status='running'` 是防覆盖：收尾都写完之后才抛的情况（比如最后那行
        汇总打印撞上 broken pipe）不该把一条已完成的任务改成"中止"。
        本身失败只警告 —— 树已经倒了，不能为了修剪把原始异常吞掉。
        """
        detail = f"{type(exc).__name__}: {exc}"[:500]
        try:
            self.conn.execute(
                "UPDATE task SET finished_at=?, status='aborted', truncated=0,"
                " stats_json=? WHERE task_id=? AND status='running'",
                (utcnow_iso(),
                 json.dumps({"error": detail}, ensure_ascii=False), task_id),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            logger.warning("标记任务 %s 为 aborted 失败（原始异常继续上抛）: %s",
                           task_id, e)

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

    def resolve_dedup_key(self, key: str) -> str | None:
        """按业务去重键查库里已有的 url_canon（跨跑批去重）。没有返回 None。

        只给"URL 会变"的通道用（当前只有搜狗微信的 token 中转链）：同一篇文章
        每次解析出的 URL 都不同，URL 去重完全失效、每次跑批都算新发现。
        命中说明这篇已经在库里，调用方应复用旧 url_canon，不要再插一行 doc。
        """
        if not key:
            return None
        row = self.conn.execute(
            "SELECT url_canon FROM doc WHERE dedupe_key=? LIMIT 1", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def rekey_doc(self, old_url: str, new_url: str) -> bool:
        """把一行的主键从 old_url 改成 new_url（连带 mention）。返回 True = 已迁移。

        唯一用途：搜狗中转链 → 永久 mp 链接（2026-10-25）。库里早期存的是
        `weixin.sogou.com/link?url=<token>`，几分钟后就失效；后来解链成功拿到了
        mp.weixin.qq.com 的永久地址。业务键相同说明是同一篇文章，但 url_canon
        不同 —— 不回写的话，**解链成果会被"复用旧 url"的逻辑原地吞掉**，
        历史行永远修不好，报告里还是死链。

        为什么返回 bool 而不是抛：new_url 可能已经单独存在于库里（例如先跑新版
        再跑旧版留下的），主键冲突时不该炸掉整个任务，调用方退回旧 url 即可。
        """
        if not old_url or not new_url or old_url == new_url:
            return False
        exists = self.conn.execute(
            "SELECT 1 FROM doc WHERE url_canon=? LIMIT 1", (new_url,)
        ).fetchone()
        if exists:
            return False
        try:
            # 必须延迟外键检查：mention.url_canon REFERENCES doc(url_canon)，
            # 而连接开着 PRAGMA foreign_keys=ON（立即检查）。直接 UPDATE 主键会
            # 撞 "FOREIGN KEY constraint failed" —— 改父表时子表还指着旧值。
            # defer 到 commit 时整体校验：那时父表和子表都已经是新值，一致通过。
            self.conn.execute("PRAGMA defer_foreign_keys = ON")
            self.conn.execute("UPDATE doc SET url_canon=? WHERE url_canon=?",
                              (new_url, old_url))
            # url_raw 若还指着旧的失效链接，一起换掉 —— 报告里的"原始链接"也修好
            self.conn.execute(
                "UPDATE doc SET url_raw=? WHERE url_canon=? AND url_raw=?",
                (new_url, new_url, old_url))
            self.conn.execute("UPDATE mention SET url_canon=? WHERE url_canon=?",
                              (new_url, old_url))
            self.conn.commit()          # 外键一致性在这里才真正校验
        except sqlite3.IntegrityError:      # 并发/顺序导致的偶发冲突
            self.conn.rollback()
            return False
        finally:
            self.conn.execute("PRAGMA defer_foreign_keys = OFF")
        return True

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
                " time_source, channel, http_status, content_type, body_chars, body_core,"
                " simhash, cluster_id, body_ref, dedupe_key, first_seen, last_seen, extra_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    url_canon, doc.url_raw or url_canon,
                    doc.domain or host_of(doc.url_raw or url_canon),
                    doc.title, doc.summary, doc.published_at, doc.time_source,
                    doc.channel, doc.http_status, doc.content_type, doc.body_chars,
                    # body_core 恒 > 0（只要抽出了正文）：它是 trim 之后的长度，
                    # 而 trim 剥掉一半以上就退回原文。所以这里不会写出 0，
                    # bodies_done 的 "body_core IS NOT NULL" 也就不会造成反复重抓。
                    doc.body_core or None,
                    sim, doc.cluster_id, doc.body_ref,
                    doc.dedupe_key or None,
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
            " body_chars=MAX(body_chars, ?), body_core=MAX(COALESCE(body_core, 0), ?),"
            " simhash=COALESCE(simhash, ?), dedupe_key=COALESCE(dedupe_key, ?)"
            " WHERE url_canon=?",
            (now, title, summary, published_at, time_source,
             doc.http_status, doc.content_type, doc.body_chars,
             doc.body_core or 0, sim, doc.dedupe_key or None, url_canon),
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
            # 两个"没做完"：正文够长却没有指纹（S5 之前抓的），
            # 以及有指纹但没有 body_core（代表选取改进之前抓的）。
            # 后者的代价同样是一次重抓，抓完就固化 —— 不补，代表永远按原始长度选，
            # 转载版挂着免责声明就继续冒充"原文"。
            sql += " AND (simhash IS NOT NULL OR body_chars < ?) AND body_core IS NOT NULL"
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
                "SELECT url_canon, simhash, body_chars, body_core, time_source,"
                " title, domain, published_at"
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
                {"title": e.title, "url": e.url, "matched": list(e.matched),
                 "when": getattr(e, "when", None)}
                for e in getattr(c, "evidence", []) or []
            ]
            self.conn.execute(
                "INSERT INTO person_card(task_id, label, tier, score, org_hits,"
                " weak_hits, docs, negatives, also_orgs, first_when, last_when,"
                " evidence_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, c.label, c.tier, int(c.score or 0), int(c.org_hits or 0),
                 int(c.weak_hits or 0), int(c.docs or 0),
                 ",".join(getattr(c, "negatives", ()) or ()) or None,
                 ",".join(getattr(c, "also_orgs", ()) or ()) or None,
                 getattr(c, "first_when", None), getattr(c, "last_when", None),
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

    # ------------------------------------------------------------ channel state
    # Phase 0（2026-10-31）：**只记录，不参与任何发送决策**。
    # 判定逻辑全在 kwsearch/health.py（纯函数、时钟可注入），这里只管读写一行 ——
    # 两边分层清楚：storage 不认识"冷却"这个概念，只认识"上次被拦是什么时候"。
    # 返回 dict 而非 Row：调用方自己包成 ChannelHealth，存储层不依赖判定层。

    def get_channel_state(self, channel: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM channel_state WHERE channel=?", (channel,)).fetchone()
        return dict(row) if row else None

    def all_channel_states(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM channel_state ORDER BY channel").fetchall()
        return [dict(r) for r in rows]

    def record_channel_ok(self, channel: str, when: str) -> None:
        """一次成功 → block_streak 清零。

        失败累积必须能被成功打断，否则连挂两次之后就永远回不到正常。
        当日用量**不复位**：同一任务里成功与被拦会交替出现，复位会把账搅乱。
        """
        self.conn.execute(
            "INSERT INTO channel_state(channel, last_ok_at, block_streak, day, used_today)"
            " VALUES (?,?,0,?,0)"
            " ON CONFLICT(channel) DO UPDATE SET"
            "   last_ok_at=excluded.last_ok_at, block_streak=0",
            (channel, when, local_day_of_iso(when)),
        )

    def record_channel_block(self, channel: str, kind: str | None, when: str,
                             *, counts: bool = True) -> int:
        """记一次失败，返回累计后的 streak。

        counts 决定这次失败算不算"被风控"：
          True  —— 对方明确拒绝（challenge / 403 / 412 / rate_limit），streak +1
          False —— 网络故障 / 内容降级（network / relaxed …），**只记时间和原因**，
                   streak 原地不动。这类失败等再久也不会好，把它算进冷却只会
                   让通道被永久封死；但完全不留痕又会让人以为那条通道好用。

        注意 False 时既不能 +1 也不能归零：归零等于把"之前连挂 3 次"洗白了。

        `channel_state.block_streak+1` 必须带表名限定：不带的话 SQLite 会优先
        解析成 excluded 的列，结果是每次都被写成 1，streak 永远涨不上去。
        """
        step = "channel_state.block_streak+1" if counts else "channel_state.block_streak"
        self.conn.execute(
            "INSERT INTO channel_state(channel, last_block_at, last_block_kind, block_streak)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(channel) DO UPDATE SET"
            "   last_block_at=excluded.last_block_at,"
            "   last_block_kind=excluded.last_block_kind,"
            f"   block_streak={step}",
            (channel, when, kind, 1 if counts else 0),
        )
        row = self.conn.execute(
            "SELECT block_streak FROM channel_state WHERE channel=?", (channel,)).fetchone()
        return int(row[0]) if row else (1 if counts else 0)

    def add_channel_usage(self, channel: str, n: int, when: str) -> int:
        """把 n 次请求记进当日用量，返回记账后的值。跨天第一笔从 0 起算。

        跨天判断写成 SQL 的 CASE 而不是先读后写：两条语句之间可能插进别的任务
        （交互页面还开着的时候），先读后写会丢掉中间那笔。

        ⚠️ 读取侧（ChannelHealth.used）也有一份"不是今天就算 0"的判断，
        两处口径必须一致：读取侧决定"现在还剩多少"，写入侧决定"明天从几开始"。
        """
        if n <= 0:
            return 0
        day = local_day_of_iso(when)
        self.conn.execute(
            "INSERT INTO channel_state(channel, day, used_today) VALUES (?,?,?)"
            " ON CONFLICT(channel) DO UPDATE SET"
            "   used_today=CASE WHEN channel_state.day=excluded.day"
            "                   THEN channel_state.used_today+excluded.used_today"
            "                   ELSE excluded.used_today END,"
            "   day=excluded.day",
            (channel, day, n),
        )
        row = self.conn.execute(
            "SELECT used_today FROM channel_state WHERE channel=?", (channel,)).fetchone()
        return int(row[0]) if row else n

    def backfill_channel_state(self) -> list[str]:
        """用 query 表的历史回填 channel_state，返回回填了哪些通道。

        为什么需要：channel_state 是 2026-10-31 才加的，在此之前几十次任务的
        "谁被拦过、今天打了几发"全散在 query 表里。不回填的话，Phase 0 上线当天
        每个通道都是"无记录"，等于把已有的证据扔掉重新学一遍。

        口径（和 Phase 0 之后的实时记账保持同一套）：
          - **按任务聚合、不按页**：一个任务翻了 3 页全被拦，那是"一次事件"，
            不是三次。冷却按事件计，不是按请求计。
          - streak = 从最近的任务往前数，连续多少个任务里出现过被拦。
          - last_ok_at = 最后一次"有成功调用"的任务时间。
          - used_today 只统计今天的调用；跨天判断在读取侧还会再校验一次。

        只补空白、不覆盖已有值 —— 实时记账（任务 / 探针）写进去的永远比历史汇总新；
        但两边的"最后一次"各取更晚的那个，免得历史里更早的记录被当成不存在。
        可反复执行，结果幂等。
        """
        # 每通道 × 每任务的**最后一次调用**。
        # 不按 created_at 取最后一条：同一任务的多条 query 是在通道结束时批量写入的，
        # 时间戳会落在同一秒，按时间排会把"前两页成功、第三页被拦"排成"先拦后成功"。
        # seq 是通道内的调用序号（翻页 / 变体都在递增），才是可靠顺序。
        rows = self.conn.execute(
            "SELECT q.channel, q.created_at AS at, q.blocked AS blocked,"
            "       q.block_kind AS kind"
            " FROM query q"
            " JOIN task t ON t.task_id = q.task_id"
            " JOIN (SELECT channel, task_id, MAX(seq) AS mx FROM query"
            "       GROUP BY channel, task_id) m"
            "   ON q.channel = m.channel AND q.task_id = m.task_id AND q.seq = m.mx"
            " ORDER BY q.channel, t.started_at DESC").fetchall()

        ok_at = {r["channel"]: r["at"] for r in self.conn.execute(
            "SELECT channel, MAX(created_at) AS at FROM query"
            " WHERE blocked=0 GROUP BY channel")}

        agg: dict[str, dict[str, Any]] = {}
        for r in rows:
            ch = r["channel"]
            d = agg.setdefault(ch, {"streak": 0, "closed": False,
                                    "last_block_at": None, "kind": None,
                                    "last_ok_at": ok_at.get(ch)})
            if d["closed"]:
                continue
            if not r["blocked"]:
                d["closed"] = True                   # 以成功收尾的任务 = 连续段到此为止
                continue
            # 不算被拦的失败（network / relaxed / boilerplate）不参与连续计数，
            # 但**仍然记下它是何时、因为什么失败**：gdelt 一直 network 这件事
            # 本身就是有效信息，丢掉它用户会以为那条通道好用。
            # 安全前提：streak 保持 0，health.cooling() 见 streak<=0 直接判不冷却。
            if classify_block(r["kind"]) != "cooldown":
                if d["last_block_at"] is None:
                    d["last_block_at"] = r["at"]
                    d["kind"] = r["kind"]
                d["closed"] = True
                continue
            # 跨天重置：两次被拦间隔超过冷却上限，说明中间已经自然恢复过了。
            # 少了这一条，B站会算成"连续 20 次"（10-23 到 10-28 每天挂一次），
            # 冷却直接封顶，而它其实每天都恢复过。
            if d["streak"] and d["last_block_at"]:
                prev, seg = parse_iso(d["last_block_at"]), parse_iso(r["at"])
                if prev and seg and (prev - seg) > timedelta(hours=COOLDOWN_MAX_H):
                    d["closed"] = True
                    continue
            d["streak"] += 1
            if d["last_block_at"] is None:       # 最近的那次
                d["last_block_at"] = r["at"]
                d["kind"] = r["kind"]

        today = local_day_of_iso(utcnow_iso())
        used = {r["channel"]: int(r["n"]) for r in self.conn.execute(
            "SELECT channel, COUNT(*) AS n FROM query"
            " WHERE substr(created_at, 1, 10)=? GROUP BY channel", (today,))}

        def _later(a: str | None, b: str | None) -> str | None:
            """两个 ISO 时刻里较晚的那个。解析不了就信现有的 a。"""
            if not a:
                return b
            if not b:
                return a
            da, db_ = parse_iso(a), parse_iso(b)
            if da and db_:
                return a if da >= db_ else b
            return a

        filled: list[str] = []
        for ch, d in agg.items():
            # 合并而不是覆盖：实时记账（任务 / 探针）写进去的比历史汇总新，
            # 但历史里有它没有的更早记录。两边各取各的"更晚一次"。
            cur = self.get_channel_state(ch) or {}
            ok_at = _later(cur.get("last_ok_at"), d["last_ok_at"])
            blk_at = _later(cur.get("last_block_at"), d["last_block_at"])
            kind = cur.get("last_block_kind") or d["kind"]

            # streak 用"谁更新听谁的"，不能取 max：
            # 库里的旧值可能是上一次用错口径算出来的（比如按任务数累计成 20），
            # 取 max 会让错误值永远盖住正确值。只有库里记的"最后一次被拦"
            # 确实比回填算出来的晚，才说明那是我没见过的实时事件。
            cur_blk = parse_iso(cur.get("last_block_at"))
            new_blk = parse_iso(blk_at)
            fresh = bool(cur_blk) and (new_blk is None or cur_blk > new_blk)
            streak = int(cur.get("block_streak") or 0) if fresh else int(d["streak"])

            # 成功发生在最后一次被拦**之后** → 连挂已经断了，streak 必须归零。
            # 必须是严格晚于：同一任务里"前两页成功、第三页被拦"的两条记录时间戳
            # 会落在同一秒（批量写入），用 >= 会把这种情况误判成"成功在后"。
            if ok_at and blk_at:
                o, b = parse_iso(ok_at), parse_iso(blk_at)
                if o and b and o > b:
                    streak = 0

            self.conn.execute(
                "INSERT INTO channel_state(channel, last_ok_at, last_block_at,"
                " last_block_kind, block_streak, day, used_today)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(channel) DO UPDATE SET"
                "   last_ok_at=excluded.last_ok_at,"
                "   last_block_at=excluded.last_block_at,"
                "   last_block_kind=excluded.last_block_kind,"
                "   block_streak=excluded.block_streak",
                # day / used_today 刻意不更新：当日用量由 add_channel_usage 维护，
                # 回填算的是"旧账"，覆盖它会让今天的预算凭空变化。
                (ch, ok_at, blk_at, kind, streak, today, used.get(ch, 0)),
            )
            filled.append(ch)
        return sorted(filled)

    # ------------------------------------------------------------------ stats

    def stats(
        self, task_id: str, *, doc_limit: int = 500,
        min_body_chars: int = DEFAULT_MIN_BODY,
    ) -> dict[str, Any]:
        """报告层的唯一数据入口。一次查全，不让报告层自己拼 SQL。

        doc_limit 是渲染上限，不是数据上限 —— 排名 500 以后的文档对报告没有信息量，
        但库里一条不少（`--json` 或直接查库都能拿到）。

        min_body_chars 只用来把"没参与归并"的条目分清楚：
        正文过短的属于"本来就不该参与"，缺指纹的属于"重跑能补上"。
        两者原来合称"无指纹未判"，用户既不知道要不要处理，也不知道处理了有没有用。
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
        # 变量名别叫 new_docs：返回里 "new_docs" 是**本任务新发现**（取自 stats_json），
        # 而这个数是**全库累计**。两者曾经共用一个名字，读代码时极易张冠李戴。
        db_total = self.conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0]

        # 人名模式的候选人卡片（普通任务里为空表，报告据此决定要不要出这一块）
        person_cards = [dict(r) for r in self.conn.execute(
            "SELECT label, tier, score, org_hits, weak_hits, docs, negatives,"
            "       also_orgs, first_when, last_when,"
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
            c["also_orgs"] = [x for x in (c.get("also_orgs") or "").split(",") if x]

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

        # 把"没参与归并"的三种情形分开：无正文 / 有正文但太短 / 有正文缺指纹。
        # 只有第三种是"重跑能修好"的，另外两种要么没抓到、要么本来就没区分度。
        no_body = short_body = pending = 0
        for r in self.conn.execute(
            "SELECT d.body_chars, d.simhash FROM mention m"
            " JOIN doc d ON d.url_canon = m.url_canon WHERE m.task_id=?", (task_id,),
        ):
            chars = int(r["body_chars"] or 0)
            if chars <= 0:
                no_body += 1
            elif r["simhash"] is None:
                if chars < min_body_chars:
                    short_body += 1
                else:
                    pending += 1

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
                "db_docs": db_total,
                "clusters": len(clusters),
                "groups": groups,
                "merged": doc_count - groups,
                "fingerprinted": fingerprinted,
                "unfingerprinted": unfingerprinted,
                "no_body": no_body,
                "short_body": short_body,
                "pending_fingerprint": pending,
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
            # 通道健康度（跨任务状态，不是本任务的统计）。报告要用它解释
            # "为什么某个通道这次一条都没出" —— 是没结果，还是被主动跳过了。
            "channel_health": [dict(r) for r in self.conn.execute(
                "SELECT * FROM channel_state ORDER BY channel")],
        }


def recent_tasks(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """最近的任务列表。`report` 不带参数时用它提示"该报哪一个"。"""
    return list(conn.execute(
        "SELECT task_id, keyword, mode, started_at, finished_at, status, truncated"
        " FROM task ORDER BY started_at DESC LIMIT ?", (limit,)))


__all__ = ["KEEP_ON_WIPE", "Repository", "connect", "init_db", "recent_tasks",
           "table_counts", "SCHEMA_PATH"]
