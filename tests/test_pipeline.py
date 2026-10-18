"""编排层端到端测试：用假通道跑完整链路，不联网。

为什么不用真通道：S4 要验的是"候选 → 去重 → 过滤 → 脱敏 → 打分 → 入库"
这条确定性链路。真通道会引入风控、限流、当天搜索结果变化，测出来的失败
分不清是代码坏了还是网络变了。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch import discover  # noqa: E402
from kwsearch.config import load_config  # noqa: E402
from kwsearch.models import Candidate, ChannelResult  # noqa: E402
from kwsearch.person import parse_person  # noqa: E402
from kwsearch.pipeline import run_task  # noqa: E402
from kwsearch.storage import Repository, connect  # noqa: E402

# --------------------------------------------------------------- 假通道

# (url, 标题, 摘要, 域名)
_SERP_ITEMS = [
    ("https://a.example.com/1", "红黑树 原理详解", "红黑树是一种自平衡二叉查找树", "a.example.com"),
    ("https://b.example.com/2", "红黑树实现（联系 13812345678）", "代码示例与注释", "b.example.com"),
    ("https://b.example.com/3", "红黑树 插入", "插入操作的旋转细节", "b.example.com"),
    ("https://b.example.com/4", "红黑树 删除", "删除操作的四种情况", "b.example.com"),
    ("https://c.example.com/5", "数据结构总览", "与关键词无关的页面", "c.example.com"),
]


def _mk(items, channel: str, keyword: str) -> ChannelResult:
    cands = [
        Candidate(
            url_raw=url, title=title, snippet=snippet, channel=channel, rank=i + 1,
            # 隔一条给一个日期，用来验证 time_source 分流
            published_hint="2026-10-01" if i % 2 == 0 else None,
            extra={"domain": domain, "hint_kind": "absolute" if i % 2 == 0 else ""},
        )
        for i, (url, title, snippet, domain) in enumerate(items)
    ]
    return ChannelResult(channel=channel, query=keyword, candidates=cands,
                         raw_count=len(cands))


def fake_serp(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """基词返回 5 条；变体只返回前 2 条（模拟"变体拿不到新东西"）。"""
    items = _SERP_ITEMS if keyword.strip() == "红黑树" else _SERP_ITEMS[:2]
    return _mk(items, "fake_serp", keyword)


def fake_blocked(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    return ChannelResult(channel="fake_blocked", query=keyword, candidates=[],
                         blocked=True, block_kind="rate_limit", detail="fake 412")


def fake_paged(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """每页都返回同一批 —— 用来验证"翻页退化自动停"。"""
    return _mk(_SERP_ITEMS[:3], "fake_paged", keyword)


def fake_zero(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """站点检索的兜底推荐：有一条标题摘要都不含关键词。"""
    return _mk([
        ("https://z.example.com/1", "红黑树 教程", "红黑树入门", "z.example.com"),
        ("https://z.example.com/2", "Java 面试题精选", "与算法无关的内容", "z.example.com"),
    ], "fake_zero", keyword)


def fake_compound(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """复合词场景：标题只出现词元（降准），不出现整串（央行降准）。

    2026-10-18 实测回归：零命中丢弃若用 relate 的整串口径，
    serp 过滤（needles 词元口径）刚放行的条目会被整批杀掉。
    """
    return _mk([
        ("https://c.example.com/1", "央行宣布降准0.5个百分点", "货币政策工具", "c.example.com"),
        ("https://c.example.com/2", "今日股市收盘行情", "与关键词无关", "c.example.com"),
    ], "fake_compound", keyword)


discover._REGISTRY["fake_serp"] = fake_serp
discover._REGISTRY["fake_zero"] = fake_zero
discover._REGISTRY["fake_blocked"] = fake_blocked
discover._REGISTRY["fake_paged"] = fake_paged
discover._REGISTRY["fake_compound"] = fake_compound


def fake_person(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """人名模式：三条命中不同锚点的结果 + 一条同名不同人（歌手）。"""
    return _mk([
        ("https://u1.example.edu.cn/1", "张军-甲大学计算机学院 教授 网络安全方向",
         "张军，甲大学教授，研究方向为网络安全", "u1.example.edu.cn"),
        ("https://u2.example.edu.cn/2", "甲大学计算机学院 师资队伍",
         "张军 教授 网络安全 博士生导师", "u2.example.edu.cn"),
        ("https://ent.example.com/3", "张军（歌手）_百科",
         "流行歌手，代表作……", "ent.example.com"),
    ], "fake_person", keyword)


discover._REGISTRY["fake_person"] = fake_person


# --------------------------------------------------------------- 配置

_BASE = """
version: 1
defaults:
  timeout: 5
  retry: {{max_attempts: 1, backoff_base: 0.01}}
  user_agent: "test-agent"
