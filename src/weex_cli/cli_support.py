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
from weex_cli.output import emit_error
from weex_cli.redaction import redact_text

T = TypeVar("T")


@dataclass
class AppContext:
    env_file: Path | None = None


def app_context(ctx: typer.Context) -> AppContext:
    value = ctx.ensure_object(AppContext)
    return value


def settings_for(ctx: typer.Context) -> Settings:
    return Settings.load(app_context(ctx).env_file)


def gateway_for(ctx: typer.Context, *, private: bool = True) -> WeexGateway:
    gateway = WeexGateway(settings_for(ctx))
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
