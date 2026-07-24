"""Read-only presentation helpers for asynchronous Actor lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ActiveExecutionWait, ExecutionTimelineEntry, LogLevel


@dataclass(frozen=True, slots=True)
class ActorLifecycleProjection:
    execution_state: str
    phase: str
    queue_phase: str | None = None
    queue_position: int | None = None
    estimated_start_at_ms: int | None = None
    proxy_limited: bool = False


_PHASE_TEXT = {
    "admitted": "已接纳，等待准备",
    "preparing": "正在准备执行条件",
    "phase_queued": "正常阶段排队",
    "opening": "正在执行开仓阶段",
    "holding": "正在持仓等待",
    "closing": "正在执行平仓阶段",
    "stopping": "正在安全停止",
    "recovering": "正在只读核验状态",
    "completed": "本次执行已完成",
    "stopped": "本次执行已停止",
    "failed": "执行器状态异常",
}


def latest_actor_lifecycle(rows: list[dict[str, Any]]) -> ActorLifecycleProjection | None:
    for event in reversed(rows):
        if _event_name(event) != "actor_lifecycle":
            continue
        state = str(_field(event, "phase") or "admitted")
        queue_phase = str(_field(event, "queue_phase") or "") or None
        queue_position = _integer_or_none(_field(event, "queue_position"))
        estimated = _integer_or_none(_field(event, "estimated_start_at_ms"))
        constraint = str(_field(event, "queue_constraint") or "")
        return ActorLifecycleProjection(
            execution_state=state,
            phase=_PHASE_TEXT.get(state, "正在执行策略阶段"),
            queue_phase=queue_phase,
            queue_position=queue_position,
            estimated_start_at_ms=estimated,
            proxy_limited=constraint in {"proxy_active", "proxy_cooldown"},
        )
    return None


def actor_active_wait(actor: ActorLifecycleProjection | None, *, updated_at_ms: int) -> ActiveExecutionWait | None:
    if actor is None or actor.execution_state != "phase_queued":
        return None
    phase = "平仓" if actor.queue_phase == "close" else "开仓"
    proxy_hint = " · 等待同代理阶段释放" if actor.proxy_limited else " · 等待全局受控槽位"
    position = f"队列第 {actor.queue_position} 位" if actor.queue_position else "正在等待槽位"
    return ActiveExecutionWait(
        key="actor-phase-queue",
        label=f"正常{phase}阶段排队",
        updated_at_ms=updated_at_ms,
        elapsed_ms=0,
        remaining_ms=None
        if actor.estimated_start_at_ms is None
        else max(0, actor.estimated_start_at_ms - updated_at_ms),
        detail=position + proxy_hint,
        deadline_at_ms=actor.estimated_start_at_ms,
    )


def actor_timeline_entry(campaign_id: str, event: dict[str, Any]) -> ExecutionTimelineEntry | None:
    if _event_name(event) != "actor_lifecycle":
        return None
    state = str(_field(event, "phase") or "admitted")
    level = (
        "warn"
        if state in {"stopping", "recovering", "failed"}
        else "success"
        if state in {"completed", "stopped"}
        else "info"
    )
    detail = _actor_detail(event)
    sequence = int(event.get("sequence") or 0)
    return ExecutionTimelineEntry(
        id=f"{campaign_id}:{sequence}",
        sequence=sequence,
        at_ms=int(_field(event, "at_ms") or 0),
        level=LogLevel(level),
        event_name="actor_lifecycle",
        title=_PHASE_TEXT.get(state, "策略执行状态更新"),
        detail=detail,
    )


def merge_actor_waits(
    waits: list[ActiveExecutionWait], actor: ActorLifecycleProjection | None, *, updated_at_ms: int
) -> list[ActiveExecutionWait]:
    queue_wait = actor_active_wait(actor, updated_at_ms=updated_at_ms)
    if queue_wait is None:
        return waits
    return [wait for wait in waits if wait.key != queue_wait.key] + [queue_wait]


def _event_name(event: dict[str, Any]) -> str:
    return str(event.get("event") or event.get("name") or "")


def _field(event: dict[str, Any], key: str) -> object:
    if key in event:
        return event[key]
    fields = event.get("fields")
    return fields.get(key) if isinstance(fields, dict) else None


def _integer_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _actor_detail(event: dict[str, Any]) -> str:
    state = str(_field(event, "phase") or "")
    if state != "phase_queued":
        return str(_field(event, "reason") or "")
    position = _integer_or_none(_field(event, "queue_position"))
    constraint = str(_field(event, "queue_constraint") or "")
    phase = "平仓" if str(_field(event, "queue_phase") or "") == "close" else "开仓"
    parts = [f"等待正常{phase}槽位"]
    if position is not None:
        parts.append(f"队列第 {position} 位")
    if constraint in {"proxy_active", "proxy_cooldown"}:
        parts.append("同代理速率限制")
    return " · ".join(parts)
