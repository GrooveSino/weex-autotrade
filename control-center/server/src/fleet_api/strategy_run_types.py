"""Public lifecycle decisions shared by routes, projection, and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import BetaCampaignView

LifecycleDisposition = Literal[
    "idle", "ready", "running", "stopping", "recovering", "cleanup_required", "unavailable"
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
