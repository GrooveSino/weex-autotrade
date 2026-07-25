"""Persist the visible condition-wait projection independently of orders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def persist_condition_projection(manager: Any, record: Any, event: Mapping[str, Any]) -> None:
    name = str(event.get("name") or "")
    fields = event.get("fields") if isinstance(event.get("fields"), Mapping) else {}
    if name == "condition_waiting":
        manager.write_coordinator.critical(
            lambda: manager.journal.update(
                record.campaign_id,
                condition_state=fields.get("condition"),
                condition_attempt=fields.get("condition_attempt"),
                next_condition_check_at_ms=fields.get("next_check_ms"),
            )
        )
    elif name in {"condition_wait_resumed", "condition_wait_rehydrated"}:
        manager.write_coordinator.critical(
            lambda: manager.journal.update(
                record.campaign_id,
                condition_state=None,
                condition_attempt=0,
                next_condition_check_at_ms=None,
            )
        )
