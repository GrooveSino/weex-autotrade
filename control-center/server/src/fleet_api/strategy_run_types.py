"""Public lifecycle decisions shared by routes, projection, and recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .models import BetaCampaignView

LifecycleDisposition = Literal[
    "idle",
    "ready",
    "running",
    "stopping",
    "recovering",
    "recovery_cleanup_required",
    "orders_cleanup_required",
    "position_blocked",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class LifecyclePreparation:
    disposition: LifecycleDisposition
    execution: BetaCampaignView | None = None
    reason_code: str | None = None
    message: str | None = None
    position_count: int = 0
    regular_order_count: int = 0
    trigger_order_count: int = 0
    cleanup_confirmation: str | None = None
    blocking_positions: tuple[dict[str, str], ...] = ()
    allowed_actions: tuple[str, ...] = ()
    boundary_checked_at_ms: int | None = None
    boundary: Mapping[str, object] | None = None
