from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

LEGACY_OWNER_USER_ID = "gg"

# This context is populated only for authenticated HTTP/SSE requests. Background
# workers intentionally run without it: they operate on already-owned instance
# ids and must survive a browser logout, API reconnect, or frontend release.
_current_owner_user_id: ContextVar[str | None] = ContextVar("fleet_owner_user_id", default=None)


def current_owner_user_id() -> str | None:
    return _current_owner_user_id.get()


def set_current_owner_user_id(user_id: str | None) -> Token[str | None]:
    return _current_owner_user_id.set(user_id)


def reset_current_owner_user_id(token: Token[str | None]) -> None:
    _current_owner_user_id.reset(token)


@contextmanager
def owner_scope(user_id: str | None) -> Iterator[None]:
    token = set_current_owner_user_id(user_id)
    try:
        yield
    finally:
        reset_current_owner_user_id(token)
