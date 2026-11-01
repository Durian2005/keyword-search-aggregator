"""同词复用（Phase 3）：TTL 内跑过的词不再重复花额度。

全部离线 —— tasks 是手工构造的字典列表，"现在几点"是传进去的 now。
不发任何网络请求，也不碰真实库。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.reuse import (  # noqa: E402
    MIN_DOCS,
    TTL_HOURS,
    ReusePolicy,
    find_reusable,
)
from kwsearch.storage import Repository, init_db  # noqa: E402

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone(timedelta(hours=8)))


def _task(tid: str, *, keyword: str = "红黑树", mode: str = "quick",
          hours_ago: float = 1.0, status: str = "partial",
          docs: int = 10) -> dict:
    return {
        "task_id": tid,
        "keyword": keyword,
        "mode": mode,
        "started_at": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
        "status": status,
        "stats": {"docs_total": docs},
    }


class TestPolicy(unittest.TestCase):
    def test_default_is_off(self):
        """默认关闭：它改变语义（这次没真的去搜），不该影响既有行为。"""
        p = ReusePolicy.from_mapping(None)
        self.assertFalse(p.enabled)
        self.assertEqual(p.ttl_hours, TTL_HOURS)
        self.assertEqual(p.min_docs, MIN_DOCS)

    def test_parses_section(self):
        p = ReusePolicy.from_mapping({"enabled": True, "ttl_hours": 6, "min_docs": 3})
        self.assertTrue(p.enabled)
        self.assertEqual(p.ttl_hours, 6.0)
        self.assertEqual(p.min_docs, 3)

    def test_bad_values_fall_back(self):
        """ttl<=0 会让「TTL 内」永远不成立 —— 功能静默失效比报错更难查。"""
        for bad in (0, -5, "abc", None):
            self.assertEqual(ReusePolicy.from_mapping({"ttl_hours": bad}).ttl_hours,
                             TTL_HOURS, bad)
        self.assertEqual(ReusePolicy.from_mapping({"min_docs": 0}).min_docs, 1)
        self.assertEqual(ReusePolicy.from_mapping({"min_docs": "x"}).min_docs, 1)

    def test_resolve_three_state(self):
        cfg = {"enabled": False, "ttl_hours": 8}
        self.assertFalse(ReusePolicy.resolve(cfg, None).enabled)        # 听配置
        self.assertTrue(ReusePolicy.resolve(cfg, True).enabled)         # --reuse
        self.assertEqual(ReusePolicy.resolve(cfg, True).ttl_hours, 8.0)  # 开关不动 TTL
        self.assertFalse(ReusePolicy.resolve({"enabled": True}, False).enabled)  # --refresh


class TestFindReusable(unittest.TestCase):
    def setUp(self):
        self.on = ReusePolicy(enabled=True, ttl_hours=24, min_docs=1)

    def _find(self, tasks, *, keyword="红黑树", mode="quick"):
        return find_reusable(tasks, keyword=keyword, mode=mode,
                             now=NOW, policy=self.on)

    def test_hits_recent_task(self):
        hit = self._find([_task("t1")])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["task_id"], "t1")
        self.assertEqual(hit["docs"], 10)
        self.assertIn("h", hit["age"])          # 给人看的时长，别再让调用方算一遍

    def test_disabled_never_hits(self):
        off = ReusePolicy(enabled=False)
        self.assertIsNone(find_reusable([_task("t1")], keyword="红黑树",
                                        mode="quick", now=NOW, policy=off))

    def test_skips_aborted(self):
        """崩掉的任务数据可能只写了一半，不能当"已有结果"复用。"""
        hit = self._find([_task("bad", status="aborted"), _task("good", hours_ago=3)])
        self.assertEqual(hit["task_id"], "good")

    def test_skips_running(self):
        self.assertIsNone(self._find([_task("r", status="running")]))

    def test_mode_must_match(self):
        """quick 的结果不能冒充 deep：deep 会遍历种子域，量级完全不同。"""
        self.assertIsNone(self._find([_task("t1", mode="deep")]))

    def test_keyword_normalized(self):
        """大小写与多余空白不该让同一个词被当成两个。"""
        self.assertIsNotNone(self._find([_task("t1", keyword="  Red Black Tree ")],
                                        keyword="red black tree"))

    def test_other_keyword_ignored(self):
        """「红黑树」和「红黑树原理」是两个词 —— 不能因为前缀相同就复用。"""
        self.assertIsNone(self._find([_task("t1", keyword="红黑树原理")]))

    def test_ttl_expired(self):
        self.assertIsNone(self._find([_task("t1", hours_ago=25)]))

    def test_ttl_boundary_hits(self):
        """刚好 24 小时前算命中：宁可少跑一次，也别在边界上多消耗一发额度。"""
        self.assertIsNotNone(self._find([_task("t1", hours_ago=24)]))

    def test_min_docs(self):
        """上一轮入库 0 条说明白跑，值得重来。"""
        self.assertIsNone(self._find([_task("zero", docs=0)]))

    def test_picks_most_recent(self):
        """tasks 已按时间降序 —— 取第一条命中的，也就是最近那次。"""
        hit = self._find([_task("new", hours_ago=1), _task("old", hours_ago=10)])
        self.assertEqual(hit["task_id"], "new")

    def test_missing_stats_counts_zero(self):
        t = {"task_id": "x", "keyword": "红黑树", "mode": "quick",
             "started_at": NOW.isoformat(), "status": "partial"}
        self.assertIsNone(self._find([t]))

    def test_bad_started_at_skipped(self):
        t = {"task_id": "x", "keyword": "红黑树", "mode": "quick",
             "started_at": "不是时间", "status": "partial",
             "stats": {"docs_total": 5}}
        self.assertIsNone(self._find([t]))


class TestTaskHistory(unittest.TestCase):
    """storage 侧的查询口径 —— 顺序错了会拿到更早的任务，然后报告"2 天前跑过"。"""

    def test_orders_desc_and_parses_stats(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = init_db(str(Path(tmp.name) / "t.db"))
        repo = Repository(conn)
        a = repo.start_task("甲", "quick")
        repo.finish_task(a, "partial", stats={"docs_total": 3})
        b = repo.start_task("乙", "quick")
        repo.finish_task(b, "ok", stats={"docs_total": 7})
        hist = repo.task_history()
        conn.close()
        self.assertEqual([h["task_id"] for h in hist], [b, a])    # 后开的排前面
        self.assertEqual(hist[0]["stats"]["docs_total"], 7)
        self.assertEqual(hist[1]["stats"]["docs_total"], 3)


if __name__ == "__main__":
    unittest.main()
