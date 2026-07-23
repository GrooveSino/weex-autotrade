"""Small serialization helpers used by the Fleet API route modules."""

from __future__ import annotations

import json

from fastapi.encoders import jsonable_encoder

from .execution import CycleExecutionStatus, ExecutionRecord
from .models import ExecutionCycleView


def monitor_sse(event_type: str, cursor: str | None, payload: dict[str, object]) -> str:
    encoded = json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":"))
    cursor_line = f"id: {cursor}\n" if cursor else ""
    return f"{cursor_line}event: {event_type}\ndata: {encoded}\n\n"


def execution_cycle_view(record: ExecutionRecord) -> ExecutionCycleView:
    return ExecutionCycleView(
        cycle_id=record.plan.cycle_id,
        sequence=record.plan.sequence,
        status=record.status.value,
        reason=record.reason,
        total_quote=str(record.plan.total_quote),
        turnover_quote=str(record.plan.turnover_quote),
        btc_long_quote=str(record.plan.btc_long_quote),
        eth_short_quote=str(record.plan.eth_short_quote),
        allocation_version=record.plan.allocation_version,
        position_hold_seconds=record.plan.position_hold_seconds,
        round_interval_seconds=record.plan.round_interval_seconds,
        sizing_mode=record.plan.sizing_mode,
        strategy_id=record.plan.strategy_id,
        created_at_ms=record.created_at_ms,
        updated_at_ms=record.updated_at_ms,
        reconciliation_required=record.status is CycleExecutionStatus.UNCERTAIN,
        retry_allowed=False,
    )
