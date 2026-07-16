from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from weex_cli.config import Mode, normalize_mode
from weex_cli.errors import ValidationError
from weex_cli.symbols import ccxt_swap_symbol, demo_symbol_id, live_symbol_id

Side = Literal["buy", "sell"]
PositionSide = Literal["long", "short"]
OrderType = Literal["limit", "market"]
TimeInForce = Literal["GTC", "IOC", "FOK", "POST_ONLY"]
TriggerPriceType = Literal["CONTRACT_PRICE", "MARK_PRICE"]

_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,36}$")


def decimal_value(
    value: str | int | float | Decimal | None,
    *,
    name: str,
    required: bool = True,
    allow_zero: bool = False,
) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ValidationError(f"{name} is required")
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"{name} must be numeric") from exc
    if not result.is_finite() or result < 0 or (result == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise ValidationError(f"{name} must be {qualifier}")
    return result


def decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class OrderIntent:
    mode: Mode
    symbol: str
    side: Side
    position_side: PositionSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None
    time_in_force: TimeInForce | None
    client_order_id: str
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    tp_trigger_type: TriggerPriceType = "CONTRACT_PRICE"
    sl_trigger_type: TriggerPriceType = "MARK_PRICE"
    reduce_only: bool = False

    @classmethod
    def create(
        cls,
        *,
        mode: str,
        symbol: str,
        side: str,
        position_side: str,
        order_type: str,
        quantity: str | float | Decimal,
        price: str | float | Decimal | None = None,
        time_in_force: str | None = None,
        client_order_id: str | None = None,
        take_profit: str | float | Decimal | None = None,
        stop_loss: str | float | Decimal | None = None,
        tp_trigger_type: str = "CONTRACT_PRICE",
        sl_trigger_type: str = "MARK_PRICE",
        reduce_only: bool = False,
    ) -> OrderIntent:
        normalized_type = order_type.strip().lower()
        normalized_side = side.strip().lower()
        normalized_position = position_side.strip().lower()
        if normalized_type not in {"limit", "market"}:
            raise ValidationError("order_type must be limit or market")
        if normalized_side not in {"buy", "sell"}:
            raise ValidationError("side must be buy or sell")
        if normalized_position not in {"long", "short"}:
            raise ValidationError("position_side must be long or short")

        if reduce_only:
            expected_side = "sell" if normalized_position == "long" else "buy"
        else:
            expected_side = "buy" if normalized_position == "long" else "sell"
        if normalized_side != expected_side:
            action = "reduce" if reduce_only else "open"
            raise ValidationError(f"{action} {normalized_position} orders must use side={expected_side}")

        normalized_tif = str(time_in_force or ("POST_ONLY" if normalized_type == "limit" else "")).upper() or None
        if normalized_type == "limit" and normalized_tif not in {"GTC", "IOC", "FOK", "POST_ONLY"}:
            raise ValidationError("time_in_force must be GTC, IOC, FOK, or POST_ONLY")
        if normalized_type == "market" and normalized_tif is not None:
            raise ValidationError("market orders cannot set time_in_force")

        normalized_tp_type = tp_trigger_type.strip().upper()
        normalized_sl_type = sl_trigger_type.strip().upper()
        if normalized_tp_type not in {"CONTRACT_PRICE", "MARK_PRICE"}:
            raise ValidationError("tp_trigger_type must be CONTRACT_PRICE or MARK_PRICE")
        if normalized_sl_type not in {"CONTRACT_PRICE", "MARK_PRICE"}:
            raise ValidationError("sl_trigger_type must be CONTRACT_PRICE or MARK_PRICE")

        limit_price = decimal_value(price, name="price", required=normalized_type == "limit")
        if normalized_type == "market":
            limit_price = None
        order_id = client_order_id or f"weex-cli-{uuid.uuid4().hex[:20]}"
        if not _CLIENT_ORDER_ID_RE.fullmatch(order_id):
            raise ValidationError("client_order_id must contain 1-36 ASCII letters, digits, or . _ : / -")

        take_profit_value = decimal_value(take_profit, name="take_profit", required=False)
        stop_loss_value = decimal_value(stop_loss, name="stop_loss", required=False)
        if normalized_type == "limit" and not reduce_only:
            assert limit_price is not None
            if normalized_position == "long":
                if take_profit_value is not None and take_profit_value <= limit_price:
                    raise ValidationError("long take_profit must be greater than the entry price")
                if stop_loss_value is not None and stop_loss_value >= limit_price:
                    raise ValidationError("long stop_loss must be less than the entry price")
            else:
                if take_profit_value is not None and take_profit_value >= limit_price:
                    raise ValidationError("short take_profit must be less than the entry price")
                if stop_loss_value is not None and stop_loss_value <= limit_price:
                    raise ValidationError("short stop_loss must be greater than the entry price")

        return cls(
            mode=normalize_mode(mode),
            symbol=symbol,
            side=normalized_side,  # type: ignore[arg-type]
            position_side=normalized_position,  # type: ignore[arg-type]
            order_type=normalized_type,  # type: ignore[arg-type]
            quantity=decimal_value(quantity, name="quantity"),  # type: ignore[arg-type]
            price=limit_price,
            time_in_force=normalized_tif,  # type: ignore[arg-type]
            client_order_id=order_id,
            take_profit=take_profit_value,
            stop_loss=stop_loss_value,
            tp_trigger_type=normalized_tp_type,  # type: ignore[arg-type]
            sl_trigger_type=normalized_sl_type,  # type: ignore[arg-type]
            reduce_only=reduce_only,
        )

    @property
    def exchange_symbol(self) -> str:
        return demo_symbol_id(self.symbol) if self.mode == "demo" else live_symbol_id(self.symbol)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "symbol": self.exchange_symbol,
            "ccxt_symbol": ccxt_swap_symbol(self.symbol),
            "side": self.side,
            "position_side": self.position_side,
            "order_type": self.order_type,
            "quantity": decimal_text(self.quantity),
            "price": decimal_text(self.price),
            "time_in_force": self.time_in_force,
            "client_order_id": self.client_order_id,
            "take_profit": decimal_text(self.take_profit),
            "stop_loss": decimal_text(self.stop_loss),
            "tp_trigger_type": self.tp_trigger_type,
            "sl_trigger_type": self.sl_trigger_type,
            "reduce_only": self.reduce_only,
        }

    def demo_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "symbol": demo_symbol_id(self.symbol),
            "side": self.side.upper(),
            "positionSide": self.position_side.upper(),
            "type": self.order_type.upper(),
            "quantity": decimal_text(self.quantity),
            "newClientOrderId": self.client_order_id,
        }
        if self.order_type == "limit":
            payload.update({"price": decimal_text(self.price), "timeInForce": self.time_in_force})
        if self.take_profit is not None:
            payload.update({"tpTriggerPrice": decimal_text(self.take_profit), "TpWorkingType": self.tp_trigger_type})
        if self.stop_loss is not None:
            payload.update({"slTriggerPrice": decimal_text(self.stop_loss), "SlWorkingType": self.sl_trigger_type})
        return payload

    def live_order(self) -> tuple[str, str, str, float, float | None, dict[str, object]]:
        params: dict[str, object] = {
            "clientOrderId": self.client_order_id,
            "positionSide": self.position_side.upper(),
        }
        if self.time_in_force:
            params["timeInForce"] = self.time_in_force
        if self.reduce_only:
            params["reduceOnly"] = True
        if self.take_profit is not None:
            params["takeProfit"] = {
                "triggerPrice": float(self.take_profit),
                "triggerPriceType": "mark" if self.tp_trigger_type == "MARK_PRICE" else "last",
            }
        if self.stop_loss is not None:
            params["stopLoss"] = {
                "triggerPrice": float(self.stop_loss),
                "triggerPriceType": "mark" if self.sl_trigger_type == "MARK_PRICE" else "last",
            }
        return (
            ccxt_swap_symbol(self.symbol),
            self.order_type,
            self.side,
            float(self.quantity),
            float(self.price) if self.price is not None else None,
            params,
        )
