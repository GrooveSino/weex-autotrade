from __future__ import annotations

import re
import time
from collections.abc import Mapping
from contextlib import suppress
from decimal import Decimal
from typing import Any

from .campaign_contracts import CampaignRecord
from .campaign_helpers import _reconciliation_confirmation, _reconciliation_required
from .models import BetaCampaignEvent, BetaCampaignView
from .service import ValidationFailed


def _sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("event") or payload.get("name") or "event")[:96]
    timestamp_ms = payload.get("timestamp_ms")
    try:
        at_ms = int(timestamp_ms) if timestamp_ms is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        at_ms = int(time.time() * 1000)
    event: dict[str, Any] = {
        "sequence": 1,
        "name": name,
        "at_ms": at_ms,
    }
    for key in ("phase", "run", "child_plan_id", "status"):
        if payload.get(key) is not None:
            event[key] = payload[key]
    text_fields = {
        "operation",
        "reason",
        "symbol",
        "action",
        "side",
        "progress_event",
        "waiting_for",
        "source",
        "decision",
        "read",
        "child_status",
        "btc",
        "eth",
        "queue_phase",
        "queue_constraint",
    }
    decimal_fields = {
        "remaining_quote",
        "total_quote",
        "child_quote",
        "seconds",
        "desired_quote",
        "opening_notional_quote",
        "quote_volume",
        "executed_quote_volume",
        "price",
        "quantity",
        "quote",
        "filled_quantity",
        "order_quantity",
        "remaining_quantity",
        "btc_quantity",
        "eth_quantity",
    }
    integer_fields = {
        "attempt",
        "max_attempts",
        "round",
        "event_index",
        "elapsed_ms",
        "remaining_ms",
        "next_check_ms",
        "started_at_ms",
        "deadline_at_ms",
        "estimated_start_at_ms",
        "leverage",
        "fill_count",
        "submissions",
        "cancels",
        "requotes",
        "queue_position",
    }
    boolean_fields = {
        "completed",
        "flat",
        "no_orders",
        "maker_only",
        "verified",
        "maker",
        "reduce_only",
    }
    fields: dict[str, object] = {}
    if payload.get("sequence") is not None:
        with suppress(TypeError, ValueError):
            fields["leg_sequence"] = int(payload["sequence"])
    for key in text_fields:
        if payload.get(key) is not None:
            fields[key] = _safe_event_text(payload[key], limit=96)
    for key in decimal_fields:
        if payload.get(key) is None:
            continue
        try:
            value = Decimal(str(payload[key]))
        except Exception:  # noqa: BLE001 - malformed observability data is omitted
            continue
        if value.is_finite():
            fields[key] = format(value, "f")
    for key in integer_fields:
        if payload.get(key) is None:
            continue
        try:
            fields[key] = int(payload[key])
        except (TypeError, ValueError):
            continue
    for key in boolean_fields:
        if isinstance(payload.get(key), bool):
            fields[key] = payload[key]
    for key in ("symbols", "active_symbols", "completed_symbols"):
        values = payload.get(key)
        if isinstance(values, (list, tuple)):
            fields[key] = [_safe_event_text(value, limit=16) for value in values[:2]]
    if payload.get("error"):
        fields["error"] = _safe_event_text(payload["error"], limit=80)
    event["fields"] = fields
    event["message"] = name.replace("_", " ")[:240]
    return event


_SAFE_EVENT_TEXT = re.compile(r"[^A-Za-z0-9._:/+\- ]+")


def _safe_event_text(value: object, *, limit: int) -> str:
    return _SAFE_EVENT_TEXT.sub("", str(value)).strip()[:limit]


def _phase_for_event(name: str) -> str:
    if name.startswith("safe_stop"):
        return "safe_stop"
    if "planning" in name:
        return "planning"
    if "run_started" in name:
        return "opening"
    if name.startswith("phase_pacing"):
        return "phase_pacing"
    if "run_completed" in name:
        return "reconciled"
    if "boundary" in name:
        return "boundary"
    if "finished" in name:
        return "finished"
    if "retry" in name:
        return "recovery"
    return name[:64]


