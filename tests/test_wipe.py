"""一键清空所有历史数据：存储层 wipe + CLI db clear。

CLI 部分复用 test_cli.py 的 CliCase 模式（重定向 stdout/stderr 调 main()），
**不发任何网络请求** —— 只碰临时目录里的 SQLite。
"""
from __future__ import annotations

import io
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.__main__ import main  # noqa: E402
from kwsearch.models import Doc, Mention  # noqa: E402
from kwsearch.storage import (  # noqa: E402
    Repository,
    connect,
    init_db,
    table_counts,
)

_CFG = """
version: 1
defaults: {timeout: 5}
channels:
  - id: none_ch
    type: serp
    parser: __not_registered__
    url_template: "https://x.test/?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
"""


def _seed(repo: Repository) -> str:
    """灌一个任务 + 一篇文档 + 一条命中 + 一条告警 + 一条 robots 缓存。"""
    tid = repo.start_task("红黑树", "quick")
    repo.upsert_doc(Doc(url_canon="https://a.example.com/1", domain="a.example.com",
                        title="红黑树原理", summary="摘要", published_at="2026-10-01",
                        time_source="serp", channel="bing_html"))
    repo.upsert_mention(Mention(task_id=tid, url_canon="https://a.example.com/1",
                                score=5.0))
    repo.add_alert(tid, "bing_html", "blocked", "测试用告警")
    repo.put_robots("a.example.com", status=200, disallow="/x", allowed=True)
    repo.conn.commit()   # 不留开着手写事务：否则同一文件上的第二个连接被锁死
    return tid


class WipeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.db")
        self.conn = init_db(self.db)
        self.repo = Repository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


class TestWipePreview(WipeCase):
    def test_empty_db_all_zero(self) -> None:
        counts = self.repo.wipe_preview()
        self.assertEqual(set(counts), {"mention", "query", "cluster_member", "cluster",
                                       "person_card", "queue", "alert", "doc", "task",
                                       "robots"})
        self.assertTrue(all(n == 0 for n in counts.values()))

    def test_preview_does_not_delete(self) -> None:
        _seed(self.repo)
        first = self.repo.wipe_preview()
        second = self.repo.wipe_preview()
        self.assertEqual(first, second)
        self.assertEqual(second["task"], 1)
        self.assertEqual(second["doc"], 1)
        self.assertEqual(second["mention"], 1)
        self.assertEqual(second["robots"], 1)
        # 预览之后数据必须还读得出来
        tasks = self.conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]
        self.assertEqual(tasks, 1)


class TestWipe(WipeCase):
    def test_wipe_returns_deleted_and_empties(self) -> None:
        _seed(self.repo)
        deleted = self.repo.wipe()
        self.assertEqual(deleted["task"], 1)
        self.assertEqual(deleted["doc"], 1)
        self.assertEqual(deleted["mention"], 1)
        self.assertEqual(deleted["alert"], 1)
        self.assertEqual(deleted["robots"], 1)
        self.assertTrue(all(n == 0 for n in table_counts(self.conn).values()))

    def test_wipe_fk_order_no_error(self) -> None:
        # mention 对 doc 有外键（foreign_keys=ON 立即检查）：
        # 先删 doc 会直接失败，这条用例守着删除顺序。
        _seed(self.repo)
        self.repo.wipe()   # 不抛就算过

    def test_wipe_twice_is_noop(self) -> None:
        _seed(self.repo)
        self.repo.wipe()
        second = self.repo.wipe()
        self.assertTrue(all(n == 0 for n in second.values()))

    def test_write_after_wipe(self) -> None:
        # 清空后 VACUUM + sqlite_sequence 重置不能影响后续正常使用
        _seed(self.repo)
        self.repo.wipe()
        tid = self.repo.start_task("清空后再跑", "quick")
        self.repo.upsert_doc(Doc(url_canon="https://b.example.com/2",
                                 domain="b.example.com", title="t2",
                                 channel="bing_html"))
        self.repo.upsert_mention(Mention(task_id=tid,
                                         url_canon="https://b.example.com/2", score=1.0))
        self.repo.finish_task(tid, "ok", False, {"docs_new": 1})
        self.conn.commit()
        counts = table_counts(self.conn)
        self.assertEqual(counts["task"], 1)
        self.assertEqual(counts["doc"], 1)

    def test_table_counts_tolerates_missing_tables(self) -> None:
        # 老库 / 空文件：表不存在记 0，不抛
        bare = connect(str(self.dir / "bare.db"))
        counts = table_counts(bare)
        self.assertTrue(all(n == 0 for n in counts.values()))
        bare.close()


class TestDbClearCommand(WipeCase):
    def _cfg_path(self) -> str:
        p = self.dir / "channels.yaml"
        p.write_text(_CFG, encoding="utf-8")
        return str(p)

    def _call(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", self._cfg_path(), "--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_init_still_works(self) -> None:
        code, out, _ = self._call("db", "init")
        self.assertEqual(code, 0)
        self.assertIn("已初始化", out)

    def test_clear_without_yes_only_previews(self) -> None:
        _seed(self.repo)
        code, out, err = self._call("db", "clear")
        self.assertEqual(code, 2, "未确认必须拒绝执行")
        self.assertIn("将删除", out)
        self.assertIn("红黑树", out)             # 打印了任务关键词所在的任务行
        self.assertIn("--yes", err)
        counts = table_counts(self.conn)
        self.assertEqual(counts["task"], 1, "不加 --yes 一行都不能少")

    def test_clear_with_yes_wipes(self) -> None:
        _seed(self.repo)
        code, out, _ = self._call("db", "clear", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("已清空", out)
        self.assertTrue(all(n == 0 for n in table_counts(self.conn).values()))

    def test_clear_missing_db_is_noop(self) -> None:
        self.conn.close()          # Windows 上删不掉还被连接占用的文件
        Path(self.db).unlink()
        code, out, _ = self._call("db", "clear")
        self.assertEqual(code, 0)
        self.assertIn("没什么可清", out)


if __name__ == "__main__":
    unittest.main()
