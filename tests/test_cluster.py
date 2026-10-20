"""S5 转载归并的测试：指纹质量、归并规则、入库账目、报告折叠。

这一层最容易"看起来对、其实错"：簇只要不出异常，报告就会安静地少几行或多几行。
所以测试集中在三件事上：
  1) 指纹对"同文异标题 / 加模板 / 改标点"不敏感，对"不同文章"敏感 —— 用真实文本卡死；
  2) 归并只按代表比，不做传递闭包（否则一篇通稿能把半个语料连成一簇）；
  3) 账目恒等式：入库 N 条 = 各组条数之和，任何一条都不能凭空消失。
"""
from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kwsearch.aggregate.cluster import (  # noqa: E402
    ClusterInput,
    cluster_docs,
    from_signed64,
    hamming64,
    normalize_text,
    simhash64,
    to_signed64,
)

# --------------------------------------------------------------- 语料
# 一篇 ~500 字的文章 + 它的几种"转载形态"。全用真实中文，不用 lorem ipsum：
# 随机字符的 3-gram 分布和中文完全不同，用假语料测出来的阈值没有参考价值。
ART = """红黑树是一种自平衡的二叉查找树，它在每个节点上增加了一个存储位表示节点的颜色，
可以是红色或者黑色。通过对任何一条从根到叶子的路径上各个节点的颜色进行约束，红黑树
确保没有一条路径会比其他路径长出两倍，因而是接近平衡的。每个节点要么是红色，要么是
黑色；根节点是黑色；每个叶子节点是黑色的空节点；如果一个节点是红色的，那么它的两个
子节点都是黑色的；从任一节点到其每个叶子的所有路径都包含相同数目的黑色节点。这些约束
强制了红黑树的关键性质：从根到叶子的最长的可能路径不多于最短的可能路径的两倍长。
结果是这个树大致上是平衡的，因为插入删除和查找某个值的最坏情况时间都要求与树的高度
成比例，这个高度理论上限允许红黑树在最坏情况下都是高效的。红黑树的操作有着良好的最坏
情况运行时间，它可以在对数时间内做查找插入和删除。左旋和右旋是最基本的两个操作，
它们只改变树的局部结构而不破坏中序遍历的顺序，理解这一点是手写红黑树的关键。"""

# 转载最常见的形态：标题换成自己的、正文前加引导语、后面挂一段免责声明
TEMPLATE = ("\n\n【免责声明】本文仅代表作者本人观点，与本网站无关。本站对文中陈述、"
            "观点判断保持中立，不对所包含内容的准确性、可靠性或完整性提供任何明示或"
            "暗示的保证。请读者仅作参考，并请自行承担全部责任。如有侵权请联系我们删除。"
            "扫描下方二维码关注公众号获取更多技术干货。")

OTHER = """咖啡豆的烘焙曲线决定了最终的风味走向。浅烘保留更多果酸与花香，适合手冲；
中烘平衡度好，适合滴滤与意式；深烘则突出焦糖与坚果调性，常用于浓缩。烘焙过程中
一爆与二爆是两个关键节点，前者代表水分大量排出，后者代表油脂开始迁移到表面。
控制升温速率比控制总时长更重要，因为美拉德反应与焦糖化反应在不同的温度区间里
占据主导，升温过快会让外表焦黑而内部夹生。同一支豆子用不同的曲线会得到完全不同
的杯中表现，这也是烘焙师最核心的手艺所在。生豆的含水率、密度与目数都会影响
计划曲线的设计，含水率高的豆子需要更长的脱水期，密度大的豆子则更耐热、可以
在后期给到更高的火力。烘焙完成后的排气也很关键，意式用的豆子通常只养几天，
手冲用的豆子反而要养到一周以上风味才稳定。"""


def _item(url: str, text: str, *, title: str = "", body: int | None = None,
          when: str | None = None) -> ClusterInput:
    return ClusterInput(
        url_canon=url, simhash=simhash64(text) or None,
        body_chars=len(normalize_text(text)) if body is None else body,
        title=title or url, domain=url.split("/")[2], published_at=when,
    )


