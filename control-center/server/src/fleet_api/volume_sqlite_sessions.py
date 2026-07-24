from decimal import Decimal

from .volume_contracts import *  # noqa: F403
from .volume_helpers import _fill_summary, _session_projection
from .volume_sqlite_rows import row_to_fill, row_to_session
class SQLiteLedgerSessionsMixin:
    def record_account_fills(self, account_id: str, mode: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        inserted = 0
        inserted_volume = Decimal(0)
        created_at_ms = int(datetime.now(UTC).timestamp() * 1000)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO instances(id, payload) VALUES (?, '{}') ON CONFLICT(id) DO NOTHING",
                (account_id,),
            )
            for fill in fills:
                identity = f"{mode}:{fill.identity}"
                row = self._connection.execute(
                    "SELECT executed_at_ms, quote_volume, symbol, mode, order_id, base_quantity, side, "
                    "position_side, position_action, maker, commission, commission_asset, realized_pnl, source, "
                    "authoritative "
                    "FROM trade_volume_fills WHERE instance_id = ? AND identity = ?",
                    (account_id, identity),
                ).fetchone()
                expected = (
                    fill.executed_at_ms,
                    str(fill.quote_volume),
                    fill.symbol,
                    mode,
                    fill.order_id,
                    str(fill.base_quantity),
                    fill.side,
                    fill.position_side,
                    fill.position_action,
                    None if fill.maker is None else int(fill.maker),
                    str(fill.commission),
                    fill.commission_asset,
                    str(fill.realized_pnl),
                    fill.source,
                    int(fill.authoritative),
                )
                if row is not None:
                    if tuple(row) != expected:
                        raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                    continue
                self._connection.execute(
                    """INSERT INTO trade_volume_fills(
                        instance_id, identity, executed_at_ms, quote_volume, symbol, mode, order_id,
                        base_quantity, side, position_side, position_action, maker, commission,
                        commission_asset, realized_pnl, source, authoritative, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        identity,
                        fill.executed_at_ms,
                        str(fill.quote_volume),
                        fill.symbol,
                        *expected[3:],
                        fill.created_at_ms if fill.created_at_ms is not None else created_at_ms,
                    ),
                )
                inserted += 1
                inserted_volume += fill.quote_volume
            if inserted:
                self._ensure_totals(account_id)
                row = self._connection.execute(
                    "SELECT lifetime_quote, fill_count FROM trade_volume_totals WHERE instance_id = ?",
                    (account_id,),
                ).fetchone()
                self._connection.execute(
                    "UPDATE trade_volume_totals SET lifetime_quote = ?, fill_count = ? WHERE instance_id = ?",
                    (str(Decimal(str(row[0])) + inserted_volume), int(row[1]) + inserted, account_id),
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
        with self._lock, self._connection:
            active = self._connection.execute(
                "SELECT 1 FROM volume_sessions WHERE account_id = ? AND mode = ? "
                "AND status IN ('active', 'recovering', 'stopping') LIMIT 1",
                (account_id, mode),
            ).fetchone()
            if active is not None:
                raise ValueError("an account can have only one active volume session")
            self._connection.execute(
                """INSERT INTO volume_sessions(
                    session_id, account_id, mode, started_at_ms, target_quote_volume, status,
                    verified_quote_volume, remaining_quote_volume, maker_only_required,
                    strategy_id, strategy_name, strategy_version, direction, target_mode,
                    strategy_target_quote_volume, baseline_lifetime_quote_volume,
                    starting_available_balance_quote
                ) VALUES (?, ?, ?, ?, ?, 'active', '0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    account_id,
                    mode,
                    started_at_ms,
                    str(target_quote_volume),
                    str(target_quote_volume),
                    int(maker_only_required),
                    strategy_id,
                    strategy_name,
                    strategy_version,
                    direction,
                    target_mode,
                    str(strategy_target_quote_volume or target_quote_volume),
                    str(baseline_lifetime_quote_volume),
                    None if starting_available_balance_quote is None else str(starting_available_balance_quote),
                ),
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> VolumeSession | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM volume_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row_to_session(row) if row else None

    def update_session(self, session_id: str, **changes: object) -> VolumeSession:
        allowed = {
            "status",
            "verified_quote_volume",
            "remaining_quote_volume",
            "last_sync_at_ms",
            "last_reconciliation_at_ms",
            "source_complete",
            "stale",
            "reconciliation_required",
            "discrepancy_quote_volume",
            "cursor",
            "high_watermark_ms",
            "pending_sync",
            "uncertain_order_state",
            "audit_status",
            "finished_at_ms",
            "result",
            "result_reason",
            "final_lifetime_quote_volume",
            "ending_available_balance_quote",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown session fields: {sorted(unknown)}")
        current = self.get_session(session_id)
        if current is None:
            raise KeyError(session_id)
        if "verified_quote_volume" in changes and "remaining_quote_volume" not in changes:
            changes["remaining_quote_volume"] = max(
                current.target_quote_volume - Decimal(str(changes["verified_quote_volume"])), Decimal(0)
            )
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = [
            int(value)
            if key in {"source_complete", "stale", "reconciliation_required", "pending_sync", "uncertain_order_state"}
            else str(value)
            if key
            in {
                "verified_quote_volume",
                "remaining_quote_volume",
                "discrepancy_quote_volume",
                "final_lifetime_quote_volume",
                "ending_available_balance_quote",
            }
            and value is not None
            else value
            for key, value in changes.items()
        ]
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE volume_sessions SET {assignments} WHERE session_id = ?", (*values, session_id)
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def _session_fills(self, session: VolumeSession) -> list[NormalizedTradeFill]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT identity, executed_at_ms, quote_volume, symbol, order_id, base_quantity, side,
                   position_side, position_action, maker, commission, commission_asset, realized_pnl,
                   source, authoritative, created_at_ms FROM trade_volume_fills
                   WHERE instance_id = ? AND mode = ? AND executed_at_ms >= ?""",
                (session.account_id, session.mode, session.started_at_ms),
            ).fetchall()
        return [row_to_fill(row) for row in rows]

    def session_projection(self, session_id: str) -> dict[str, object]:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        fills = self._session_fills(session)
        verified = sum((fill.quote_volume for fill in fills if fill.authoritative), Decimal(0))
        if session.stale or session.reconciliation_required or not session.source_complete:
            verified = session.verified_quote_volume
        return _session_projection(session, fills, verified)

    def account_summary(self, account_id: str, mode: str) -> dict[str, object]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT identity, executed_at_ms, quote_volume, symbol, order_id, base_quantity, side,
                   position_side, position_action, maker, commission, commission_asset, realized_pnl,
                   source, authoritative, created_at_ms FROM trade_volume_fills
                   WHERE instance_id = ? AND mode = ?""",
                (account_id, mode),
            ).fetchall()
        fills = [row_to_fill(row) for row in rows]
        return _fill_summary(fills)

    def fills_for_account(self, account_id: str, mode: str, started_at_ms: int = 0) -> tuple[NormalizedTradeFill, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT identity, executed_at_ms, quote_volume, symbol, order_id, base_quantity, side,
                   position_side, position_action, maker, commission, commission_asset, realized_pnl,
                   source, authoritative, created_at_ms FROM trade_volume_fills
                   WHERE instance_id = ? AND mode = ? AND executed_at_ms >= ?""",
                (account_id, mode, started_at_ms),
            ).fetchall()
        return tuple(row_to_fill(row) for row in rows)
