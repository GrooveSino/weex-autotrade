from __future__ import annotations

import uuid

import typer

from weex_cli.cli_support import compact_rows, gateway_for, invoke, settings_for
from weex_cli.errors import ValidationError
from weex_cli.models import decimal_text, decimal_value
from weex_cli.output import emit
from weex_cli.safety import action_confirmation, require_execution
from weex_cli.service import TradingService
from weex_cli.symbols import live_symbol_id

app = typer.Typer(help="Manage live WEEX conditional take-profit and stop-loss orders.")


def _client_id(prefix: str = "risk") -> str:
    return f"weex-{prefix}-{uuid.uuid4().hex[:16]}"


def _valid_position_side(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"LONG", "SHORT"}:
        raise typer.BadParameter("position-side must be LONG or SHORT")
    return normalized


def _valid_trigger_type(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"CONTRACT_PRICE", "MARK_PRICE"}:
        raise typer.BadParameter("trigger-price-type must be CONTRACT_PRICE or MARK_PRICE")
    return normalized


def _nonnegative_decimal(value: str, *, name: str) -> str:
    parsed = decimal_value(value, name=name, allow_zero=True)
    assert parsed is not None
    return decimal_text(parsed) or "0"


def _positive_decimal(value: str, *, name: str) -> str:
    parsed = decimal_value(value, name=name)
    assert parsed is not None
    return decimal_text(parsed) or "0"


def _bracket_values(take_profit: str, stop_loss: str, quantity: str, side: str) -> tuple[str, str, str]:
    tp = _positive_decimal(take_profit, name="take_profit")
    sl = _positive_decimal(stop_loss, name="stop_loss")
    normalized_quantity = _nonnegative_decimal(quantity, name="quantity")
    tp_value = decimal_value(tp, name="take_profit")
    sl_value = decimal_value(sl, name="stop_loss")
    if (side == "LONG" and tp_value <= sl_value) or (side == "SHORT" and tp_value >= sl_value):
        relationship = "greater than" if side == "LONG" else "less than"
        raise ValidationError(f"{side.lower()} take-profit must be {relationship} stop-loss")
    return tp, sl, normalized_quantity


@app.command("orders")
def algo_orders(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None),
    history: bool = typer.Option(False),
    full: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    payload = invoke(lambda: gateway_for(ctx).algo_orders(symbol, history=history))
    emit(payload if full else compact_rows(payload), json_output=json_output)


