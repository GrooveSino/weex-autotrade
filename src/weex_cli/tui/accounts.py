from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import quote, urlsplit

from weex_cli.core.config import Settings
from weex_cli.core.errors import ConfigurationError, SafetyError
from weex_cli.live_profile import LiveProfile

ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROXY_SCHEMES = {"http", "https", "socks5"}
DEFAULT_ACCOUNT_FILE = Path("config/tui-accounts.toml")
DEFAULT_RUNTIME_DIRECTORY = Path("data/tui-runtime")


@dataclass(frozen=True, slots=True)
class TuiSafety:
    allow_live_mutations: bool
    post_only_only: bool


@dataclass(frozen=True, slots=True, repr=False)
class TuiAccount:
    account_id: str
    name: str
    enabled: bool
    api_key: str
    api_secret: str
    passphrase: str
    proxy_scheme: str
    proxy: str

    def __repr__(self) -> str:
        return (
            f"TuiAccount(account_id={self.account_id!r}, name={self.name!r}, enabled={self.enabled!r}, "
            f"api_key_tail={self.api_key_tail!r}, proxy_host={self.proxy_host!r})"
        )

    @property
    def api_key_tail(self) -> str:
        return self.api_key[-4:].upper().rjust(4, "-")

    @property
    def proxy_host(self) -> str:
        return self.proxy.split(":", 1)[0]

    @property
    def proxy_url(self) -> str:
        host, port, username, password = self.proxy.split(":", 3)
        return f"{self.proxy_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"

    def live_profile(
        self,
        catalog_path: Path,
        safety: TuiSafety,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> LiveProfile:
        values = dict(os.environ if environ is None else environ)
        values.update(
            {
                "WEEX_DEFAULT_MODE": "live",
                "WEEX_API_KEY": self.api_key,
                "WEEX_API_SECRET": self.api_secret,
                "WEEX_API_PASSPHRASE": self.passphrase,
            }
        )
        settings = Settings.load(environ=values)
        return LiveProfile(
            path=catalog_path,
            settings=settings,
            proxy_url=self.proxy_url,
            allow_live_mutations=safety.allow_live_mutations,
            post_only_only=safety.post_only_only,
        )


@dataclass(frozen=True, slots=True)
class TuiAccountCatalog:
    path: Path
    safety: TuiSafety
    beta_url: str = field(repr=False)
    accounts: tuple[TuiAccount, ...]

    def get(self, account_id: str) -> TuiAccount:
        for account in self.accounts:
            if account.account_id == account_id:
                return account
        raise ConfigurationError("selected TUI account no longer exists")


def load_tui_account_catalog(path: Path = DEFAULT_ACCOUNT_FILE, *, start: Path | None = None) -> TuiAccountCatalog:
    root = project_root(start or Path.cwd())
    candidate = path.expanduser()
    candidate = candidate if candidate.is_absolute() else (start or Path.cwd()) / candidate
    if candidate.is_symlink():
        raise ConfigurationError("TUI account file must not be a symbolic link")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ConfigurationError("TUI account file must be inside the current project root")
    try:
        file_stat = resolved.stat()
    except FileNotFoundError:
        raise ConfigurationError(f"TUI account file does not exist: {resolved}") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise ConfigurationError("TUI account file must be a regular file")
    if file_stat.st_mode & 0o077:
        raise ConfigurationError("TUI account file permissions must be 0600 or stricter")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise ConfigurationError("TUI account file must be owned by the current user")
    try:
        payload = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError("TUI account file is unreadable or invalid TOML") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ConfigurationError("TUI account file requires schema_version = 1")
    safety = _parse_safety(payload.get("safety"))
    beta_url = _parse_beta_url(payload.get("beta"))
    default_passphrase = _parse_default_passphrase(payload.get("defaults"))
    rows = payload.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ConfigurationError("TUI account file must contain at least one [[accounts]] entry")
    accounts = tuple(
        _parse_account(row, index, default_passphrase=default_passphrase) for index, row in enumerate(rows, start=1)
    )
    identifiers = [account.account_id for account in accounts]
    if len(identifiers) != len(set(identifiers)):
        raise ConfigurationError("TUI account names must be unique")
    normalized_names = [_normalized_account_name(account.name) for account in accounts]
    if len(normalized_names) != len(set(normalized_names)):
        raise ConfigurationError("TUI account names must be unique")
    return TuiAccountCatalog(path=resolved, safety=safety, beta_url=beta_url, accounts=accounts)


def project_root(start: Path) -> Path:
    current = start.resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists() or (directory / "pyproject.toml").is_file():
            return directory
    raise ConfigurationError("cannot locate the current project root")


def _parse_safety(value: object) -> TuiSafety:
    if not isinstance(value, dict):
        raise ConfigurationError("TUI account file requires a [safety] table")
    allow_live_mutations = value.get("allow_live_mutations", False)
    post_only_only = value.get("post_only_only", True)
    if not isinstance(allow_live_mutations, bool) or not isinstance(post_only_only, bool):
        raise ConfigurationError("TUI safety values must be true or false")
    if not post_only_only:
        raise ConfigurationError("TUI requires safety.post_only_only = true")
    return TuiSafety(allow_live_mutations=allow_live_mutations, post_only_only=post_only_only)


def _parse_beta_url(value: object) -> str:
    if not isinstance(value, dict):
        raise ConfigurationError("TUI account file requires a [beta] table")
    raw_url = value.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ConfigurationError("TUI account file requires beta.url")
    url = raw_url.strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ConfigurationError("TUI beta.url must be a valid HTTP(S) URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.fragment:
        raise ConfigurationError("TUI beta.url must be a valid HTTP(S) URL without a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("TUI beta.url must be a valid HTTP(S) URL")
    return url


def _parse_default_passphrase(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigurationError("TUI account file [defaults] must be a table")
    return _optional_text(value, "passphrase")


def _parse_account(value: object, index: int, *, default_passphrase: str | None) -> TuiAccount:
    if not isinstance(value, dict):
        raise ConfigurationError(f"TUI account entry {index} must be a table")
    name = _required_text(value, "name", index)
    if len(name) > 64:
        raise ConfigurationError(f"TUI account entry {index} name must not exceed 64 characters")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"TUI account entry {index} enabled must be true or false")
    proxy_scheme = str(value.get("proxy_scheme") or "http").strip().lower()
    if proxy_scheme not in PROXY_SCHEMES:
        raise ConfigurationError(f"TUI account entry {index} has an invalid proxy scheme")
    proxy = _required_text(value, "proxy", index)
    _validate_proxy(proxy, index)
    passphrase = _optional_text(value, "passphrase") or default_passphrase
    if not passphrase:
        raise ConfigurationError(f"TUI account entry {index} requires passphrase or a global defaults.passphrase")
    return TuiAccount(
        account_id=_runtime_account_id(name),
        name=name,
        enabled=enabled,
        api_key=_required_text(value, "api_key", index),
        api_secret=_required_text(value, "api_secret", index),
        passphrase=passphrase,
        proxy_scheme=proxy_scheme,
        proxy=proxy,
    )


def _runtime_account_id(name: str) -> str:
    digest = hashlib.sha256(_normalized_account_name(name).encode("utf-8")).hexdigest()[:16]
    return f"account-{digest}"


def _normalized_account_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _required_text(value: Mapping[str, object], key: str, index: int) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ConfigurationError(f"TUI account entry {index} requires {key}")
    return text.strip()


def _optional_text(value: Mapping[str, object], key: str) -> str | None:
    text = value.get(key)
    if text is None:
        return None
    if not isinstance(text, str):
        raise ConfigurationError(f"TUI account {key} must be text")
    return text.strip() or None


def _validate_proxy(value: str, index: int) -> None:
    parts = value.split(":", 3)
    if len(parts) != 4 or not all(parts):
        raise ConfigurationError(f"TUI account entry {index} proxy must use host:port:username:password")
    try:
        port = int(parts[1])
    except ValueError:
        raise ConfigurationError(f"TUI account entry {index} proxy port must be an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"TUI account entry {index} proxy port is outside 1-65535")


class AccountInUseError(SafetyError):
    pass


class AccountLease:
    def __init__(self, account_id: str, runtime_directory: Path = DEFAULT_RUNTIME_DIRECTORY) -> None:
        if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise ConfigurationError("invalid account ID for runtime lock")
        self.account_id = account_id
        self.path = runtime_directory / "locks" / f"{account_id}.lock"
        self._handle: object | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> Self:
        if self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise AccountInUseError(f"account {self.account_id!r} is already in use by another terminal") from None
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "account_id": self.account_id,
                "pid": os.getpid(),
                "host": socket.gethostname(),
            },
            handle,
            separators=(",", ":"),
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    @classmethod
    def is_locked(cls, account_id: str, runtime_directory: Path = DEFAULT_RUNTIME_DIRECTORY) -> bool:
        lease = cls(account_id, runtime_directory)
        try:
            lease.acquire()
        except AccountInUseError:
            return True
        else:
            lease.release()
            return False
