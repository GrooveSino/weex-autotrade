from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import ccxt
import typer

from weex_cli.config import Settings, normalize_mode
from weex_cli.errors import WeexCliError
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile, load_live_profile
from weex_cli.output import emit_error
from weex_cli.redaction import redact_text

T = TypeVar("T")


@dataclass
class AppContext:
    env_file: Path | None = None
    profile_file: Path | None = None
    profile: LiveProfile | None = None
    language: str = "zh"


def app_context(ctx: typer.Context) -> AppContext:
    value = ctx.ensure_object(AppContext)
    return value


def settings_for(ctx: typer.Context) -> Settings:
    context = app_context(ctx)
    if context.profile_file is None:
        return Settings.load(context.env_file)
    return profile_for(ctx).settings


def profile_for(ctx: typer.Context) -> LiveProfile:
    context = app_context(ctx)
    if context.profile_file is None:
        raise ValueError("--profile is required for this operation")
    if context.env_file is not None:
        raise ValueError("--profile and --env-file cannot be used together")
    if context.profile is None:
        context.profile = load_live_profile(context.profile_file)
    return context.profile


def gateway_for(ctx: typer.Context, *, private: bool = True) -> WeexGateway:
    context = app_context(ctx)
    proxy_url = profile_for(ctx).proxy_url if context.profile_file is not None else None
    gateway = WeexGateway(settings_for(ctx), proxy_url=proxy_url)
    if not private:
        gateway.public_client()
    return gateway


def selected_mode(ctx: typer.Context, mode: str | None) -> str:
    settings = settings_for(ctx)
    return normalize_mode(mode or settings.default_mode)


def invoke(action: Callable[[], T]) -> T:
    try:
        return action()
    except (WeexCliError, ccxt.BaseError, ValueError) as exc:
        emit_error(redact_text(exc))
        raise typer.Exit(1) from exc


def compact_rows(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [_compact_row(row) for row in rows]


def _compact_row(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    keys = (
        "id",
        "orderId",
        "algoId",
        "clientOrderId",
        "clientAlgoId",
        "symbol",
        "side",
        "positionSide",
        "status",
        "algoStatus",
        "type",
        "orderType",
        "price",
        "triggerPrice",
        "amount",
        "origQty",
        "executedQty",
        "size",
        "contracts",
        "leverage",
        "marginType",
        "unrealizePnl",
        "liquidatePrice",
        "timestamp",
        "datetime",
    )
    compact = {key: row[key] for key in keys if key in row and row[key] is not None}
    if compact:
        return compact
    return row
