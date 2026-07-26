"""Shared client construction and low-level WEEX gateway safeguards."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import ccxt

from weex_cli.core.config import Settings
from weex_cli.core.errors import UnsupportedModeError, ValidationError
from weex_cli.core.models import decimal_text


def build_client(settings: Settings, *, require_private: bool, proxy_url: str | None = None) -> Any:
    try:
        import ccxt
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging guarantees this dependency
        raise SystemExit("Missing ccxt; run uv sync") from exc

    config: dict[str, Any] = {
        "enableRateLimit": settings.enable_rate_limit,
        "timeout": settings.timeout_ms,
        "requests_trust_env": True,
        "options": {"defaultType": "swap"},
    }
    if proxy_url:
        scheme = urlsplit(proxy_url).scheme.lower()
        if scheme in {"http", "https"}:
            config["httpsProxy"] = proxy_url
        elif scheme in {"socks5", "socks5h"}:
            config["socksProxy"] = proxy_url
        else:
            raise ValidationError("proxy URL must use HTTP(S) or SOCKS5")
        config["requests_trust_env"] = False
    credentials = settings.require_credentials() if require_private else settings.credentials
    if credentials.configured:
        config.update(
            {
                "apiKey": credentials.api_key,
                "secret": credentials.api_secret,
                "password": credentials.passphrase,
            }
        )
    return ccxt.weex(config)


def summarize_position_size(row: dict[str, Any]) -> str:
    raw = row.get("size", row.get("contracts", row.get("positionAmt", "0")))
    return str(decimal_text(_decimal_or_zero(raw)))


def _decimal_or_zero(value: Any):
    try:
        return abs(Decimal(str(value or "0")))
    except InvalidOperation:
        return Decimal("0")


def _position_id_for_side(rows: Any, position_side: str) -> str:
    target = position_side.strip().lower()
    if target not in {"long", "short"}:
        raise ValidationError("position_side must be long or short")
    matches: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or summarize_position_size(row) == "0":
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        side = str(row.get("side") or info.get("side") or info.get("positionSide") or "").lower()
        position_id = row.get("id") or row.get("positionId") or info.get("id") or info.get("positionId")
        if side == target and position_id is not None:
            matches.append(str(position_id))
    if not matches:
        raise ValidationError(f"no active {target} position with a position ID was found")
    if len(matches) > 1:
        raise ValidationError(f"multiple active {target} positions were found; refusing ambiguous close")
    return matches[0]


def ensure_live(mode: str, operation: str) -> None:
    if mode != "live":
        raise UnsupportedModeError(f"{operation} is not exposed by the WEEX demo API")


def _weex_mutation(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except ccxt.ExchangeError as exc:
        payload = _success_envelope(str(exc))
        if payload is None:
            raise
        return payload


def _success_envelope(message: str) -> dict[str, Any] | None:
    start = message.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(message[start:])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("code")) != "200" or str(payload.get("msg") or "").lower() != "success":
        return None
    return {"status": "accepted", "exchange_code": "200"}
