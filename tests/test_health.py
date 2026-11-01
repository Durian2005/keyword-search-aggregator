"""通道健康度：冷却曲线 / 额度 / 跨天清零 / 历史回填。

全部离线 —— 只碰临时目录里的 SQLite，**不发任何网络请求**。

时间一律构造带时区的 datetime 传进去，不 patch 系统时间：
`cooldown_hours` / `cooling` / `used` 都接受 now，正是为了让"假设现在是某时刻"
变成一次普通函数调用。本机时区 +08:00 是前提（项目其余部分同样依赖）。
"""
from __future__ import annotations

import io
import contextlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.__main__ import main  # noqa: E402
from kwsearch.health import (  # noqa: E402
    COOLDOWN_BASE_H,
    COOLDOWN_FACTOR,
    COOLDOWN_MAX_H,
    MODE_NORMAL,
    MODE_PROBE,
    MODE_SKIP,
    ChannelHealth,
    CooldownPolicy,
    HealthPolicy,
    ProbePolicy,
    classify_block,
    cooldown_hours,
    decide,
    human_delta,
    local_day,
    local_day_of_iso,
    parse_iso,
    plan,
)
from kwsearch.storage import (  # noqa: E402
    KEEP_ON_WIPE,
    Repository,
    init_db,
)

LOCAL = datetime.now().astimezone().tzinfo
NOW = datetime(2026, 10, 31, 23, 0, tzinfo=timezone(timedelta(hours=8)))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class TestCooldownCurve(unittest.TestCase):
    """冷却曲线：6h 起步、翻倍、24h 封顶。"""

    def test_curve(self):
        self.assertEqual(cooldown_hours(0), 0.0)
        self.assertEqual(cooldown_hours(1), COOLDOWN_BASE_H)
        self.assertEqual(cooldown_hours(2), 12.0)
        self.assertEqual(cooldown_hours(3), COOLDOWN_MAX_H)

    def test_capped(self):
        # 连挂很多次也只封顶 24h：实测恢复上界 25.3h，再长就是白等
        for streak in (4, 9, 50):
            self.assertEqual(cooldown_hours(streak), COOLDOWN_MAX_H)

    def test_negative_is_zero(self):
        self.assertEqual(cooldown_hours(-3), 0.0)


class TestClassifyBlock(unittest.TestCase):
    """不是所有 blocked 都算"被风控"——这条错了会把通道永久封死。"""

    def test_cooldown_kinds(self):
        for kind in ("challenge", "captcha", "http_403", "http_412", "rate_limit"):
            self.assertEqual(classify_block(kind), "cooldown", kind)

    def test_neutral_kinds(self):
        # network 是本机网络问题（gdelt 恒 network），relaxed 是对方降级内容
        for kind in ("network", "timeout", "conn", "relaxed", "boilerplate",
                     "empty", "unknown_format", "api_error", "parse_fail"):
            self.assertEqual(classify_block(kind), "neutral", kind)

    def test_case_and_none(self):
        self.assertEqual(classify_block("CHALLENGE"), "cooldown")
        self.assertEqual(classify_block(None), "neutral")
        self.assertEqual(classify_block(""), "neutral")

    def test_unknown_is_neutral(self):
        # 看不懂就当没被拦：宁可少冷却，也不要因为一个陌生 kind 封死通道
        self.assertEqual(classify_block("something_new"), "neutral")


class TestParsing(unittest.TestCase):
    def test_parse_iso_bad_input(self):
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso(""))
        self.assertIsNone(parse_iso("坏数据"))
        self.assertIsNotNone(parse_iso("2026-10-31T10:00:00+08:00"))

    def test_date_only_gets_tz(self):
        # "2026-10-31" 是合法输入（naive），补上本地时区后仍可用 —— 不该当坏数据丢掉
        dt = parse_iso("2026-10-31")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_parse_iso_naive_gets_tz(self):
        dt = parse_iso("2026-10-31T10:00:00")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_local_day(self):
        self.assertEqual(local_day(NOW.astimezone()), "2026-10-31")

    def test_local_day_of_iso(self):
        self.assertEqual(local_day_of_iso("2026-10-31T23:00:00+08:00"), "2026-10-31")
        self.assertEqual(local_day_of_iso("坏数据"), "")

    def test_human_delta(self):
        self.assertEqual(human_delta(timedelta(hours=5, minutes=12)), "5h12m")
        self.assertEqual(human_delta(timedelta(minutes=43)), "43m")
        self.assertEqual(human_delta(timedelta(seconds=7)), "7s")
        self.assertEqual(human_delta(timedelta(seconds=-5)), "刚刚")


