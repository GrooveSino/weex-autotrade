from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from fleet_api.execution.contracts.execution_contracts import (
    CycleExecutionStatus,
    ExecutionStateError,
    PairCyclePlan,
    PairDirection,
    PairExecutionOutcome,
    PairLegAction,
    PositionCloseOutcome,
)
from fleet_api.models import ExposureSnapshot
from fleet_api.runtime.telemetry import AccountTelemetryContext
from fleet_api.volume.core.volume_history import NormalizedTradeFill, TradeVolumeLedger


def record_fills(
    ledger: TradeVolumeLedger,
    clock_ms: Callable[[], int],
    context: AccountTelemetryContext,
    fills: tuple[NormalizedTradeFill, ...],
) -> int:
    inserted = ledger.record_account_fills(context.instance.id, context.instance.mode.value, fills)
    ledger.refresh_sessions(
        context.instance.id,
        context.instance.mode.value,
        now_ms=max((fill.executed_at_ms for fill in fills), default=clock_ms()),
        source_complete=True,
        stale=False,
    )
    return inserted


def validate_outcome(
    plan: PairCyclePlan,
    outcome: PairExecutionOutcome,
    *,
    expected_status: CycleExecutionStatus,
    expected_action: PairLegAction,
) -> None:
    if outcome.status in {CycleExecutionStatus.REJECTED, CycleExecutionStatus.UNCERTAIN}:
        return
    if outcome.status is not expected_status:
        raise ExecutionStateError("execution adapter returned an unexpected pair phase")
    legs = {(leg.symbol, leg.direction, leg.action): leg for leg in outcome.legs}
    btc = legs.get(("BTCUSDT", PairDirection.LONG, expected_action))
    eth = legs.get(("ETHUSDT", PairDirection.SHORT, expected_action))
    if btc is None or eth is None or len(legs) != 2:
        raise ExecutionStateError("pair phase must contain BTC long and ETH short")
    if btc.fill.quote_volume != plan.btc_long_quote:
        raise ExecutionStateError("BTC long fill does not match pair plan")
    if eth.fill.quote_volume != plan.eth_short_quote:
        raise ExecutionStateError("ETH short fill does not match pair plan")


def validate_position_close_outcome(exposure: ExposureSnapshot, outcome: PositionCloseOutcome) -> None:
    if outcome.status in {CycleExecutionStatus.REJECTED, CycleExecutionStatus.UNCERTAIN}:
        return
    expected: dict[tuple[str, PairDirection, PairLegAction], Decimal] = {}
    btc_quote, eth_quote = Decimal(str(exposure.btc_long)), Decimal(str(exposure.eth_short))
    if btc_quote > 0:
        expected[("BTCUSDT", PairDirection.LONG, PairLegAction.CLOSE)] = btc_quote
    if eth_quote > 0:
        expected[("ETHUSDT", PairDirection.SHORT, PairLegAction.CLOSE)] = eth_quote
    actual = {(leg.symbol, leg.direction, leg.action): leg.fill.quote_volume for leg in outcome.legs}
    if len(actual) != len(outcome.legs) or actual != expected:
        raise ExecutionStateError("position close fills do not match the current exposure snapshot")


def snapshot_close_operation_id(instance_id: str, completed_cycles: int, exposure: ExposureSnapshot) -> str:
    btc_quote = Decimal(str(exposure.btc_long)).normalize()
    eth_quote = Decimal(str(exposure.eth_short)).normalize()
    return f"snapshot-close:{instance_id}:{completed_cycles}:{btc_quote}:{eth_quote}"
