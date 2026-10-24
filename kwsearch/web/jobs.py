"""后台作业：把一次 run_task 包成「可提交、可查询、可展示」的 Job。

三条设计约束，每条都对应一个真实会发生的用法：

1. **同一时刻只跑一个任务**。上游限速（Bing 一页 10 条、GDELT ≥5s 间隔）本来就是
   全局的，两个任务并发不会更快，只会让 SQLite 写互相锁死、日志互相穿插。
   第二个请求直接拿到 409，前端把按钮置灰。
2. **进度逐行收集**。pipeline 的 progress 回调本来就是给人看的，原样收进
   `Job.lines`，前端轮询 `/api/jobs/<id>` 即可，不需要 WebSocket / SSE。
3. **runner 可注入**。默认 runner 调真的 run_task；单测换一个假 runner，
   保证 412 条老测试一条真请求都不发（见 tests/test_web.py）。

线程模型：HTTP 服务是 ThreadingHTTPServer，每个请求一个线程；Job 跑在
独立的 daemon 线程里。`Job.lines` 只由 job 线程写、请求线程读，统一走
`JobRegistry._lock`，不做无锁读写。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

# Windows 文件名保留字符 + 路径分隔符：关键词会变成 report/<关键词>/ 的目录名，
# 这些字符要么建不出目录（<>:"|?*），要么直接穿越（/ \）。
_ILLEGAL_KW = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_KEYWORD_LEN = 100
MAX_LOG_LINES = 2000

Log = Callable[[str], None]
Runner = Callable[["RunPayload", Log], "str | None"]


@dataclass(slots=True)
class RunPayload:
    """一次抓取的输入。字段与 CLI 的 run 子命令一一对应。"""

    keyword: str
    person: bool = False
    orgs: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    fetch_body: bool | None = None      # None = 听配置的
    stealth: bool = False               # Scrapling 隐身浏览器档（默认关，反爬可选项）
    robots_mode: str = "obey"           # obey | record | off（默认遵守）
    robots_on_failure: str = "deny"     # deny | allow（默认保守禁止）
    max_docs: int | None = None
    expand: int | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "RunPayload":
        """解析 /api/run 的 JSON body。任何不合法都抛 ValueError（→ HTTP 400）。"""
        if not isinstance(data, Mapping):
            raise ValueError("请求体必须是 JSON 对象")

        raw_kw = data.get("keyword")
        keyword = str(raw_kw or "").strip()
        if not keyword:
            raise ValueError("关键词不能为空")
        if len(keyword) > MAX_KEYWORD_LEN:
            raise ValueError(f"关键词最长 {MAX_KEYWORD_LEN} 字符")
        if _ILLEGAL_KW.search(keyword):
            raise ValueError(f"关键词含有非法字符（不能出现 <>:\"/\\|?* 和控制字符）: {keyword!r}")

        def _anchors(key: str) -> tuple[str, ...]:
            vals = data.get(key)
            if vals is None:
                return ()
            if isinstance(vals, str) or not isinstance(vals, Sequence):
                vals = [vals]
            out: list[str] = []
            for v in vals:
                for part in re.split(r"[,，]", str(v)):
                    part = part.strip()
                    if part and part not in out:
                        out.append(part)
            return tuple(out)

        def _int_or_none(key: str) -> int | None:
            v = data.get(key)
            if v in (None, ""):
                return None
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数") from None

        fetch_body = data.get("fetch_body")
        if fetch_body is not None and not isinstance(fetch_body, bool):
            raise ValueError("fetch_body 必须是 true/false")

        stealth = data.get("stealth", False)
        if not isinstance(stealth, bool):
            raise ValueError("stealth 必须是 true/false")

        robots_mode = data.get("robots_mode") or "obey"
        if robots_mode not in ("obey", "record", "off"):
            raise ValueError("robots_mode 必须是 obey / record / off")
        robots_on_failure = data.get("robots_on_failure") or "deny"
        if robots_on_failure not in ("deny", "allow"):
            raise ValueError("robots_on_failure 必须是 deny / allow")

        return cls(
            keyword=keyword,
            person=bool(data.get("person")),
            orgs=_anchors("orgs"),
            fields=_anchors("fields"),
            roles=_anchors("roles"),
            fetch_body=fetch_body,
            stealth=stealth,
            robots_mode=str(robots_mode),
            robots_on_failure=str(robots_on_failure),
            max_docs=_int_or_none("max_docs"),
            expand=_int_or_none("expand"),
        )


@dataclass(slots=True)
class Job:
    """一个后台作业的快照。lines 是渲染好的日志行（含进度与收尾表）。"""

    id: str
    keyword: str
    person: bool
    status: str = "running"            # running / done / error
    lines: list[str] = field(default_factory=list)
    report_url: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "keyword": self.keyword,
            "person": self.person,
            "status": self.status,
            "lines": list(self.lines),
            "report_url": self.report_url,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRegistry:
    """作业注册表。核心不变量：任意时刻最多一个 running。"""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._current: Job | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    def start(self, payload: RunPayload) -> Job | None:
        """提交作业；已有任务在跑时返回 None（调用方映射成 409）。"""
        with self._lock:
            if self._current is not None:
                return None
            job = Job(id=uuid.uuid4().hex[:12], keyword=payload.keyword,
                      person=payload.person)
            self._jobs[job.id] = job
            self._current = job

        t = threading.Thread(target=self._run, args=(job, payload),
                             name=f"kwsearch-job-{job.id}", daemon=True)
        t.start()
        return job

    def _run(self, job: Job, payload: RunPayload) -> None:
        def log(msg: object) -> None:
            line = str(msg)
            with self._lock:
                job.lines.append(line)
                if len(job.lines) > MAX_LOG_LINES:
                    del job.lines[: len(job.lines) - MAX_LOG_LINES]

        try:
            job.report_url = self._runner(payload, log)
            job.status = "done"
        except BaseException as e:  # noqa: BLE001 —— 后台线程里任何异常都要变成 error 状态
            # KeyboardInterrupt 也走这里：Ctrl+C 打在主线程上，但 runner 里若被
            # 打断同样要留下痕迹，不能让人看到一个永远 running 的作业。
            job.error = f"{type(e).__name__}: {e}"
            job.status = "error"
        finally:
            job.finished_at = time.time()
            with self._lock:
                self._current = None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> Job | None:
        with self._lock:
            if not self._jobs:
                return None
            return max(self._jobs.values(), key=lambda j: j.started_at)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(),
                          key=lambda j: j.started_at, reverse=True)


def format_result_lines(result: object) -> list[str]:
    """收尾表复用 CLI 的 _run_summary（同一种格式，避免两处漂移）。

    result 是 pipeline.RunResult；这里不 import 它，保持 jobs 层对 pipeline
    只有 runner 这一个依赖点。
    """
    from kwsearch.__main__ import _run_summary  # 延迟导入：CLI 模块不该被 web 层钉住

    return _run_summary(result).splitlines()  # type: ignore[arg-type]
