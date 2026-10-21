"""采集层：URL → FetchResult。只负责拿到字节，不认识任何站点的版式。

档位（按优先级降级）：
    curl_cffi  —— TLS 指纹伪装，几 MB，无浏览器。默认主力
    httpx      —— 兜底，无指纹伪装
    scrapling  —— 隐身浏览器档（fetch/stealth.py），默认不启用；
                  显式 opt-in（--stealth / defaults.stealth.enabled / --fetcher scrapling）
                  才出现在候选链里，可过 Cloudflare 挑战

⚠️ 使用边界（2026-10-21 用户修订，替代早前"三不"口径）：
    - **禁止将本产品用于任何恶意行为**：未授权批量抓取、凭证爆破、薅券、
      DDoS、侵犯他人权益等。隐身档是"像普通浏览器一样访问公开页面"的能力，
      不是攻击工具 —— 是否越界取决于使用它的人。
    - 仍然不做：抓登录态（不携带任何账号 Cookie）、引代理池（ProxyRotator
      不接）、存全文/二次分发。
    - **仍然遵守 robots.txt**：隐身档只换"怎么发请求"，不换"允不允许发"
      （BaseFetcher.robots_fetcher 让 robots.txt 走轻量档，不启动浏览器）。

⚠️ Windows + 中文目录：curl_cffi 打不开非 ASCII 路径下的 CA 文件（前作 坑 17）。
   本项目目录同样含中文，所以 verify 一律走 fetch.ca.ca_bundle_arg()，
   绝不退化成 verify=False —— 那是把证书校验关掉。

重试原则（沿用 前作 的结论）：
    只对超时/连接错误/429/5xx 退避重试；
    401/403 直接放弃 —— 重试只会让风控更严，还会拖长整个任务。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from kwsearch.fetch.ca import ca_bundle_arg
from kwsearch.fetch.stealth import (
    INSTALL_HINT,
    StealthConfig,
    StealthFetcher,
    StealthUnavailable,
)

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class FetchResult:
    """一次请求的全部事实。final_url 是跟随重定向后的真实地址。"""

    status: int
    body: str
    content_type: str = ""
    final_url: str = ""
    elapsed_ms: int = 0
    fetcher: str = ""
    content_bytes: int = 0      # 原始响应字节数（limits.max_bytes 按它计，别用字符数）
    # 原始字节。body 是"猜"出来的文本（依赖响应头 charset，很多中文站不写或写错），
    # 抽取层需要自己按 <meta charset> 重解一次 —— 没有 raw 就只能眼睁睁看着 GBK 站变乱码。
    raw: bytes = b""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def size(self) -> int:
        """原始字节数。取不到时用 utf-8 编码长度近似 —— 只用于展示和配额。"""
        return self.content_bytes or len(self.body.encode("utf-8", "replace"))

    @property
    def chars(self) -> int:
        return len(self.body)


class FetchError(Exception):
    """kind: http（状态码不可重试）| timeout | conn | blocked（401/403）"""

    def __init__(self, message: str, status: int | None = None, kind: str = "http"):
        super().__init__(message)
        self.status = status
        self.kind = kind


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base: float = 1.5
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504)

    @classmethod
    def from_mapping(cls, d: dict[str, Any] | None) -> RetryPolicy:
        d = d or {}
        return cls(
            max_attempts=int(d.get("max_attempts", 3)),
            backoff_base=float(d.get("backoff_base", 1.5)),
            retry_on_status=tuple(d.get("retry_on_status", (429, 500, 502, 503, 504))),
        )


def default_headers(defaults: dict[str, Any] | None = None) -> dict[str, str]:
    """从 config.defaults 生成基础请求头。通道自己的 headers 会覆盖这些。"""
    defaults = defaults or {}
    return {
        "User-Agent": defaults.get("user_agent", DEFAULT_UA),
        "Accept-Language": defaults.get("accept_language", "zh-CN,zh;q=0.9"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _safe_text(resp) -> str:  # noqa: ANN001
    """拿文本，编码异常时退化到带替换的解码，不让一个坏字节炸掉整条链路。"""
    try:
        return resp.text
    except Exception:  # UnicodeDecodeError / LookupError 等
        raw = getattr(resp, "content", b"") or b""
        return raw.decode("utf-8", errors="replace")


class BaseFetcher(ABC):
    name = "base"

    @abstractmethod
    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20) -> FetchResult:
        raise NotImplementedError

    def close(self) -> None:
        """释放抓取器持有的资源（浏览器会话等）。默认无操作。

        fetch_pages 批末统一调用 —— 加默认实现而不是 getattr 探测，
        这样每个实现类不可能忘记声明，调用方也不用写 hasattr。
        """

    @property
    def robots_fetcher(self) -> "BaseFetcher":
        """robots.txt 用哪个抓取器取。默认自己。

        重档（隐身浏览器）覆盖它返回轻量档：为一行 robots 规则启动一次
        浏览器是纯浪费，且浏览器 UA 与 robots 判定用的 UA 也不是一回事。
        """
        return self


class CurlCffiFetcher(BaseFetcher):
    """curl_cffi：TLS/HTTP2 指纹伪装，仅几 MB，无需浏览器。M1 主力。"""

    name = "curl_cffi"

    def __init__(self, impersonate: str = "chrome"):
        from curl_cffi import requests as _req  # 延迟导入，未装则 build_fetcher 自动跳过

        self._req = _req
        self._impersonate = impersonate
        self._verify = ca_bundle_arg()

    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20) -> FetchResult:
        t0 = time.perf_counter()
        resp = self._req.get(
            url,
            headers=headers,
            impersonate=self._impersonate,
            timeout=timeout,
            verify=self._verify,
            allow_redirects=True,
        )
        content = getattr(resp, "content", b"") or b""
        return FetchResult(
            status=resp.status_code,
            body=_safe_text(resp),
            content_type=resp.headers.get("content-type", ""),
            final_url=str(resp.url),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            fetcher=self.name,
            content_bytes=len(content),
            raw=content,
        )


class HttpxFetcher(BaseFetcher):
    """httpx：无指纹伪装，纯兜底。JSON 接口用它完全够。"""

    name = "httpx"

    def fetch(self, url: str, headers: dict[str, str], timeout: int = 20) -> FetchResult:
        import httpx

        t0 = time.perf_counter()
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        content = getattr(resp, "content", b"") or b""
        return FetchResult(
            status=resp.status_code,
            body=resp.text,
            content_type=resp.headers.get("content-type", ""),
            final_url=str(resp.url),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            fetcher=self.name,
            content_bytes=len(content),
            raw=content,
        )


def build_fetcher(
    prefer: str = "auto",
    impersonate: str = "chrome",
    stealth: bool | dict[str, Any] | StealthConfig | None = None,
) -> BaseFetcher:
    """按优先级挑第一个可用实现。全部不可用则抛 RuntimeError 并列出各家的失败原因。

    `stealth` 是隐身档开关（默认 None = 不启用）：
        None/False   完全不考虑 scrapling（默认行为，零变化）
        True         用 StealthConfig() 默认档
        dict         StealthConfig.from_mapping(dict)（channels.yaml 的 defaults.stealth）
        StealthConfig 原样使用
    显式要了隐身档却装不上 scrapling → **直接抛 StealthUnavailable**，不降级：
    静默退回 curl_cffi 会让"我开了 stealth"变成假象。
    """
    cfg: StealthConfig | None = None
    if stealth is not None and stealth is not False:
        if isinstance(stealth, StealthConfig):
            cfg = stealth
        elif isinstance(stealth, dict):
            cfg = StealthConfig.from_mapping(stealth)
        else:
            cfg = StealthConfig()

    order = [prefer] if prefer != "auto" else []
    order += [n for n in ("curl_cffi", "httpx") if n not in order]
    if cfg is not None and "scrapling" not in order:
        order.insert(0, "scrapling")
    if "scrapling" in order and cfg is None:
        cfg = StealthConfig()

    errors: list[str] = []
    for name in order:
        try:
            if name in ("scrapling", "scrapling_stealth"):
                return StealthFetcher(cfg)  # 没装 → StealthUnavailable 直接上抛
            if name == "curl_cffi":
                return CurlCffiFetcher(impersonate=impersonate)
            if name == "httpx":
                return HttpxFetcher()
        except ImportError as e:
            errors.append(f"{name}: {e}")
            continue
    raise RuntimeError("无可用 Fetcher：" + ("; ".join(errors) or "依赖缺失"))


def fetch_with_retry(
    fetcher: BaseFetcher,
    url: str,
    headers: dict[str, str],
    policy: RetryPolicy | None = None,
    timeout: int = 20,
) -> FetchResult:
    """带指数退避的抓取。

    返回：200 的 FetchResult。
    抛出：FetchError（status/kind 已填好，调用方据此决定"熔断"还是"放弃"）。
    """
    policy = policy or RetryPolicy()
    last_err: Exception | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            res = fetcher.fetch(url, headers, timeout)
        except FetchError:
            # 抓取器自己已经把失败性质判清楚了（404 / 403 / 超时…），原样上抛。
            # 不这么写会有两个真 bug：① 重试一个不会自愈的状态码；
            # ② `except Exception` 重新包装时把 status 丢掉，404 变成 conn，
            #    下游 classify_error 全部误判（S3d 测试就是这么逮到的）。
            raise
        except Exception as e:  # 超时 / 连接错误 / TLS 握手失败
            last_err = e
            kind = "timeout" if "timeout" in type(e).__name__.lower() else "conn"
            if attempt < policy.max_attempts:
                wait = policy.backoff_base * (2 ** (attempt - 1))
                logger.warning("请求异常(%s) %s，%.1fs 后重试 (%d/%d): %s",
                               kind, url, wait, attempt, policy.max_attempts, e)
                time.sleep(wait)
                continue
            raise FetchError(f"重试 {policy.max_attempts} 次仍失败: {e}", kind=kind) from e

        if res.status == 200:
            logger.debug("200 %s (%dB, %dms, %s)", url, res.size, res.elapsed_ms, res.fetcher)
            return res

        if res.status in (401, 403):
            # 不重试：重试只会加重风控，而且这类状态码基本不会自愈
            raise FetchError(f"HTTP {res.status}（不放行，不重试）", status=res.status, kind="blocked")

        if res.status in policy.retry_on_status and attempt < policy.max_attempts:
            wait = policy.backoff_base * (2 ** (attempt - 1))
            logger.warning("HTTP %d %s，%.1fs 后重试 (%d/%d)",
                           res.status, url, wait, attempt, policy.max_attempts)
            time.sleep(wait)
            continue

        raise FetchError(f"HTTP {res.status}", status=res.status, kind="http")

    raise FetchError(f"重试耗尽: {last_err}", kind="conn")


__all__ = [
    "BaseFetcher",
    "CurlCffiFetcher",
    "HttpxFetcher",
    "FetchResult",
    "FetchError",
    "RetryPolicy",
    "StealthConfig",
    "StealthFetcher",
    "StealthUnavailable",
    "INSTALL_HINT",
    "build_fetcher",
    "default_headers",
    "fetch_with_retry",
]
