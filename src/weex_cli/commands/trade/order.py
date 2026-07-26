from __future__ import annotations

import typer

from weex_cli.cli_support import gateway_for, invoke, selected_mode, settings_for
from weex_cli.core.models import OrderIntent
from weex_cli.core.safety import order_confirmation, require_execution
from weex_cli.execution.service import TradingService
from weex_cli.presentation.output import emit

app = typer.Typer(help="Plan and place WEEX contract orders.")


def _intent(
    *,
    mode: str,
    symbol: str,
    side: str,
    position_side: str,
    order_type: str,
    quantity: str,
    price: str | None,
    time_in_force: str | None,
    client_order_id: str | None,
    take_profit: str | None,
    stop_loss: str | None,
    tp_trigger_type: str,
    sl_trigger_type: str,
    reduce_only: bool,
) -> OrderIntent:
    return OrderIntent.create(
        mode=mode,
        symbol=symbol,
        side=side,
        position_side=position_side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        tp_trigger_type=tp_trigger_type,
        sl_trigger_type=sl_trigger_type,
        reduce_only=reduce_only,
    )


def _plan_payload(intent: OrderIntent) -> dict[str, object]:
    return {
        "status": "dry_run",
        "intent": intent.as_dict(),
        "exchange_payload": intent.demo_payload() if intent.mode == "demo" else intent.live_order()[-1],
        "confirm": order_confirmation(intent),
        "safety": {
            "no_automatic_retry": True,
            "no_price_chasing": True,
            "existing_position_guard": not intent.reduce_only,
            "live_env_gate": intent.mode == "live",
        },
    }


@app.command("plan")
def plan(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    side: str = typer.Option(..., help="buy or sell"),
    position_side: str = typer.Option(..., help="long or short"),
    quantity: str = typer.Option(...),
    order_type: str = typer.Option("limit", help="limit or market"),
    price: str | None = typer.Option(None),
    time_in_force: str | None = typer.Option(None, help="Defaults to POST_ONLY for limit orders"),
    mode: str | None = typer.Option(None, help="demo or live"),
    client_order_id: str | None = typer.Option(None),
    take_profit: str | None = typer.Option(None),
    stop_loss: str | None = typer.Option(None),
    tp_trigger_type: str = typer.Option("CONTRACT_PRICE"),
    sl_trigger_type: str = typer.Option("MARK_PRICE"),
    reduce_only: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    intent = invoke(
        lambda: _intent(
            mode=selected,
            symbol=symbol,
            side=side,
            position_side=position_side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
            take_profit=take_profit,
            stop_loss=stop_loss,
            tp_trigger_type=tp_trigger_type,
            sl_trigger_type=sl_trigger_type,
            reduce_only=reduce_only,
        )
    )
    emit(_plan_payload(intent), json_output=json_output)


@app.command("place")
def place(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    side: str = typer.Option(..., help="buy or sell"),
    position_side: str = typer.Option(..., help="long or short"),
    quantity: str = typer.Option(...),
    order_type: str = typer.Option("limit", help="limit or market"),
    price: str | None = typer.Option(None),
    time_in_force: str | None = typer.Option(None),
    mode: str | None = typer.Option(None, help="demo or live"),
    client_order_id: str | None = typer.Option(None),
    take_profit: str | None = typer.Option(None),
    stop_loss: str | None = typer.Option(None),
    tp_trigger_type: str = typer.Option("CONTRACT_PRICE"),
    sl_trigger_type: str = typer.Option("MARK_PRICE"),
    reduce_only: bool = typer.Option(False),
    allow_existing: bool = typer.Option(False, help="Allow an existing position or regular order"),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    intent = invoke(
        lambda: _intent(
            mode=selected,
            symbol=symbol,
            side=side,
            position_side=position_side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
            take_profit=take_profit,
            stop_loss=stop_loss,
            tp_trigger_type=tp_trigger_type,
            sl_trigger_type=sl_trigger_type,
            reduce_only=reduce_only,
        )
    )
    phrase = order_confirmation(intent)
    if not execute:
        emit(_plan_payload(intent), json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode=intent.mode, settings=settings)
        service = TradingService(gateway_for(ctx))
        return service.submit_order(intent, allow_existing=allow_existing)

    emit(invoke(action), json_output=json_output)
