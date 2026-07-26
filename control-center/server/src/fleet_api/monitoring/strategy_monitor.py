from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Any

from weex_cli.control_api.progress import (
    EXECUTION_PROGRESS_PROJECTION_VERSION,
    ExecutionProgressProjector,
    condition_presentation,
)

from fleet_api.auth.ownership import LEGACY_OWNER_USER_ID
from fleet_api.campaigns.persistence.campaigns import CampaignJournal, CampaignRecord, ExecutionMonitorProjection
from fleet_api.models import ActiveExecutionWait, StrategyMonitorSnapshot
from fleet_api.monitoring.strategy_monitor_actor import latest_actor_lifecycle, merge_actor_waits
from fleet_api.monitoring.strategy_monitor_snapshot_helpers import (
    decimal_value,
    monitor_boundary_state,
    nonnegative_int,
    recovery_phase,
    text_or_none,
    timeline_entries,
)
from fleet_api.volume.core.volume_history import TradeVolumeLedger


@dataclass(frozen=True)
class StrategyProgressProjection:
    verified_quote_volume: Decimal
    volume_source: str
    updated_at_ms: int
    active_waits: tuple[ActiveExecutionWait, ...]


class StrategyMonitorService:
    def __init__(self, journal: CampaignJournal, ledger: TradeVolumeLedger, executor_generation: str) -> None:
        self.journal = journal
        self.ledger = ledger
        self.executor_generation = executor_generation
        self._subscriber_count = 0
        self._reset_count = 0
        self._metrics_lock = RLock()

    def snapshot(
        self,
        instance_id: str,
        *,
        session_id: str | None = None,
        before_sequence: int | None = None,
        limit: int = 200,
        event_rows: list[dict[str, Any]] | None = None,
        owner_user_id: str | None = None,
    ) -> StrategyMonitorSnapshot:
        server_time_ms = time.time_ns() // 1_000_000
        record = self.journal.monitor_record(instance_id, session_id)
        if record is None:
            return StrategyMonitorSnapshot(
                instance_id=instance_id,
                executor_generation=self.executor_generation,
                status="idle",
                phase="暂无策略运行记录",
                server_time_ms=server_time_ms,
                updated_at_ms=server_time_ms,
            )

        projection = self._ensure_projection(record)
        projection, stored_rows, latest_sequence = self.journal.monitor_read(
            record.campaign_id,
            before_sequence,
            max(limit * 5, limit),
        )
        if projection is not None and owner_user_id is not None and projection.owner_user_id != owner_user_id:
            raise KeyError(record.campaign_id)
        state = projection.state if projection is not None else ExecutionProgressProjector().snapshot()
        projected_sequence = projection.projected_sequence if projection is not None else 0
        projection_version = projection.projection_version if projection is not None else 0
        projection_lag = max(0, latest_sequence - projected_sequence)

        selected_session_id = text_or_none(record.metadata.get("session_id"))
        # Releases before the session ledger migration can leave a campaign
        # journal behind without its matching volume session.  A monitor read
        # must remain read-only and explicit about that gap; raising here turns
        # an otherwise healthy SSE stream into a 500/reconnect loop.
        missing_session = False
        try:
            session = self.ledger.session_projection(selected_session_id) if selected_session_id else None
        except KeyError:
            session = None
            missing_session = True
        started_at_ms = int(session.get("started_at_ms") or 0) if session else 0
        finished_at_ms = nonnegative_int(session.get("finished_at_ms")) or None if session else None
        fills = (
            [
                fill
                for fill in self.ledger.fills_for_account(instance_id, "live", started_at_ms)
                if fill.authoritative and (finished_at_ms is None or fill.executed_at_ms <= finished_at_ms)
            ]
            if session
            else []
        )
        btc_quote = sum((fill.quote_volume for fill in fills if fill.symbol.upper().startswith("BTC")), Decimal(0))
        eth_quote = sum((fill.quote_volume for fill in fills if fill.symbol.upper().startswith("ETH")), Decimal(0))
        target_quote = decimal_value(session, "target_quote_volume", record.campaign.target_turnover_quote)
        ledger_verified = decimal_value(session, "verified_quote_volume")
        journal_verified = decimal_value(state, "execution_verified_quote_volume")
        journal_btc = decimal_value(state, "btc_quote_volume")
        journal_eth = decimal_value(state, "eth_quote_volume")
        journal_unknown_fills = nonnegative_int(state.get("execution_unknown_fill_count"))
        rows = event_rows if event_rows is not None else stored_rows
        # A delta may contain only a fill or wait event.  Actor lifecycle is
        # durable execution state, not a timeline-only detail, so resolve it
        # from the persisted window rather than dropping queue/phase state
        # whenever the latest delta has no actor_lifecycle event.
        actor = latest_actor_lifecycle(stored_rows)
        timeline = timeline_entries(record.campaign_id, rows)[-limit:]
        first_sequence = int(rows[0].get("sequence") or 0) if rows else 0
        cursor = self.cursor(record.campaign_id, projected_sequence) if projected_sequence else None
        session_stale = bool(session.get("stale", True)) if session else True
        reconciliation_required = bool(session.get("reconciliation_required", False)) if session else missing_session
        pending_sync = bool(session.get("pending_sync", False)) if session else False
        audit_status = str(session.get("audit_status") or "pending") if session else "pending"
        ledger_sync_state = (
            "queued"
            if pending_sync
            else "stale"
            if session_stale
            else "complete"
            if session and session.get("source_complete")
            else "idle"
        )
        freshness = (
            "rebuilding" if projection_lag else "stale" if session_stale or reconciliation_required else "current"
        )
        volume_source = (
            "ledger"
            if session and session.get("source_complete") and not session_stale and not reconciliation_required
            else "execution_journal"
            if journal_verified > 0
            else "pending"
        )
        if volume_source == "execution_journal":
            verified_quote = journal_verified
            remaining_quote = max(target_quote - verified_quote, Decimal(0))
            btc_quote = journal_btc
            eth_quote = journal_eth
            maker_fill_count = 0
            taker_fill_count = 0
            unknown_fill_count = journal_unknown_fills
        else:
            verified_quote = ledger_verified
            remaining_quote = max(target_quote - verified_quote, Decimal(0))
            maker_fill_count = sum(1 for fill in fills if fill.maker is True)
            taker_fill_count = sum(1 for fill in fills if fill.maker is False)
            unknown_fill_count = sum(1 for fill in fills if fill.maker is None)
        active_waits = [ActiveExecutionWait.model_validate(wait) for wait in state.get("active_waits", [])]
        active_waits = merge_actor_waits(
            active_waits,
            actor,
            updated_at_ms=projection.updated_at_ms if projection is not None else server_time_ms,
        )
        recovery_state = text_or_none(record.metadata.get("recovery_state"))
        condition_state = text_or_none(state.get("condition_state"))
        if condition_state is None and actor is not None and actor.execution_state == "condition_waiting":
            condition_state = actor.reason
        _condition_label, condition_action = condition_presentation(condition_state)
        display_phase = (
            recovery_phase(recovery_state, record.metadata.get("reason"))
            if record.status in {"recovering", "uncertain"}
            else actor.phase
            if actor is not None
            and actor.execution_state
            in {"admitted", "preparing", "condition_waiting", "phase_queued", "stopping", "recovering"}
            else str(state.get("phase") or record.metadata.get("phase") or "启动")
        )

        return StrategyMonitorSnapshot(
            instance_id=instance_id,
            session_id=selected_session_id,
            execution_id=record.campaign_id,
            executor_generation=self.executor_generation,
            status=(
                record.status
                if record.status in {"planned", "executing", "stopping"}
                else str(session.get("status") or record.status)
                if session
                else record.status
            ),
            phase=display_phase,
            execution_state=None if actor is None else actor.execution_state,
            phase_queue_position=None if actor is None else actor.queue_position,
            phase_queue_estimated_start_at_ms=None if actor is None else actor.estimated_start_at_ms,
            phase_queue_proxy_limited=False if actor is None else actor.proxy_limited,
            current_run=int(state.get("current_run") or record.metadata.get("current_run") or 0),
            current_round=int(state.get("current_round") or 0),
            target_quote_volume=target_quote,
            verified_quote_volume=verified_quote,
            ledger_verified_quote_volume=ledger_verified,
            remaining_quote_volume=remaining_quote,
            volume_source=volume_source,
            source_complete=bool(session.get("source_complete", False)) if session else False,
            stale=session_stale,
            reconciliation_required=reconciliation_required,
            ledger_sync_state=ledger_sync_state,
            audit_status=audit_status if audit_status in {"verified", "pending", "discrepant"} else "pending",
            recovery_state=recovery_state,
            recovery_attempt=nonnegative_int(record.metadata.get("recovery_attempt")),
            next_recovery_check_at_ms=nonnegative_int(record.metadata.get("next_recovery_check_at_ms")) or None,
            condition_state=condition_state,
            condition_attempt=nonnegative_int(state.get("condition_attempt")),
            next_condition_check_at_ms=nonnegative_int(state.get("next_condition_check_at_ms")) or None,
            condition_action=condition_action if condition_state is not None else None,
            boundary_state=monitor_boundary_state(record),
            btc_quote_volume=btc_quote,
            eth_quote_volume=eth_quote,
            maker_fill_count=maker_fill_count,
            taker_fill_count=taker_fill_count,
            unknown_fill_count=unknown_fill_count,
            submissions=int(state.get("submissions") or 0),
            cancels=int(state.get("cancels") or 0),
            requotes=int(state.get("requotes") or 0),
            active_waits=active_waits,
            timeline=timeline,
            projection_sequence=projected_sequence,
            projection_version=projection_version,
            ledger_revision=len(fills),
            server_time_ms=server_time_ms,
            updated_at_ms=projection.updated_at_ms if projection is not None else server_time_ms,
            freshness=freshness,
            stream_state="catching_up" if projection_lag else "ready",
            cursor=cursor,
            has_more=first_sequence > 1,
        )

    def cursor(self, campaign_id: str, sequence: int) -> str:
        return f"{self.executor_generation}:{campaign_id}:{sequence}"

    def progress_for_session(self, instance_id: str, session_id: str | None) -> StrategyProgressProjection | None:
        """Expose the same durable monitor projection used by the live drawer.

        This method performs SQLite-only reads. It never polls WEEX, syncs
        history, or changes a campaign, so account-list publication can use it
        to stay in lockstep with the detailed execution view.
        """
        snapshot = self.snapshot(instance_id, session_id=session_id, limit=1)
        if snapshot.execution_id is None:
            return None
        return StrategyProgressProjection(
            verified_quote_volume=snapshot.verified_quote_volume,
            volume_source=snapshot.volume_source,
            updated_at_ms=snapshot.updated_at_ms,
            active_waits=tuple(snapshot.active_waits),
        )

    def parse_cursor(self, cursor: str | None) -> tuple[str, str, int] | None:
        if not cursor:
            return None
        try:
            generation, campaign_id, sequence = cursor.rsplit(":", 2)
            return generation, campaign_id, int(sequence)
        except (TypeError, ValueError):
            return None

    def subscriber_opened(self) -> None:
        with self._metrics_lock:
            self._subscriber_count += 1

    def subscriber_closed(self) -> None:
        with self._metrics_lock:
            self._subscriber_count = max(0, self._subscriber_count - 1)

    def reset_recorded(self) -> None:
        with self._metrics_lock:
            self._reset_count += 1

    def metrics(self) -> dict[str, int]:
        with self._metrics_lock:
            return {"subscriber_count": self._subscriber_count, "reset_count": self._reset_count}

    def rebuild_all(self) -> int:
        rebuilt = 0
        for record in self.journal.list_all():
            before = self.journal.monitor_projection(record.campaign_id)
            projection = self._ensure_projection(record)
            if projection is not None and (
                before is None
                or before.projected_sequence != projection.projected_sequence
                or before.projection_version != projection.projection_version
            ):
                rebuilt += 1
        return rebuilt

    def _ensure_projection(self, record: CampaignRecord) -> ExecutionMonitorProjection | None:
        projection, _rows, latest_sequence = self.journal.monitor_read(record.campaign_id, None, 1)
        if (
            projection is not None
            and projection.projection_version == EXECUTION_PROGRESS_PROJECTION_VERSION
            and projection.projected_sequence == latest_sequence
        ):
            return projection
        projector = ExecutionProgressProjector()
        sequence = 0
        last_event_at_ms = time.time_ns() // 1_000_000
        while True:
            batch = self.journal.events_after(record.campaign_id, sequence, 1_000)
            if not batch:
                break
            first = int(batch[0].get("sequence") or 0)
            if first != sequence + 1:
                return projection
            for event in batch:
                projector.apply(event, at_ms=int(event.get("at_ms") or 0))
            sequence = int(batch[-1].get("sequence") or sequence)
            last_event_at_ms = int(batch[-1].get("at_ms") or last_event_at_ms)
        if sequence == 0:
            return projection
        rebuilt = ExecutionMonitorProjection(
            owner_user_id=str(record.metadata.get("owner_user_id") or LEGACY_OWNER_USER_ID),
            account_id=record.instance_id,
            execution_id=record.campaign_id,
            session_id=text_or_none(record.metadata.get("session_id")),
            executor_generation=self.executor_generation,
            projected_sequence=sequence,
            projection_version=EXECUTION_PROGRESS_PROJECTION_VERSION,
            state=projector.snapshot(),
            updated_at_ms=last_event_at_ms,
        )
        self.journal.replace_monitor_projection(rebuilt)
        return self.journal.monitor_projection(record.campaign_id)
