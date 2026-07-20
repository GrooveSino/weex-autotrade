from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Any, Protocol

from weex_cli.models import decimal_text
from weex_cli.trade_reporting import TradeReportService


class TradeReporter(Protocol):
    def report(
        self,
        *,
        mode: str,
        symbol: str | None,
        start_time: int,
        end_time: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LegFillRequest:
    sequence: int
    symbol: str
    action: str
    expected_quantity: Decimal
    tolerance_quantity: Decimal
    order_ids: tuple[str, ...]
    started_at_ms: int
    ended_at_ms: int


@dataclass(frozen=True)
class LegFillReport:
    status: str
    source_complete: bool
    fill_count: int
    order_count: int
    executed_quantity: Decimal
    quote_volume: Decimal
    maker_only: bool
    maker_count: int
    taker_count: int
    unknown_liquidity_count: int
    commission_by_asset: Mapping[str, Decimal]
    realized_pnl: Decimal
    warnings: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": "user_trades",
            "source_complete": self.source_complete,
            "fill_count": self.fill_count,
            "order_count": self.order_count,
            "executed_quantity": decimal_text(self.executed_quantity),
            "quote_volume": decimal_text(self.quote_volume),
            "maker_only": self.maker_only,
            "maker_count": self.maker_count,
            "taker_count": self.taker_count,
            "unknown_liquidity_count": self.unknown_liquidity_count,
            "commission_by_asset": {
                asset: decimal_text(value) for asset, value in sorted(self.commission_by_asset.items())
            },
            "realized_pnl": decimal_text(self.realized_pnl),
            "warnings": list(self.warnings),
        }


class LegFillReconciler(Protocol):
    def reconcile(self, request: LegFillRequest) -> LegFillReport: ...


class LiveLegFillReconciler:
    """Reconcile one completed leg against WEEX's authoritative live fill endpoint."""

    def __init__(
        self,
        gateway: Any,
        *,
        reporter: TradeReporter | None = None,
        attempts: int = 10,
        visibility_delay_seconds: float = 1.0,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("reconciliation attempts must be positive")
        self.reporter = reporter or TradeReportService(gateway)
        self.attempts = attempts
        self.visibility_delay_seconds = visibility_delay_seconds
        self.now_ms = now_ms
        self.sleep = sleep

    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        if not request.order_ids:
            return _empty_report("missing_order_identity")

        last = _empty_report("fills_not_visible")
        for attempt in range(1, self.attempts + 1):
            try:
                start_time = max(0, request.started_at_ms - 2_000)
                end_time = max(request.ended_at_ms, self.now_ms())
                report_order_ids = getattr(self.reporter, "report_order_ids", None)
                if callable(report_order_ids):
                    payload = report_order_ids(
                        symbol=request.symbol,
                        order_ids=request.order_ids,
                        start_time=start_time,
                        end_time=end_time,
                    )
                else:
                    payload = self.reporter.report(
                        mode="live",
                        symbol=request.symbol,
                        start_time=start_time,
                        end_time=end_time,
                    )
            except Exception:  # noqa: BLE001 - bounded read-only retry; never resubmits an order
                if attempt >= self.attempts:
                    raise
                self.sleep(self.visibility_delay_seconds * attempt)
                continue
            last = _report_for_request(payload, request)
            if last.status not in {"fills_not_visible", "fill_source_incomplete", "quantity_mismatch"}:
                return last
            if attempt < self.attempts:
                self.sleep(self.visibility_delay_seconds * attempt)
        return last


def _report_for_request(payload: Mapping[str, Any], request: LegFillRequest) -> LegFillReport:
    source_complete = payload.get("complete") is True
    warnings = tuple(str(item) for item in payload.get("warnings", ()) if item)
    rows = payload.get("trades")
    if not isinstance(rows, list):
        return _empty_report("invalid_fill_payload", source_complete=source_complete, warnings=warnings)

    accepted_ids = set(request.order_ids)
    trades = [row for row in rows if isinstance(row, Mapping) and str(row.get("order_id") or "") in accepted_ids]
    if not trades:
        status = "fills_not_visible" if source_complete else "fill_source_incomplete"
        return _empty_report(status, source_complete=source_complete, warnings=warnings)

    quantity = Decimal(0)
    quote = Decimal(0)
    realized_pnl = Decimal(0)
    commission_by_asset: dict[str, Decimal] = defaultdict(Decimal)
    maker_count = 0
    taker_count = 0
    unknown_count = 0
    order_ids: set[str] = set()
    action_mismatch = False
    for trade in trades:
        quantity += _decimal(trade.get("quantity"))
        quote += _decimal(trade.get("quote_quantity"))
        realized_pnl += _decimal(trade.get("realized_pnl"))
        order_ids.add(str(trade.get("order_id") or ""))
        action_mismatch = action_mismatch or str(trade.get("position_action") or "") != request.action
        maker = trade.get("maker")
        if maker is True:
            maker_count += 1
        elif maker is False:
            taker_count += 1
        else:
            unknown_count += 1
        asset = trade.get("commission_asset")
        if asset:
            commission_by_asset[str(asset)] += _decimal(trade.get("commission"))

    if not source_complete:
        status = "fill_source_incomplete"
    elif action_mismatch:
        status = "position_action_mismatch"
    elif taker_count:
        status = "taker_fill_detected"
    elif unknown_count:
        status = "unknown_liquidity"
    elif abs(quantity - request.expected_quantity) > request.tolerance_quantity:
        status = "quantity_mismatch"
    elif quote <= 0:
        status = "invalid_quote_volume"
    else:
        status = "verified"

    return LegFillReport(
        status=status,
        source_complete=source_complete,
        fill_count=len(trades),
        order_count=len(order_ids - {""}),
        executed_quantity=quantity,
        quote_volume=quote,
        maker_only=bool(trades) and maker_count == len(trades),
        maker_count=maker_count,
        taker_count=taker_count,
        unknown_liquidity_count=unknown_count,
        commission_by_asset=dict(commission_by_asset),
        realized_pnl=realized_pnl,
        warnings=warnings,
    )


def _empty_report(
    status: str,
    *,
    source_complete: bool = False,
    warnings: Sequence[str] = (),
) -> LegFillReport:
    return LegFillReport(
        status=status,
        source_complete=source_complete,
        fill_count=0,
        order_count=0,
        executed_quantity=Decimal(0),
        quote_volume=Decimal(0),
        maker_only=False,
        maker_count=0,
        taker_count=0,
        unknown_liquidity_count=0,
        commission_by_asset={},
        realized_pnl=Decimal(0),
        warnings=tuple(warnings),
    )


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (DecimalException, TypeError, ValueError):
        return Decimal(0)
    return result if result.is_finite() else Decimal(0)
