from __future__ import annotations

from typing import Annotated, Any

import typer

from weex_cli.cli_support import compact_rows, gateway_for, invoke, selected_mode, settings_for
from weex_cli.presentation.output import emit
from weex_cli.trade_reporting import TradeReportService, current_timestamp_ms


def status(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Contract base asset")] = "BTC",
    mode: Annotated[str | None, typer.Option(help="demo or live; defaults to project config")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Stable machine-readable output")] = False,
) -> None:
    """Show the account state needed before a trading workflow."""
    selected = selected_mode(ctx, mode)
    settings = settings_for(ctx)
    gateway = gateway_for(ctx)
    positions = _read_section(lambda: gateway.positions(selected, symbol))
    orders = _read_section(lambda: gateway.open_orders(symbol, mode=selected))
    emit(
        {
            "view": "status",
            "mode": selected,
            "symbol": symbol.upper(),
            "credentials": {
                "api": settings.credentials.configured,
                "web": settings.web_credentials.configured,
                "live_enabled": settings.live_trading_enabled,
            },
            "position": positions,
            "orders": orders,
        },
        json_output=json_output,
    )


def activity(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Contract base asset")] = "BTC",
    hours: Annotated[int, typer.Option(min=1, max=24 * 30, help="Lookback window in hours")] = 24,
    mode: Annotated[str | None, typer.Option(help="demo or live; defaults to project config")] = None,
    details: Annotated[bool, typer.Option("--details", help="Show normalized execution rows")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Stable machine-readable output")] = False,
) -> None:
    """Show recent executed volume without requiring timestamps."""
    selected = selected_mode(ctx, mode)
    end_time = current_timestamp_ms()
    start_time = end_time - hours * 60 * 60 * 1000
    payload = invoke(
        lambda: TradeReportService(gateway_for(ctx)).report(
            mode=selected,
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
        )
    )
    payload = {**payload, "view": "activity"}
    if not details:
        payload["trades"] = []
    emit(payload, json_output=json_output)


def _read_section(action) -> dict[str, Any]:
    try:
        rows = action()
    except Exception as exc:  # noqa: BLE001 - status remains useful when one read surface is unavailable
        return {"count": None, "rows": [], "error": type(exc).__name__}
    compact = compact_rows(rows)
    normalized = compact if isinstance(compact, list) else []
    return {"count": len(normalized), "rows": normalized, "error": None}
