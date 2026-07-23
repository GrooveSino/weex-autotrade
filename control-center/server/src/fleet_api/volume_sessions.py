from __future__ import annotations

from decimal import Decimal

from .volume_contracts import TERMINAL_SESSION_STATUSES, NormalizedTradeFill, TradeVolumeLedger
from .volume_helpers import _fill_signature


class SessionVolumeService:
    """Small application boundary for session progress and explicit reconciliation."""

    def __init__(self, ledger: TradeVolumeLedger) -> None:
        self.ledger = ledger

    def start(
        self,
        *,
        session_id: str,
        account_id: str,
        mode: str,
        started_at_ms: int,
        target_quote_volume: Decimal,
        maker_only_required: bool = False,
        strategy_id: str | None = None,
        strategy_name: str | None = None,
        strategy_version: int | None = None,
        target_mode: str = "incremental",
        strategy_target_quote_volume: Decimal | None = None,
        baseline_lifetime_quote_volume: Decimal = Decimal(0),
        starting_available_balance_quote: Decimal | None = None,
    ) -> dict[str, object]:
        session = self.ledger.create_session(
            session_id,
            account_id,
            mode,
            started_at_ms,
            target_quote_volume,
            maker_only_required=maker_only_required,
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            target_mode=target_mode,
            strategy_target_quote_volume=strategy_target_quote_volume,
            baseline_lifetime_quote_volume=baseline_lifetime_quote_volume,
            starting_available_balance_quote=starting_available_balance_quote,
        )
        return self.ledger.session_projection(session.session_id)

    def progress(self, session_id: str) -> dict[str, object]:
        return self.ledger.session_projection(session_id)

    def mark_stopping(self, session_id: str) -> dict[str, object]:
        self.ledger.update_session(session_id, status="stopping")
        return self.progress(session_id)

    def mark_recovering(self, session_id: str, *, reason: str, finished_at_ms: int) -> dict[str, object]:
        self.ledger.update_session(
            session_id,
            status="recovering",
            audit_status="pending",
            result=None,
            result_reason=reason,
            finished_at_ms=finished_at_ms,
            uncertain_order_state=True,
            source_complete=False,
            stale=True,
            reconciliation_required=True,
            pending_sync=True,
        )
        return self.progress(session_id)

    def finalize(
        self,
        session_id: str,
        *,
        result: str,
        reason: str | None,
        finished_at_ms: int,
        final_lifetime_quote_volume: Decimal,
        ending_available_balance_quote: Decimal | None = None,
    ) -> dict[str, object]:
        if result not in TERMINAL_SESSION_STATUSES:
            raise ValueError("session result must be completed or stopped")
        projection = self.progress(session_id)
        verified = Decimal(str(projection["verified_quote_volume"]))
        target = Decimal(str(projection["target_quote_volume"]))
        verified_state = (
            bool(projection["source_complete"])
            and not bool(projection["stale"])
            and not bool(projection["reconciliation_required"])
            and not bool(projection["pending_sync"])
            and not bool(projection.get("uncertain_order_state", False))
        )
        audit_status = "verified" if verified_state and (result != "completed" or verified >= target) else "pending"
        self.ledger.update_session(
            session_id,
            status=result,
            audit_status=audit_status,
            result=result,
            result_reason=reason,
            finished_at_ms=finished_at_ms,
            final_lifetime_quote_volume=final_lifetime_quote_volume,
            ending_available_balance_quote=ending_available_balance_quote,
        )
        return self.progress(session_id)

    def reconcile(
        self,
        session_id: str,
        authoritative_fills: tuple[NormalizedTradeFill, ...],
        *,
        reconciled_at_ms: int,
    ) -> dict[str, object]:
        session = self.ledger.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        expected = {fill.identity: fill for fill in authoritative_fills if fill.executed_at_ms >= session.started_at_ms}
        existing = {
            fill.identity: fill
            for fill in getattr(self.ledger, "fills_for_account", lambda *_: ())(
                session.account_id, session.mode, session.started_at_ms
            )
        }
        missing = set(expected) - set(existing)
        extra = set(existing) - set(expected)
        shared = set(expected) & set(existing)
        changed = {key for key in shared if _fill_signature(expected[key]) != _fill_signature(existing[key])}
        discrepancy = (
            sum((expected[key].quote_volume for key in missing), Decimal(0))
            + sum((existing[key].quote_volume for key in extra), Decimal(0))
            + sum(
                (abs(expected[key].quote_volume - existing[key].quote_volume) for key in changed),
                Decimal(0),
            )
        )
        if missing or extra or changed:
            self.ledger.update_session(
                session_id,
                audit_status="discrepant",
                reconciliation_required=True,
                stale=True,
                discrepancy_quote_volume=discrepancy,
                last_reconciliation_at_ms=reconciled_at_ms,
                pending_sync=False,
            )
        else:
            self.ledger.update_session(
                session_id,
                reconciliation_required=False,
                audit_status="verified",
                stale=False,
                source_complete=True,
                discrepancy_quote_volume=Decimal(0),
                last_reconciliation_at_ms=reconciled_at_ms,
                pending_sync=False,
            )
        return self.ledger.session_projection(session_id)

    def recover_stopped(
        self,
        session_id: str,
        authoritative_fills: tuple[NormalizedTradeFill, ...],
        *,
        reconciled_at_ms: int,
        ending_available_balance_quote: Decimal | None = None,
    ) -> dict[str, object]:
        """Close a recoverable old run after a complete authoritative read.

        This is deliberately a ledger-only transition.  The caller must verify
        the exchange boundary and source completeness before invoking it.
        """
        session = self.ledger.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        self.ledger.record_account_fills(session.account_id, session.mode, authoritative_fills)
        projection = self.reconcile(
            session_id,
            authoritative_fills,
            reconciled_at_ms=reconciled_at_ms,
        )
        if bool(projection["reconciliation_required"]):
            return projection
        self.ledger.update_session(
            session_id,
            uncertain_order_state=False,
            source_complete=True,
            stale=False,
            pending_sync=False,
        )
        lifetime = self.ledger.aggregate(session.account_id, 0).lifetime
        return self.finalize(
            session_id,
            result="stopped",
            reason="automatic_startup_recovery",
            finished_at_ms=reconciled_at_ms,
            final_lifetime_quote_volume=lifetime,
            ending_available_balance_quote=ending_available_balance_quote,
        )
