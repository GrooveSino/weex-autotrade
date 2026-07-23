import pytest

from fleet_api.models import ProxyType
from fleet_api.proxy import ProxyValidationError, normalize_proxy_url, proxy_host


@pytest.mark.parametrize(
    ("proxy_type", "value", "expected"),
    [
        (ProxyType.HTTP, "user:pass@proxy.example.com:9341", "proxy.example.com:9341"),
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


def test_http_proxy_normalizes_to_an_explicit_http_scheme() -> None:
    assert normalize_proxy_url(ProxyType.HTTP, "user:pass@proxy.example.com:8080") == (
        "http://user:pass@proxy.example.com:8080"
    )

    with pytest.raises(ProxyValidationError, match="scheme"):
        normalize_proxy_url(ProxyType.HTTP, "https://user:pass@proxy.example.com:8080")


def test_webshare_host_port_username_password_format_is_supported() -> None:
    assert normalize_proxy_url(ProxyType.HTTP, "proxy.example.com:8080:user:pass") == (
        "http://user:pass@proxy.example.com:8080"
    )


def test_invalid_proxy_port_is_reported_as_validation_error() -> None:
    with pytest.raises(ProxyValidationError, match="port must be an integer"):
        proxy_host(ProxyType.HTTP, "proxy.example.com:not-a-port:user:pass")


def test_proxy_requires_port() -> None:
    with pytest.raises(ProxyValidationError, match="host and port"):
        proxy_host(ProxyType.HTTPS, "user:pass@host.test")


def test_none_proxy_is_not_a_url() -> None:
    with pytest.raises(ProxyValidationError, match="proxy URL is required"):
        proxy_host(ProxyType.NONE, "")
