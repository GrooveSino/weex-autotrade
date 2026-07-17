from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from weex_cli.errors import ConfigurationError

Mode = Literal["demo", "live"]
TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)


@dataclass(frozen=True)
class WebCredentials:
    cc_token: str
    terminal_code: str

    @property
    def configured(self) -> bool:
        return bool(self.cc_token and self.terminal_code)


@dataclass(frozen=True)
class Settings:
    credentials: Credentials
    web_credentials: WebCredentials = WebCredentials(cc_token="", terminal_code="")
    default_mode: Mode = "demo"
    live_trading_enabled: bool = False
    timeout_ms: int = 15_000
    enable_rate_limit: bool = True
    env_file: str | None = None

    @classmethod
    def load(
        cls,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        loaded_file: str | None = None
        if environ is None:
            candidate = env_file or _project_env_file()
            if candidate:
                load_dotenv(candidate, override=False)
                loaded_file = str(candidate.resolve())
            values: Mapping[str, str] = os.environ
        else:
            values = environ

        mode = normalize_mode(values.get("WEEX_DEFAULT_MODE", "demo"))
        timeout_text = values.get("WEEX_TIMEOUT_MS", "15000").strip()
        try:
            timeout_ms = int(timeout_text)
        except ValueError as exc:
            raise ConfigurationError("WEEX_TIMEOUT_MS must be an integer") from exc
        if timeout_ms < 1_000:
            raise ConfigurationError("WEEX_TIMEOUT_MS must be at least 1000")

        credentials = Credentials(
            api_key=values.get("WEEX_API_KEY", "").strip(),
            api_secret=(values.get("WEEX_API_SECRET") or values.get("WEEX_SECRET_KEY") or "").strip(),
            passphrase=(values.get("WEEX_API_PASSPHRASE") or values.get("WEEX_PASSPHRASE") or "").strip(),
        )
        web_credentials = WebCredentials(
            cc_token=values.get("WEEX_WEB_CC_TOKEN", "").strip(),
            terminal_code=values.get("WEEX_WEB_TERMINAL_CODE", "").strip(),
        )
        return cls(
            credentials=credentials,
            web_credentials=web_credentials,
            default_mode=mode,
            live_trading_enabled=_as_bool(values.get("WEEX_LIVE_TRADING_ENABLED", "false")),
            timeout_ms=timeout_ms,
            enable_rate_limit=_as_bool(values.get("WEEX_ENABLE_RATE_LIMIT", "true")),
            env_file=loaded_file,
        )

    def require_credentials(self) -> Credentials:
        if not self.credentials.configured:
            raise ConfigurationError(
                "Missing WEEX credentials. Set WEEX_API_KEY, WEEX_API_SECRET, and WEEX_API_PASSPHRASE in .env."
            )
        return self.credentials

    def require_web_credentials(self) -> WebCredentials:
        if not self.web_credentials.configured:
            raise ConfigurationError(
                "Missing WEEX Demo Web credentials. Set WEEX_WEB_CC_TOKEN and WEEX_WEB_TERMINAL_CODE in .env."
            )
        return self.web_credentials


def normalize_mode(value: str | None) -> Mode:
    text = str(value or "demo").strip().lower()
    aliases = {"sim": "demo", "paper": "demo", "sandbox": "demo", "prod": "live"}
    text = aliases.get(text, text)
    if text not in {"demo", "live"}:
        raise ConfigurationError("mode must be demo or live")
    return text  # type: ignore[return-value]


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _project_env_file(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    directories = (current, *current.parents)
    boundary = next(
        (
            directory
            for directory in directories
            if (directory / ".git").exists() or (directory / "pyproject.toml").is_file()
        ),
        None,
    )
    if boundary is None:
        candidate = current / ".env"
        return candidate if candidate.is_file() else None
    for directory in directories:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
        if directory == boundary:
            break
    return None
