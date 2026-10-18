"""kwsearch —— 指定关键词的全网公开信息聚合爬虫。

分层（依赖方向只能向下，不许回头）：

    config      声明式配置（channels.yaml）→ 带校验的 dataclass
    models      层间契约：Candidate / Doc / Mention / ChannelResult
    discover    发现层：关键词 → 候选 URL（唯一会"问搜索引擎"的层）
    fetch       采集层：URL → (status, body)，只负责拿到字节
    extract     抽取层：HTML/JSON → Doc，不做网络
    aggregate   聚合层：URL 规范化 / 相关度打分，不做网络
    sanitize    脱敏层：入库前的 PII 遮罩与丢弃判定
    storage     存储层：SQLite，只认 models 里的 dataclass
    report      展示层：DB → HTML 报告，不做网络
    pipeline    编排层：把上面这些串成"一次任务"（唯一同时碰网络与写库的地方）

关键约束（沿用同一套方法论，都是踩出来的）：
  1) 抽取层不碰网络，采集层不碰解析 —— 换抓取档位时解析逻辑一行不改
  2) 任何"少拿到数据"的情况都要落 alert，不许静默吞掉
  3) 每个通道独立熔断，一个通道被封不许拖垮整个任务
  4) 脱敏在入库前做，没有例外分支
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
