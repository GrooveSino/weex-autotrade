from __future__ import annotations

from urllib.parse import urlsplit

from fleet_api.models import ProxyType


class ProxyValidationError(ValueError):
    pass


def _parsed_proxy(proxy_type: ProxyType, raw_value: str):
    value = raw_value.strip()
    if proxy_type is ProxyType.NONE or not value:
        raise ProxyValidationError("proxy URL is required")
    if value.count(":") == 3 and "://" not in value and "@" not in value:
        host, port, username, password = value.split(":", 3)
        value = f"{username}:{password}@{host}:{port}"
    default_scheme = "http" if proxy_type is ProxyType.HTTP else "https" if proxy_type is ProxyType.HTTPS else "socks5"
    parsed = urlsplit(value if "://" in value else f"{default_scheme}://{value}")
    allowed = {
        ProxyType.HTTP: {"http"},
        ProxyType.HTTPS: {"http", "https"},
        ProxyType.SOCKS5: {"socks5", "socks5h"},
    }[proxy_type]
    if parsed.scheme.lower() not in allowed:
        raise ProxyValidationError(f"proxy scheme must match {proxy_type.value}")
    if not parsed.hostname:
        raise ProxyValidationError("proxy must include host and port")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyValidationError("proxy port must be an integer") from exc
    if port is None:
        raise ProxyValidationError("proxy must include host and port")
    if not 1 <= port <= 65535:
        raise ProxyValidationError("proxy port must be between 1 and 65535")
    return parsed


def proxy_host(proxy_type: ProxyType, raw_value: str) -> str:
    parsed = _parsed_proxy(proxy_type, raw_value)
    assert parsed.hostname is not None and parsed.port is not None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{host}:{parsed.port}"


def normalize_proxy_url(proxy_type: ProxyType, raw_value: str) -> str:
    parsed = _parsed_proxy(proxy_type, raw_value)
    assert parsed.hostname is not None and parsed.port is not None
    credentials = ""
    if parsed.username is not None:
        credentials = parsed.username
        if parsed.password is not None:
            credentials += f":{parsed.password}"
        credentials += "@"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    scheme = parsed.scheme.lower()
    return f"{scheme}://{credentials}{host}:{parsed.port}"
