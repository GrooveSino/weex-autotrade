from __future__ import annotations

import time
from decimal import Decimal

from .execution_contracts import (
    CancelOrdersOutcome,
    CycleExecutionStatus,
    PairCyclePlan,
    PairDirection,
    PairedExecutionAdapter,
    PairExecutionLeg,
    PairExecutionOutcome,
    PairLegAction,
    PositionCloseOutcome,
)
from .telemetry import AccountTelemetryContext
from .volume_history import NormalizedTradeFill


class MockPairedExecutionAdapter:
    """Produces deterministic simulated fills and never opens a network connection."""

    async def open_once(
        self,
        context: AccountTelemetryContext,
        plan: PairCyclePlan,
    ) -> PairExecutionOutcome:
        executed_at_ms = time.time_ns() // 1_000_000
        return PairExecutionOutcome(
            status=CycleExecutionStatus.OPENED,
            reason="mock_pair_opened",
            legs=(
                PairExecutionLeg(
                    symbol="BTCUSDT",
                    direction=PairDirection.LONG,
                    action=PairLegAction.OPEN,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:btc-long-open",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.btc_long_quote,
                        symbol="BTCUSDT",
                        position_action="open",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
                PairExecutionLeg(
                    symbol="ETHUSDT",
                    direction=PairDirection.SHORT,
                    action=PairLegAction.OPEN,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:eth-short-open",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.eth_short_quote,
                        symbol="ETHUSDT",
                        position_action="open",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
            ),
        )

    async def close_once(
        self,
        context: AccountTelemetryContext,
        plan: PairCyclePlan,
    ) -> PairExecutionOutcome:
        del context
        executed_at_ms = time.time_ns() // 1_000_000
        return PairExecutionOutcome(
            status=CycleExecutionStatus.COMPLETED,
            reason="mock_pair_closed",
            legs=(
                PairExecutionLeg(
                    symbol="BTCUSDT",
                    direction=PairDirection.LONG,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:btc-long-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.btc_long_quote,
                        symbol="BTCUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
                PairExecutionLeg(
                    symbol="ETHUSDT",
                    direction=PairDirection.SHORT,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:eth-short-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.eth_short_quote,
                        symbol="ETHUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
            ),
        )

    async def close_positions_once(
        self,
        context: AccountTelemetryContext,
        operation_id: str,
    ) -> PositionCloseOutcome:
        if not operation_id.strip():
            raise ValueError("position close operation id cannot be empty")
        executed_at_ms = time.time_ns() // 1_000_000
        exposure = context.instance.exposure
        legs: list[PairExecutionLeg] = []
        btc_quote = Decimal(str(exposure.btc_long))
        eth_quote = Decimal(str(exposure.eth_short))
        if btc_quote > 0:
            legs.append(
                PairExecutionLeg(
                    symbol="BTCUSDT",
                    direction=PairDirection.LONG,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{operation_id}:btc-long-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=btc_quote,
                        symbol="BTCUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                )
            )
        if eth_quote > 0:
            legs.append(
                PairExecutionLeg(
                    symbol="ETHUSDT",
                    direction=PairDirection.SHORT,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{operation_id}:eth-short-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=eth_quote,
                        symbol="ETHUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                )
            )
        return PositionCloseOutcome(
            status=CycleExecutionStatus.COMPLETED,
            reason="mock_positions_closed",
            legs=tuple(legs),
        )

    async def cancel_active_orders(self, context: AccountTelemetryContext) -> CancelOrdersOutcome:
        del context
        return CancelOrdersOutcome(verified=True, canceled_count=0, reason="no_active_orders")

    async def aclose(self) -> None:
        return None


class MockPairedExecutionAdapterFactory:
    def create(self, instance_id: str) -> PairedExecutionAdapter:
        return MockPairedExecutionAdapter()