@app.command("tp-sl")
def place_tp_sl(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    plan_type: str = typer.Option(..., help="TAKE_PROFIT or STOP_LOSS"),
    trigger_price: str = typer.Option(...),
    position_side: str = typer.Option(...),
    quantity: str = typer.Option("0", help="0 means the full position"),
    execute_price: str = typer.Option("0", help="0 means market execution"),
    trigger_price_type: str = typer.Option("MARK_PRICE"),
    client_algo_id: str | None = typer.Option(None),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    kind = plan_type.strip().upper()
    if kind not in {"TAKE_PROFIT", "STOP_LOSS"}:
        raise typer.BadParameter("plan-type must be TAKE_PROFIT or STOP_LOSS")
    trigger, normalized_quantity, normalized_execute_price = invoke(
        lambda: (
            _positive_decimal(trigger_price, name="trigger_price"),
            _nonnegative_decimal(quantity, name="quantity"),
            _nonnegative_decimal(execute_price, name="execute_price"),
        )
    )
    side = _valid_position_side(position_side)
    trigger_type = _valid_trigger_type(trigger_price_type)
    client_id = client_algo_id or _client_id("exit")
    phrase = action_confirmation("live", "tp-sl", live_symbol_id(symbol), kind, trigger, side, normalized_quantity)
    plan = {
        "status": "dry_run",
        "mode": "live",
        "action": "place_tp_sl",
        "symbol": live_symbol_id(symbol),
        "plan_type": kind,
        "trigger_price": trigger,
        "position_side": side,
        "quantity": normalized_quantity,
        "execute_price": normalized_execute_price,
        "trigger_price_type": trigger_type,
        "client_algo_id": client_id,
        "confirm": phrase,
    }
    if not execute:
        emit(plan, json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return gateway_for(ctx).place_tp_sl(
            symbol=symbol,
            plan_type=kind,
            trigger_price=trigger,
            position_side=side,
            client_algo_id=client_id,
            execute_price=normalized_execute_price,
            quantity=normalized_quantity,
            trigger_price_type=trigger_type,
        )

    emit(invoke(action), json_output=json_output)


@app.command("bracket")
def bracket(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    position_side: str = typer.Option(...),
    take_profit: str = typer.Option(...),
    stop_loss: str = typer.Option(...),
    quantity: str = typer.Option("0"),
    trigger_price_type: str = typer.Option("MARK_PRICE"),
    client_prefix: str | None = typer.Option(None),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    side = _valid_position_side(position_side)
    trigger_type = _valid_trigger_type(trigger_price_type)
    tp, sl, normalized_quantity = invoke(lambda: _bracket_values(take_profit, stop_loss, quantity, side))
    prefix = client_prefix or _client_id("bracket")
    if len(prefix) > 33:
        raise typer.BadParameter("client-prefix must be at most 33 characters")
    phrase = action_confirmation("live", "bracket", live_symbol_id(symbol), side, tp, sl, normalized_quantity)
    plan = {
        "status": "dry_run",
        "mode": "live",
        "action": "place_bracket",
        "symbol": live_symbol_id(symbol),
        "position_side": side,
        "take_profit": tp,
        "stop_loss": sl,
        "quantity": normalized_quantity,
        "trigger_price_type": trigger_type,
        "client_prefix": prefix,
        "submission_order": ["stop_loss", "take_profit"],
        "confirm": phrase,
    }
    if not execute:
        emit(plan, json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return TradingService(gateway_for(ctx)).place_bracket(
            symbol=symbol,
            position_side=side,
            take_profit=tp,
            stop_loss=sl,
            quantity=normalized_quantity,
            trigger_price_type=trigger_type,
            client_prefix=prefix,
        )

    emit(invoke(action), json_output=json_output)


@app.command("replace-stop")
def replace_stop(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    old_order_id: str = typer.Option(...),
    trigger_price: str = typer.Option(...),
    position_side: str = typer.Option(...),
    quantity: str = typer.Option("0"),
    trigger_price_type: str = typer.Option("MARK_PRICE"),
    client_algo_id: str | None = typer.Option(None),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    side = _valid_position_side(position_side)
    trigger_type = _valid_trigger_type(trigger_price_type)
    trigger, normalized_quantity = invoke(
        lambda: (
            _positive_decimal(trigger_price, name="trigger_price"),
            _nonnegative_decimal(quantity, name="quantity"),
        )
    )
    client_id = client_algo_id or _client_id("replace-sl")
    phrase = action_confirmation(
        "live", "replace-stop", live_symbol_id(symbol), old_order_id, trigger, side, normalized_quantity
    )
    plan = {
        "status": "dry_run",
        "mode": "live",
        "action": "replace_stop",
        "symbol": live_symbol_id(symbol),
        "old_order_id": old_order_id,
        "new_trigger_price": trigger,
        "position_side": side,
        "quantity": normalized_quantity,
        "client_algo_id": client_id,
        "replacement_order": ["submit_new", "verify_new", "cancel_old"],
        "confirm": phrase,
    }
    if not execute:
        emit(plan, json_output=json_output)
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return TradingService(gateway_for(ctx)).replace_stop(
            symbol=symbol,
            old_order_id=old_order_id,
            trigger_price=trigger,
            position_side=side,
            quantity=normalized_quantity,
            trigger_price_type=trigger_type,
            client_algo_id=client_id,
        )

    emit(invoke(action), json_output=json_output)


@app.command("modify")
def modify_tp_sl(
    ctx: typer.Context,
    order_id: str = typer.Argument(...),
    trigger_price: str = typer.Option(...),
    execute_price: str = typer.Option("0"),
    trigger_price_type: str = typer.Option("MARK_PRICE"),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    trigger, normalized_execute_price = invoke(
        lambda: (
            _positive_decimal(trigger_price, name="trigger_price"),
            _nonnegative_decimal(execute_price, name="execute_price"),
        )
    )
    trigger_type = _valid_trigger_type(trigger_price_type)
    phrase = action_confirmation("live", "modify-tp-sl", order_id, trigger, normalized_execute_price, trigger_type)
    if not execute:
        emit(
            {
                "status": "dry_run",
                "mode": "live",
                "action": "modify_tp_sl",
                "order_id": order_id,
                "trigger_price": trigger,
                "execute_price": normalized_execute_price,
                "trigger_price_type": trigger_type,
                "confirm": phrase,
            },
            json_output=json_output,
        )
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return gateway_for(ctx).modify_tp_sl(
            order_id=order_id,
            trigger_price=trigger,
            execute_price=normalized_execute_price,
            trigger_price_type=trigger_type,
        )

    emit(invoke(action), json_output=json_output)


@app.command("cancel")
def cancel_algo(
    ctx: typer.Context,
    order_id: str = typer.Argument(...),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    phrase = action_confirmation("live", "cancel-algo", order_id)
    if not execute:
        emit(
            {"status": "dry_run", "mode": "live", "action": "cancel_algo", "order_id": order_id, "confirm": phrase},
            json_output=json_output,
        )
        return

    def action():
        settings = settings_for(ctx)
        require_execution(execute=True, supplied=confirm, expected=phrase, mode="live", settings=settings)
        return gateway_for(ctx).cancel_algo_order(order_id)

    emit(invoke(action), json_output=json_output)
