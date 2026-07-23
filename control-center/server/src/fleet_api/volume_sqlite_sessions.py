import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import RLock

from .volume_contracts import *  # noqa: F403
from .volume_helpers import _aggregate, _fill_signature, _fill_summary, _normalized_session_status, _session_projection
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

    def _row_to_session(self, row: sqlite3.Row) -> VolumeSession:
        return VolumeSession(
            session_id=str(row["session_id"]),
            account_id=str(row["account_id"]),
            mode=str(row["mode"]),
            started_at_ms=int(row["started_at_ms"]),
            target_quote_volume=Decimal(str(row["target_quote_volume"])),
            status=str(row["status"]),
            verified_quote_volume=Decimal(str(row["verified_quote_volume"])),
            remaining_quote_volume=Decimal(str(row["remaining_quote_volume"])),
            last_sync_at_ms=None if row["last_sync_at_ms"] is None else int(row["last_sync_at_ms"]),
            last_reconciliation_at_ms=None
            if row["last_reconciliation_at_ms"] is None
            else int(row["last_reconciliation_at_ms"]),
            source_complete=bool(row["source_complete"]),
            stale=bool(row["stale"]),
            reconciliation_required=bool(row["reconciliation_required"]),
            discrepancy_quote_volume=Decimal(str(row["discrepancy_quote_volume"])),
            cursor=row["cursor"],
            high_watermark_ms=row["high_watermark_ms"],
            pending_sync=bool(row["pending_sync"]),
            maker_only_required=bool(row["maker_only_required"]),
            uncertain_order_state=bool(row["uncertain_order_state"]),
            strategy_id=row["strategy_id"],
            strategy_name=row["strategy_name"],
            strategy_version=None if row["strategy_version"] is None else int(row["strategy_version"]),
            target_mode=str(row["target_mode"] or "incremental"),
            strategy_target_quote_volume=Decimal(
                str(row["strategy_target_quote_volume"] or row["target_quote_volume"])
            ),
            baseline_lifetime_quote_volume=Decimal(str(row["baseline_lifetime_quote_volume"] or "0")),
            finished_at_ms=None if row["finished_at_ms"] is None else int(row["finished_at_ms"]),
            result=row["result"],
            result_reason=row["result_reason"],
            final_lifetime_quote_volume=(
                None
                if row["final_lifetime_quote_volume"] is None
                else Decimal(str(row["final_lifetime_quote_volume"]))
            ),
            starting_available_balance_quote=(
                None
                if row["starting_available_balance_quote"] is None
                else Decimal(str(row["starting_available_balance_quote"]))
            ),
            ending_available_balance_quote=(
                None
                if row["ending_available_balance_quote"] is None
                else Decimal(str(row["ending_available_balance_quote"]))
            ),
        )

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
    ) -> VolumeSession:
        if started_at_ms < 0 or target_quote_volume <= 0 or not target_quote_volume.is_finite():
            raise ValueError("invalid volume session parameters")
        with self._lock, self._connection:
            active = self._connection.execute(
                "SELECT 1 FROM volume_sessions WHERE account_id = ? AND mode = ? "
                "AND status IN ('active', 'stopping', 'verification_pending', 'uncertain') LIMIT 1",
                (account_id, mode),
            ).fetchone()
            if active is not None:
                raise ValueError("an account can have only one active volume session")
            self._connection.execute(
                """INSERT INTO volume_sessions(
                    session_id, account_id, mode, started_at_ms, target_quote_volume, status,
                    verified_quote_volume, remaining_quote_volume, maker_only_required,
                    strategy_id, strategy_name, strategy_version, target_mode,
                    strategy_target_quote_volume, baseline_lifetime_quote_volume,
                    starting_available_balance_quote
                ) VALUES (?, ?, ?, ?, ?, 'active', '0', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        return self._row_to_session(row) if row else None

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
        return [
            NormalizedTradeFill(
                identity=str(row[0]).split(":", 1)[-1],
                executed_at_ms=int(row[1]),
                quote_volume=Decimal(str(row[2])),
                symbol=str(row[3]),
                order_id=str(row[4]),
                base_quantity=Decimal(str(row[5])),
                side=str(row[6]),
                position_side=str(row[7]),
                position_action=str(row[8]),
                maker=None if row[9] is None else bool(row[9]),
                commission=Decimal(str(row[10])),
                commission_asset=row[11],
                realized_pnl=Decimal(str(row[12])),
                source=str(row[13]),
                authoritative=bool(row[14]),
                created_at_ms=int(row[15]),
            )
            for row in rows
        ]

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
        fills = [
            NormalizedTradeFill(
                identity=str(row[0]).split(":", 1)[-1],
                executed_at_ms=int(row[1]),
                quote_volume=Decimal(str(row[2])),
                symbol=str(row[3]),
                order_id=str(row[4]),
                base_quantity=Decimal(str(row[5])),
                side=str(row[6]),
                position_side=str(row[7]),
                position_action=str(row[8]),
                maker=None if row[9] is None else bool(row[9]),
                commission=Decimal(str(row[10])),
                commission_asset=row[11],
                realized_pnl=Decimal(str(row[12])),
                source=str(row[13]),
                authoritative=bool(row[14]),
                created_at_ms=int(row[15]),
            )
            for row in rows
        ]
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
        return tuple(
            NormalizedTradeFill(
                identity=str(row[0]).split(":", 1)[-1],
                executed_at_ms=int(row[1]),
                quote_volume=Decimal(str(row[2])),
                symbol=str(row[3]),
                order_id=str(row[4]),
                base_quantity=Decimal(str(row[5])),
                side=str(row[6]),
                position_side=str(row[7]),
                position_action=str(row[8]),
                maker=None if row[9] is None else bool(row[9]),
                commission=Decimal(str(row[10])),
                commission_asset=row[11],
                realized_pnl=Decimal(str(row[12])),
                source=str(row[13]),
                authoritative=bool(row[14]),
                created_at_ms=int(row[15]),
            )
            for row in rows
        )