channels:
{channels}
expand:
  enabled: true
  max_queries: 3
  suffixes: ["原理"]
  zero_gain_streak: 2
  require_keyword: true
filter:
  pii_patterns: {{}}
  drop_url_patterns: ["\\\\.(zip|exe)$"]
  drop_domains: []
  max_per_domain: 2
  drop_zero_hit: true
limits:
  max_docs: {max_docs}
  max_minutes: 5
extract:
  summary_chars: 100
  challenge_keywords: ["安全验证"]
  # 这里的通道全是假的（fake.test），没有真实页面可抓。
  # 不关掉的话会在每条候选上真的发一次 DNS 超时，把 1 秒的单测拖到 17 秒。
  fetch_body: false
crawl: {{}}
"""

_SERP_CH = """
  - id: fake_serp
    type: serp
    parser: fake_serp
    url_template: "https://fake.test/s?q={q}&first={offset}"
    paging: {mode: offset, param: first, start: 1, step: 10, max_pages: 1}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

_BLOCKED_CH = """
  - id: fake_blocked
    type: structured_api
    parser: fake_blocked
    url_template: "https://fake.test/api?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1}
"""

_PAGED_CH = """
  - id: fake_paged
    type: site_search
    parser: fake_paged
    url_template: "https://fake.test/p?q={q}&page={offset}"
    paging: {mode: offset, param: page, start: 1, step: 1, max_pages: 4}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""


_ZERO_CH = """
  - id: fake_zero
    type: site_search
    parser: fake_zero
    url_template: "https://fake.test/z?q={q}&page={offset}"
    paging: {mode: offset, param: page, start: 1, step: 1, max_pages: 1}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

_COMPOUND_CH = """
  - id: fake_compound
    type: site_search
    parser: fake_compound
    url_template: "https://fake.test/c?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

_PERSON_CH = """
  - id: fake_person
    type: serp
    parser: fake_person
    url_template: "https://fake.test/p?q={q}&first={offset}"
    paging: {mode: offset, param: first, start: 1, step: 10, max_pages: 1}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""


class RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cfg(self, channels: str, max_docs: int = 20):
        p = self.dir / "channels.yaml"
        p.write_text(_BASE.format(channels=channels, max_docs=max_docs), encoding="utf-8")
        return load_config(p)

    def run_kw(self, cfg, keyword: str = "红黑树", **kw):
        return run_task(cfg, keyword, db_path=self.db, progress=None, **kw)

    def rows(self, sql: str, *args):
        conn = connect(self.db)
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()


class TestHappyPath(RunCase):
    def test_basic_counts(self) -> None:
        r = self.run_kw(self.cfg(_SERP_CH))
        # 基词 5 条 + 变体 2 条 = 原始 7；其中 1 条不含基词被过滤、2 条重复；
        # b 域 3 条受单域配额只留 2 条 → 入库 3
        self.assertEqual(r.candidates, 7)
        self.assertEqual(r.filtered, 1)
        self.assertEqual(r.unique, 4)
        self.assertEqual(r.kept, 3)
        self.assertEqual(r.docs_new, 3)
        self.assertGreaterEqual(r.dropped_cap, 1)

    def test_query_logged_with_variant_text(self) -> None:
        r = self.run_kw(self.cfg(_SERP_CH))
        qs = self.rows("SELECT * FROM query WHERE task_id=?", r.task_id)
        self.assertGreaterEqual(len(qs), 2)              # 基词 + 至少一个变体
        self.assertEqual(qs[0]["query_text"], "红黑树")   # 基词永远第一条
        self.assertTrue(qs[0]["got_count"] > 0)
        # 变体拿到的东西全是重复 → new_count = 0，这正是"早停"的依据
        self.assertEqual(qs[1]["new_count"], 0)

    def test_mention_one_per_doc(self) -> None:
        r = self.run_kw(self.cfg(_SERP_CH))
        mentions = self.rows("SELECT COUNT(*) AS n FROM mention WHERE task_id=?", r.task_id)[0]["n"]
        self.assertEqual(mentions, r.kept)

    def test_time_source_split(self) -> None:
        self.run_kw(self.cfg(_SERP_CH))
        sources = {row["time_source"] for row in self.rows("SELECT time_source FROM doc")}
        self.assertIn("serp", sources)        # 有绝对日期的
        self.assertIn("fallback", sources)    # 没日期的老实说没有

    def test_task_finished_ok(self) -> None:
        r = self.run_kw(self.cfg(_SERP_CH))
        task = self.rows("SELECT * FROM task WHERE task_id=?", r.task_id)[0]
        self.assertEqual(task["status"], "ok")
        self.assertEqual(task["truncated"], 0)
        self.assertIsNotNone(task["finished_at"])


