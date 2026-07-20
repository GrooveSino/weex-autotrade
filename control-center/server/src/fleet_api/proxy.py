from __future__ import annotations

from urllib.parse import urlsplit

from .models import ProxyType


class ProxyValidationError(ValueError):
    pass


def proxy_host(proxy_type: ProxyType, raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise ProxyValidationError("proxy URL is required")

    default_scheme = "https" if proxy_type is ProxyType.HTTPS else "socks5"
    parsed = urlsplit(value if "://" in value else f"{default_scheme}://{value}")
    allowed = {"http", "https"} if proxy_type is ProxyType.HTTPS else {"socks5", "socks5h"}
    if parsed.scheme.lower() not in allowed:
        raise ProxyValidationError(f"proxy scheme must match {proxy_type.value}")
    if not parsed.hostname or parsed.port is None:
        raise ProxyValidationError("proxy must include host and port")
    if not 1 <= parsed.port <= 65535:
        raise ProxyValidationError("proxy port must be between 1 and 65535")

    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{host}:{parsed.port}"
