-- ============================================================================
--  kwsearch · SQLite Schema (V1)
-- ----------------------------------------------------------------------------
--  设计原则（每条都对应 前作踩过的坑）：
--   1) 任务维度贯穿全表：唯一约束必须带 task_id。
--      坑 9：前作 用 fetched_at 做唯一键，同一秒内的两轮采集被
--      INSERT OR IGNORE 静默吞掉。task_id 才是"哪一次搜索"的权威标识。
--   2) 不存正文全文：doc 只留 ≤500 字摘要 + simhash，全文按需落 bodies/。
--   3) 任何"少拿到数据"都要留痕：query 表记每次通道调用的结果，
--      这样报告能自报"覆盖率"，而不是假装抓全了。
--   4) 时间必须带 time_source：让"兜底时间"和"真实发布时间"在数据层就分得开。
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- 任务：一次"给定关键词"的搜索
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task (
    task_id     TEXT PRIMARY KEY,             -- uuid4
    keyword     TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'quick',-- quick | deep | watch
    timespan    TEXT,                          -- 例如 30d，交给自己决定时间窗
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running', -- running|ok|partial|failed|aborted
    truncated   INTEGER NOT NULL DEFAULT 0,    -- 触顶上限被强制停止 -> 1，报告必须写明
    stats_json  TEXT                           -- {"bing_html": 30, "gdelt": 75}
);
CREATE INDEX IF NOT EXISTS idx_task_keyword ON task(keyword, started_at DESC);

-- ----------------------------------------------------------------------------
-- 通道调用明细：报告"覆盖率自评"的唯一数据来源
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query (
    task_id     TEXT NOT NULL,
    channel     TEXT NOT NULL,
    seq         INTEGER NOT NULL,              -- 该通道第几次调用（翻页序号 / 变体序号）
    url         TEXT NOT NULL,                 -- 实际请求的 URL，便于复现
    query_text  TEXT,                          -- 实际发出的查询词（变体模式下与 task.keyword 不同）
    http_status INTEGER,
    raw_count   INTEGER,                       -- 通道声称的命中总数（B站 numResults）
    got_count   INTEGER NOT NULL DEFAULT 0,    -- 实际解析出的候选数
    new_count   INTEGER NOT NULL DEFAULT 0,    -- 去重后真正新增的条数（判断"这个变体有没有用"）
    blocked     INTEGER NOT NULL DEFAULT 0,    -- 被限流/挑战/需登录
    block_kind  TEXT,                          -- http_403|challenge|captcha|rate_limit|empty
    elapsed_ms  INTEGER,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, channel, seq)
);
CREATE INDEX IF NOT EXISTS idx_query_blocked ON query(task_id, blocked);