# --------------------------------------------------------------- 指纹质量
class TestSimhash(unittest.TestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(simhash64(ART), simhash64(ART))

    def test_stable_across_processes(self) -> None:
        """指纹必须是跨进程稳定的常量。

        锁死这个值不是洁癖：库里的 doc.simhash 是**持久化**的，
        一旦算法变了（哪怕只是换了哈希函数），新算的指纹和库里旧的
        就不在同一个尺度上，汉明距离全变假 —— 而报告会照常输出结论。
        改算法时必须同时清空 doc.simhash 并更新这个常量。
        """
        text = "红黑树的插入与删除都需要旋转和变色，这保证了最坏情况下依然是对数复杂度。"
        self.assertEqual(simhash64(text), 15962176677458494982)

    def test_identical_text_is_zero_distance(self) -> None:
        self.assertEqual(hamming64(simhash64(ART), simhash64(ART)), 0)

    def test_punctuation_and_width_ignored(self) -> None:
        """全角/半角、空格、标点差异不该影响指纹 —— 转载站最爱改这些。"""
        variant = ART.replace("，", ",").replace("。", "。 ").replace("红黑树", "红黑树 ")
        self.assertLessEqual(hamming64(simhash64(ART), simhash64(variant)), 1)

    def test_reprint_with_disclaimer_still_close(self) -> None:
        """同文 + 180 字免责声明 = 转载最标准的样子。这条卡住阈值不能调回 3。"""
        d = hamming64(simhash64(ART), simhash64(ART + TEMPLATE))
        self.assertLessEqual(d, 10)
        self.assertGreater(d, 3)       # 证明"照抄教程用 3"会漏掉这种常见情况

    def test_different_articles_far_apart(self) -> None:
        d = hamming64(simhash64(ART), simhash64(OTHER))
        self.assertGreater(d, 20)

    def test_short_or_empty_text_is_zero(self) -> None:
        """太短的文本算不出指纹，必须返回 0（= 没有指纹），不是一个"和谁都像"的值。"""
        self.assertEqual(simhash64(""), 0)
        self.assertEqual(simhash64("红黑树"), 0)

    def test_signed_storage_roundtrip(self) -> None:
        """SQLite 只有 int64，最高位为 1 的指纹必须按补码存取，64 位一位不丢。"""
        h = simhash64("红黑树的插入与删除都需要旋转和变色，这保证了最坏情况下依然是对数复杂度。")
        self.assertGreaterEqual(h, 1 << 63)        # 这条正好落在"存不进 int64"的区间
        self.assertEqual(from_signed64(to_signed64(h)), h)
        self.assertLess(to_signed64(h), 0)

    def test_negative_value_hamming(self) -> None:
        """库里读出来的负数必须先还原，否则 bit_count 只有 1 位，一切都被判成相似。"""
        a = 0xFFFF_FFFF_FFFF_FFFF
        self.assertEqual(hamming64(from_signed64(-1), from_signed64(0)), 64)


# --------------------------------------------------------------- 归并规则
class TestClusterDocs(unittest.TestCase):
    def test_reprint_merged(self) -> None:
        items = [
            _item("https://a.test/1", ART, title="红黑树详解"),
            _item("https://b.test/2", ART + TEMPLATE, title="【转载】红黑树详解"),
            _item("https://c.test/3", ART),
        ]
        rep = cluster_docs(items)
        self.assertEqual(len(rep.clusters), 1)
        self.assertEqual(rep.clusters[0].member_count, 3)
        self.assertEqual(rep.merged, 2)

    def test_different_articles_not_merged(self) -> None:
        items = [_item("https://a.test/1", ART), _item("https://b.test/2", OTHER)]
        rep = cluster_docs(items)
        self.assertEqual(len(rep.clusters), 2)
        self.assertEqual(rep.merged, 0)

    def test_rep_is_longest_body(self) -> None:
        """代表取正文最长的那个：短的去和长的比，反了基准就是抖的。"""
        short = ART[:260]
        items = [
            _item("https://short.test/1", short),
            _item("https://long.test/2", ART + TEMPLATE),
        ]
        rep = cluster_docs(items, min_body_chars=200)
        c = rep.clusters[0]
        self.assertEqual(c.rep_url, "https://long.test/2")
        self.assertEqual(c.rep, c.members[0])

    def test_rep_is_original_not_the_reprint_with_disclaimer(self) -> None:
        """转载版挂着免责声明 → 原始长度更长，但内容一样多 → 代表必须是原文。

        这是"代表取正文最长"这条老规则的漏洞：多出来的那 130 字是模板，
        按字数选等于奖励转载。
        """
        body = len(normalize_text(ART))
        items = [
            ClusterInput(url_canon="https://origin.test/1", simhash=simhash64(ART),
                         body_chars=body, body_core=body, published_at="2026-03-01"),
            ClusterInput(url_canon="https://mirror.test/2",
                         simhash=simhash64(ART + TEMPLATE),
                         body_chars=body + 130, body_core=body,
                         published_at="2026-03-05"),
        ]
        rep = cluster_docs(items)
        self.assertEqual(len(rep.clusters), 1)
        self.assertEqual(rep.clusters[0].rep_url, "https://origin.test/1")

    def test_rep_without_core_falls_back_to_length(self) -> None:
        """老数据没有 body_core（补列后为 NULL → 0）时退回原始长度，行为不变。"""
        short = ART[:260]
        items = [
            ClusterInput(url_canon="https://short.test/1", simhash=simhash64(short),
                         body_chars=len(normalize_text(short))),
            ClusterInput(url_canon="https://long.test/2", simhash=simhash64(ART),
                         body_chars=len(normalize_text(ART))),
        ]
        self.assertEqual(cluster_docs(items).clusters[0].rep_url, "https://long.test/2")

    def test_rep_prefers_earlier_time_when_lengths_tie(self) -> None:
        """内容量、原始长度都一样时，发布更早的当代表（转载一般晚于原文）。"""
        n = len(normalize_text(ART))
        sh = simhash64(ART)
        items = [
            ClusterInput(url_canon="https://late.test/1", simhash=sh,
                         body_chars=n, body_core=n, published_at="2026-05-01"),
            ClusterInput(url_canon="https://early.test/2", simhash=sh,
                         body_chars=n, body_core=n, published_at="2026-03-01"),
        ]
        self.assertEqual(cluster_docs(items).clusters[0].rep_url,
                         "https://early.test/2")

    def test_untrusted_time_does_not_decide_rep(self) -> None:
        """时间不可信（站点构建时间）时不许拿它比先后。

        VitePress/Hugo 的页脚"最近更新"是全站同一个值，谁最近重建过谁就"更早"，
        跟原文是谁毫无关系 —— 只能退回 URL 兜底。
        """
        n = len(normalize_text(ART))
        sh = simhash64(ART)
        items = [
            ClusterInput(url_canon="https://a.test/1", simhash=sh,
                         body_chars=n, body_core=n, published_at="2026-05-01"),
            # 站点构建时间把它标成 3 月，但它不是原文
            ClusterInput(url_canon="https://b.test/2", simhash=sh,
                         body_chars=n, body_core=n, published_at="2026-03-01",
                         time_trusted=False),
        ]
        self.assertEqual(cluster_docs(items).clusters[0].rep_url, "https://a.test/1")


        """A~B、B~C 不能推出 A~C。

        构造法：让 B 成为 A 与 C 的中点（各差 6 位），阈值 10 时 A 和 C 相距 12。
        并查集会把三条并成一簇；贪心法只按"代表"比，所以最多并进两条。
        """
        base = 0x0F0F_0F0F_0F0F_0F0F
        mid = base ^ 0b111111                       # 距 a 6 位
        far = mid ^ 0b111111_000000                 # 距 b 6 位，距 a 12 位
        items = [
            ClusterInput(url_canon="https://a.test/1", simhash=base, body_chars=900),
            ClusterInput(url_canon="https://b.test/2", simhash=mid, body_chars=800),
            ClusterInput(url_canon="https://c.test/3", simhash=far, body_chars=700),
        ]
        rep = cluster_docs(items, hamming=10)
        # 代表是 a（正文最长）。b 距 a 6 位 → 并入；c 距 a 12 位 → 自成一组。
        sizes = sorted(c.member_count for c in rep.clusters)
        self.assertEqual(sizes, [1, 2])
        # 不变量：任意两个簇的代表之间都超过阈值
        self.assertGreater(hamming64(base, far), 10)

    def test_no_simhash_is_counted_not_grouped(self) -> None:
        items = [
            ClusterInput(url_canon="https://a.test/1", simhash=None, body_chars=900),
            ClusterInput(url_canon="https://b.test/2", simhash=None, body_chars=900),
        ]
        rep = cluster_docs(items)
        self.assertEqual(rep.clusters, [])
        self.assertEqual(rep.no_simhash, 2)
        self.assertEqual(rep.groups, 2)        # 各自算一组，不是"合成一组"

    def test_too_short_excluded(self) -> None:
        items = [_item("https://a.test/1", ART, body=120)]
        rep = cluster_docs(items, min_body_chars=200)
        self.assertEqual(rep.too_short, 1)
        self.assertEqual(rep.clusters, [])

    def test_accounting_identity(self) -> None:
        """入库 N 条 = 各簇成员数 + 无指纹 + 过短。少一条都会让报告对不上账。"""
        items = [
            _item("https://a.test/1", ART),
            _item("https://b.test/2", ART + TEMPLATE),
            _item("https://c.test/3", OTHER),
            ClusterInput(url_canon="https://d.test/4", simhash=None, body_chars=900),
            _item("https://e.test/5", ART, body=100),
        ]
        rep = cluster_docs(items)
        self.assertEqual(
            rep.member_total + rep.no_simhash + rep.too_short, len(items))
        self.assertEqual(rep.groups + rep.merged, len(items))

    def test_order_independent(self) -> None:
        """同一批输入换个顺序，分组结果必须一样（否则报告随抓取顺序抖动）。"""
        items = [
            _item("https://a.test/1", ART, when="2024-01-01"),
            _item("https://b.test/2", ART, when="2024-02-01"),
            _item("https://c.test/3", OTHER, when="2024-03-01"),
        ]
        base = cluster_docs(items)
        shuffled = list(items)
        random.Random(7).shuffle(shuffled)
        again = cluster_docs(shuffled)
        self.assertEqual(
            [(c.rep_url, c.member_count) for c in base.clusters],
            [(c.rep_url, c.member_count) for c in again.clusters],
        )

    def test_summary_line_readable(self) -> None:
        rep = cluster_docs([_item("https://a.test/1", ART), _item("https://b.test/2", ART)])
        line = rep.summary_line()
        self.assertIn("组独立内容", line)
        self.assertIn("并入 1 条", line)


# --------------------------------------------------------------- 存储层
class TestClusterStorage(unittest.TestCase):
    def setUp(self) -> None:
        from kwsearch.storage import init_db

        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = init_db(str(self.dir / "c.db"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def repo(self):  # noqa: ANN201
        from kwsearch.storage import Repository

        return Repository(self.conn)

    def _docs(self, repo, n: int = 2) -> list[str]:
        from kwsearch.models import Doc

        urls = []
        for i in range(n):
            u = f"https://s{i}.test/{i}"
            repo.upsert_doc(Doc(url_canon=u, domain=f"s{i}.test", title=f"标题{i}",
                                summary="摘要", simhash=simhash64(ART)))
            urls.append(u)
        return urls

    def test_replace_is_idempotent(self) -> None:
        """重跑必须整批重写，不能留下上一轮的幽灵成员（簇号每轮重编）。"""
        repo = self.repo()
        tid = repo.start_task("红黑树")
        urls = self._docs(repo, 2)
        rep = cluster_docs([ClusterInput(url_canon=u, simhash=simhash64(ART),
                                         body_chars=800) for u in urls])
        self.assertEqual(repo.replace_clusters(tid, rep.clusters), 1)
        repo.replace_clusters(tid, rep.clusters)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cluster WHERE task_id=?",
                              (tid,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cluster_member WHERE task_id=?",
                              (tid,)).fetchone()[0], 2)

    def test_simhash_survives_signed_storage(self) -> None:
        """指纹最高位为 1 时 SQLite 只能存负数，读回来必须还原成同一个值。"""
        from kwsearch.models import Doc

        repo = self.repo()
        h = simhash64("红黑树的插入与删除都需要旋转和变色，这保证了最坏情况下依然是对数复杂度。")
        self.assertGreaterEqual(h, 1 << 63)
        repo.upsert_doc(Doc(url_canon="https://s.test/x", domain="s.test",
                            title="t", summary="s", body_chars=900, simhash=h))
        rows = repo.doc_fingerprints(["https://s.test/x"])
        self.assertEqual(rows[0]["simhash"], h)

    def test_bodies_done_can_demand_fingerprint(self) -> None:
        """有正文但没指纹的行，聚簇开着时算"没做完"—— 否则老数据永远聚不上。"""
        from kwsearch.models import Doc

        repo = self.repo()
        repo.upsert_doc(Doc(url_canon="https://s.test/legacy", domain="s.test",
                            title="t", summary="s", body_chars=900, body_core=900))
        repo.upsert_doc(Doc(url_canon="https://s.test/new", domain="s.test",
                            title="t", summary="s", body_chars=900, body_core=900,
                            simhash=simhash64(ART)))
        urls = ["https://s.test/legacy", "https://s.test/new"]
        self.assertEqual(len(repo.bodies_done(urls)), 2)                       # 默认口径
        self.assertEqual(repo.bodies_done(urls, need_simhash_above=200),
                         {"https://s.test/new"})                               # 要求指纹

    def test_short_body_not_forced(self) -> None:
        """正文本来就短（< 阈值）的行不能被要求补指纹，否则每轮都要重抓一遍。"""
        from kwsearch.models import Doc

        repo = self.repo()
        repo.upsert_doc(Doc(url_canon="https://s.test/short", domain="s.test",
                            title="t", summary="s", body_chars=120, body_core=120))
        self.assertEqual(
            repo.bodies_done(["https://s.test/short"], need_simhash_above=200),
            {"https://s.test/short"})

    def test_old_row_without_core_is_refetched(self) -> None:
        """有指纹但没有 body_core 的行算"没做完"，重跑时补抓一次。

        不补这一抓，代表永远按原始长度选 —— 带免责声明的转载版会继续冒充"原文"。
        代价是每条老数据多抓一次，抓完 body_core 落库就不再重抓。
        """
        from kwsearch.models import Doc

        repo = self.repo()
        repo.upsert_doc(Doc(url_canon="https://s.test/old", domain="s.test",
                            title="t", summary="s", body_chars=900,
                            simhash=simhash64(ART)))
        self.assertEqual(len(repo.bodies_done(["https://s.test/old"])), 1)
        self.assertEqual(repo.bodies_done(["https://s.test/old"], need_simhash_above=200),
                         set())

    def test_core_persisted_and_read_back(self) -> None:
        """body_core 要能被聚簇读到：它是代表排序的唯一依据。"""
        from kwsearch.models import Doc

        repo = self.repo()
        repo.upsert_doc(Doc(url_canon="https://s.test/core", domain="s.test",
                            title="t", summary="s", body_chars=1200, body_core=950))
        row = repo.doc_fingerprints(["https://s.test/core"])[0]
        self.assertEqual((row["body_chars"], row["body_core"]), (1200, 950))

    def test_repository_migrates_old_db(self) -> None:
        """老库（没有 body_core 列）用 connect 打开也必须能读。

        report / db 子命令走的是 connect 而不是 init_db，只在那里补列的话，
        老库上第一句 SELECT body_core 就会 "no such column"。
        """
        from kwsearch.storage import Repository, connect

        self.conn.execute("ALTER TABLE doc DROP COLUMN body_core")
        self.conn.commit()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(doc)")}
        self.assertNotIn("body_core", cols)

        conn = connect(str(self.dir / "c.db"))
        try:
            Repository(conn)                      # 构造即补列
        finally:
            conn.close()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(doc)")}
        self.assertIn("body_core", cols)


# --------------------------------------------------------------- pipeline 接线
_P_ITEMS = [
    ("https://a.test/1", "红黑树 原理详解"),
    ("https://b.test/2", "【转载】红黑树原理详解"),
    # 第三条必须**也讲红黑树**：pipeline 对 serp 通道有关键词过滤
    # （标题和摘要里都没有关键词的条目会被丢掉），拿一篇讲咖啡的来凑数
    # 会在过滤那一关就消失，测试看起来像"聚类漏了"。
    ("https://c.test/3", "红黑树与 AVL 树的区别"),
]

# 同主题、不同文章：距离 30 上下，必须各自成组
THIRD = """红黑树的删除操作比插入复杂得多。删除一个节点后，如果被删的是红色节点，
通常不会破坏性质；如果被删的是黑色节点，就会让某条路径上的黑色节点数量减少，
需要自底向上逐层修复。修复分四种情况：兄弟节点是红色、兄弟节点是黑色且两个子节点
都是黑色、兄弟节点是黑色且近侄子红色、兄弟节点是黑色且远侄子红色。每种情况对应
一组旋转与变色操作，最终把黑高恢复平衡。理解删除的关键是先想清楚缺少一个黑色
这个状态如何在树上向上传递，直到遇到红色节点或者根节点为止。很多面试题会问
为什么红黑树比 AVL 树更适合做映射，答案是红黑树的插入删除旋转次数更少。"""

_PAGE = """<html><head><title>{title} - 站点</title></head><body>
<div class="article">
<p>{p1}</p><p>{p2}</p><p>{p3}</p>{tail}
</div></body></html>"""


def _paras(text: str) -> tuple[str, str, str]:
    n = len(text)
    return text[:n // 3], text[n // 3: 2 * n // 3], text[2 * n // 3:]


class _FakeFetcher:
    """按 URL 查表返回。表里没有的一律 404 —— 比抛异常更接近真实站点行为。

    自带一份而不是复用 test_enrich 的：那个模块在导入时会注册自己的假通道，
    两个文件互相 import 会让"某个通道有没有被注册"变得依赖测试执行顺序。
    """

    name = "fake"

    def __init__(self, pages: dict[str, str]) -> None:
        from kwsearch.fetch import FetchError, FetchResult

        self._err = FetchError
        self._res = FetchResult
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20):  # noqa: ANN201
        v = self.pages.get(url)
        self.calls.append(url)
        if v is None:
            raise self._err("404 not found", status=404, kind="http")
        raw = v.encode("utf-8")
        return self._res(status=200, body=v, raw=raw,
                         content_type="text/html; charset=utf-8",
                         final_url=url, content_bytes=len(raw), fetcher=self.name)


def fake_serp_cluster(keyword: str, offset: int, ctx):  # noqa: ANN001
    from kwsearch.models import Candidate, ChannelResult

    cands = [
        Candidate(url_raw=url, title=t, snippet="搜索摘要",
                  channel="fake_serp_cluster", rank=i + 1,
                  extra={"domain": url.split("/")[2], "hint_kind": ""})
        for i, (url, t) in enumerate(_P_ITEMS)
    ]
    return ChannelResult(channel="fake_serp_cluster", query=keyword,
                         candidates=cands, raw_count=len(cands))


PIPE_CFG = """version: 1
defaults:
  timeout: 5
  retry: {{max_attempts: 1, backoff_base: 0.01}}
  user_agent: "test-agent"
channels:
  - id: fake_serp_cluster
    type: serp
    parser: fake_serp_cluster
    url_template: "https://fake.test/s?q={{q}}&first={{offset}}"
    paging: {{mode: offset, param: first, start: 1, step: 10, max_pages: 1}}
    throttle: {{min_interval_s: 0}}
    assertions: {{min_items: 1, required_fields: [url, title]}}
expand:
  enabled: false
  max_queries: 1
filter:
  pii_patterns: {{}}
  drop_url_patterns: []
  drop_domains: []
  max_per_domain: 10
  drop_zero_hit: false
limits:
  max_docs: 20
  max_minutes: 5
extract:
  summary_chars: 200
  fetch_body: true
  max_fetch: 10
  min_body_chars: 200
  challenge_keywords: ["安全验证"]
crawl:
  robots_obey: false
  per_domain_qps: 0
cluster:
  enabled: {enabled}
  hamming: 10
  min_body_chars: 200
"""


class TestPipelineWiring(unittest.TestCase):
    def setUp(self) -> None:
        from kwsearch import discover

        discover._REGISTRY["fake_serp_cluster"] = fake_serp_cluster
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_pipe(self, enabled: str = "true", *, tail: str = ""):  # noqa: ANN201
        from kwsearch.config import load_config
        from kwsearch.pipeline import run_task

        p = self.dir / "c.yaml"
        p.write_text(PIPE_CFG.format(enabled=enabled), encoding="utf-8")
        a1, a2, a3 = _paras(ART)
        o1, o2, o3 = _paras(THIRD)
        pages = {
            "https://a.test/1": _PAGE.format(title="红黑树 原理详解", p1=a1, p2=a2, p3=a3, tail=""),
            "https://b.test/2": _PAGE.format(title="【转载】红黑树原理详解", p1=a1, p2=a2,
                                             p3=a3, tail=tail),
            "https://c.test/3": _PAGE.format(title="红黑树与 AVL 树的区别", p1=o1, p2=o2,
                                             p3=o3, tail=""),
        }
        return run_task(load_config(p), "红黑树", db_path=self.db,
                        progress=None, fetcher=_FakeFetcher(pages))

    def clusters(self) -> list[tuple[int, str]]:
        """**本任务**的簇。簇是任务维度的，重跑会产生一个新 task_id ——
        统计时不带 task_id 就会把两轮的行加在一起，看着像"重复写入"。"""
        from kwsearch.storage import connect

        conn = connect(self.db)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT member_count, rep_url FROM cluster"
                " WHERE task_id=(SELECT task_id FROM task ORDER BY rowid DESC LIMIT 1)"
                " ORDER BY member_count DESC")]
        finally:
            conn.close()

    def test_reprint_merged_in_run(self) -> None:
        r = self.run_pipe()
        self.assertEqual(r.kept, 3)
        self.assertEqual(r.cluster_groups, 2)
        self.assertEqual(r.cluster_merged, 1)
        self.assertEqual(self.clusters(), [(2, "https://a.test/1"), (1, "https://c.test/3")])

    def test_rerun_is_stable(self) -> None:
        """第二遍命中"库里已有正文"，指纹只能从库里读 —— 结果必须和第一遍一致。"""
        self.run_pipe()
        r2 = self.run_pipe()
        self.assertEqual((r2.cluster_groups, r2.cluster_merged), (2, 1))
        self.assertEqual(len(self.clusters()), 2)      # 没有重复写入

    def test_reprint_with_disclaimer_does_not_become_rep(self) -> None:
        """端到端：转载页挂了一段免责声明 → 正文比原文长，但代表仍是原文。

        这是"代表取正文最长"的真 bug：多出来的那 100 多字是模板，
        按字数选等于奖励转载，报告里的"原文链接"会指向二手站。
        """
        tail = ('<p>【免责声明】本文仅代表作者本人观点，与本网站无关。'
                '本站对文中陈述、观点判断保持中立。如有侵权请联系我们删除。'
                '扫描下方二维码关注公众号获取更多干货。</p>')
        r = self.run_pipe(tail=tail)
        self.assertEqual(r.kept, 3)
        self.assertEqual(r.cluster_merged, 1)
        self.assertEqual(self.clusters()[0], (2, "https://a.test/1"))

    def test_disabled_leaves_no_clusters(self) -> None:
        r = self.run_pipe("false")
        self.assertEqual((r.cluster_groups, r.cluster_merged), (0, 0))
        self.assertEqual(self.clusters(), [])


