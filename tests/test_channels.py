"""M1 验收：JSON 类通道（GDELT / B站）的解析层。

全部离线，用内联 JSON 复刻真实响应里那几个坑：
    GDELT 的 seendate 无分隔符 UTC、标题带实体、超限返回纯文本
    B站的 <em class="keyword"> 高亮、pubdate unix 秒、result 为 null、code=-412

跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kwsearch.discover.bilibili import PAGE_SIZE, parse_result  # noqa: E402
from kwsearch.discover.gdelt import parse_articles, parse_seendate  # noqa: E402
from kwsearch.htmlx import strip_tags  # noqa: E402

GDELT_BODY = json.dumps({
    "articles": [
        {"url": "https://www.reuters.com/a", "title": "Zcash &amp; privacy",
         "seendate": "20261018T120000Z", "domain": "reuters.com",
         "language": "English", "sourcecountry": "United States"},
        {"url": "https://www.reuters.com/a", "title": "重复项",
         "seendate": "20261018T120000Z", "domain": "reuters.com"},
        {"url": "https://example.cn/b", "title": "零知识证明入门",
         "seendate": "2026-10-18T12:00:00Z", "domain": "www.example.cn"},
        {"url": "", "title": "没有链接的条目", "seendate": "20261018T120000Z"},
        {"url": "https://x.com/c", "title": "", "seendate": "20261018T120000Z"},
    ]
}, ensure_ascii=False)

BILI_BODY = json.dumps({
    "code": 0, "message": "OK",
    "data": {"page": 2, "pagesize": 20, "numResults": 1000, "numPages": 50,
             "result": [
                 {"arcurl": "https://www.bilibili.com/video/BV1xx411c7mD",
                  "bvid": "BV1xx411c7mD",
                  "title": "零知识证明<em class=\"keyword\">入门</em>（上）",
                  "description": "讲清楚 &quot;证明&quot; 与 &quot;知识&quot;的边界",
                  "pubdate": 1673789512, "author": "某UP", "play": 1234,
                  "danmaku": 12, "typename": "科技"},
                 {"arcurl": "https://www.bilibili.com/video/BV1yy411c7mE",
                  "bvid": "BV1yy411c7mE", "title": "ZK 电路实践",
                  "description": "", "pubdate": 0, "author": "另一UP"},
             ]},
}, ensure_ascii=False)


class TestGdelt(unittest.TestCase):
    def setUp(self) -> None:
        self.r = parse_articles(GDELT_BODY)

    def test_counts_and_skips(self) -> None:
        self.assertEqual(self.r.raw_count, 5)
        self.assertEqual(len(self.r.candidates), 2)      # 重复 1、无链接 1、无标题 1 被剔掉
        self.assertFalse(self.r.blocked)
        self.assertIn("重复 1", self.r.detail)

    def test_title_entities_decoded(self) -> None:
        self.assertEqual(self.r.candidates[0].title, "Zcash & privacy")

    def test_seendate_to_local_iso(self) -> None:
        iso = self.r.candidates[0].extra["published_iso"]
        self.assertEqual(datetime.fromisoformat(iso).astimezone(timezone.utc).hour, 12)
        self.assertTrue(self.r.candidates[0].published_hint.startswith("2026-10-1"))

    def test_domain_stripped(self) -> None:
        self.assertEqual(self.r.candidates[1].extra["domain"], "example.cn")

    def test_bad_seendate_not_guessed(self) -> None:
        # 非法格式必须留空，不许瞎给时间
        self.assertIsNone(parse_seendate("2026-10-18T12:00:00Z"))
        self.assertIsNone(parse_seendate(""))

    def test_rate_limit_text(self) -> None:
        r = parse_articles("Please limit requests to one every 5 seconds")
        self.assertTrue(r.blocked)
        self.assertEqual(r.block_kind, "rate_limit")

    def test_empty_articles_is_not_blocked(self) -> None:
        r = parse_articles('{"articles": []}')
        self.assertFalse(r.blocked)          # 冷门词 0 条是结论，不是失败
        self.assertEqual(r.block_kind, "empty")

    def test_broken_json(self) -> None:
        r = parse_articles('{"articles": [')
        self.assertTrue(r.blocked)
        self.assertEqual(r.block_kind, "unknown_format")


class TestBilibili(unittest.TestCase):
    def setUp(self) -> None:
        self.r = parse_result(BILI_BODY, page=2)

    def test_rows(self) -> None:
        self.assertEqual(len(self.r.candidates), 2)
        self.assertEqual(self.r.raw_count, 1000)
        self.assertFalse(self.r.blocked)

    def test_title_highlight_stripped(self) -> None:
        self.assertEqual(self.r.candidates[0].title, "零知识证明入门（上）")

    def test_snippet_quotes_decoded(self) -> None:
        self.assertEqual(self.r.candidates[0].snippet, '讲清楚 "证明" 与 "知识"的边界')

    def test_rank_continues_across_pages(self) -> None:
        self.assertEqual(self.r.candidates[0].rank, PAGE_SIZE + 1)   # 第 2 页从 21 起

    def test_pubdate(self) -> None:
        hint = self.r.candidates[0].published_hint
        self.assertRegex(hint, r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsNone(self.r.candidates[1].published_hint)        # pubdate=0 → 没有

    def test_rate_limited_code(self) -> None:
        r = parse_result(json.dumps({"code": -412, "message": "请求被拦截"}))
        self.assertTrue(r.blocked)
        self.assertEqual(r.block_kind, "rate_limit")

    def test_risk_control_code(self) -> None:
        r = parse_result(json.dumps({"code": -352, "message": "风控校验失败"}))
        self.assertEqual(r.block_kind, "challenge")

    def test_null_result(self) -> None:
        body = json.dumps({"code": 0, "data": {"result": None, "numResults": 0}})
        r = parse_result(body)
        self.assertFalse(r.blocked)
        self.assertEqual(r.block_kind, "empty")

    def test_non_json(self) -> None:
        r = parse_result("<!DOCTYPE html><html>412</html>")
        self.assertEqual(r.block_kind, "unknown_format")


class TestStripTags(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(strip_tags('红黑树<em class="keyword">左旋</em>'), "红黑树左旋")

    def test_entities_decoded_after_stripping(self) -> None:
        # 先还原实体会把 &lt;script&gt; 变成真标签再被剥掉 —— 顺序必须是先剥后还原
        self.assertEqual(strip_tags("a &lt; b"), "a < b")

    def test_lt_in_text_not_eaten(self) -> None:
        self.assertEqual(strip_tags("若 a < b 且 b > c 则"), "若 a < b 且 b > c 则")


class TestFetchOverride(unittest.TestCase):
    """通道级 fetch 覆盖（timeout / retry）。

    起因：GDELT 单次 13~20s，全局 20s×3 次重试把 --no-body 快速任务拖到 56s；
    但全局收紧超时又会饿死慢站 —— 超时是通道属性，不是全局属性。
    """

    def _make_cfg(self, ch_raw: dict):
        from kwsearch.config import AppConfig, ChannelConfig

        ch = ChannelConfig(
            id=ch_raw["id"], name=ch_raw.get("name", ch_raw["id"]),
            type=ch_raw.get("type", "structured_api"), parser=ch_raw.get("parser", "gdelt"),
            url_template=ch_raw.get("url_template", "https://x/?q={q}"),
            fetch_over=ch_raw.get("fetch") or {},
        )
        return AppConfig(
            version=1,
            defaults={"timeout": 20, "retry": {"max_attempts": 3, "backoff_base": 1.5}},
            channels=[ch], crawl={}, extract={}, filter={}, limits={},
        )

    def _ctx(self, ch_raw: dict):
        from kwsearch.discover.base import build_context

        return build_context(self._make_cfg(ch_raw), ch_raw["id"], fetcher=object())

    def test_channel_override_wins(self) -> None:
        ctx = self._ctx({"id": "gdelt",
                         "fetch": {"timeout": 10, "retry": {"max_attempts": 2}}})
        self.assertEqual(ctx.timeout, 10)
        self.assertEqual(ctx.policy.max_attempts, 2)
        self.assertEqual(ctx.policy.backoff_base, 1.5)   # 没覆盖的键沿用全局

    def test_no_override_uses_defaults(self) -> None:
        ctx = self._ctx({"id": "bing_html"})
        self.assertEqual(ctx.timeout, 20)
        self.assertEqual(ctx.policy.max_attempts, 3)

    def test_real_config_gdelt_is_tight(self) -> None:
        # 真实 channels.yaml 的守护测试：谁把 GDELT 的超时改回全局默认，这里就红
        from kwsearch.config import load_config
        from kwsearch.discover.base import build_context

        cfg = load_config()
        ctx = build_context(cfg, "gdelt", fetcher=object())
        self.assertEqual(ctx.timeout, 10)
        self.assertEqual(ctx.policy.max_attempts, 2)
        # 慢站通道不受影响，仍是全局默认
        ctx_bing = build_context(cfg, "bing_html", fetcher=object())
        self.assertEqual(ctx_bing.timeout, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