-- ----------------------------------------------------------------------------
-- 文档：URL 规范化后唯一
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc (
    url_canon    TEXT PRIMARY KEY,             -- 规范化 URL（去 utm、合并 m./amp）
    url_raw      TEXT,
    domain       TEXT NOT NULL,                -- 单独成列，便于统计来源分布 / 单域配额
    title        TEXT,
    summary      TEXT,                         -- ≤500 字，绝不存全文
    published_at TEXT,                         -- 归一化后的发布时间
    time_source  TEXT NOT NULL DEFAULT 'fallback',
                                               -- serp|api|serp_relative      ← 通道给的
                                               -- jsonld|meta|regex           ← 页面自己声明的
                                               -- regex_updated               ← 只找到"最近更新"
                                               -- fallback                    ← 没拿到
                                               -- ⚠️ fallback 时 published_at 必须为 NULL，
                                               --    不许拿抓取时间冒充（first_seen 已有抓取时间）
                                               -- ⚠️ serp_relative 是"5 天之前"回推的，精度只到天，
                                               --    报告里与 fallback 一起标"时间不可信"
                                               -- ⚠️ regex_updated 多是站点构建时间（全站同一个值），
                                               --    同样标"时间不可信"，不许当发布时间用
    channel      TEXT,                         -- 首次发现它的通道
    http_status  INTEGER,
    content_type TEXT,
    body_chars   INTEGER,                      -- 正文长度，用于识别"空壳页/JS 站"
    body_core    INTEGER,                      -- 剥掉首尾转载模板后的正文长度（NULL=老数据未算）
                                               -- ⚠️ S5 的代表按它排，不按 body_chars：
                                               --    转载版挂一段免责声明就比原文长，
                                               --    按原始长度选会把二手站选成"原文"
    simhash      INTEGER,                      -- 64bit 正文指纹，**按补码存成有符号**（SQLite 只有 int64）
                                               -- 读写都要过 aggregate.cluster.to_signed64 / from_signed64
    cluster_id   INTEGER,                      -- ⚠️ 未使用：簇是任务维度的，权威映射见 cluster_member
    body_ref     TEXT,                         -- 全文落盘相对路径，NULL=未存
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    extra_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_domain    ON doc(domain, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_doc_published ON doc(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_doc_simhash   ON doc(simhash);
CREATE INDEX IF NOT EXISTS idx_doc_cluster   ON doc(cluster_id);

-- ----------------------------------------------------------------------------
-- 命中：关键词 × 文档。同一篇文档被多个通道发现也只留一行，分数取最大值
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mention (
    task_id      TEXT NOT NULL,
    url_canon    TEXT NOT NULL,
    hit_title    INTEGER NOT NULL DEFAULT 0,
    hit_body     INTEGER NOT NULL DEFAULT 0,
    exact_phrase INTEGER NOT NULL DEFAULT 0,   -- 是否出现完整短语
    bm25         REAL NOT NULL DEFAULT 0,
    score        REAL NOT NULL DEFAULT 0,      -- 最终排序分（含标题/位置权重）
    created_at   TEXT NOT NULL,
    PRIMARY KEY (task_id, url_canon),
    FOREIGN KEY (url_canon) REFERENCES doc(url_canon)
);
CREATE INDEX IF NOT EXISTS idx_mention_score ON mention(task_id, score DESC);

-- ----------------------------------------------------------------------------
-- 转载簇：中文互联网最大噪声来源。
-- 报告里只展示代表文档 + "另有 N 篇转载"，而不是把 20 家门户的同一篇稿子
-- 铺满一屏（那会让"噪音率"看起来像"热度"）。
-- ⚠️ 簇是**任务维度**的：同一篇文章在两次搜索里可以落在不同簇里，
--    所以权威映射在 cluster_member（带 task_id），不要指望 doc.cluster_id。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cluster (
    cluster_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL,
    rep_url       TEXT NOT NULL,               -- 代表文档（正文最长的那个，见 aggregate/cluster.py）
    member_count  INTEGER NOT NULL DEFAULT 1,
    max_hamming   INTEGER,                     -- 簇内最差成员与代表的汉明距离（相似度下限）
    method        TEXT,                        -- simhash | none（没算指纹时整批不聚类）
    first_seen_at TEXT NOT NULL,               -- 簇内最早/最晚的发布时间，都拿不到时记抓取时间
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cluster_task ON cluster(task_id, member_count DESC);

-- 簇成员：报告展开"另有 N 篇转载"时逐条列出。
-- 单条成簇（member_count=1）也写一行 —— 这样"入库 N 条 = 各组条数之和"
-- 才能对上账，否则报告里"独立内容 M 组"是个无法复核的数字。
CREATE TABLE IF NOT EXISTS cluster_member (
    task_id    TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    url_canon  TEXT NOT NULL,
    hamming    INTEGER NOT NULL DEFAULT 0,     -- 与代表的距离；代表自己为 0
    is_rep     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, cluster_id, url_canon)
);
CREATE INDEX IF NOT EXISTS idx_cmember_url ON cluster_member(task_id, url_canon);

-- ----------------------------------------------------------------------------
-- 人名模式：候选人卡片（见 person.py）。
-- 按命中的机构分组 —— **不同机构绝不合并**，这是红线；
-- 无机构命中的候选统一记 label='未匹配机构'，报告里折叠为"同名未归因"。
-- 卡片是任务维度的派生结果，重跑就整体重写（簇号同理）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person_card (
    task_id       TEXT NOT NULL,
    label         TEXT NOT NULL,               -- 机构原文 / 未匹配机构
    tier          TEXT NOT NULL,               -- confirmed|likely|suspect|unrelated
    score         INTEGER NOT NULL DEFAULT 0,
    org_hits      INTEGER NOT NULL DEFAULT 0,
    weak_hits     INTEGER NOT NULL DEFAULT 0,
    docs          INTEGER NOT NULL DEFAULT 0,
    negatives     TEXT,                        -- 逗号分隔：演艺/招聘/法律
    evidence_json TEXT,                        -- [{title,url,matched}]
    PRIMARY KEY (task_id, label)
);

-- ----------------------------------------------------------------------------
-- 待抓队列：跨通道统一调度。
-- Scrapling 的 crawldir 只管它自己的队列，发现层产出的候选还得我们自己排。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS queue (
    url_raw     TEXT PRIMARY KEY,
    url_canon   TEXT,
    task_id     TEXT NOT NULL,
    channel     TEXT,
    depth       INTEGER NOT NULL DEFAULT 0,    -- 0=发现层直接给的，1+=链接跟出来的
    priority    INTEGER NOT NULL DEFAULT 0,    -- 越大越先抓（标题命中/权威域优先）
    state       TEXT NOT NULL DEFAULT 'pending', -- pending|done|failed|skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_pick ON queue(task_id, state, priority DESC, depth);

-- ----------------------------------------------------------------------------
-- robots 留档：合规证据，一条域名一行。
-- 前作写在 YAML 注释里，这里升级成表，因为指定搜索会碰到成百上千个域。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS robots (
    domain       TEXT PRIMARY KEY,
    checked_at   TEXT NOT NULL,
    status       INTEGER,                      -- 200|404|其他
    disallow     TEXT,                         -- 命中的 Disallow 规则（逗号分隔，可能很长）
    crawl_delay  REAL,
    allowed      INTEGER NOT NULL DEFAULT 1,   -- 本任务是否放行
    note         TEXT
);

-- ----------------------------------------------------------------------------
-- 告警：复用 前作 的 alert，kind 换成本项目的语义
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT,
    channel    TEXT,
    kind       TEXT NOT NULL,   -- parse_fail|schema_drift|blocked|challenge
                                -- |robots_disallow|limit_reached|js_required
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_created ON alert(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_task    ON alert(task_id, kind);
