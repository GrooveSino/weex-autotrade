"""Read-only Fleet projections shared by HTTP, SSE, and lifecycle code."""

from __future__ import annotations

from datetime import UTC, datetime

from .campaign_log import campaign_event_log
from .instance_projection import project_instance_session
from .main_context import FleetAppContext
from .models import AccountInstance, LogBatch, LogLine
from .service import UnsafeOperation
from .strategy import StrategyRunBlocked, StrategyTargetReached, resolve_strategy_run_plan


def install_projection_support(ctx: FleetAppContext) -> None:
    def projected_instances() -> list[AccountInstance]:
        return [
            project_instance_session(
                instance,
                ctx.volume_ledger,
                ctx.strategy_monitor,
                ctx.strategy_run_lifecycle,
            )
            for instance in ctx.service.list_instances()
        ]

    def combined_log_updates(instance_id: str, limit: int, after: str | None) -> LogBatch:
        ctx.service.get_instance(instance_id)
        system = ctx.service.log_updates(instance_id, 500, None).lines
        clear_boundaries = ctx.repository.log_clear_boundaries(instance_id)
        ranked: list[tuple[int, int, LogLine]] = []
        for index, line in enumerate(system):
            try:
                at_ms = int(datetime.fromisoformat(line.timestamp.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                at_ms = index
            ranked.append((at_ms, index, line))
        rank = len(ranked)
        for record in ctx.campaign_journal.list_for_instance(instance_id):
            cleared_through = clear_boundaries.get(record.campaign_id.lower(), 0)
            for event in ctx.campaign_journal.events_before(record.campaign_id, None, 500):
                sequence = int(event.get("sequence") or 0)
                if sequence <= cleared_through:
                    continue
                rendered = campaign_event_log(event)
                if rendered is None:
                    continue
                level, message = rendered
                at_ms = int(event.get("at_ms") or 0)
                # Releases before the single-journal architecture copied this
                # same rendered row into instance_logs. Prefer the audit row
                # when the legacy copy is adjacent in time.
                ranked = [
                    item
                    for item in ranked
                    if not (item[2].message == message and abs(item[0] - at_ms) <= 2_000)
                ]
                ranked.append(
                    (
                        at_ms,
                        rank,
                        LogLine(
                            id=f"execution:{record.campaign_id}:{sequence}",
                            timestamp=datetime.fromtimestamp(at_ms / 1000, tz=UTC).isoformat(),
                            level=level,
                            message=message,
                        ),
                    )
                )
                rank += 1
        ranked.sort(key=lambda item: (item[0], item[1]))
        combined = [item[2] for item in ranked]
        window = combined[-500:]
        reset = False
        if after is None:
            lines = window[-limit:]
        else:
            cursor_index = next((index for index, line in enumerate(window) if line.id == after), None)
            if cursor_index is None:
                lines = window[-limit:]
                reset = True
            else:
                lines = window[cursor_index + 1 : cursor_index + 1 + limit]
        cursor = lines[-1].id if lines else (None if reset else after)
        return LogBatch(lines=lines, cursor=cursor, reset=reset)

    def strategy_run_plan(instance: AccountInstance):
        try:
            return resolve_strategy_run_plan(
                instance,
                ctx.volume_ledger.active_session(instance.id, instance.mode.value),
            )
        except (StrategyRunBlocked, StrategyTargetReached) as exc:
            raise UnsafeOperation(str(exc)) from None

    ctx.projected_instances = projected_instances
    ctx.combined_log_updates = combined_log_updates
    ctx.strategy_run_plan = strategy_run_plan
