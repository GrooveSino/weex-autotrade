from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from secrets import compare_digest

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, SecretStr, field_validator

SESSION_COOKIE_NAME = "weex_fleet_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
_PASSWORD_LENGTH = 32


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=48)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in normalized):
            raise ValueError("username must use lowercase letters, digits, _ or -")
        return normalized


@dataclass(frozen=True, slots=True)
class LocalUser:
    user_id: str
    password: str


class LocalUserRegistry:
    """Owner-only local TOML registry. Passwords never leave this module."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    @staticmethod
    def _validate_user_id(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("local user id must be a string")
        user_id = value.strip()
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not user_id or len(user_id) > 48 or any(char not in allowed for char in user_id):
            raise ValueError("local user id must use lowercase letters, digits, _ or -")
        return user_id

    @staticmethod
    def _validate_password(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("local user password must be a string")
        if len(value) != _PASSWORD_LENGTH or not value.isascii() or any(char.isspace() for char in value):
            raise ValueError("local user passwords must be exactly 32 printable ASCII characters")
        return value

    def _validate_permissions(self) -> None:
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError as exc:
            raise RuntimeError(f"local user registry is missing: {self.path}") from exc
        if mode & 0o077:
            raise RuntimeError("local user registry must be owner-only (0600)")

    def load(self) -> dict[str, LocalUser]:
        self._validate_permissions()
        try:
            with self.path.open("rb") as source:
                raw = tomllib.load(source)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError("local user registry is invalid TOML") from exc
        users_raw = raw.get("users")
        if not isinstance(users_raw, dict) or not users_raw:
            raise RuntimeError("local user registry must define at least one user")
        users: dict[str, LocalUser] = {}
        for raw_user_id, payload in users_raw.items():
            user_id = self._validate_user_id(raw_user_id)
            if not isinstance(payload, dict):
                raise RuntimeError("local user registry entries must be TOML tables")
            users[user_id] = LocalUser(user_id=user_id, password=self._validate_password(payload.get("password")))
        return users

    def authenticate(self, username: str, password: str) -> LocalUser | None:
        user = self.load().get(username)
        if user is None or not compare_digest(user.password, password):
            return None
        return user

    def user(self, user_id: str) -> LocalUser | None:
        return self.load().get(user_id)

    def issue_session(self, user: LocalUser, now_seconds: int | None = None) -> str:
        now = int(time.time()) if now_seconds is None else now_seconds
        payload = json.dumps({"u": user.user_id, "e": now + SESSION_TTL_SECONDS}, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(user.password.encode("ascii"), encoded, hashlib.sha256).digest()
        return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"

    def verify_session(self, token: str, now_seconds: int | None = None) -> str | None:
        try:
            encoded, encoded_signature = token.split(".", 1)
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            user_id = self._validate_user_id(payload.get("u"))
            expires_at = int(payload.get("e"))
            signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if expires_at < (int(time.time()) if now_seconds is None else now_seconds):
            return None
        user = self.user(user_id)
        if user is None:
            return None
        expected = hmac.new(user.password.encode("ascii"), encoded.encode("ascii"), hashlib.sha256).digest()
        return user_id if hmac.compare_digest(signature, expected) else None


def registry_path_from_env() -> Path:
    raw = os.environ.get("FLEET_USERS_TOML", "~/Library/Application Support/WEEXFleet/users.toml")
    return Path(raw).expanduser()


def authentication_required_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="local login is required")
