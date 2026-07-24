from __future__ import annotations

import time

from .campaign_contracts import CampaignRecord
from .campaign_events import _view
from .models import BetaCampaignView
from .strategy_run_types import LifecycleDisposition, LifecyclePreparation


def boundary_counts(boundary: dict[str, object]) -> dict[str, int]:
    return {
        "position_count": int(boundary.get("position_count") or 0),
        "regular_order_count": int(boundary.get("regular_order_count") or 0),
        "trigger_order_count": int(boundary.get("trigger_order_count") or 0),
    }


def active_preparation(record: CampaignRecord | None) -> LifecyclePreparation:
    if record is None:
        return LifecyclePreparation("running")
    disposition: LifecycleDisposition = "stopping" if record.status == "stopping" else "running"
    return LifecyclePreparation(disposition, execution=_view(record, include_events=False))


def view_or_none(record: CampaignRecord | None) -> BetaCampaignView | None:
    return _view(record, include_events=False) if record is not None else None


def lifecycle_now_ms() -> int:
    return time.time_ns() // 1_000_000
