from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Protocol
from zoneinfo import ZoneInfo

from .models import AccountInstance
from .vault import CredentialMaterial

SHANGHAI = ZoneInfo("Asia/Shanghai")

ACTIVE_SESSION_STATUSES = frozenset({"active", "recovering", "stopping"})
TERMINAL_SESSION_STATUSES = frozenset({"completed", "stopped"})


class FillConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedTradeFill:
    identity: str
    executed_at_ms: int
    quote_volume: Decimal
    symbol: str
    order_id: str = ""
    base_quantity: Decimal = Decimal(0)
    side: str = ""
    position_side: str = ""
    position_action: str = "unknown"
    maker: bool | None = None
    commission: Decimal = Decimal(0)
    commission_asset: str | None = None
    realized_pnl: Decimal = Decimal(0)
    source: str = "user_trades"
    authoritative: bool = True
    created_at_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise ValueError("fill identity cannot be empty")
        if self.executed_at_ms < 0:
            raise ValueError("fill timestamp cannot be negative")
        if not self.quote_volume.is_finite() or self.quote_volume < 0:
            raise ValueError("fill quote volume must be finite and non-negative")
        if not self.symbol.strip():
            raise ValueError("fill symbol cannot be empty")
        if self.created_at_ms is not None and self.created_at_ms < 0:
            raise ValueError("fill creation timestamp cannot be negative")
        for value, name in (
            (self.base_quantity, "base quantity"),
            (self.commission, "commission"),
            (self.realized_pnl, "realized pnl"),
        ):
            if not value.is_finite():
                raise ValueError(f"fill {name} must be finite")


@dataclass(frozen=True, slots=True)
class TradeHistoryPage:
    fills: tuple[NormalizedTradeFill, ...]
    next_cursor: str | None
    complete: bool = True
    high_watermark_ms: int | None = None
    # Completeness of the requested scan window, independent from whether the
    # account's full lifetime history is covered.
    window_complete: bool | None = None


@dataclass(frozen=True, slots=True)
class TradeHistoryContext:
    instance: AccountInstance
    credentials: CredentialMaterial | None


@dataclass(frozen=True, slots=True)
class TradeVolumeAggregate:
    lifetime: Decimal
    today: Decimal
    fill_count: int
    complete: bool


@dataclass(frozen=True, slots=True)
class TradeHistorySyncResult:
    aggregate: TradeVolumeAggregate
    pages_fetched: int
    fills_inserted: int
    stop_reason: str
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class VolumeSession:
    session_id: str
    account_id: str
    mode: str
    started_at_ms: int
    target_quote_volume: Decimal
    status: str
    verified_quote_volume: Decimal
    remaining_quote_volume: Decimal
    last_sync_at_ms: int | None
    last_reconciliation_at_ms: int | None
    source_complete: bool
    stale: bool
    reconciliation_required: bool
    discrepancy_quote_volume: Decimal
    cursor: str | None
    high_watermark_ms: int | None
    pending_sync: bool
    maker_only_required: bool
    uncertain_order_state: bool
    audit_status: str = "pending"
    strategy_id: str | None = None
    strategy_name: str | None = None
    strategy_version: int | None = None
    target_mode: str = "incremental"
    strategy_target_quote_volume: Decimal | None = None
    baseline_lifetime_quote_volume: Decimal = Decimal(0)
    finished_at_ms: int | None = None
    result: str | None = None
    result_reason: str | None = None
    final_lifetime_quote_volume: Decimal | None = None
    starting_available_balance_quote: Decimal | None = None
    ending_available_balance_quote: Decimal | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "account_id": self.account_id,
            "mode": self.mode,
            "started_at_ms": self.started_at_ms,
            "target_quote_volume": str(self.target_quote_volume),
            "status": self.status,
            "verified_quote_volume": str(self.verified_quote_volume),
            "remaining_quote_volume": str(self.remaining_quote_volume),
            "last_sync_at_ms": self.last_sync_at_ms,
            "last_reconciliation_at_ms": self.last_reconciliation_at_ms,
            "source_complete": self.source_complete,
            "stale": self.stale,
            "reconciliation_required": self.reconciliation_required,
            "discrepancy_quote_volume": str(self.discrepancy_quote_volume),
            "cursor": self.cursor,
            "high_watermark_ms": self.high_watermark_ms,
            "pending_sync": self.pending_sync,
            "maker_only_required": self.maker_only_required,
            "uncertain_order_state": self.uncertain_order_state,
            "audit_status": self.audit_status,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "target_mode": self.target_mode,
            "strategy_target_quote_volume": str(
                self.strategy_target_quote_volume or self.target_quote_volume
            ),
            "baseline_lifetime_quote_volume": str(self.baseline_lifetime_quote_volume),
            "finished_at_ms": self.finished_at_ms,
            "result": self.result,
            "result_reason": self.result_reason,
            "final_lifetime_quote_volume": (
                None
                if self.final_lifetime_quote_volume is None
                else str(self.final_lifetime_quote_volume)
            ),
            "starting_available_balance_quote": (
                None
                if self.starting_available_balance_quote is None
                else str(self.starting_available_balance_quote)
            ),
            "ending_available_balance_quote": (
                None
                if self.ending_available_balance_quote is None
                else str(self.ending_available_balance_quote)
            ),
        }


