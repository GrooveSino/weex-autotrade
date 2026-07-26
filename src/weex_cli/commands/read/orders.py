from __future__ import annotations

import typer

from weex_cli.cli_support import compact_rows, gateway_for, invoke, selected_mode, settings_for
from weex_cli.core.safety import action_confirmation, require_execution
from weex_cli.core.symbols import demo_symbol_id, live_symbol_id
from weex_cli.exchange.rest.gateway import ensure_live
from weex_cli.presentation.output import emit

app = typer.Typer(help="Inspect and cancel WEEX contract orders.")


@app.command("open")
def open_orders(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None),
    mode: str = typer.Option("live", help="demo or live; defaults to live for backward compatibility"),
    trigger: bool = typer.Option(False, help="Show conditional orders instead of regular orders"),
    full: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    payload = invoke(lambda: gateway_for(ctx).open_orders(symbol, trigger=trigger, mode=selected))
    emit(payload if full else compact_rows(payload), json_output=json_output)


@app.command("history")
def history(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None),
    mode: str | None = typer.Option(None, help="demo or live"),
    limit: int = typer.Option(100, min=1, max=1000),
    start_time: int | None = typer.Option(None, help="Unix milliseconds"),
    end_time: int | None = typer.Option(None, help="Unix milliseconds"),
    full: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    payload = invoke(lambda: gateway_for(ctx).order_history(selected, symbol, limit, start_time, end_time))
    emit(payload if full else compact_rows(payload), json_output=json_output)


@app.command("cancel")
def cancel_order(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    order_id: str = typer.Argument(...),
    mode: str = typer.Option("live", help="demo or live; defaults to live for backward compatibility"),
    trigger: bool = typer.Option(False),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    kind = "trigger" if trigger else "regular"
    target = demo_symbol_id(symbol) if selected == "demo" else live_symbol_id(symbol)
    phrase = action_confirmation(selected, "cancel", target, order_id, kind)
    plan = {
        "status": "dry_run",
        "mode": selected,
        "action": "cancel_order",
        "symbol": target,
        "order_id": order_id,
        "trigger": trigger,
        "confirm": phrase,
    }
    if not execute:
        emit(plan, json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        if selected == "live":
            ensure_live(selected, "cancel order")
        require_execution(execute=True, supplied=confirm, expected=phrase, mode=selected, settings=settings)
        return gateway_for(ctx).cancel_order(symbol, order_id, trigger=trigger, mode=selected)

    emit(invoke(action), json_output=json_output)


@app.command("cancel-all")
def cancel_all_orders(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None),
    mode: str = typer.Option("live", help="demo or live; defaults to live for backward compatibility"),
    trigger: bool = typer.Option(False),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    target = (demo_symbol_id(symbol) if selected == "demo" else live_symbol_id(symbol)) if symbol else "ALL"
    kind = "trigger" if trigger else "regular"
    phrase = action_confirmation(selected, "cancel-all", target, kind)
    plan = {
        "status": "dry_run",
        "mode": selected,
        "action": "cancel_all_orders",
        "symbol": target,
        "trigger": trigger,
        "confirm": phrase,
    }
    if not execute:
        emit(plan, json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        if selected == "live":
            ensure_live(selected, "cancel all orders")
        require_execution(execute=True, supplied=confirm, expected=phrase, mode=selected, settings=settings)
        return gateway_for(ctx).cancel_all_orders(symbol, trigger=trigger, mode=selected)

    emit(invoke(action), json_output=json_output)