class TestChannelHealth(unittest.TestCase):
    def test_no_streak_never_cools(self):
        h = ChannelHealth(channel="x", last_block_at=_iso(NOW))
        self.assertIsNone(h.allowed_at(NOW))
        self.assertEqual(h.cooling(NOW), (False, ""))

    def test_cooling_window(self):
        h = ChannelHealth(channel="x", last_block_at=_iso(NOW), block_streak=1)
        # 起点 +1 小时：还在冷却，剩 5 小时
        self.assertTrue(h.cooling(NOW + timedelta(hours=1))[0])
        self.assertIn("5h00m", h.cooling(NOW + timedelta(hours=1))[1])
        # 起点 +6 小时：刚好结束
        self.assertFalse(h.cooling(NOW + timedelta(hours=6))[0])
        self.assertFalse(h.cooling(NOW + timedelta(hours=7))[0])

    def test_streak_two_doubles(self):
        h = ChannelHealth(channel="x", last_block_at=_iso(NOW), block_streak=2)
        self.assertEqual(h.allowed_at(NOW), NOW + timedelta(hours=12))

    def test_used_resets_on_new_day(self):
        h = ChannelHealth(channel="x", day="2026-10-30", used_today=9)
        self.assertEqual(h.used(NOW), 0)                 # 昨天的账不算今天
        h2 = ChannelHealth(channel="x", day="2026-10-31", used_today=9)
        self.assertEqual(h2.used(NOW), 9)

    def test_quota_left(self):
        h = ChannelHealth(channel="x", day="2026-10-31", used_today=4)
        self.assertEqual(h.quota_left(NOW, 6), 2)
        self.assertEqual(h.quota_left(NOW, 4), 0)
        self.assertEqual(h.quota_left(NOW, 3), 0)        # 超了不为负
        self.assertIsNone(h.quota_left(NOW, None))       # 没设预算 = 不限
        self.assertIsNone(h.quota_left(NOW, 0))

    def test_exhausted(self):
        h = ChannelHealth(channel="x", day="2026-10-31", used_today=6)
        done, why = h.exhausted(NOW, 6)
        self.assertTrue(done)
        self.assertIn("6/6", why)
        self.assertFalse(h.exhausted(NOW, 10)[0])
        self.assertFalse(h.exhausted(NOW, None)[0])

    def test_from_mapping_tolerates_missing(self):
        h = ChannelHealth.from_mapping({"channel": "b"})
        self.assertEqual(h.block_streak, 0)
        self.assertEqual(h.used_today, 0)
        self.assertIsNone(h.last_block_at)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = init_db(self.db)
        self.repo = Repository(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def state(self, ch: str) -> ChannelHealth:
        return ChannelHealth.from_mapping(self.repo.get_channel_state(ch) or {"channel": ch})


class TestPolicy(unittest.TestCase):
    """配置层：channels.yaml 的 health 段怎么写都不会把通道锁死。"""

    def test_defaults_match_constants(self):
        p = CooldownPolicy()
        self.assertEqual(p.base_hours, COOLDOWN_BASE_H)
        self.assertEqual(p.max_hours, COOLDOWN_MAX_H)
        self.assertEqual(cooldown_hours(2, p), 12.0)

    def test_custom_curve(self):
        p = CooldownPolicy.from_mapping({"base_hours": 1, "factor": 3, "max_hours": 10})
        self.assertEqual(cooldown_hours(1, p), 1.0)
        self.assertEqual(cooldown_hours(2, p), 3.0)
        self.assertEqual(cooldown_hours(3, p), 9.0)
        self.assertEqual(cooldown_hours(4, p), 10.0)     # 封顶

    def test_factor_below_one_falls_back(self):
        # factor < 1 会让曲线往下走（连挂越多冷却越短），配置写错也不能这样
        p = CooldownPolicy.from_mapping({"factor": 0.5})
        self.assertEqual(p.factor, COOLDOWN_FACTOR)

    def test_max_below_base_clamped(self):
        p = CooldownPolicy.from_mapping({"base_hours": 8, "max_hours": 2})
        self.assertGreaterEqual(p.max_hours, p.base_hours)

    def test_quota_parsing(self):
        p = HealthPolicy.from_mapping(
            {"quota": {"default": 10, "per_channel": {"baidu_html": 6}}})
        self.assertEqual(p.quota_for("baidu_html"), 6)
        self.assertEqual(p.quota_for("other"), 10)

    def test_quota_is_unlimited_by_default(self):
        p = HealthPolicy.from_mapping({})
        self.assertIsNone(p.quota_for("baidu_html"))
        self.assertIsNone(p.quota_for("whatever"))

    def test_bad_quota_entry_skipped(self):
        # 单个通道写错不该让整份配置失效；那一项按"不限"处理
        p = HealthPolicy.from_mapping(
            {"quota": {"per_channel": {"a": "abc", "b": 3, "c": 0}}})
        self.assertNotIn("a", p.quota)
        self.assertEqual(p.quota_for("a"), None)
        self.assertEqual(p.quota_for("b"), 3)
        self.assertIsNone(p.quota_for("c"))       # 0 当"不限"，不能当"零额度"


class TestDecide(unittest.TestCase):
    def test_pass_when_clean(self):
        h = ChannelHealth("x", day="2026-10-31", used_today=1)
        self.assertEqual(decide(h, NOW, HealthPolicy.from_mapping(
            {"quota": {"per_channel": {"x": 6}}})), (False, ""))

    def test_cooling_skips(self):
        h = ChannelHealth("x", last_block_at=_iso(NOW), block_streak=1)
        skip, why = decide(h, NOW)
        self.assertTrue(skip)
        self.assertIn("冷却中", why)

    def test_quota_skips(self):
        h = ChannelHealth("x", day="2026-10-31", used_today=3)
        p = HealthPolicy.from_mapping({"quota": {"per_channel": {"x": 3}}})
        skip, why = decide(h, NOW, p)
        self.assertTrue(skip)
        self.assertIn("额度", why)

    def test_cooldown_reported_before_quota(self):
        """两个都不满足时先说冷却：否则会给出"额度还有 3 发"这种自相矛盾的说明。"""
        h = ChannelHealth("x", last_block_at=_iso(NOW), block_streak=1,
                          day="2026-10-31", used_today=99)
        p = HealthPolicy.from_mapping({"quota": {"per_channel": {"x": 3}}})
        skip, why = decide(h, NOW, p)
        self.assertTrue(skip)
        self.assertIn("冷却中", why)
        self.assertNotIn("额度", why)


class TestPlan(unittest.TestCase):
    """Phase 2 的三态：skip / probe / normal。

    探针的判据只有一条 —— `block_streak > 0` 且冷却已过。能这么简单是因为
    record_channel_ok() 会清零 streak：streak 大于 0 就等于"最后一次事件是被拦、
    之后没成功过"，也就是池子按模型算该回满了、但没被证实。
    """

    def setUp(self):
        self.policy = HealthPolicy.from_mapping({
            "cooldown": {"base_hours": 6, "factor": 2, "max_hours": 24},
            "quota": {"per_channel": {"x": 6}},
            "probe": {"enabled": True, "pages": 1},
        })

    def test_never_blocked_is_normal(self):
        self.assertEqual(plan(ChannelHealth("x"), NOW, self.policy), (MODE_NORMAL, ""))

    def test_after_success_is_normal(self):
        """成功会清零 streak —— 恢复之后不该一直当探针打。"""
        h = ChannelHealth("x", last_block_at=_iso(NOW - timedelta(hours=8)),
                          last_ok_at=_iso(NOW - timedelta(hours=1)), block_streak=0)
        self.assertEqual(plan(h, NOW, self.policy)[0], MODE_NORMAL)

    def test_cooldown_passed_unverified_is_probe(self):
        h = ChannelHealth("x", last_block_at=_iso(NOW - timedelta(hours=7)),
                          block_streak=1)
        mode, why = plan(h, NOW, self.policy)
        self.assertEqual(mode, MODE_PROBE)
        self.assertIn("探针", why)
        self.assertIn("1 页", why)

    def test_cooling_wins_over_probe(self):
        h = ChannelHealth("x", last_block_at=_iso(NOW), block_streak=1)
        self.assertEqual(plan(h, NOW, self.policy)[0], MODE_SKIP)

    def test_quota_wins_over_probe(self):
        """预算用尽时说"先打一发探针"是自相矛盾的：那一发也要花额度。"""
        h = ChannelHealth("x", last_block_at=_iso(NOW - timedelta(hours=7)),
                          block_streak=1, day="2026-10-31", used_today=6)
        self.assertEqual(plan(h, NOW, self.policy)[0], MODE_SKIP)

    def test_probe_disabled_is_normal(self):
        off = HealthPolicy.from_mapping({"probe": {"enabled": False}})
        h = ChannelHealth("x", last_block_at=_iso(NOW - timedelta(hours=7)),
                          block_streak=1)
        self.assertEqual(plan(h, NOW, off)[0], MODE_NORMAL)

    def test_stale_ok_after_block_is_normal(self):
        """自相矛盾的记录（成功晚于被拦）按已验证处理。

        正常流程走不到这里（成功就清零 streak 了），但历史回填或手改过的行可能有。
        那种情况把一次全量任务降级成探针，等于白丢一轮数据。
        注意 streak 只能是 1：streak=2 会让冷却变成 12h，9 小时前那次就还在冷却里，
        测的就不是这条防线了。
        """
        h = ChannelHealth("x", last_block_at=_iso(NOW - timedelta(hours=9)),
                          last_ok_at=_iso(NOW - timedelta(hours=2)), block_streak=1)
        self.assertEqual(plan(h, NOW, self.policy)[0], MODE_NORMAL)

    def test_neutral_failure_does_not_trigger_probe(self):
        """network 类失败不涨 streak，自然也不会触发探针。"""
        h = ChannelHealth("x", last_block_at=_iso(NOW - timedelta(hours=7)),
                          last_block_kind="network", block_streak=0)
        self.assertEqual(plan(h, NOW, self.policy)[0], MODE_NORMAL)


class TestProbePolicy(unittest.TestCase):
    def test_defaults(self):
        p = HealthPolicy().probe
        self.assertTrue(p.enabled)
        self.assertEqual(p.pages, 1)

    def test_pages_clamped_to_one(self):
        """pages<=0 会让探针变成"一个请求都不发"，那它是跳过而不是探针。"""
        self.assertEqual(ProbePolicy.from_mapping({"pages": 0}).pages, 1)
        self.assertEqual(ProbePolicy.from_mapping({"pages": -3}).pages, 1)
        self.assertEqual(ProbePolicy.from_mapping({"pages": "x"}).pages, 1)

    def test_custom_pages(self):
        self.assertEqual(ProbePolicy.from_mapping({"pages": 2}).pages, 2)

    def test_missing_section_defaults_on(self):
        """不写 health.probe 就是开着 —— 少抓一轮数据好过把额度打空。"""
        self.assertTrue(ProbePolicy.from_mapping(None).enabled)


class TestStorage(_DbCase):
    def test_block_accumulates(self):
        self.assertEqual(self.repo.record_channel_block("b", "challenge", _iso(NOW)), 1)
        self.assertEqual(self.repo.record_channel_block("b", "challenge", _iso(NOW)), 2)
        h = self.state("b")
        self.assertEqual(h.block_streak, 2)
        self.assertEqual(h.last_block_kind, "challenge")

    def test_ok_clears_streak_but_keeps_block_stamp(self):
        self.repo.record_channel_block("b", "challenge", _iso(NOW))
        self.repo.record_channel_ok("b", _iso(NOW + timedelta(minutes=5)))
        h = self.state("b")
        self.assertEqual(h.block_streak, 0)
        # 被拦的历史时间要留着：它是"上次为什么挂"的唯一证据
        self.assertIsNotNone(h.last_block_at)
        self.assertEqual(h.last_block_kind, "challenge")

    def test_neutral_failure_neither_adds_nor_clears(self):
        """network 失败既不能把 streak +1，也不能把它洗白 —— 它只是"没信息"。"""
        self.repo.record_channel_block("b", "challenge", _iso(NOW))
        self.repo.record_channel_block("b", "challenge", _iso(NOW))
        self.repo.record_channel_block("b", "network", _iso(NOW), counts=False)
        h = self.state("b")
        self.assertEqual(h.block_streak, 2)
        self.assertEqual(h.last_block_kind, "network")
        self.assertIsNotNone(h.last_block_at)

    def test_neutral_failure_on_blank_row(self):
        # 空表上的中性失败：留下痕迹，但 streak 仍是 0（= 永不冷却）
        self.repo.record_channel_block("b", "network", _iso(NOW), counts=False)
        h = self.state("b")
        self.assertEqual(h.block_streak, 0)
        self.assertFalse(h.cooling(NOW)[0])

    def test_usage_accumulates_same_day(self):
        self.repo.add_channel_usage("b", 3, _iso(NOW))
        self.assertEqual(self.repo.add_channel_usage("b", 2, _iso(NOW)), 5)
        self.assertEqual(self.state("b").used(NOW), 5)

    def test_usage_resets_across_day(self):
        self.repo.add_channel_usage("b", 9, _iso(NOW - timedelta(days=1)))
        self.assertEqual(self.state("b").used(NOW), 0)      # 读的时候按天判断
        self.assertEqual(self.repo.add_channel_usage("b", 1, _iso(NOW)), 1)  # 写的时候重开

    def test_usage_ignores_zero(self):
        self.assertEqual(self.repo.add_channel_usage("b", 0, _iso(NOW)), 0)
        self.assertIsNone(self.repo.get_channel_state("b"))

    def test_channel_state_survives_wipe(self):
        self.repo.record_channel_block("b", "challenge", _iso(NOW))
        self.conn.commit()
        self.assertIn("channel_state", KEEP_ON_WIPE)
        self.repo.wipe()
        # 清空历史数据不该把"IP 已经被封过"这件事一起清掉
        self.assertIsNotNone(self.repo.get_channel_state("b"))


class TestBackfill(_DbCase):
    """历史回填：按 seq 判断"任务以什么收尾"，不按时间（同批写入会同秒）。"""

    def _task(self, keyword: str, started: datetime) -> str:
        tid = self.repo.start_task(keyword, "quick")
        self.conn.execute("UPDATE task SET started_at=? WHERE task_id=?",
                          (_iso(started), tid))
        return tid

    def _q(self, tid: str, ch: str, seq: int, *, blocked: bool,
           kind: str | None, when: datetime) -> None:
        self.repo.log_query(tid, ch, seq, f"https://x.test/{seq}",
                            blocked=blocked, block_kind=kind, got=0 if blocked else 5)
        self.conn.execute(
            "UPDATE query SET created_at=? WHERE task_id=? AND channel=? AND seq=?",
            (_iso(when), tid, ch, seq))

    def test_streak_counts_tasks_not_pages(self):
        """一个任务翻 3 页全被拦 = 一次事件，不是三次。"""
        t1 = self._task("a", NOW - timedelta(hours=2))
        for seq in (1, 2, 3):
            self._q(t1, "bilibili_search", seq, blocked=True, kind="http_412", when=NOW)
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        self.assertEqual(self.state("bilibili_search").block_streak, 1)

    def test_last_page_decides(self):
        """前两页成功、第三页被拦 → 该任务以被拦收尾，streak 记 1。"""
        t1 = self._task("a", NOW - timedelta(hours=1))
        self._q(t1, "ch", 1, blocked=False, kind=None, when=NOW)
        self._q(t1, "ch", 2, blocked=False, kind=None, when=NOW)
        self._q(t1, "ch", 3, blocked=True, kind="challenge", when=NOW)
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        h = self.state("ch")
        self.assertEqual(h.block_streak, 1)
        self.assertEqual(h.last_block_kind, "challenge")
        self.assertIsNotNone(h.last_ok_at)     # 成功过这件事不能被"以被拦收尾"抹掉

    def test_cross_day_resets_streak(self):
        """隔了 3 天再被拦，不是"连挂 2 次"—— 中间早就自然恢复过了。"""
        old = self._task("a", NOW - timedelta(days=4))
        self._q(old, "ch", 1, blocked=True, kind="challenge",
                when=NOW - timedelta(days=4))
        new = self._task("b", NOW - timedelta(hours=1))
        self._q(new, "ch", 1, blocked=True, kind="challenge", when=NOW)
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        self.assertEqual(self.state("ch").block_streak, 1)

    def test_consecutive_tasks_accumulate(self):
        for i in range(3):
            tid = self._task(f"k{i}", NOW - timedelta(minutes=30 * (3 - i)))
            self._q(tid, "ch", 1, blocked=True, kind="rate_limit", when=NOW)
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        self.assertEqual(self.state("ch").block_streak, 3)

    def test_network_failure_leaves_trace_but_no_streak(self):
        """gdelt 恒 network：要能看到"它一直失败"，但不能因此被冷却。"""
        t1 = self._task("a", NOW - timedelta(hours=1))
        self._q(t1, "gdelt", 1, blocked=True, kind="network", when=NOW)
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        h = self.state("gdelt")
        self.assertEqual(h.block_streak, 0)
        self.assertEqual(h.last_block_kind, "network")
        self.assertIsNotNone(h.last_block_at)
        self.assertFalse(h.cooling(NOW)[0])            # 关键：不冷却

    def test_success_after_block_clears_streak(self):
        """库里已有实时成功（探针）时，历史里的被拦不该把 streak 顶起来。"""
        self.repo.record_channel_ok("ch", _iso(NOW))
        t1 = self._task("a", NOW - timedelta(days=2))
        self._q(t1, "ch", 1, blocked=True, kind="challenge", when=NOW - timedelta(days=2))
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        h = self.state("ch")
        self.assertEqual(h.block_streak, 0)
        self.assertIsNotNone(h.last_block_at)          # 历史照样补进来
        self.assertFalse(h.cooling(NOW)[0])

    def test_backfill_is_idempotent(self):
        t1 = self._task("a", NOW - timedelta(hours=1))
        self._q(t1, "ch", 1, blocked=True, kind="challenge", when=NOW)
        self.conn.commit()
        self.repo.backfill_channel_state()
        self.conn.commit()
        first = self.state("ch")
        self.repo.backfill_channel_state()
        self.conn.commit()
        second = self.state("ch")
        self.assertEqual(first.block_streak, second.block_streak)
        self.assertEqual(first.last_block_at, second.last_block_at)


class TestHealthCli(_DbCase):
    _CFG = """
version: 1
defaults: {timeout: 5}
channels:
  - id: ch_a
    type: serp
    parser: __not_registered__
    url_template: "https://x.test/?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
"""

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(list(argv))
        return buf.getvalue()

    def test_health_on_empty_db(self):
        out = self._run("--db", str(self.db), "health")
        self.assertIn("通道健康度", out)
        self.assertIn("还没有任何记录", out)

    def test_health_on_missing_db(self):
        missing = Path(self.tmp.name) / "nope.db"
        out = self._run("--db", str(missing), "health")
        self.assertIn("数据库还不存在", out)

    def test_health_lists_channel(self):
        self.repo.record_channel_block("ch_a", "challenge", _iso(NOW))
        self.conn.commit()
        cfg = Path(self.tmp.name) / "c.yaml"
        cfg.write_text(self._CFG, encoding="utf-8")
        out = self._run("--config", str(cfg), "--db", str(self.db), "health")
        self.assertIn("ch_a", out)
        self.assertIn("challenge", out)


if __name__ == "__main__":
    unittest.main()
