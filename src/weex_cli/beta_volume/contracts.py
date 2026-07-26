from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from weex_cli.core.models import decimal_text
from weex_cli.core.reliability import ReadRetryPolicy
from weex_cli.exchange.rest.gateway import WeexGateway
from weex_cli.execution.reconciliation import (
    LegFillReconciler,
    LegFillRequest,
)
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

PLAN_MAX_AGE_SECONDS = 900
MAX_PRICE_DRIFT = Decimal("0.01")
MARGIN_BUFFER = Decimal("1.20")
MAX_AUTO_LEVERAGE = 99
MAX_FIXED_LEVERAGE = 400
DEFAULT_STRATEGY_DIRECTION = "btc_long_eth_short"
DEFAULT_TAKER_DUST_MAX_QUOTE = Decimal("10.00")
STRATEGY_DIRECTIONS = {DEFAULT_STRATEGY_DIRECTION, "btc_short_eth_long"}
POST_FLAT_ACCOUNTING_ATTEMPTS = 8
BETA_READ_RETRY_POLICY = ReadRetryPolicy(attempts=8, initial_delay_seconds=1, max_delay_seconds=8)
POSITION_READ_RETRY_POLICY = ReadRetryPolicy(attempts=6, initial_delay_seconds=0.5, max_delay_seconds=4)
RETRYABLE_ACCOUNTING_STATUSES = {"fills_not_visible", "fill_source_incomplete", "quantity_mismatch"}
DEFAULT_PLAN_DIRECTORY = Path("data/beta-volume-plans")
PhaseWaiter = Callable[[str, str, int], bool]


@dataclass(frozen=True)
class PairLegPlan:
    symbol: str
    position_side: str
    opening_side: str
    closing_side: str
    allocated_quote: Decimal
    reference_price: Decimal
    quantity: Decimal
    amount_step: Decimal
    open_client_prefix: str
    close_client_prefix: str

    def as_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "opening_side": self.opening_side,
            "closing_side": self.closing_side,
            "allocated_quote": decimal_text(self.allocated_quote) or "0",
            "reference_price": decimal_text(self.reference_price) or "0",
            "quantity": decimal_text(self.quantity) or "0",
            "amount_step": decimal_text(self.amount_step) or "0",
            "open_client_order_id": f"{self.open_client_prefix}-001",
            "close_client_order_id": f"{self.close_client_prefix}-001",
            "time_in_force": "POST_ONLY",
        }


VenueFactory = Callable[[WeexGateway, str, str], LiveAdaptiveMakerVenue]
GatewayFactory = Callable[[], WeexGateway]
ReconcilerFactory = Callable[[WeexGateway], LegFillReconciler]
EventSink = Callable[[Mapping[str, Any]], None]
DelaySelector = Callable[[int], float]


@dataclass(frozen=True)
class CycleLegSpec:
    plan: PairLegPlan
    action: str
    side: str
    target_position: float
    client_prefix: str


@dataclass(frozen=True)
class ExecutionLane:
    gateway: WeexGateway
    venue: LiveAdaptiveMakerVenue
    reconciler: LegFillReconciler


@dataclass(frozen=True)
class _PendingFillReconciliation:
    request: LegFillRequest
    executor_status: str
    executor_reason: str
