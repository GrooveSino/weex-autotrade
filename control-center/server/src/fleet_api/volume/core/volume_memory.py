from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock

from fleet_api.volume.core.volume_contracts import (
    ACTIVE_SESSION_STATUSES,
    TERMINAL_SESSION_STATUSES,
    FillConflictError,
    NormalizedTradeFill,
    TradeVolumeAggregate,
    VolumeSession,
)
from fleet_api.volume.core.volume_helpers import (
    _aggregate,
    _fill_summary,
    _in_session_window,
    _normalized_session_status,
    _session_projection,
)


class InMemoryTradeVolumeLedger:
    def __init__(self) -> None:
        self._fills: dict[str, dict[str, NormalizedTradeFill]] = {}
        self._complete: dict[str, bool] = {}
        self._account_fills: dict[tuple[str, str], dict[str, NormalizedTradeFill]] = {}
        self._sessions: dict[str, VolumeSession] = {}
        self._checkpoints: dict[tuple[str, str], dict[str, object]] = {}
        self._lock = RLock()

    def record(self, instance_id: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        with self._lock:
            current = self._fills.setdefault(instance_id, {})
            proposed = dict(current)
            for fill in fills:
                existing = proposed.get(fill.identity)
                if existing is not None and existing != fill:
                    raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                proposed[fill.identity] = fill
            inserted = len(proposed) - len(current)
            self._fills[instance_id] = proposed
            return inserted

    def set_complete(self, instance_id: str, complete: bool) -> None:
        with self._lock:
            self._complete[instance_id] = complete

    def aggregate(self, instance_id: str, today_start_ms: int) -> TradeVolumeAggregate:
        with self._lock:
            fills = tuple(self._fills.get(instance_id, {}).values())
            complete = self._complete.get(instance_id, False)
        return _aggregate(fills, today_start_ms, complete)

    def remove(self, instance_id: str) -> None:
        with self._lock:
            self._fills.pop(instance_id, None)
            self._complete.pop(instance_id, None)

    def close(self) -> None:
        return None

    def record_account_fills(self, account_id: str, mode: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        with self._lock:
            current = self._account_fills.setdefault((account_id, mode), {})
            proposed = dict(current)
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            normalized = tuple(
                fill
                if fill.created_at_ms is not None
                else replace(
                    fill,
                    created_at_ms=(current[fill.identity].created_at_ms if fill.identity in current else now_ms),
                )
                for fill in fills
            )
            for fill in normalized:
                existing = proposed.get(fill.identity)
                if existing is not None and existing != fill:
                    raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                proposed[fill.identity] = fill
            inserted = len(proposed) - len(current)
            self._account_fills[(account_id, mode)] = proposed
            self.record(
                account_id,
                tuple(replace(fill, identity=f"{mode}:{fill.identity}") for fill in normalized),
            )
            return inserted

    def create_session(
        self,
        session_id: str,
        account_id: str,
        mode: str,
        started_at_ms: int,
        target_quote_volume: Decimal,
        *,
        maker_only_required: bool = False,
        strategy_id: str | None = None,
        strategy_name: str | None = None,
        strategy_version: int | None = None,
        direction: str = "btc_long_eth_short",
        target_mode: str = "incremental",
        strategy_target_quote_volume: Decimal | None = None,
        baseline_lifetime_quote_volume: Decimal = Decimal(0),
        starting_available_balance_quote: Decimal | None = None,
    ) -> VolumeSession:
        if started_at_ms < 0 or target_quote_volume <= 0 or not target_quote_volume.is_finite():
            raise ValueError("invalid volume session parameters")
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"session {session_id!r} already exists")
            if any(
                session.account_id == account_id
                and session.mode == mode
                and _normalized_session_status(session.status) in ACTIVE_SESSION_STATUSES
                for session in self._sessions.values()
            ):
                raise ValueError("an account can have only one active volume session")
            session = VolumeSession(
                session_id=session_id,
                account_id=account_id,
                mode=mode,
                started_at_ms=started_at_ms,
                target_quote_volume=target_quote_volume,
                status="active",
                verified_quote_volume=Decimal(0),
                remaining_quote_volume=target_quote_volume,
                last_sync_at_ms=None,
                last_reconciliation_at_ms=None,
                source_complete=False,
                stale=True,
                reconciliation_required=False,
                discrepancy_quote_volume=Decimal(0),
                cursor=None,
                high_watermark_ms=None,
                pending_sync=True,
                maker_only_required=maker_only_required,
                uncertain_order_state=False,
                audit_status="pending",
                strategy_id=strategy_id,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                direction=direction,
                target_mode=target_mode,
                strategy_target_quote_volume=strategy_target_quote_volume or target_quote_volume,
                baseline_lifetime_quote_volume=baseline_lifetime_quote_volume,
                starting_available_balance_quote=starting_available_balance_quote,
            )
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> VolumeSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def update_session(self, session_id: str, **changes: object) -> VolumeSession:
        with self._lock:
            current = self._sessions[session_id]
            updated = replace(current, **changes)
            if "verified_quote_volume" in changes and "remaining_quote_volume" not in changes:
                updated = replace(
                    updated,
                    remaining_quote_volume=max(updated.target_quote_volume - updated.verified_quote_volume, Decimal(0)),
                )
            self._sessions[session_id] = updated
            return updated

    def _session_fills(self, session: VolumeSession) -> list[NormalizedTradeFill]:
        return [
            fill
            for fill in self._account_fills.get((session.account_id, session.mode), {}).values()
            if _in_session_window(fill, session)
        ]

    def session_projection(self, session_id: str) -> dict[str, object]:
        with self._lock:
            session = self._sessions[session_id]
            fills = self._session_fills(session)
            eligible = [fill for fill in fills if fill.authoritative]
            verified = sum((fill.quote_volume for fill in eligible), Decimal(0))
            if session.stale or session.reconciliation_required or not session.source_complete:
                verified = session.verified_quote_volume
            return _session_projection(session, fills, verified)

    def account_summary(self, account_id: str, mode: str) -> dict[str, object]:
        with self._lock:
            fills = list(self._account_fills.get((account_id, mode), {}).values())
        return _fill_summary(fills)

    def fills_for_account(self, account_id: str, mode: str, started_at_ms: int = 0) -> tuple[NormalizedTradeFill, ...]:
        with self._lock:
            return tuple(
                fill
                for fill in self._account_fills.get((account_id, mode), {}).values()
                if fill.executed_at_ms >= started_at_ms
            )

    def save_sync_checkpoint(self, account_id: str, mode: str, **values: object) -> None:
        with self._lock:
            current = self._checkpoints.get((account_id, mode), {})
            self._checkpoints[(account_id, mode)] = {
                **current,
                **values,
                "updated_at_ms": time.time_ns() // 1_000_000,
            }

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            value = self._checkpoints.get((account_id, mode))
            return dict(value) if value is not None else None

    def refresh_sessions(
        self,
        account_id: str,
        mode: str,
        *,
        now_ms: int,
        source_complete: bool,
        stale: bool,
        coverage_start_ms: int | None = None,
        high_watermark_ms: int | None = None,
    ) -> None:
        with self._lock:
            for session_id, session in tuple(self._sessions.items()):
                if session.account_id != account_id or session.mode != mode:
                    continue
                if _normalized_session_status(session.status) in TERMINAL_SESSION_STATUSES:
                    continue
                fills = self._session_fills(session)
                verified = sum((fill.quote_volume for fill in fills if fill.authoritative), Decimal(0))
                session_window_complete = source_complete and (
                    session.source_complete or coverage_start_ms is None or coverage_start_ms <= session.started_at_ms
                )
                updated = replace(
                    session,
                    verified_quote_volume=verified,
                    remaining_quote_volume=max(session.target_quote_volume - verified, Decimal(0)),
                    last_sync_at_ms=now_ms,
                    source_complete=session_window_complete,
                    stale=stale or not session_window_complete,
                    pending_sync=stale or not session_window_complete,
                    high_watermark_ms=high_watermark_ms or session.high_watermark_ms,
                )
                projected = _session_projection(updated, fills, verified)
                self._sessions[session_id] = replace(updated, status=str(projected["status"]))

    def latest_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.account_id == account_id and s.mode == mode]
            if not sessions:
                return None
            return self.session_projection(max(sessions, key=lambda item: item.started_at_ms).session_id)

    def active_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.account_id == account_id
                and session.mode == mode
                and _normalized_session_status(session.status) in ACTIVE_SESSION_STATUSES
            ]
            if not sessions:
                return None
            selected = max(sessions, key=lambda item: (item.started_at_ms, item.session_id))
        return self.session_projection(selected.session_id)

    def latest_terminal_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.account_id == account_id
                and session.mode == mode
                and _normalized_session_status(session.status) in TERMINAL_SESSION_STATUSES
            ]
            if not sessions:
                return None
            selected = max(sessions, key=lambda item: (item.started_at_ms, item.session_id))
        return self.session_projection(selected.session_id)

    def list_sessions(
        self, account_id: str, mode: str, *, limit: int, cursor: str | None = None
    ) -> tuple[list[dict[str, object]], str | None]:
        with self._lock:
            sessions = sorted(
                (
                    session
                    for session in self._sessions.values()
                    if session.account_id == account_id and session.mode == mode
                ),
                key=lambda item: (item.started_at_ms, item.session_id),
                reverse=True,
            )
        if cursor is not None:
            try:
                index = next(index for index, item in enumerate(sessions) if item.session_id == cursor) + 1
            except StopIteration:
                return [], None
            sessions = sessions[index:]
        selected = sessions[: limit + 1]
        next_cursor = selected[limit - 1].session_id if len(selected) > limit else None
        return [self.session_projection(item.session_id) for item in selected[:limit]], next_cursor

    def mark_sessions_reconciliation(self, account_id: str, mode: str, *, discrepancy: Decimal = Decimal(0)) -> None:
        with self._lock:
            for session_id, session in tuple(self._sessions.items()):
                if session.account_id == account_id and session.mode == mode:
                    self._sessions[session_id] = replace(
                        session,
                        audit_status="discrepant",
                        reconciliation_required=True,
                        stale=True,
                        discrepancy_quote_volume=discrepancy,
                        pending_sync=True,
                    )
