import pytest

from fleet_api.models import ProxyType
from fleet_api.proxy import ProxyValidationError, proxy_host


@pytest.mark.parametrize(
    ("proxy_type", "value", "expected"),
    [
        (ProxyType.HTTPS, "user:pass@proxy.example.com:9341", "proxy.example.com:9341"),
        (ProxyType.HTTPS, "http://user:pass@proxy.example.com:9341", "proxy.example.com:9341"),
        (ProxyType.SOCKS5, "socks5://user:pass@proxy.example.com:1080", "proxy.example.com:1080"),
        (ProxyType.SOCKS5, "user:pass@[2001:db8::1]:1080", "[2001:db8::1]:1080"),
    ],
)
def test_proxy_host_strips_credentials(proxy_type: ProxyType, value: str, expected: str) -> None:
    assert proxy_host(proxy_type, value) == expected


def test_proxy_scheme_must_match_selected_type() -> None:
    with pytest.raises(ProxyValidationError, match="scheme"):
        proxy_host(ProxyType.SOCKS5, "https://user:pass@host.test:443")


def test_proxy_requires_port() -> None:
    with pytest.raises(ProxyValidationError, match="host and port"):
        proxy_host(ProxyType.HTTPS, "user:pass@host.test")