class TestIdempotency(RunCase):
    def test_rerun_adds_no_docs(self) -> None:
        cfg = self.cfg(_SERP_CH)
        first = self.run_kw(cfg)
        second = self.run_kw(cfg)
        # Task 层是新的，doc 层是全局的 —— 重跑不该让"总条数"翻倍
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(second.kept, first.kept)
        self.assertEqual(second.docs_new, 0)
        total = self.rows("SELECT COUNT(*) AS n FROM doc")[0]["n"]
        self.assertEqual(total, first.kept)

    def test_mention_scoped_by_task(self) -> None:
        cfg = self.cfg(_SERP_CH)
        first = self.run_kw(cfg)
        second = self.run_kw(cfg)
        a = self.rows("SELECT COUNT(*) AS n FROM mention WHERE task_id=?", first.task_id)[0]["n"]
        b = self.rows("SELECT COUNT(*) AS n FROM mention WHERE task_id=?", second.task_id)[0]["n"]
        self.assertEqual(a, b)
        self.assertEqual(a, first.kept)      # 不是累加


class TestSanitizeInPipeline(RunCase):
    def test_phone_masked_before_insert(self) -> None:
        self.run_kw(self.cfg(_SERP_CH))
        titles = [row["title"] for row in self.rows("SELECT title FROM doc")]
        joined = " ".join(titles)
        self.assertNotIn("13812345678", joined)     # 原始号码绝不落库
        self.assertIn("138****5678", joined)
        alerts = self.rows("SELECT kind FROM alert WHERE kind='pii_masked'")
        self.assertTrue(alerts)

    def test_drop_url_pattern(self) -> None:
        # 把一个候选改成 .exe 下载链接，应被丢弃并留痕
        old = fake_serp
        discover._REGISTRY["fake_serp"] = lambda k, o, c: _mk(
            [("https://a.example.com/setup.exe", "红黑树工具下载", "红黑树", "a.example.com")],
            "fake_serp", k)
        try:
            r = self.run_kw(self.cfg(_SERP_CH))
        finally:
            discover._REGISTRY["fake_serp"] = old
        self.assertEqual(r.kept, 0)
        self.assertEqual(r.dropped_url, 1)
        self.assertTrue(self.rows("SELECT * FROM alert WHERE kind='dropped'"))


class TestBlocked(RunCase):
    def test_blocked_channel_does_not_abort_task(self) -> None:
        r = self.run_kw(self.cfg(_SERP_CH + _BLOCKED_CH))
        # 一条通道被拦，另一条照常出结果
        self.assertGreater(r.kept, 0)
        self.assertEqual(r.status, "partial")
        kinds = {row["kind"] for row in self.rows("SELECT kind FROM alert")}
        self.assertIn("blocked", kinds)

    def test_blocked_only_channel_is_failed(self) -> None:
        r = self.run_kw(self.cfg(_BLOCKED_CH))
        self.assertEqual(r.kept, 0)
        self.assertEqual(r.status, "failed")
        task = self.rows("SELECT status FROM task WHERE task_id=?", r.task_id)[0]
        self.assertEqual(task["status"], "failed")


class TestPagingDegraded(RunCase):
    def test_same_page_stops_paging(self) -> None:
        r = self.run_kw(self.cfg(_PAGED_CH))
        queries = self.rows("SELECT * FROM query WHERE channel='fake_paged'")
        # max_pages=4，但第 2 页就与第 1 页完全相同 → 只应请求 2 次
        self.assertEqual(len(queries), 2)
        run = r.per_channel[0]
        self.assertIn("分页失效", run.stopped_reason)


class TestZeroHit(RunCase):
    def test_zero_hit_dropped_for_site_search(self) -> None:
        r = self.run_kw(self.cfg(_ZERO_CH))
        # 两条里只有一条真的提到关键词；另一条是站点给的兜底推荐
        self.assertEqual(r.kept, 1)
        self.assertEqual(r.dropped_zero, 1)
        title = self.rows("SELECT title FROM doc")[0]["title"]
        self.assertIn("红黑树", title)

    def test_compound_keyword_not_killed_by_zero_hit(self) -> None:
        # "央行降准"整串几乎不会连着出现在标题里（实际是"央行宣布降准"），
        # 零命中丢弃必须用 needles 词元口径复核，否则 serp 过滤放行的全被杀
        r = self.run_kw(self.cfg(_COMPOUND_CH), keyword="央行降准")
        self.assertEqual(r.kept, 1)
        self.assertEqual(r.dropped_zero, 1)
        title = self.rows("SELECT title FROM doc")[0]["title"]
        self.assertIn("降准", title)


