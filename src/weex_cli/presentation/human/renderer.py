"""Top-level dispatch for human-readable command output."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.console import Console
from rich.panel import Panel

from weex_cli.presentation.i18n import translate_message

from .results import (
    render_activity,
    render_beta_campaign_result,
    render_beta_volume_recovery_result,
    render_beta_volume_result,
    render_dry_run,
    render_live_maker_volume_result,
    render_maker_result,
    render_soak_result,
    render_status,
)
from .shared import render_rows


def render_human(payload: Any, console: Console) -> bool:
    if isinstance(payload, list):
        render_rows(payload, console)
        return True
    if not isinstance(payload, Mapping):
        return False
    if payload.get("view") == "status":
        render_status(payload, console)
        return True
    if payload.get("view") == "activity" or ("summary" in payload and "start_datetime" in payload):
        render_activity(payload, console)
        return True
    if payload.get("status") == "dry_run" and payload.get("confirm"):
        render_dry_run(payload, console)
        return True
    if payload.get("kind") == "beta_volume_execution":
        render_beta_volume_result(payload, console)
        return True
    if payload.get("kind") == "beta_volume_recovery":
        render_beta_volume_recovery_result(payload, console)
        return True
    if payload.get("kind") == "beta_volume_campaign_execution":
        render_beta_campaign_result(payload, console)
        return True
    if payload.get("kind") == "live_maker_volume_execution":
        render_live_maker_volume_result(payload, console)
        return True
    if "rounds" in payload and "rounds_requested" in payload:
        render_soak_result(payload, console)
        return True
    if payload.get("status") in {"completed", "failed", "uncertain"} and (
        "maker_only" in payload or "execution" in payload
    ):
        render_maker_result(payload, console)
        return True
    if payload.get("view") == "message":
        style = "green" if payload.get("status") in {"ok", "completed"} else "yellow"
        console.print(Panel(translate_message(payload.get("message") or ""), border_style=style))
        return True
    return False
