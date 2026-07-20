from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from weex_cli.config import Settings
from weex_cli.errors import ConfigurationError, SafetyError


@dataclass(frozen=True)
class LiveProfile:
    path: Path
    settings: Settings = field(repr=False)
    proxy_url: str | None = field(repr=False)
    allow_live_mutations: bool
    post_only_only: bool

    def require_maker_execution(self) -> None:
        if not self.allow_live_mutations:
            raise SafetyError("live profile blocks mutations; set safety.allow_live_mutations = true")
        if not self.post_only_only:
            raise SafetyError("live Maker execution requires safety.post_only_only = true")


def load_live_profile(path: Path, *, environ: dict[str, str] | None = None) -> LiveProfile:
    resolved = path.expanduser().resolve()
    root = _project_root(Path.cwd())
    if not resolved.is_relative_to(root):
        raise ConfigurationError("live profile must be inside the current project root")
    if not resolved.is_file():
        raise ConfigurationError(f"live profile does not exist: {resolved}")

    try:
        payload = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read live profile: {resolved}") from exc

    weex = _table(payload, "weex")
    credentials = _table(payload, "credentials")
    network = _table(payload, "network")
    safety = _table(payload, "safety")
    mode = str(weex.get("mode") or "live").strip().lower()
    if mode != "live":
        raise ConfigurationError('live profile requires weex.mode = "live"')

    values = dict(os.environ if environ is None else environ)
    values.update(
        {
            "WEEX_DEFAULT_MODE": "live",
            "WEEX_API_KEY": _required_text(credentials, "api_key"),
            "WEEX_API_SECRET": _required_text(credentials, "api_secret"),
            "WEEX_API_PASSPHRASE": _required_text(credentials, "passphrase"),
        }
    )
    settings = Settings.load(environ=values)
    settings = Settings(
        credentials=settings.credentials,
        web_credentials=settings.web_credentials,
        default_mode=settings.default_mode,
        live_trading_enabled=settings.live_trading_enabled,
        timeout_ms=settings.timeout_ms,
        enable_rate_limit=settings.enable_rate_limit,
        env_file=str(resolved),
    )
    return LiveProfile(
        path=resolved,
        settings=settings,
        proxy_url=_proxy_url(network),
        allow_live_mutations=_boolean(safety, "allow_live_mutations", default=False),
        post_only_only=_boolean(safety, "post_only_only", default=True),
    )


def _project_root(start: Path) -> Path:
    current = start.resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists() or (directory / "pyproject.toml").is_file():
            return directory
    raise ConfigurationError("cannot locate the current project root")


def _table(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"live profile [{key}] must be a TOML table")
    return value


def _required_text(table: dict[str, object], key: str) -> str:
    value = str(table.get(key) or "").strip()
    if not value:
        raise ConfigurationError(f"live profile is missing credentials.{key}")
    return value


def _boolean(table: dict[str, object], key: str, *, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"live profile safety.{key} must be true or false")
    return value


def _proxy_url(network: dict[str, object]) -> str | None:
    raw = str(network.get("proxy") or "").strip()
    if not raw:
        return None
    scheme = str(network.get("scheme") or "http").strip().lower()
    if scheme not in {"http", "https", "socks5"}:
        raise ConfigurationError("live profile network.scheme must be http, https, or socks5")
    parts = raw.split(":", 3)
    if len(parts) != 4 or not all(parts):
        raise ConfigurationError("network.proxy must use host:port:username:password")
    host, port_text, username, password = parts
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigurationError("network.proxy port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("network.proxy port must be between 1 and 65535")
    return f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