class TestPersonMode(RunCase):
    """人名模式：裸姓名一次 + 结果内归因 + 候选人卡片。"""

    def test_cards_grouped_by_org(self) -> None:
        p = parse_person("张军", ["甲大学计算机学院"], ["网络安全"])
        r = self.run_kw(self.cfg(_PERSON_CH), keyword="张军", person=p)
        self.assertEqual(r.kept, 3)
        # 甲大学一张卡 + 未匹配机构（歌手）一张卡
        self.assertEqual(r.person_cards, 2)
        self.assertEqual(r.person_tiers.get("confirmed"), 1)
        rows = self.rows("SELECT label, tier, docs FROM person_card ORDER BY docs DESC")
        by_label = {x["label"]: x for x in rows}
        self.assertEqual(by_label["甲大学计算机学院"]["tier"], "confirmed")
        self.assertEqual(by_label["未匹配机构"]["docs"], 1)

    def test_only_bare_name_queried(self) -> None:
        # 配置里有 suffix "原理"，但人名模式必须清掉 —— 任何"姓名+第二个词"
        # 都会触发 Bing 静默降级（见 discover/quality.py）
        p = parse_person("张军", ["甲大学计算机学院"])
        self.run_kw(self.cfg(_PERSON_CH), keyword="张军", person=p)
        qs = self.rows("SELECT query_text FROM query WHERE channel='fake_person'")
        self.assertEqual([q["query_text"] for q in qs], ["张军"])

    def test_no_anchor_alerts(self) -> None:
        p = parse_person("张军")          # 没有机构/方向锚点
        r = self.run_kw(self.cfg(_PERSON_CH), keyword="张军", person=p)
        kinds = [x["kind"] for x in
                 self.rows("SELECT kind FROM alert WHERE task_id=?", r.task_id)]
        self.assertIn("person_no_anchor", kinds)
        # 无锚点也要出卡片，但只能是"未匹配机构"
        labels = [x["label"] for x in self.rows("SELECT label FROM person_card")]
        self.assertEqual(labels, ["未匹配机构"])


class TestLimits(RunCase):
    def test_max_docs_truncates_and_flags(self) -> None:
        r = self.run_kw(self.cfg(_SERP_CH, max_docs=2), max_docs=2)
        self.assertEqual(r.kept, 2)
        self.assertTrue(r.truncated)
        task = self.rows("SELECT truncated FROM task WHERE task_id=?", r.task_id)[0]
        self.assertEqual(task["truncated"], 1)
        self.assertTrue(self.rows("SELECT * FROM alert WHERE kind='limit_reached'"))

    def test_higher_score_kept_first(self) -> None:
        # 截到 1 条时，留下的应该是最相关的那条（标题含基词的 a 域页面，
        # 而不是排在后面、只有域名相同的条目）
        r = self.run_kw(self.cfg(_SERP_CH, max_docs=1), max_docs=1)
        self.assertEqual(r.kept, 1)
        kept = self.rows("SELECT title, score FROM doc JOIN mention USING(url_canon)"
                         " WHERE mention.task_id=?", r.task_id)
        self.assertEqual(kept[0]["title"], "红黑树 原理详解")


class TestRepository(RunCase):
    def test_upsert_doc_returns_new_flag_and_keeps_title(self) -> None:
        from kwsearch.models import Doc
        from kwsearch.storage import init_db

        conn = init_db(self.db)
        repo = Repository(conn)

        first = Doc(url_canon="https://x.com/1", url_raw="https://x.com/1",
                    domain="x.com", title="原标题", summary="旧摘要",
                    published_at=None, time_source="fallback")
        self.assertTrue(repo.upsert_doc(first))

        second = Doc(url_canon="https://x.com/1", url_raw="https://x.com/1",
                     domain="x.com", title="#新标题#", summary="",
                     published_at="2026-10-01", time_source="serp")
        self.assertFalse(repo.upsert_doc(second))

        row = dict(conn.execute("SELECT * FROM doc WHERE url_canon='https://x.com/1'").fetchone())
        self.assertEqual(row["title"], "原标题")          # 不被后到的通道污染
        self.assertEqual(row["published_at"], "2026-10-01")
        self.assertEqual(row["time_source"], "serp")      # 时间与来源必须成对更新
        conn.close()

    def test_mention_keeps_max_score(self) -> None:
        from kwsearch.models import Doc, Mention
        from kwsearch.storage import init_db

        conn = init_db(self.db)
        repo = Repository(conn)
        repo.upsert_doc(Doc(url_canon="https://y.com/1", domain="y.com", title="t"))
        repo.upsert_mention(Mention(task_id="t1", url_canon="https://y.com/1", score=1.5))
        repo.upsert_mention(Mention(task_id="t1", url_canon="https://y.com/1", score=4.0))
        repo.upsert_mention(Mention(task_id="t1", url_canon="https://y.com/1", score=2.0))
        rows = list(conn.execute("SELECT score FROM mention WHERE task_id='t1'"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 4.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