class TradeHistorySource(Protocol):
    async def fetch_page(
        self,
        context: TradeHistoryContext,
        *,
        cursor: str | None,
        limit: int,
    ) -> TradeHistoryPage: ...


class TradeVolumeLedger(Protocol):
    def record(self, instance_id: str, fills: tuple[NormalizedTradeFill, ...]) -> int: ...

    def set_complete(self, instance_id: str, complete: bool) -> None: ...

    def aggregate(self, instance_id: str, today_start_ms: int) -> TradeVolumeAggregate: ...

    def remove(self, instance_id: str) -> None: ...

    def close(self) -> None: ...

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
        target_mode: str = "incremental",
        strategy_target_quote_volume: Decimal | None = None,
        baseline_lifetime_quote_volume: Decimal = Decimal(0),
        starting_available_balance_quote: Decimal | None = None,
    ) -> VolumeSession: ...

    def get_session(self, session_id: str) -> VolumeSession | None: ...

    def update_session(self, session_id: str, **changes: object) -> VolumeSession: ...

    def session_projection(self, session_id: str) -> dict[str, object]: ...

    def record_account_fills(
        self,
        account_id: str,
        mode: str,
        fills: tuple[NormalizedTradeFill, ...],
    ) -> int: ...

    def account_summary(self, account_id: str, mode: str) -> dict[str, object]: ...

    def save_sync_checkpoint(
        self,
        account_id: str,
        mode: str,
        *,
        cursor: str | None,
        high_watermark_ms: int | None,
        pending: bool,
        source_complete: bool,
        coverage_complete: bool,
        stale: bool,
    ) -> None: ...

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, object] | None: ...

    def fills_for_account(
        self, account_id: str, mode: str, started_at_ms: int = 0
    ) -> tuple[NormalizedTradeFill, ...]: ...

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
    ) -> None: ...

    def latest_session(self, account_id: str, mode: str) -> dict[str, object] | None: ...

    def active_session(self, account_id: str, mode: str) -> dict[str, object] | None: ...

    def latest_terminal_session(self, account_id: str, mode: str) -> dict[str, object] | None: ...

    def list_sessions(
        self, account_id: str, mode: str, *, limit: int, cursor: str | None = None
    ) -> tuple[list[dict[str, object]], str | None]: ...

    def mark_sessions_reconciliation(
        self, account_id: str, mode: str, *, discrepancy: Decimal = Decimal(0)
    ) -> None: ...