# --------------------------------------------------------------- 报告折叠
class TestReportFolding(unittest.TestCase):
    def setUp(self) -> None:
        from kwsearch.storage import init_db

        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = init_db(str(self.dir / "r.db"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self) -> tuple[str, str]:
        from kwsearch.models import Doc, Mention
        from kwsearch.storage import Repository

        repo = Repository(self.conn)
        tid = repo.start_task("红黑树")
        rows = [
            ("https://a.test/1", "红黑树 原理详解", "a.test", simhash64(ART)),
            ("https://b.test/2", "【转载】红黑树原理详解", "b.test", simhash64(ART)),
            ("https://c.test/3", "咖啡烘焙曲线", "c.test", simhash64(OTHER)),
        ]
        for u, t, d, h in rows:
            repo.upsert_doc(Doc(url_canon=u, url_raw=u, domain=d, title=t,
                                summary="摘要" * 20, body_chars=800, simhash=h))
            repo.upsert_mention(Mention(task_id=tid, url_canon=u, score=5.0))
        rep = cluster_docs([ClusterInput(url_canon=u, simhash=h,
                                         body_chars=800, title=t, domain=d)
                            for u, t, d, h in rows])
        repo.replace_clusters(tid, rep.clusters)
        repo.finish_task(tid, "ok", False, {})
        return tid, ""

    def test_members_folded_under_rep(self) -> None:
        from kwsearch.report import render_html

        tid, _ = self._seed()
        from kwsearch.storage import Repository

        data = Repository(self.conn).stats(tid)
        out = render_html(data)
        self.assertEqual(out.count('<article class="doc">'), 2)   # 3 条只渲染 2 张卡
        self.assertIn("同文 2 篇", out)
        self.assertIn("另有 1 篇同文转载", out)
        self.assertIn("【转载】红黑树原理详解", out)                 # 成员标题仍在，只是折叠了

    def test_totals_accounting(self) -> None:
        from kwsearch.storage import Repository

        tid, _ = self._seed()
        totals = Repository(self.conn).stats(tid)["totals"]
        self.assertEqual((totals["docs"], totals["groups"], totals["merged"]), (3, 2, 1))
        self.assertEqual(totals["clusters"], 2)


class TestUnjudgedBreakdown(unittest.TestCase):
    """没参与归并的条目要分清"有救的"和"没救的"。

    原来三种情形合称"无指纹未判"：用户既不知道要不要处理，也不知道处理完有没有用。
    区分之后：待补算 = 重跑该词会自动补；正文过短 = 本来就不该参与，不是缺数据。
    """

    def setUp(self) -> None:
        from kwsearch.storage import init_db

        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "u.db")
        self.conn = init_db(self.db)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self) -> str:
        from kwsearch.models import Doc, Mention
        from kwsearch.storage import Repository

        repo = Repository(self.conn)
        tid = repo.start_task("红黑树")
        rows = [
            # (url, body_chars, simhash)：四种状态各一条
            ("https://a.test/ok", 800, simhash64(ART)),
            ("https://b.test/pending", 800, None),
            ("https://c.test/short", 120, None),
            ("https://d.test/nobody", 0, None),
        ]
        for u, chars, h in rows:
            repo.upsert_doc(Doc(url_canon=u, url_raw=u, domain=u.split("/")[2],
                                title="红黑树", summary="摘要" * 20,
                                body_chars=chars, simhash=h))
            repo.upsert_mention(Mention(task_id=tid, url_canon=u, score=1.0))
        # 只有"有指纹"的那条会进簇（单条成簇）。这样 unfingerprinted 正好等于
        # 待补算 + 过短 + 无正文，账目能对上 —— 对不上就说明分档有漏。
        rep = cluster_docs([ClusterInput(url_canon="https://a.test/ok",
                                         simhash=simhash64(ART), body_chars=800)])
        repo.replace_clusters(tid, rep.clusters)
        repo.finish_task(tid, "ok", False, {})
        return tid

    def test_three_reasons_split(self) -> None:
        from kwsearch.storage import Repository

        totals = Repository(self.conn).stats(self._seed(), min_body_chars=200)["totals"]
        self.assertEqual(totals["pending_fingerprint"], 1)   # 有正文没指纹 → 重跑能补
        self.assertEqual(totals["short_body"], 1)            # 正文不够长 → 本来就不判
        self.assertEqual(totals["no_body"], 1)               # 没抓到正文
        self.assertEqual(totals["unfingerprinted"], 3)
        self.assertEqual(
            totals["pending_fingerprint"] + totals["short_body"] + totals["no_body"],
            totals["unfingerprinted"])

    def test_threshold_follows_config(self) -> None:
        """阈值调大，原本"够长"的那条就归到"过短"里去。"""
        from kwsearch.storage import Repository

        tid = self._seed()
        totals = Repository(self.conn).stats(tid, min_body_chars=1000)["totals"]
        self.assertEqual((totals["pending_fingerprint"], totals["short_body"]), (0, 2))


if __name__ == "__main__":
    unittest.main()
