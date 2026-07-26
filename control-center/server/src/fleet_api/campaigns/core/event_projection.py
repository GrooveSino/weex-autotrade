"""Pure mappings from durable campaign events to monitor projections."""

from __future__ import annotations

import re

_SAFE_EVENT_TEXT = re.compile(r"[^A-Za-z0-9._:/+\- ]+")


def safe_event_text(value: object, *, limit: int) -> str:
    return _SAFE_EVENT_TEXT.sub("", str(value)).strip()[:limit]


def phase_for_event(name: str) -> str:
    if name.startswith("safe_stop"):
        return "safe_stop"
    if "planning" in name:
        return "planning"
    if name.startswith("condition_wait"):
        return "condition_waiting"
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


def publishes_fleet_snapshot(name: str) -> bool:
    return name in {
        "campaign_boundary_completed",
        "campaign_child_planning_completed",
        "campaign_run_started",
        "campaign_run_completed",
        "preflight_completed",
        "preflight_rejected",
        "cycle_started",
        "cycle_plan_created",
        "cycle_completed",
        "cycle_stopped",
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
        "condition_waiting",
        "condition_wait_resumed",
        "owned_close_maker_retry",
        "owned_close_maker_retry_resumed",
        "dust_close_detected",
        "market_close_intent_persisted",
        "market_close_accepted",
        "market_close_verified",
        "market_close_uncertain",
    }
