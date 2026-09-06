"""HTTPS 连接的证书处理。

Mac 上很常见的一个坑：Python 默认不读系统钥匙串里的根证书，
于是任何 https 请求都报 CERTIFICATE_VERIFY_FAILED。
这里按可靠性从高到低依次尝试，让用户不需要额外装任何东西。
"""

from __future__ import annotations

import ssl
import urllib.request
from typing import Any

_context: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """返回一个能正常校验证书的 SSLContext（结果会缓存）。"""
    global _context
    if _context is not None:
        return _context

    # 1) certifi：最可靠。用户装过 requests / httpx 的话通常已经有了
    try:
        import certifi

        _context = ssl.create_default_context(cafile=certifi.where())
        return _context
    except Exception:  # noqa: BLE001 - 没装 certifi 或证书文件坏了都往下走
        pass

    # 2) 系统默认（Linux 和配置正确的 Mac 走这条）
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        _context = context
        return _context

    # 3) 兜底：macOS 钥匙串导出的证书路径
    for path in (
        "/etc/ssl/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
    ):
        try:
            candidate = ssl.create_default_context(cafile=path)
        except (OSError, ssl.SSLError):
            continue
        if candidate.cert_store_stats().get("x509_ca", 0) > 0:
            _context = candidate
            return _context

    _context = context  # 一个 CA 都没找到，让它照常报错，别静默降级成不校验
    return _context


def urlopen(request: Any, timeout: int = 30) -> Any:
    """带正确证书配置的 urlopen。"""
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())


def is_cert_error(err: BaseException) -> bool:
    """判断一个异常是不是「找不到根证书」这一类问题。"""
    reason = getattr(err, "reason", err)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(err)


CERT_HELP = (
    "Python 找不到根证书，所以拒绝连接 HTTPS（不是你的 Key 或网络的问题）。\n"
    "在 Terminal 里执行下面这一条，装上证书包，然后回来重试：\n"
    "    python3 -m pip install --user certifi\n"
    "如果上面那条没用，再试：\n"
    '    open "/Applications/Python 3.13/Install Certificates.command"\n'
    "（把 3.13 换成你实际的版本号；文件夹名可以在「访达 → 应用程序」里看到）"
)
