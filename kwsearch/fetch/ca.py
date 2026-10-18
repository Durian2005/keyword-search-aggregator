"""CA 证书路径适配 —— 唯一一个"提前写好、以后不许改"的基础设施件。

为什么它不能等 M1 再写：
    Windows 上的 libcurl 打不开【非 ASCII 路径】下的 CA 文件。本项目目录正好
    是 E:\\爬虫实验（指定搜索）\\，certifi 默认给出的
        E:\\爬虫实验（指定搜索）\\...\\certifi\\cacert.pem
    会让每个请求都失败：
        curl: (77) error adding trust anchors from locations: CAfile: ...
    同一份 pem 换到纯 ASCII 路径（C:\\...\\Local\\cacert.pem）就 200。
    （实测 2026-10-11，交叉验证：ASCII 路径 OK / 中文路径 FAIL。）

处理：路径含非 ASCII 时，把 pem 复制到 %LOCALAPPDATA% 再引用。
    绝【不】退回 verify=False —— 那等于关掉证书校验。
    复制也失败时宁可沿用原路径（请求可能失败），也不降低校验强度。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def ca_bundle_arg() -> bool | str:
    """给 curl_cffi 的 verify 参数算一个可用值。True = 交给它自己处理。"""
    import certifi

    pem = Path(certifi.where())
    if str(pem).isascii():
        return True

    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    if not str(base).isascii():
        return True  # 兜底目录也是非 ASCII，无能为力，不降级校验

    dst = Path(base) / "kwsearch" / "cacert.pem"
    try:
        if not dst.exists() or dst.stat().st_size != pem.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pem, dst)
        return str(dst)
    except OSError as e:
        logger.warning("CA 复制到 ASCII 路径失败（%s），沿用原路径", e)
        return True


__all__ = ["ca_bundle_arg"]
