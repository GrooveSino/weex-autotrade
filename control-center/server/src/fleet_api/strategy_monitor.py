from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.execution_progress import ExecutionProgressProjector, event_name

from .campaigns import CampaignJournal, CampaignRecord
from .models import (
    ActiveExecutionWait,
    ExecutionTimelineEntry,
    LogLevel,
    StrategyMonitorSnapshot,
)
from .volume_history import TradeVolumeLedger


class StrategyMonitorService:
    def __init__(self, journal: CampaignJournal, ledger: TradeVolumeLedger, executor_generation: str) -> None:
        self.journal = journal
        self.ledger = ledger
        self.executor_generation = executor_generation

    def snapshot(
        self,
        instance_id: str,
        *,
        session_id: str | None = None,
        before_sequence: int | None = None,
        limit: int = 200,
        event_rows: list[dict[str, Any]] | None = None,
    ) -> StrategyMonitorSnapshot:
        record = self.journal.monitor_record(instance_id, session_id)
        if record is None:
            return StrategyMonitorSnapshot(
                instance_id=instance_id,
                executor_generation=self.executor_generation,
                status="idle",
                phase="暂无策略运行记录",
            )
        selected_session_id = _text_or_none(record.metadata.get("session_id"))
        session = self.ledger.session_projection(selected_session_id) if selected_session_id else None
        started_at_ms = int(session.get("started_at_ms") or 0) if session else 0
        fills = [
            fill
            for fill in self.ledger.fills_for_account(instance_id, "live", started_at_ms)
            if fill.authoritative and fill.executed_at_ms >= started_at_ms
        ]
        btc_quote = sum((fill.quote_volume for fill in fills if fill.symbol.upper().startswith("BTC")), Decimal(0))
        eth_quote = sum((fill.quote_volume for fill in fills if fill.symbol.upper().startswith("ETH")), Decimal(0))
        monitor_state = record.metadata.get("monitor_state")
        state = (
            monitor_state
            if isinstance(monitor_state, dict) and monitor_state.get("schema_version") == 2
            else _reconstruct_state(self.journal, record)
        )
        target_quote = _decimal(session, "target_quote_volume", record.campaign.target_turnover_quote)
        ledger_verified = _decimal(session, "verified_quote_volume")
        execution_verified = _decimal(state, "execution_verified_quote_volume")
        ledger_is_current = bool(session) and bool(session.get("source_complete")) and not bool(
            session.get("stale") or session.get("reconciliation_required")
        )
        verified_quote = ledger_verified if ledger_is_current else max(ledger_verified, execution_verified)
        remaining_quote = max(target_quote - verified_quote, Decimal(0))
        projected_btc_quote = _decimal(state, "btc_quote_volume")
        projected_eth_quote = _decimal(state, "eth_quote_volume")
        if ledger_is_current:
            volume_source = "ledger"
        elif execution_verified > 0:
            volume_source = "execution_journal"
        else:
            volume_source = "pending"
        rows = event_rows
        if rows is None:
            rows = self.journal.events_before(record.campaign_id, before_sequence, max(limit * 5, limit))
        timeline = _timeline(record.campaign_id, rows)[-limit:]
        latest = self.journal.events_before(record.campaign_id, None, 1)
        last_sequence = int(latest[-1].get("sequence") or 0) if latest else 0
        first_sequence = int(rows[0].get("sequence") or 0) if rows else 0
        cursor = self.cursor(record.campaign_id, last_sequence) if last_sequence else None

        return StrategyMonitorSnapshot(
            instance_id=instance_id,
            session_id=selected_session_id,
            execution_id=record.campaign_id,
            executor_generation=self.executor_generation,
            status=(
                record.status
                if record.status in {"planned", "executing", "stopping"}
                else str(session.get("status") or record.status) if session else record.status
            ),
            phase=str(state.get("phase") or record.metadata.get("phase") or "启动"),
            current_run=int(state.get("current_run") or record.metadata.get("current_run") or 0),
            current_round=int(state.get("current_round") or 0),
            target_quote_volume=target_quote,
            verified_quote_volume=verified_quote,
            ledger_verified_quote_volume=ledger_verified,
            remaining_quote_volume=remaining_quote,
            volume_source=volume_source,
            source_complete=bool(session.get("source_complete", False)) if session else False,
            stale=bool(session.get("stale", True)) if session else True,
            reconciliation_required=bool(session.get("reconciliation_required", False)) if session else False,
            btc_quote_volume=btc_quote if ledger_is_current else max(btc_quote, projected_btc_quote),
            eth_quote_volume=eth_quote if ledger_is_current else max(eth_quote, projected_eth_quote),
            maker_fill_count=sum(1 for fill in fills if fill.maker is True),
            taker_fill_count=sum(1 for fill in fills if fill.maker is False),
            unknown_fill_count=sum(1 for fill in fills if fill.maker is None),
            submissions=int(state.get("submissions") or 0),
            cancels=int(state.get("cancels") or 0),
            requotes=int(state.get("requotes") or 0),
            active_waits=[ActiveExecutionWait.model_validate(wait) for wait in state.get("active_waits", [])],
            timeline=timeline,
            cursor=cursor,
            has_more=first_sequence > 1,
        )

    def cursor(self, campaign_id: str, sequence: int) -> str:
        return f"{self.executor_generation}:{campaign_id}:{sequence}"

    def parse_cursor(self, cursor: str | None) -> tuple[str, str, int] | None:
        if not cursor:
            return None
        try:
            generation, campaign_id, sequence = cursor.rsplit(":", 2)
            return generation, campaign_id, int(sequence)
        except (TypeError, ValueError):
            return None


def _reconstruct_state(journal: CampaignJournal, record: CampaignRecord) -> dict[str, Any]:
    projector = ExecutionProgressProjector()
    for event in journal.events_before(record.campaign_id, None, 2_000):
        projector.apply(event, at_ms=int(event.get("at_ms") or 0))
    return projector.snapshot()


def _timeline(campaign_id: str, rows: list[dict[str, Any]]) -> list[ExecutionTimelineEntry]:
    projector = ExecutionProgressProjector()
    timeline: list[ExecutionTimelineEntry] = []
    for event in rows:
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


def _decimal(source: dict[str, object] | None, key: str, default: Decimal = Decimal(0)) -> Decimal:
    if source is None or source.get(key) is None:
        return default
    try:
        value = Decimal(str(source[key]))
    except Exception:  # noqa: BLE001 - malformed display data fails closed to zero
        return default
    return value if value.is_finite() else default


def _text_or_none(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None
