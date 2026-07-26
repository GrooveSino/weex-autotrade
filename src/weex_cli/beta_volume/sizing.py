from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError
from weex_cli.core.models import decimal_text
from weex_cli.exchange.rest.gateway import WeexGateway

from .contracts import ExecutionLane, PairLegPlan
from .numeric import decimal_from_exchange


def _mid_price(gateway: WeexGateway, symbol: str) -> Decimal:
    book = gateway.order_book(symbol, 5)
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise ValidationError(f"{symbol} order book is missing bids or asks")
    bid = decimal_from_exchange(bids[0][0], f"{symbol} bid")
    ask = decimal_from_exchange(asks[0][0], f"{symbol} ask")
    if bid <= 0 or ask <= bid:
        raise ValidationError(f"{symbol} order book is invalid")
    return (bid + ask) / 2


def _choose_pair_quantities(
    gateway: WeexGateway,
    target: Decimal,
    btc_quote: Decimal,
    eth_quote: Decimal,
    btc_price: Decimal,
    eth_price: Decimal,
    btc_step: Decimal,
    eth_step: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    return _choose_pair_quantities_for_gateways(
        gateway,
        gateway,
        target,
        btc_quote,
        eth_quote,
        btc_price,
        eth_price,
        btc_step,
        eth_step,
    )


def _choose_pair_quantities_for_gateways(
    btc_gateway: WeexGateway,
    eth_gateway: WeexGateway,
    target: Decimal,
    btc_quote: Decimal,
    eth_quote: Decimal,
    btc_price: Decimal,
    eth_price: Decimal,
    btc_step: Decimal,
    eth_step: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    btc_candidates = _quantity_candidates(btc_gateway, "BTC", btc_quote / btc_price, btc_step)
    eth_candidates = _quantity_candidates(eth_gateway, "ETH", eth_quote / eth_price, eth_step)
    candidates: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for btc_quantity in btc_candidates:
        for eth_quantity in eth_candidates:
            estimated = Decimal(2) * (btc_quantity * btc_price + eth_quantity * eth_price)
            if estimated < target:
                continue
            allocation_error = abs(btc_quantity * btc_price - btc_quote) + abs(eth_quantity * eth_price - eth_quote)
            candidates.append((estimated - target, allocation_error, btc_quantity, eth_quantity))
    if not candidates:
        raise ValidationError("no precision-valid BTC/ETH quantity pair reaches the turnover target")
    overshoot, _, btc_quantity, eth_quantity = min(candidates)
    return btc_quantity, eth_quantity, target + overshoot


def _quantity_candidates(gateway: WeexGateway, symbol: str, desired: Decimal, step: Decimal) -> tuple[Decimal, ...]:
    floor = gateway.amount_to_precision(symbol, desired)
    if floor <= 0 or floor < step:
        floor = gateway.amount_to_precision(symbol, step)
    if floor <= 0 or floor < step:
        raise ValidationError(f"{symbol} quantity is below WEEX minimum precision")
    if floor == desired:
        return (floor,)
    ceiling = gateway.amount_to_precision(symbol, floor + step)
    if ceiling <= floor:
        raise ValidationError(f"{symbol} amount precision could not produce a larger quantity")
    return floor, ceiling


def size_cycle(
    plan: Any,
    lanes: Mapping[str, ExecutionLane],
    desired_turnover: Decimal,
    *,
    market_data: Any | None = None,
) -> tuple[PairLegPlan, PairLegPlan, dict[str, str]]:
    opening_budget = desired_turnover / 2
    btc_quote = opening_budget * plan.allocation.btc_long_weight
    eth_quote = opening_budget * plan.allocation.eth_short_weight
    # Fleet supplies the single shared public book here.  Private lane
    # gateways remain responsible for account state and mutations, but every
    # account must size the same market snapshot rather than fan out public
    # REST reads under concurrency.
    price_source = market_data or lanes["BTC"].gateway
    btc_price = _mid_price(price_source, "BTC")
    eth_price = _mid_price(price_source, "ETH")
    btc_step = lanes["BTC"].gateway.amount_step("BTC")
    eth_step = lanes["ETH"].gateway.amount_step("ETH")
    btc_quantity, eth_quantity, estimated = _choose_pair_quantities_for_gateways(
        lanes["BTC"].gateway,
        lanes["ETH"].gateway,
        desired_turnover,
        btc_quote,
        eth_quote,
        btc_price,
        eth_price,
        btc_step,
        eth_step,
    )
    btc_notional = btc_quantity * btc_price
    eth_notional = eth_quantity * eth_price
    if btc_notional >= plan.max_position_quote or eth_notional >= plan.max_position_quote:
        raise SafetyError("a cycle leg reaches or exceeds max_position_quote")
    btc = replace(
        plan.btc,
        allocated_quote=btc_quote,
        reference_price=btc_price,
        quantity=btc_quantity,
        amount_step=btc_step,
    )
    eth = replace(
        plan.eth,
        allocated_quote=eth_quote,
        reference_price=eth_price,
        quantity=eth_quantity,
        amount_step=eth_step,
    )
    return (
        btc,
        eth,
        {
            "estimated_turnover_quote": decimal_text(estimated) or "0",
            "planned_open_beta": decimal_text(eth_notional / btc_notional) or "0",
            "opening_notional_quote": decimal_text(btc_notional + eth_notional) or "0",
        },
    )
