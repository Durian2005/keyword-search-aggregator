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
from kwsearch.fetch import BaseFetcher, FetchError, FetchResult  # noqa: E402
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


def fake_single(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """单次配额通道（channels.yaml 里 variants: false）：只该收到基词一次。"""
    return _mk(_SERP_ITEMS[:2], "fake_single", keyword)


# 搜狗微信场景：中转链 `link?url=<token>` 的 token 每次请求都轮换，
# URL 每次都变，但 dedupe_key（公众号+标题+日期）恒定。
_ROTATE = {"n": 0}


def fake_rotating(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    _ROTATE["n"] += 1
    tok = _ROTATE["n"]
    return ChannelResult(channel="fake_rotating", query=keyword, candidates=[
        Candidate(
            url_raw=f"https://s.example.com/link?url=TOK{tok}",
            title="红黑树 原理详解",
            snippet="红黑树是一种自平衡二叉查找树",
            channel="fake_rotating", rank=1, published_hint="2026-10-01",
            dedupe_key="fixed-article-key-1",
            extra={"domain": "s.example.com"},
        ),
    ], raw_count=1)


discover._REGISTRY["fake_rotating"] = fake_rotating


discover._REGISTRY["fake_serp"] = fake_serp
discover._REGISTRY["fake_zero"] = fake_zero
discover._REGISTRY["fake_blocked"] = fake_blocked
discover._REGISTRY["fake_paged"] = fake_paged
discover._REGISTRY["fake_compound"] = fake_compound
discover._REGISTRY["fake_single"] = fake_single


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


def fake_person_noname(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """站内检索把"机构页面"也带回来了：机构对得上，但整页没有这个人名。"""
    return _mk([
        ("https://v1.example.edu.cn/1", "张军-甲大学计算机学院 教授",
         "张军，研究方向网络安全", "v1.example.edu.cn"),
        ("https://v2.example.edu.cn/2", "甲大学计算机学院师资队伍",
         "本院现有专任教师 120 人，其中教授 30 人", "v2.example.edu.cn"),
    ], "fake_person_noname", keyword)


def fake_baidu_person(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """百度（单人单次配额）：人名模式下只该收到「姓名 site:域」这一发。

    返回的是**只有站内检索才捞得到**的目标人物深页 —— 裸姓名池里没有它
    （2026-10-21 张军 + 计算机学院实测同款场景：site: 第一条就是本人主页）。
    """
    return _mk([
        ("https://u1.example.edu.cn/deep/1", "【最美教师】张军教授：信息化浪潮席卷生活",
         "张军，男，教授，研究方向网络安全与保密", "u1.example.edu.cn"),
    ], "baidu_html", keyword)


def fake_namesake(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """同名者：页面有姓名、没有机构锚点，域名也归不到任何机构（平台域/无关域）。"""
    return _mk([
        ("https://ent.example.com/1", "张军（歌手）_百科", "流行歌手，代表作……",
         "ent.example.com"),
        ("https://other.example.org/2", "张军 教授", "某大学教师", "other.example.org"),
    ], "fake_namesake", keyword)


discover._REGISTRY["fake_person"] = fake_person
discover._REGISTRY["fake_person_noname"] = fake_person_noname
discover._REGISTRY["fake_baidu_person"] = fake_baidu_person
discover._REGISTRY["fake_namesake"] = fake_namesake


def fake_listpage(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """返回一个栏目列表页 + 一条普通文章候选。

    列表页仿计算机学院官网同款形态（/xsjs/list38.htm），页内锚文本里
    藏着两条含关键词的文章 —— 只有抓下列表页才拿得到。
    摘要里带关键词：真实百度返回列表页时，摘要引用的正是页内匹配片段。
    """
    return _mk([
        ("https://u1.example.edu.cn/xsjs/list38.htm", "学术动态",
         "红黑树系列讲座公告栏目，含历年讲座安排", "u1.example.edu.cn"),
        ("https://u2.example.edu.cn/2021/0525/c282a3495/page.htm",
         "红黑树讲座顺利开讲", "本次讲座介绍红黑树原理与旋转操作", "u2.example.edu.cn"),
    ], "fake_listpage", keyword)


def fake_listpage_dead(keyword: str, offset: int, ctx) -> ChannelResult:  # noqa: ANN001
    """列表页摘要含关键词（所以能进池），但页内锚文本没有一个含关键词：
    这才是「解析不出文章」的场景 —— 丢弃并说明原因。"""
    return _mk([
        ("https://u1.example.edu.cn/677/list3.htm", "院系风采",
         "红黑树相关活动的院系展示栏目", "u1.example.edu.cn"),
    ], "fake_listpage_dead", keyword)


discover._REGISTRY["fake_listpage"] = fake_listpage
discover._REGISTRY["fake_listpage_dead"] = fake_listpage_dead


# --------------------------------------------------------------- 假 fetcher
class _FakeFetcher(BaseFetcher):
    """按 URL 查表返回；表外一律 404（robots.txt 也走这条 → 视为无限制）。"""

    name = "fake"

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20) -> FetchResult:
        self.calls.append(url)
        body = self.pages.get(url)
        if body is None:
            raise FetchError("404 not found", status=404, kind="http")
        raw = body.encode("utf-8")
        return FetchResult(
            status=200, body=body, raw=raw,
            content_type="text/html; charset=utf-8",
            final_url=url, content_bytes=len(raw), fetcher=self.name,
        )


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
  max_per_domain: {max_per_domain}
  drop_zero_hit: {drop_zero}
limits:
  max_docs: {max_docs}
  max_minutes: 5
extract:
  summary_chars: 100
  challenge_keywords: ["安全验证"]
  # 这里的通道全是假的（fake.test），没有真实页面可抓。
  # 不关掉的话会在每条候选上真的发一次 DNS 超时，把 1 秒的单测拖到 17 秒。
  # 列表页解析测试显式传 fetch_body: true，并注入假 fetcher。
  fetch_body: {fetch_body}
crawl: {crawl}
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

# 单次配额通道（百度 2026-10-21 实测：第二次请求就出图形验证码）：
# variants: false 的 serp 通道必须只打基词一次，走 _run_paged 而不是 _run_serp。
_SINGLE_CH = """
  - id: fake_single
    type: serp
    parser: fake_single
    url_template: "https://fake.test/single?q={q}"
    variants: false
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

# 站内检索（site_search）：没有"标题必须含关键词"这层过滤，
# 机构页面能一路走到归因层 —— 正是 name_missing 要处理的情形。
_PERSON_NONAME_CH = """
  - id: fake_person_noname
    type: site_search
    parser: fake_person_noname
    url_template: "https://fake.test/n?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# 百度（id 必须是 baidu_html：pipeline 按 id 认出"认 site: 的单发通道"）。
# variants: false = 单人单次配额，第 2 请求就出安全验证（2026-10-21 实测）。
_PERSON_BAIDU_CH = """
  - id: baidu_html
    type: serp
    parser: fake_baidu_person
    url_template: "https://fake.test/bd?q={q}"
    variants: false
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# serp 型但永远被拦：验机构探测"被拦即收手"。
# 注意不能带 variants:false —— 单发配额通道不参与探测（那是另一条规则），
# 这个通道要验的恰恰是"探测被拦"这条路径。
_ROTATING_CH = """
  - id: fake_rotating
    type: serp
    parser: fake_rotating
    url_template: "https://fake.test/rot?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

_BLOCKED_SERP_CH = """
  - id: fake_blocked_serp
    type: serp
    parser: fake_blocked
    url_template: "https://fake.test/bs?q={q}"
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# 同名者通道：探不到官网域（结果全是平台域/无关域），用于验"机构未命中"告警
_PERSON_NAMESAKE_CH = """
  - id: fake_namesake
    type: serp
    parser: fake_namesake
    url_template: "https://fake.test/ns?q={q}"
    variants: false
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# 列表页通道：候选里混着栏目页（list38.htm 同款形态）
_LISTPAGE_CH = """
  - id: fake_listpage
    type: serp
    parser: fake_listpage
    url_template: "https://fake.test/lp?q={q}&first={offset}"
    paging: {mode: offset, param: first, start: 1, step: 10, max_pages: 1}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# 死列表页通道：页内没有任何含关键词的文章
_LISTPAGE_DEAD_CH = """
  - id: fake_listpage_dead
    type: serp
    parser: fake_listpage_dead
    url_template: "https://fake.test/lpd?q={q}&first={offset}"
    paging: {mode: offset, param: first, start: 1, step: 10, max_pages: 1}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# 单发配额 serp 通道（variants:false）：机构探测不许打它，子弹留给主查询
_SINGLE_PROBE_CH = """
  - id: fake_single_probe
    type: serp
    parser: fake_serp
    url_template: "https://fake.test/sp?q={q}"
    variants: false
    paging: {mode: none}
    throttle: {min_interval_s: 0}
    assertions: {min_items: 1, required_fields: [url, title]}
"""

# 列表页样本：两条锚文本含「红黑树」的文章 + 一条不含 + 一条导航
_LIST_HTML = """<html><head><title>学术动态</title></head><body>
<div class="list">
  <a href="/2019/0426/c90a139/page.htm">红黑树十周年：算法思想的信息化浪潮</a>
  <a href="/2025/0508/c676a19624/page.htm">激发创新活力，校园大赛决赛圆满落幕</a>
  <a href="/2021/0525/c282a3495/page.htm">红黑树首次全校公开课顺利开讲</a>
</div>
<nav><a href="/">首页</a><a href="/sitemap.htm">网站地图</a></nav>
</body></html>"""

# 文章正文页（够长，能过 min_body_chars，顺带验正文抽取不受影响）
_ARTICLE_HTML = """<html><head><title>红黑树公开课</title>
<script type="application/ld+json">{"datePublished":"2021-05-25T10:00:00+08:00"}</script>
</head><body><div class="article-body">
<p>红黑树是一种自平衡二叉查找树，本次公开课系统讲解它的五条性质与旋转操作。</p>
<p>课堂上演示了插入与删除过程中的左旋右旋，以及变色如何维持黑高平衡。</p>
<p>听课师生就工程实践中 map 与 set 的红黑树实现进行了提问与讨论。</p>
</div></body></html>"""

# 死列表页：锚文本里没有任何关键词
_LIST_HTML_DEAD = """<html><head><title>院系风采</title></head><body>
<div class="list">
  <a href="/a/1/page.htm">计算机学院简介</a>
  <a href="/a/2/page.htm">管理学院简介</a>
</div>
<nav><a href="/">首页</a></nav>
</body></html>"""


class RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cfg(self, channels: str, max_docs: int = 20, *,
            drop_zero_hit: bool = True, max_per_domain: int = 2,
            fetch_body: bool = False, crawl: str = "{}"):
        p = self.dir / "channels.yaml"
        p.write_text(_BASE.format(
            channels=channels, max_docs=max_docs,
            drop_zero="true" if drop_zero_hit else "false",
            max_per_domain=max_per_domain,
            fetch_body="true" if fetch_body else "false",
            crawl=crawl,
        ), encoding="utf-8")
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


class TestDedupeKeyAcrossRuns(RunCase):
    """同一篇文章换了 URL（搜狗的中转链 token 轮换）：跨跑批只能算一条。

    没有 dedupe_key 的通道仍靠 URL 去重（TestIdempotency 已覆盖），
    这里验的是"URL 变了但业务键没变"这一种。
    """

    def setUp(self) -> None:
        super().setUp()
        _ROTATE["n"] = 0

    def test_second_run_reuses_existing_doc(self) -> None:
        cfg = self.cfg(_ROTATING_CH)
        first = self.run_kw(cfg)
        # 不管这一跑触发了几个变体（每次都换 URL），doc 表只应有一篇
        self.assertEqual(first.docs_new, 1)

        second = self.run_kw(cfg)
        self.assertEqual(second.docs_new, 0)              # 不再算"新发现"
        self.assertGreaterEqual(second.dropped_dup, 1)    # 业务键命中库里已有条目
        self.assertGreaterEqual(second.kept, 1)           # 仍算本任务的一条结果

        docs = self.rows("SELECT url_canon, dedupe_key FROM doc")
        self.assertEqual(len(docs), 1)                    # 库里只有一行
        self.assertEqual(docs[0]["dedupe_key"], "fixed-article-key-1")
        # 所有 mention 指向同一篇（第二次没有新建 doc）
        urls = self.rows("SELECT DISTINCT url_canon FROM mention")
        self.assertEqual(len(urls), 1)

    def test_dedupe_key_persisted_on_first_insert(self) -> None:
        self.run_kw(self.cfg(_ROTATING_CH))
        row = self.rows("SELECT dedupe_key FROM doc")[0]
        self.assertEqual(row["dedupe_key"], "fixed-article-key-1")


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
        qs = self.rows("SELECT query_text FROM query WHERE channel='fake_person'"
                       " ORDER BY seq")
        # 裸姓名之外只有机构域探测（负 seq，排在前面）：它不发"姓名+词"，
        # 没有降级风险；探测只用来推导官网域，候选不进文档池。
        self.assertEqual([q["query_text"] for q in qs],
                         ["[机构探测] 甲大学计算机学院", "张军"])

    def test_no_anchor_alerts(self) -> None:
        p = parse_person("张军")          # 没有机构/方向锚点
        r = self.run_kw(self.cfg(_PERSON_CH), keyword="张军", person=p)
        kinds = [x["kind"] for x in
                 self.rows("SELECT kind FROM alert WHERE task_id=?", r.task_id)]
        self.assertIn("person_no_anchor", kinds)
        # 无锚点也要出卡片，但只能是"未匹配机构"
        labels = [x["label"] for x in self.rows("SELECT label FROM person_card")]
        self.assertEqual(labels, ["未匹配机构"])

    def test_name_missing_counted_and_alerted(self) -> None:
        """机构页面（没有这个人名）不进卡片，但必须记账 —— 否则像"一条都没找到"。

        drop_zero_hit 默认是开的，这类页面通常更早一步就被零命中丢弃；
        这里关掉它，验证归因层自己也会把关并留下账目（用户改配置时的兜底）。
        """
        p = parse_person("张军", ["甲大学计算机学院"], ["教授"])
        r = self.run_kw(self.cfg(_PERSON_NONAME_CH, drop_zero_hit=False),
                        keyword="张军", person=p)
        self.assertEqual(r.kept, 2)
        self.assertEqual(r.person_cards, 1)          # 只有真提到姓名的那条进卡片
        rows = self.rows("SELECT label, docs FROM person_card")
        self.assertEqual([(x["label"], x["docs"]) for x in rows],
                         [("甲大学计算机学院", 1)])
        alerts = self.rows("SELECT kind, detail FROM alert WHERE task_id=?", r.task_id)
        hit = [a for a in alerts if a["kind"] == "person_name_missing"]
        self.assertTrue(hit)
        self.assertIn("1 条", hit[0]["detail"])


class TestPersonOrgScoping(RunCase):
    """2026-10-21：机构锚点从"只打分"变成"也检索"。

    病因（张军 + 某某大学计算机学院实测）：裸姓名池被名人同名者占满
    （23 条里 0 条目标机构），机构锚点没有配套检索动作就永远打空，
    报告于是只剩一堆同名者。修复三件套：
      1. 官网域探测：先搜机构名拿官网域（Bing，候选不进池）
      2. 百度站内检索：「姓名 site:域」**替代**裸姓名（单人单次配额）
      3. 域名归因证据：页面在机构官网域上 = 强锚点（中文站通篇"我校"，
         机构全称常常一个字都不出现）
    """

    def _both(self, **kw):
        p = parse_person("张军", ["甲大学计算机学院"], ["教授", "网络安全"])
        return p, self.run_kw(self.cfg(_PERSON_CH + _PERSON_BAIDU_CH,
                                        max_per_domain=5, **kw),
                              keyword="张军", person=p)

    def test_site_query_replaces_bare_name_on_baidu(self) -> None:
        p, r = self._both()
        qs = self.rows("SELECT query_text FROM query WHERE channel='baidu_html'")
        # 只此一发，且必须是 site: 形态；裸姓名在百度侧是浪费（池子 Bing 已有）
        self.assertEqual([q["query_text"] for q in qs],
                         ["张军 site:example.edu.cn"])

    def test_domain_evidence_confirms_deep_page(self) -> None:
        p, r = self._both()
        cards = {c["label"]: c for c in
                 self.rows("SELECT label, tier, docs FROM person_card")}
        card = cards.get("甲大学计算机学院")
        self.assertIsNotNone(card, "官网域上的目标人物必须成卡")
        self.assertEqual(card["tier"], "confirmed")
        # 文本命中 2 条（u1/u2）+ 域名命中 1 条（深页，标题没有机构全称）
        self.assertEqual(card["docs"], 3)
        tiers = {t: n for t, n in r.person_tiers.items() if n}
        self.assertEqual(tiers.get("confirmed"), 1)
        # 域名命中的证据要标注出来：看的人能分清"页面写了校名"和"站在人家网站上"
        ev = self.rows("SELECT evidence_json FROM person_card"
                       " WHERE label='甲大学计算机学院'")[0]["evidence_json"]
        self.assertIn("（官网域名）", ev)
        self.assertIn("最美教师", ev)

    def test_no_org_no_probe_no_override(self) -> None:
        p = parse_person("张军", [], ["教授"])       # 没有机构锚点
        r = self.run_kw(self.cfg(_PERSON_CH + _PERSON_BAIDU_CH, max_per_domain=5),
                        keyword="张军", person=p)
        qs = self.rows("SELECT query_text FROM query WHERE channel='fake_person'")
        self.assertEqual([q["query_text"] for q in qs], ["张军"])
        qs = self.rows("SELECT query_text FROM query WHERE channel='baidu_html'")
        self.assertEqual([q["query_text"] for q in qs], ["张军"])   # 照旧裸姓名

    def test_org_probe_blocked_stops_discovery(self) -> None:
        # 机构查询被拦：探测整体收手，绝不多打（项目铁律：不加重风控）
        p = parse_person("张军", ["甲大学计算机学院"])
        r = self.run_kw(self.cfg(_BLOCKED_SERP_CH), keyword="张军", person=p)
        qs = self.rows("SELECT query_text, blocked FROM query"
                       " WHERE channel='fake_blocked_serp' ORDER BY seq")
        self.assertEqual([q["query_text"] for q in qs],
                         ["[机构探测] 甲大学计算机学院", "张军"])
        self.assertEqual(qs[0]["blocked"], 1)
        self.assertEqual(r.kept, 0)

    def test_org_unmatched_alerted(self) -> None:
        # 给了机构却一条都没命中：必须留下"为什么没有"的告警 ——
        # 只显示"候选人 0 个"的话，用户会以为这人没上网
        # （2026-10-21 张军实测：23 条同名者，0 条目标机构）
        p = parse_person("张军", ["甲大学计算机学院"])
        r = self.run_kw(self.cfg(_PERSON_NAMESAKE_CH), keyword="张军", person=p)
        hit = [a for a in
               self.rows("SELECT kind, detail FROM alert WHERE task_id=?", r.task_id)
               if a["kind"] == "person_org_unmatched"]
        self.assertTrue(hit, "机构未命中必须告警")
        self.assertIn("同名者", hit[0]["detail"])
        # 没有机构命中 → 唯一一张卡是折叠的"未匹配机构"
        labels = [c["label"] for c in self.rows("SELECT label FROM person_card")]
        self.assertEqual(labels, ["未匹配机构"])

    def test_org_text_hit_no_unmatched_alert(self) -> None:
        # 文本命中机构时不出未命中告警（探到域并做了站内检索，更不算"未尝试"）
        p = parse_person("张军", ["甲大学计算机学院"])
        r = self.run_kw(self.cfg(_PERSON_CH), keyword="张军", person=p)
        kinds = [a["kind"] for a in
                 self.rows("SELECT kind FROM alert WHERE task_id=?", r.task_id)]
        self.assertNotIn("person_org_unmatched", kinds)


class TestListResolution(RunCase):
    """2026-10-21：栏目列表页不当结果，但它链着的相关文章是。

    用户原话：「结合上个版本的爬取结果，我只是增加要求尽量能够跳转到原文章」——
    所以处理方式是**抓下列表页、把含关键词的文章捞出来当结果**（链接直指正文），
    而不是把整页丢掉了事（丢页 = 把页里的文章一起丢）。百度 site: 查询返回
    列表页的原因就是列表里链着目标文章。
    """

    def _pages(self) -> dict[str, str]:
        base = "https://u1.example.edu.cn"
        return {
            f"{base}/xsjs/list38.htm": _LIST_HTML,
            f"{base}/2019/0426/c90a139/page.htm": _ARTICLE_HTML,
            f"{base}/2021/0525/c282a3495/page.htm": _ARTICLE_HTML,
            "https://u2.example.edu.cn/2021/0525/c282a3495/page.htm": _ARTICLE_HTML,
        }

    def test_list_page_resolved_to_articles(self) -> None:
        fake = _FakeFetcher(self._pages())
        r = self.run_kw(self.cfg(_LISTPAGE_CH, fetch_body=True,
                                 crawl="{robots_obey: false, per_domain_qps: 0}"),
                        keyword="红黑树", fetcher=fake)
        # 列表页里的 2 篇相关文章进池；不含关键词的那条锚文本不进
        self.assertEqual(r.resolved_list, 2)
        docs = {d["url_raw"]: d["title"] for d in
                self.rows("SELECT url_raw, title FROM doc")}
        # 列表页本身不在库里；它的文章在，且标题是锚文本（点开就是正文）
        self.assertNotIn("https://u1.example.edu.cn/xsjs/list38.htm", docs)
        self.assertIn("红黑树十周年：算法思想的信息化浪潮", docs.values())
        self.assertIn("红黑树首次全校公开课顺利开讲", docs.values())
        # 来源通道的普通候选不受影响
        self.assertIn("红黑树讲座顺利开讲", docs.values())
        # 告警说清"哪个列表页解析出了什么"
        hit = [a for a in
               self.rows("SELECT kind, detail FROM alert WHERE task_id=?", r.task_id)
               if a["kind"] == "resolved"]
        self.assertTrue(hit, "列表页解析必须记账")
        self.assertIn("list38.htm", hit[0]["detail"])

    def test_dead_list_page_dropped_with_reason(self) -> None:
        # 页内没有含关键词的文章：丢弃，但原因要写进告警（不能静默消失）
        fake = _FakeFetcher({"https://u1.example.edu.cn/677/list3.htm": _LIST_HTML_DEAD})
        r = self.run_kw(self.cfg(_LISTPAGE_DEAD_CH, fetch_body=True,
                                 crawl="{robots_obey: false, per_domain_qps: 0}"),
                        keyword="红黑树", fetcher=fake)
        self.assertEqual(r.resolved_list, 0)
        self.assertEqual(r.kept, 0)
        hit = [a for a in
               self.rows("SELECT kind, detail FROM alert WHERE task_id=?", r.task_id)
               if a["kind"] == "dropped"]
        self.assertTrue(hit, "解析不出文章的列表页必须记账")
        self.assertIn("栏目列表页", hit[0]["detail"])
        self.assertIn("没有含关键词的文章", hit[0]["detail"])

    def test_no_body_mode_drops_list_page(self) -> None:
        # --no-body：一个请求都不发，列表页维持丢弃（不能凭空解析）
        r = self.run_kw(self.cfg(_LISTPAGE_CH, fetch_body=False),
                        keyword="红黑树")
        self.assertEqual(r.resolved_list, 0)
        urls = [d["url_raw"] for d in self.rows("SELECT url_raw FROM doc")]
        self.assertNotIn("https://u1.example.edu.cn/xsjs/list38.htm", urls)

    def test_single_shot_channel_skipped_by_org_probe(self) -> None:
        # 单发配额通道（variants:false）不参与机构探测：那一发子弹必须留给
        # 主查询的「姓名 site:官网域」。探测打它 = 用子弹换 Bing 已能问到的域名
        # （2026-10-21 实测：探测打百度 → 主查询必吃验证码）
        p = parse_person("张军", ["甲大学计算机学院"])
        self.run_kw(self.cfg(_SINGLE_PROBE_CH), keyword="张军", person=p)
        qs = [q["query_text"] for q in
              self.rows("SELECT query_text FROM query WHERE channel='fake_single_probe'")]
        self.assertEqual(qs, ["张军"])       # 只有主查询，没有 [机构探测]


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


class TestSingleQuotaChannel(RunCase):
    """variants: false 的 serp 通道（百度式单次配额）只打基词一次。"""

    def test_single_channel_gets_base_query_only(self) -> None:
        r = self.run_kw(self.cfg(_SINGLE_CH))
        qs = self.rows("SELECT * FROM query WHERE task_id=? AND channel='fake_single'",
                       r.task_id)
        self.assertEqual(len(qs), 1)                  # 一次请求，没有变体
        self.assertEqual(qs[0]["query_text"], "红黑树")
        self.assertEqual(qs[0]["got_count"], 2)

    def test_variants_channel_still_expands(self) -> None:
        # 对照组：同一份配置里普通 serp 照旧走变体矩阵，开关没有全局副作用
        cfg = self.cfg(_SERP_CH + _SINGLE_CH)
        r = self.run_kw(cfg)
        serp_qs = self.rows("SELECT * FROM query WHERE task_id=? AND channel='fake_serp'",
                            r.task_id)
        single_qs = self.rows("SELECT * FROM query WHERE task_id=? AND channel='fake_single'",
                              r.task_id)
        self.assertGreaterEqual(len(serp_qs), 2)      # 基词 + 变体
        self.assertEqual(len(single_qs), 1)           # 仍然只有基词
        self.assertTrue(all(q["query_text"] == "红黑树" for q in single_qs))


if __name__ == "__main__":
    unittest.main()