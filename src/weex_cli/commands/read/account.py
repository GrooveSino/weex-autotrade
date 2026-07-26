from __future__ import annotations

import typer

from weex_cli.cli_support import compact_rows, gateway_for, invoke, selected_mode, settings_for
from weex_cli.core.safety import action_confirmation, require_execution
from weex_cli.core.symbols import live_symbol_id
from weex_cli.exchange.rest.gateway import ensure_live
from weex_cli.presentation.output import emit

app = typer.Typer(help="Read and manage WEEX contract account state.")


@app.command("balance")
def balance(
    ctx: typer.Context,
    mode: str | None = typer.Option(None, help="demo or live; defaults to WEEX_DEFAULT_MODE"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    payload = invoke(lambda: gateway_for(ctx).balance(selected))
    emit(payload, json_output=json_output)


@app.command("positions")
def positions(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None),
    mode: str | None = typer.Option(None, help="demo or live"),
    full: bool = typer.Option(False, help="Show full exchange payloads"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    payload = invoke(lambda: gateway_for(ctx).positions(selected, symbol))
    emit(payload if full else compact_rows(payload), json_output=json_output)


@app.command("configure")
def configure_position(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    leverage: int = typer.Option(..., min=1, max=125),
    margin_mode: str = typer.Option("isolated", help="isolated or cross"),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option("", help="Exact phrase printed by dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    margin = margin_mode.strip().lower()
    if margin not in {"isolated", "cross"}:
        raise typer.BadParameter("margin-mode must be isolated or cross")
    phrase = action_confirmation("live", "configure", live_symbol_id(symbol), margin, f"{leverage}x")
    plan = {
        "status": "dry_run",
        "mode": "live",
        "action": "configure_position",
        "symbol": live_symbol_id(symbol),
        "margin_mode": margin,
        "leverage": leverage,
        "confirm": phrase,
    }
    if not execute:
        emit(plan, json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        ensure_live("live", "position configuration")
        return gateway_for(ctx).configure_position(symbol, leverage, margin)

    emit(invoke(action), json_output=json_output)


@app.command("close")
def close_position(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    position_side: str | None = typer.Option(None, help="Optional LONG or SHORT"),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    side = position_side.strip().upper() if position_side else "ALL"
    if side not in {"ALL", "LONG", "SHORT"}:
        raise typer.BadParameter("position-side must be LONG or SHORT")
    phrase = action_confirmation("live", "close", live_symbol_id(symbol), side)
    if not execute:
        emit(
            {
                "status": "dry_run",
                "mode": "live",
                "action": "close_position",
                "symbol": live_symbol_id(symbol),
                "position_side": side,
                "confirm": phrase,
            },
            json_output=json_output,
        )
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return gateway_for(ctx).close_position(symbol, None if side == "ALL" else side)

    emit(invoke(action), json_output=json_output)


@app.command("close-all")
def close_all_positions(
    ctx: typer.Context,
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    phrase = action_confirmation("live", "close-all", "positions")
    if not execute:
        emit(
            {"status": "dry_run", "mode": "live", "action": "close_all_positions", "confirm": phrase},
            json_output=json_output,
        )
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return gateway_for(ctx).close_all_positions()

    emit(invoke(action), json_output=json_output)
