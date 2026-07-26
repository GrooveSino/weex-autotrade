"""Display-only helpers for strategy monitor snapshots."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.control_api.progress import ExecutionProgressProjector, event_name

from fleet_api.campaigns.persistence.campaigns import CampaignRecord
from fleet_api.models import ExecutionTimelineEntry, LogLevel
from fleet_api.monitoring.strategy_monitor_actor import actor_timeline_entry


def timeline_entries(campaign_id: str, rows: list[dict[str, Any]]) -> list[ExecutionTimelineEntry]:
    projector = ExecutionProgressProjector()
    timeline: list[ExecutionTimelineEntry] = []
    for event in rows:
        actor_entry = actor_timeline_entry(campaign_id, event)
        if actor_entry is not None:
            timeline.append(actor_entry)
            continue
        presentation = projector.apply(event, at_ms=int(event.get("at_ms") or 0))
        if presentation is None:
            continue
        sequence = int(event.get("sequence") or 0)
        timeline.append(
            ExecutionTimelineEntry(
                id=f"{campaign_id}:{sequence}",
                sequence=sequence,
                at_ms=int(event.get("at_ms") or 0),
                level=LogLevel(presentation.level),
                event_name=event_name(event),
                title=presentation.title,
                detail=presentation.detail,
            )
        )
    return timeline


def decimal_value(source: dict[str, object] | None, key: str, default: Decimal = Decimal(0)) -> Decimal:
    if source is None or source.get(key) is None:
        return default
    try:
        value = Decimal(str(source[key]))
    except Exception:
        return default
    return value if value.is_finite() else default


def nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def text_or_none(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def monitor_boundary_state(record: CampaignRecord) -> str:
    value = str(record.metadata.get("recovery_boundary_state") or "unknown")
    return value if value in {"flat", "owned_exposure", "external_exposure", "unknown"} else "unknown"


def recovery_phase(state: str | None, reason: object) -> str:
    if state == "checking":
        return "正在只读核验账户边界"
    if state == "cleanup_required":
        return "当前任务仓位待安全收尾"
    if state == "waiting_boundary":
        return "账户仍有仓位，等待下一次只读核验"
    if state == "waiting_read":
        return "账户状态暂时无法读取，等待重试"
    raw = str(reason or "")
    if "typeerror" in raw or "position_quantity_invalid" in raw:
        return "仓位数量格式异常，等待恢复检查"
    return "恢复检查尚未开始"
