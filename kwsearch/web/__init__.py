"""kwsearch.web —— 本地交互页面（stdlib HTTP 服务 + 单文件前端）。

用法：``python -m kwsearch serve``，然后浏览器里点着用。
本包不新增任何第三方依赖；测试见 tests/test_web.py（假 runner，零真实请求）。
"""
from kwsearch.web.jobs import Job, JobRegistry, RunPayload, format_result_lines
from kwsearch.web.server import build_handler, make_default_runner, serve

__all__ = [
    "Job", "JobRegistry", "RunPayload", "format_result_lines",
    "build_handler", "make_default_runner", "serve",
]