def _publishes_fleet_snapshot(name: str) -> bool:
    return name in {
        "campaign_boundary_completed",
        "campaign_child_planning_completed",
        "campaign_run_started",
        "campaign_run_completed",
        "preflight_completed",
        "preflight_rejected",
        "cycle_started",
        "cycle_completed",
        "cycle_stopped",
        # These are low-frequency, fill-reconciled state changes.  Publishing
        # them makes the account table follow the same durable monitor
        # projection without broadcasting the 125ms maker-wait heartbeats.
        "leg_completed",
        "leg_stopped",
        "leg_uncertain",
        "hold_started",
        "hold_completed",
        "round_gap_started",
        "round_gap_completed",
        "final_acceptance_completed",
        "workflow_finished",
        "campaign_finished",
        "campaign_uncertain",
        "campaign_recovering",
        "launch_aborted",
        "phase_pacing_started",
        "phase_pacing_completed",
        "phase_pacing_cancelled",
    }


def submission_attempted(record: CampaignRecord) -> bool:
    """Return whether the journal crossed the exchange mutation boundary."""
    for event in record.events:
        name = str(event.get("name") or "")
        fields = event.get("fields") if isinstance(event.get("fields"), Mapping) else {}
        progress_event = str(fields.get("progress_event") or "")
        if progress_event in {"order_submission_attempted", "submit"}:
            return True
        # Releases before the explicit boundary marker only journaled these
        # events after an accepted submission or reconciled fill.
        if name in {"leg_completed", "leg_uncertain", "cycle_completed"}:
            return True
    return False


