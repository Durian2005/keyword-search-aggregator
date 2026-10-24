"""Scrapling 隐身抓取档（**可选，默认不启用**）。

接入位（fetch/__init__.py 档位表里预留的第三档）：
    curl_cffi → httpx → scrapling（本文件，显式 opt-in 才出现）

它做什么：把"发一次 GET"换成"用一个指纹伪装过的真实浏览器打开页面"，
因此能通过 Cloudflare Turnstile / Interstitial 之类的机器人挑战
（StealthyFetcher + solve_cloudflare，底层是 Patchright 或 Camoufox）。
它不做什么 —— 本文件写死这几条，比写在文档里更难被自己绕过：
    - **不接管 robots 判定**：robots.txt 照旧在抓取前检查（见 BaseFetcher.robots_fetcher，
      重档只负责"怎么发请求"，不负责"允不允许发"；放不放行由 robots 档位决定）；
    - **不携带任何账号 Cookie**：当前实现没有登录态支持；
    - **不接代理池**：ProxyRotator 不启用，IP 就是本机 IP；
    - **不落全文**：返回的 HTML 只进既有的编码判定 → 抽取 → 摘要链路，
      与其它档完全同路，存储行为零变化。

红线（2026-10-22 用户修订，替代 2026-10-21 口径）：
    项目唯一红线 = **不抓政府网站**（.gov.cn 硬拦截，过滤层+抓取层双保险，
    与本档无关也无开关可绕）。旧红线（恶意行为禁令/登录态/代理池/存全文）
    已作废 —— 上面"不做什么"是现状描述，不再是合规承诺。
    robots 的 record/off 绕过档是显式能力，强制抓取全留痕。

启用方式（三处，任一即可，全部默认关闭）：
    CLI            python -m kwsearch run "词" --stealth
    probe/page/extract 的 --fetcher scrapling
    配置            channels.yaml → defaults.stealth.enabled: true

未安装 scrapling 时的报错必须一眼能修：StealthUnavailable 带安装提示。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 安装提示。fetchers extra 才会带上浏览器引擎，装完还要 `scrapling install`
#: 下载浏览器二进制与指纹依赖 —— 只 pip install scrapling 是不够的。
INSTALL_HINT = "pip install 'scrapling[fetchers]' && scrapling install"


class StealthUnavailable(RuntimeError):
    """scrapling 没装 / 装齐。故意继承 RuntimeError 而不是 ImportError：

    build_fetcher 的降级循环只捕 ImportError；显式 opt-in 的隐身档必须
    **大声失败**，不允许静默退回 curl_cffi —— 那会让"我明明开了 stealth"
    变成"其实根本没走浏览器"，是最难发现的一类假象。
    """


@dataclass(slots=True)
class StealthConfig:
    """隐身档参数。默认值 = 官方示例里最常见的一组。"""

    headless: bool = True
    solve_cloudflare: bool = True          # 自动应对 Turnstile/Interstitial
    network_idle: bool = True              # 等网络空闲再返回（SPA 的 XHR 已完成）
    disable_resources: bool = False        # True = 禁图片/字体提速，但更像爬虫
    humanize: bool = False                 # 模拟人类鼠标轨迹
    use_camoufox: bool = False             # True = Camoufox（Firefox 改指纹）而非 Patchright
    timeout_ms: int | None = 60_000        # 单页导航超时；None = 引擎默认

    @classmethod
    def from_mapping(cls, d: dict[str, Any] | None) -> "StealthConfig":
        """从 channels.yaml 的 defaults.stealth 读。未知键忽略（如 enabled）。

        timeout_ms 语义：**缺键** = 用类默认 60000（懒配置不必写全）；
        显式写 `timeout_ms: null`（或 0）才是"引擎默认"。
        """
        d = d or {}
        raw_ms = d.get("timeout_ms", 60_000)
        return cls(
            headless=bool(d.get("headless", True)),
            solve_cloudflare=bool(d.get("solve_cloudflare", True)),
            network_idle=bool(d.get("network_idle", True)),
            disable_resources=bool(d.get("disable_resources", False)),
            humanize=bool(d.get("humanize", False)),
            use_camoufox=bool(d.get("use_camoufox", False)),
            timeout_ms=(int(raw_ms) if raw_ms else None),
        )

    def session_kwargs(self) -> dict[str, Any]:
        """StealthySession 构造参数（官方 README 确认存在的一组）。"""
        kw: dict[str, Any] = {
            "headless": self.headless,
            "solve_cloudflare": self.solve_cloudflare,
            "network_idle": self.network_idle,
            "disable_resources": self.disable_resources,
        }
        # 以下两个是社区文档记录的增强项，只在显式开启时才传：
        # 默认不传 = 用引擎默认行为，不为"以后可能加的参数"提前绑死。
        if self.humanize:
            kw["humanize"] = True
        if self.use_camoufox:
            kw["use_camoufox"] = True
        return kw

    def fetch_kwargs(self, timeout_s: int | float | None = None) -> dict[str, Any]:
        """session.fetch 的单次参数。timeout 单位毫秒（scrapling 口径）。

        配置的 timeout_ms 权威：浏览器页面导航不该被通用 HTTP 策略的 20s
        压低（隐身档的站点本来就慢）。仅当配置显式为 None（引擎默认）时，
        才用调用方给的 timeout_s 兜底。
        """
        kw: dict[str, Any] = {}
        ms = self.timeout_ms
        if ms is None and timeout_s:
            ms = int(float(timeout_s) * 1000)
        if ms:
            kw["timeout"] = int(ms)
        return kw


class StealthFetcher:
    """BaseFetcher 契约的 scrapling 实现。

    刻意**不继承** BaseFetcher：那会让 fetch/__init__.py 在导入时就依赖
    scrapling 的存在（哪怕只是类型检查）。这里用鸭子类型 + 懒导入，
    scrapling 没装时这个类根本不会被构造出来。
    """

    name = "scrapling_stealth"

    def __init__(self, config: StealthConfig | None = None) -> None:
        try:
            from scrapling.fetchers import StealthySession  # noqa: F401
        except ImportError as e:
            raise StealthUnavailable(
                f"scrapling 未安装（{INSTALL_HINT}）：{e}"
            ) from e
        self._cfg = config or StealthConfig()
        self._session = None
        self.fetches = 0

    # -- 会话生命周期 -------------------------------------------------------
    def _ensure_session(self):
        """第一次 fetch 时才启动浏览器。整批复用同一个会话：

        一次性 StealthyFetcher.fetch 是"每次请求开一个浏览器再关掉"，
        对 S3a 一批 40 条来说等于启动 40 次浏览器，不可用。
        """
        if self._session is None:
            from scrapling.fetchers import StealthySession

            self._session = StealthySession(**self._cfg.session_kwargs())
        return self._session

    def close(self) -> None:
        """关浏览器。幂等；失败只警告不抛 —— 收尾不该把数据冲掉。"""
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭隐身浏览器会话失败: %s", e)

    # -- BaseFetcher 契约 ----------------------------------------------------
    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20):
        from kwsearch.fetch import FetchResult

        t0 = time.perf_counter()
        page = self._ensure_session().fetch(url, **self._cfg.fetch_kwargs(timeout))
        self.fetches += 1

        raw = getattr(page, "body", b"") or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        # 浏览器已经把页面解过码，scrapling 内部按 utf-8 存字节
        body = raw.decode("utf-8", "replace")

        hdrs = getattr(page, "headers", None) or {}
        ct = ""
        if isinstance(hdrs, dict):
            ct = hdrs.get("content-type") or hdrs.get("Content-Type") or ""
        return FetchResult(
            status=int(getattr(page, "status", 0) or 0),
            body=body,
            content_type=ct or "text/html; charset=utf-8",
            final_url=str(getattr(page, "url", "") or url),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            fetcher=self.name,
            content_bytes=len(raw),
            raw=raw,
        )

    @property
    def robots_fetcher(self):
        """robots.txt 用轻量档取 —— 为一行规则启动一次浏览器是纯浪费。

        fetch_pages 与 CLI 都走这个属性；取不到轻量档时返回自己（退化行为）。
        """
        from kwsearch.fetch import build_fetcher

        try:
            return build_fetcher("curl_cffi")
        except RuntimeError:
            try:
                return build_fetcher("httpx")
            except RuntimeError:
                return self


__all__ = ["StealthConfig", "StealthFetcher", "StealthUnavailable", "INSTALL_HINT"]
