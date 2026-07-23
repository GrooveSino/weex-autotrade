from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import RLock

from .volume_contracts import *  # noqa: F403
from .volume_helpers import _aggregate, _fill_signature, _fill_summary, _normalized_session_status, _session_projection


class SQLiteLedgerBasicMixin:
    def record(self, instance_id: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        inserted = 0
        inserted_volume = Decimal(0)
        with self._lock, self._connection:
            self._ensure_totals(instance_id)
            for fill in fills:
                row = self._connection.execute(
                    """
                        SELECT executed_at_ms, quote_volume, symbol
                        FROM trade_volume_fills
                        WHERE instance_id = ? AND identity = ?
                        """,
                    (instance_id, fill.identity),
                ).fetchone()
                if row is not None:
                    existing = (int(row[0]), Decimal(row[1]), str(row[2]))
                    expected = (fill.executed_at_ms, fill.quote_volume, fill.symbol)
                    if existing != expected:
                        raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                    continue
                self._connection.execute(
                    """
                        INSERT INTO trade_volume_fills(
                            instance_id, identity, executed_at_ms, quote_volume, symbol
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                    (
                        instance_id,
                        fill.identity,
                        fill.executed_at_ms,
                        str(fill.quote_volume),
                        fill.symbol,
                    ),
                )
                inserted += 1
                inserted_volume += fill.quote_volume
            if inserted:
                row = self._connection.execute(
                    "SELECT lifetime_quote, fill_count FROM trade_volume_totals WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()
                self._connection.execute(
                    """
                    UPDATE trade_volume_totals
                    SET lifetime_quote = ?, fill_count = ?
                    WHERE instance_id = ?
                    """,
                    (str(Decimal(row[0]) + inserted_volume), int(row[1]) + inserted, instance_id),
                )
        return inserted

    def set_complete(self, instance_id: str, complete: bool) -> None:
        with self._lock, self._connection:
            self._ensure_totals(instance_id)
            self._connection.execute(
                "UPDATE trade_volume_totals SET complete = ? WHERE instance_id = ?",
                (int(complete), instance_id),
            )

    def aggregate(self, instance_id: str, today_start_ms: int) -> TradeVolumeAggregate:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT lifetime_quote, fill_count, complete
                FROM trade_volume_totals
                WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
            amounts = self._connection.execute(
                """
                SELECT quote_volume FROM trade_volume_fills
                WHERE instance_id = ? AND executed_at_ms >= ?
                """,
                (instance_id, today_start_ms),
            ).fetchall()
        if row is None:
            return TradeVolumeAggregate(Decimal(0), Decimal(0), 0, False)
        today = sum((Decimal(amount[0]) for amount in amounts), start=Decimal(0))
        return TradeVolumeAggregate(Decimal(row[0]), today, int(row[1]), bool(row[2]))

    def remove(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM trade_volume_totals WHERE instance_id = ?", (instance_id,))
            self._connection.execute("DELETE FROM trade_volume_fills WHERE instance_id = ?", (instance_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()