def _view(record: CampaignRecord | None, *, include_events: bool = True) -> BetaCampaignView:
    if record is None:
        raise ValidationFailed("campaign was not found")
    campaign = record.campaign
    metadata = record.metadata
    result = record.result or {}
    generated = Decimal(str(metadata.get("generated_quote", result.get("executed_quote_volume", "0"))))
    remaining = Decimal(
        str(
            metadata.get(
                "remaining_quote",
                result.get("remaining_quote", max(Decimal(0), campaign.target_turnover_quote - generated)),
            )
        )
    )
    excess = Decimal(
        str(
            metadata.get(
                "excess_quote", result.get("excess_quote", max(Decimal(0), generated - campaign.target_turnover_quote))
            )
        )
    )
    started = metadata.get("started_at_ms")
    finished = metadata.get("finished_at_ms")
    return BetaCampaignView(
        campaign_id=campaign.campaign_id,
        instance_id=record.instance_id,
        status=record.status,
        schema_version=campaign.schema_version,
        strategy_id=str(metadata["strategy_id"]) if metadata.get("strategy_id") else None,
        strategy_name=str(metadata["strategy_name"]) if metadata.get("strategy_name") else None,
        strategy_version=int(metadata["strategy_version"]) if metadata.get("strategy_version") is not None else None,
        strategy_snapshot=dict(metadata["strategy_snapshot"])
        if isinstance(metadata.get("strategy_snapshot"), dict)
        else None,
        session_id=str(metadata["session_id"]) if metadata.get("session_id") else None,
        target_mode=str(metadata["target_mode"]) if metadata.get("target_mode") else None,
        run_disposition=str(metadata["run_disposition"]) if metadata.get("run_disposition") else None,
        strategy_target_quote_volume=(
            Decimal(str(metadata["strategy_target_quote"]))
            if metadata.get("strategy_target_quote") is not None
            else None
        ),
        execution_target_quote_volume=(
            Decimal(str(metadata["session_target_quote"])) if metadata.get("session_target_quote") is not None else None
        ),
        baseline_lifetime_quote_volume=(
            Decimal(str(metadata["baseline_lifetime_quote"]))
            if metadata.get("baseline_lifetime_quote") is not None
            else None
        ),
        direction=campaign.direction,
        selected_target_quote_volume=Decimal(
            str(metadata.get("strategy_target_quote") or campaign.target_turnover_quote)
        ),
        leverage=campaign.leverage,
        margin_mode=campaign.margin_mode,
        target_quote=campaign.target_turnover_quote,
        round_turnover_quote_min=campaign.round_turnover_quote_min,
        cycle_volume=campaign.round_turnover_quote,
        authorized_max_quote=campaign.authorized_max_turnover_quote,
        hold_min_seconds=int(campaign.hold_min_seconds),
        hold_max_seconds=int(campaign.hold_max_seconds),
        round_gap_min_seconds=int(campaign.round_gap_min_seconds),
        round_gap_max_seconds=int(campaign.round_gap_max_seconds),
        max_runs=campaign.max_runs,
        beta=campaign.allocation.beta,
        beta_version=campaign.allocation.version,
        beta_source=campaign.allocation.source,
        beta_as_of_ms=campaign.allocation.as_of_ms,
        beta_age_ms=Decimal(max(0, int(time.time() * 1000) - campaign.allocation.as_of_ms)),
        beta_max_age_ms=Decimal("10000"),
        btc_long_weight=campaign.allocation.btc_long_weight,
        eth_short_weight=campaign.allocation.eth_short_weight,
        available_quote=Decimal(str(metadata["available_quote"]))
        if metadata.get("available_quote") is not None
        else None,
        required_leverage=int(metadata["required_leverage"]) if metadata.get("required_leverage") is not None else None,
        planned_leverage=int(metadata["planned_leverage"]) if metadata.get("planned_leverage") is not None else None,
        max_supported_turnover_quote=Decimal(str(metadata["max_supported_turnover_quote"]))
        if metadata.get("max_supported_turnover_quote")
        else None,
        confirmation=str(metadata["confirmation"]),
        stop_confirmation=str(metadata["stop_confirmation"]),
        reconciliation_confirmation=(
            _reconciliation_confirmation(campaign.campaign_id) if _reconciliation_required(record) else None
        ),
        reconciliation_required=_reconciliation_required(record),
        retry_allowed=False,
        risk_acknowledged=bool(metadata.get("risk_acknowledged", False)),
        current_run=int(metadata.get("current_run", 0)),
        generated_quote=generated,
        remaining_quote=remaining,
        excess_quote=excess,
        maker_quote=Decimal(
            str(
                metadata.get(
                    "maker_quote", result.get("executed_quote_volume", "0") if result.get("maker_only") else "0"
                )
            )
        ),
        taker_quote=Decimal(str(metadata.get("taker_quote", "0"))),
        unknown_quote=Decimal(str(metadata.get("unknown_quote", "0"))),
        btc_quote=Decimal(str(metadata.get("btc_quote", "0"))),
        eth_quote=Decimal(str(metadata.get("eth_quote", "0"))),
        fill_count=int(metadata.get("fill_count", 0)),
        maker_count=int(metadata.get("maker_count", 0)),
        taker_count=int(metadata.get("taker_count", 0)),
        unknown_count=int(metadata.get("unknown_count", 0)),
        order_count=int(metadata.get("order_count", 0)),
        cancel_count=int(metadata.get("cancel_count", 0)),
        requote_count=int(metadata.get("requote_count", 0)),
        phase=str(metadata.get("phase", record.status)),
        reason=str(metadata["reason"]) if metadata.get("reason") else None,
        started_at_ms=int(started) if started else None,
        finished_at_ms=int(finished) if finished else None,
        elapsed_ms=(int(finished) - int(started)) if started and finished else None,
        last_event=BetaCampaignEvent.model_validate(record.events[-1]) if record.events else None,
        events=[BetaCampaignEvent.model_validate(event) for event in record.events] if include_events else [],
    )
