"""HTTP 层：本地交互页面的服务端。

只用标准库（http.server / threading），零新增依赖 —— 这个项目的依赖纪律是
「PyYAML + 一个 HTTP 客户端」，一个本地页面不值得为此引入 Flask/FastAPI。

安全边界（都是本地工具的真实风险，不是洁癖）：

- **只绑 127.0.0.1**。默认 host 就是回环；换成 0.0.0.0 等于把「能驱动爬虫 +
  能读你所有报告」的接口暴露给整个局域网。
- **静态目录锁死在 out_dir 内**。`/r/<关键词>/index.html` 的路径要过
  `_safe_static`：逐段拒绝 `..` / `.` / Windows 保留字符（`\\ : * ? " < > |`），
  再 resolve 后复核仍在根目录内。`Path(root) / "C:/x"` 在 Windows 上会**替换**
  整个路径，所以盘符必须死在段校验里。
- **所有响应带 Content-Length，不用 HTTP/1.1 keep-alive**。本地轮询场景下
  连接复用收益为零，却要处理 half-close / 分块的边角。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping

from kwsearch import __version__
from kwsearch.web.jobs import JobRegistry, RunPayload, format_result_lines

INDEX_HTML = Path(__file__).resolve().parent / "index.html"
DEFAULT_PORT = 8787
PORTS_TO_TRY = 20

# 单个路径段的合法字符：除 Windows 保留字符外的「可见字符」白名单的补集写法。
# 段里出现 \ : * ? " < > | 一律拒绝（前两个是穿越与盘符，其余是保留字符）。
_SEGMENT_OK = re.compile(r'^[^\\/:*?"<>|]+$')


@dataclass
class WebContext:
    """handler 闭包捕获的上下文。cfg 用 object 标注，避免 web 层反向依赖 config。"""

    cfg: object
    db_path: str
    out_dir: Path                 # 报告根目录，已 resolve
    registry: JobRegistry


# ── 静态文件 ──────────────────────────────────────────────────────────────


def _safe_static(root: Path, rel: str) -> Path | None:
    """把 URL 路径段解析成 root 内的真实文件；任何可疑形态返回 None。"""
    rel = urllib.parse.unquote(rel).strip("/")
    if not rel:
        return None
    segments = rel.split("/")
    for seg in segments:
        if seg in (".", "..") or not _SEGMENT_OK.match(seg):
            return None
    try:
        p = (root.joinpath(*segments)).resolve()
        p.relative_to(root)       # 软链接指出去也要拦
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}


# ── 默认 runner：把一次抓取接到页面上 ─────────────────────────────────────


def make_default_runner(cfg: object, db_path: str, out_dir: Path) -> Callable:
    """生产 runner：run_task → 收尾表 → render 报告 → 返回 /r/ 下的 URL。"""

    def runner(payload: RunPayload, log: Callable[[object], None]) -> str | None:
        from kwsearch.person import PersonQuery, anchor_warnings
        from kwsearch.pipeline import run_task
        from kwsearch.report import render
        from kwsearch.storage import connect

        person = None
        if payload.person:
            person = PersonQuery(
                name=payload.keyword, orgs=payload.orgs,
                fields=payload.fields, roles=payload.roles,
            )
            for w in anchor_warnings(person):
                log(f"  ! {w}")

        result = run_task(
            cfg,  # type: ignore[arg-type]
            payload.keyword,
            db_path=db_path,
            max_docs=payload.max_docs,
            expand=payload.expand,
            progress=log,
            fetch_body=payload.fetch_body,
            stealth=payload.stealth,
            person=person,
        )
        for line in format_result_lines(result):
            log(line)

        if not result.kept:
            log("没有入库任何文档，本次不生成报告。")
            return None

        cluster = dict(getattr(cfg, "cluster", None) or {})
        conn = connect(db_path)
        try:
            path = Path(render(conn, result.task_id, out_dir,
                               min_body_chars=int(cluster.get("min_body_chars") or 200)))
        finally:
            conn.close()
        log(f"报告：{path}")

        # URL 一律走 /r/ 同源提供：file:// 的 iframe 会被浏览器拦，
        # 而且同源才能让页面里的「新窗口打开」直接可用。
        try:
            rel = path.resolve().relative_to(out_dir)
            return "/r/" + "/".join(urllib.parse.quote(p) for p in rel.parts)
        except (OSError, ValueError):
            return "/r/" + urllib.parse.quote(payload.keyword) + "/index.html"

    return runner


# ── 引导数据 ──────────────────────────────────────────────────────────────


def _channel_states(cfg: object) -> list[dict[str, object]]:
    from kwsearch.discover import registered_names

    have = set(registered_names())
    out: list[dict[str, object]] = []
    for c in cfg.channels:  # type: ignore[attr-defined]
        if not c.enabled:
            state = "停用"
        elif c.parser not in have:
            state = "未注册"
        else:
            reason = c.unavailable_reason()
            state = f"不可用：{reason}" if reason else "就绪"
        out.append({"id": c.id, "type": c.type,
                    "enabled": bool(c.enabled), "state": state})
    return out


def _recent_tasks(db_path: str, out_dir: Path, limit: int = 12) -> list[dict[str, object]]:
    p = Path(db_path)
    if not p.exists():
        return []
    from kwsearch.storage import connect, recent_tasks

    conn = connect(db_path)
    try:
        rows = recent_tasks(conn, limit)
    finally:
        conn.close()

    out: list[dict[str, object]] = []
    for r in rows:
        kw = r["keyword"]
        report = out_dir / kw / "index.html"
        out.append({
            "task_id": r["task_id"],
            "keyword": kw,
            "mode": r["mode"],
            "started_at": (r["started_at"] or "")[:16].replace("T", " "),
            "status": r["status"],
            "truncated": bool(r["truncated"]),
            "report_url": ("/r/" + urllib.parse.quote(kw) + "/index.html"
                           if report.is_file() else None),
        })
    return out


# ── HTTP handler ──────────────────────────────────────────────────────────


def build_handler(ctx: WebContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"kwsearch-web/{__version__}"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # 默认实现会把每个请求甩到 stderr，和爬虫日志混在一起

        # ---- 基础回复 ----
        def _send(self, code: int, body: bytes,
                  content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: object, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _not_found(self, msg: str = "not found") -> None:
            self._json({"error": msg}, 404)

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.unquote(self.path.split("?", 1)[0])
            try:
                if path in ("/", "/index.html"):
                    return self._serve_index()
                if path == "/api/bootstrap":
                    return self._serve_bootstrap()
                if path.startswith("/api/jobs/"):
                    return self._serve_job(path[len("/api/jobs/"):])
                if path == "/api/wipe":
                    return self._serve_wipe_preview()
                if path.startswith("/r/"):
                    return self._serve_static(path[len("/r/"):])
                self._not_found()
            except (BrokenPipeError, ConnectionResetError):
                pass  # 用户关页面 / 中断轮询，不是错误
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.unquote(self.path.split("?", 1)[0])
            try:
                if path not in ("/api/run", "/api/wipe"):
                    return self._not_found()
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    return self._json({"error": f"请求体不是合法 JSON: {e}"}, 400)
                if path == "/api/wipe":
                    return self._post_wipe(data)
                try:
                    payload = RunPayload.from_mapping(data)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                job = ctx.registry.start(payload)
                if job is None:
                    return self._json({"error": "已有任务正在运行，请等它结束"},
                                      409)
                self._json(job.json(), 202)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        # ---- 各路由实现 ----
        def _serve_index(self) -> None:
            try:
                body = INDEX_HTML.read_bytes()
            except OSError as e:
                return self._json({"error": f"页面文件缺失: {e}"}, 500)
            self._send(200, body)

        def _serve_bootstrap(self) -> None:
            latest = ctx.registry.latest()
            self._json({
                "version": __version__,
                "channels": _channel_states(ctx.cfg),
                "recent_tasks": _recent_tasks(ctx.db_path, ctx.out_dir),
                "latest_job": latest.json() if latest else None,
                "db_path": str(Path(ctx.db_path).resolve()),
                "out_dir": str(ctx.out_dir),
            })

        def _serve_job(self, job_id: str) -> None:
            job = ctx.registry.get(job_id)
            if job is None:
                return self._not_found(f"没有作业 {job_id!r}")
            self._json(job.json())

        # ---- 清空所有历史数据：GET 预览 / POST 确认 ----
        def _open_repo(self):
            """开一条只读/短写连接。库不存在时返回 None（预览按空库处理）。"""
            if not Path(ctx.db_path).exists():
                return None
            from kwsearch.storage import Repository, connect

            conn = connect(ctx.db_path)
            return Repository(conn)

        def _serve_wipe_preview(self) -> None:
            repo = self._open_repo()
            if repo is None:
                tables: dict[str, int] = {}
                total = 0
            else:
                try:
                    tables = repo.wipe_preview()
                    total = sum(tables.values())
                finally:
                    repo.conn.close()
            self._json({"tables": tables, "total": total,
                        "db_path": str(Path(ctx.db_path).resolve())})

        def _post_wipe(self, data: object) -> None:
            if not isinstance(data, Mapping) or data.get("confirm") is not True:
                return self._json({"error": "这是破坏性操作：请带 {\"confirm\": true} 重试"},
                                  400)
            if ctx.registry.busy:
                return self._json({"error": "有任务正在运行，清空会打断它；请等它结束"},
                                  409)
            repo = self._open_repo()
            if repo is None:
                return self._json({"deleted": {}, "total": 0,
                                   "db_path": str(Path(ctx.db_path).resolve())})
            try:
                deleted = repo.wipe()
            finally:
                repo.conn.close()
            self._json({"deleted": deleted, "total": sum(deleted.values()),
                        "db_path": str(Path(ctx.db_path).resolve())})

        def _serve_static(self, rel: str) -> None:
            p = _safe_static(ctx.out_dir, rel)
            if p is None:
                return self._not_found("报告不存在")
            try:
                body = p.read_bytes()
            except OSError as e:
                return self._json({"error": f"读取失败: {e}"}, 500)
            ctype = _CONTENT_TYPES.get(p.suffix.lower(),
                                       "application/octet-stream")
            self._send(200, body, ctype)

    return Handler


# ── 启动 ──────────────────────────────────────────────────────────────────


def _port_busy(host: str, port: int) -> bool:
    """端口是否正被监听。

    不能靠 bind 试探：HTTPServer 默认带 SO_REUSEADDR，而 Windows 上
    SO_REUSEADDR 允许两个 socket 绑同一个端口 —— bind 成功不代表端口闲着。
    直接尝试连接，连得上就是有东西在听。
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    cfg: object,
    db_path: str = "kwsearch.db",
    out_dir: str = "report",
    open_browser: bool = True,
    runner: Callable | None = None,
    log: Callable[[object], None] = print,
) -> int:
    """起服务并阻塞。端口被占就顺序往上试，最多 PORTS_TO_TRY 个。"""
    root = Path(out_dir).resolve()
    registry = JobRegistry(runner or make_default_runner(cfg, db_path, root))
    handler = build_handler(WebContext(cfg=cfg, db_path=db_path,
                                       out_dir=root, registry=registry))

    httpd = None
    actual = port
    for i in range(PORTS_TO_TRY):
        cand = port + i
        if _port_busy(host, cand):
            continue
        try:
            httpd = ThreadingHTTPServer((host, cand), handler)
            actual = cand
            break
        except OSError:
            continue
    if httpd is None:
        log(f"✗ 端口 {port} ~ {port + PORTS_TO_TRY - 1} 全部被占用，换一个 --port")
        return 2

    url = f"http://{host}:{actual}/"
    log(f"kwsearch 交互页面 → {url}")
    log(f"数据库 {Path(db_path).resolve()} · 报告目录 {root}")
    log("Ctrl+C 停止。抓取全程在本机完成，不经过任何第三方。")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 —— 没有默认浏览器的容器里不该因此起不来
            log("（没能自动打开浏览器，请手动访问上面的地址）")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\n已停止。")
    finally:
        httpd.server_close()
    return 0
