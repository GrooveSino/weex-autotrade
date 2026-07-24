from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from threading import RLock

from .execution_contracts import (
    _TERMINAL_STATUSES,
    BeginCycleResult,
    CycleExecutionStatus,
    ExecutionRecord,
    ExecutionStateError,
    PairCyclePlan,
    _require_reason_code,
)


class InMemoryExecutionJournal:
    def __init__(self) -> None:
        self._records: dict[str, ExecutionRecord] = {}
        self._sequences: dict[tuple[str, int], str] = {}
        self._lock = RLock()

    def begin(self, instance_id: str, plan: PairCyclePlan) -> BeginCycleResult:
        with self._lock:
            existing_id = self._sequences.get((instance_id, plan.sequence))
            if existing_id is not None:
                return BeginCycleResult(self._records[existing_id], False)
            now_ms = time.time_ns() // 1_000_000
            record = ExecutionRecord(
                instance_id=instance_id,
                plan=plan,
                status=CycleExecutionStatus.PLANNED,
                reason="prepared_before_submit",
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            self._records[plan.cycle_id] = record
            self._sequences[(instance_id, plan.sequence)] = plan.cycle_id
            return BeginCycleResult(record, True)

    def finish(
        self,
        cycle_id: str,
        status: CycleExecutionStatus,
        reason: str,
    ) -> ExecutionRecord:
        if status is CycleExecutionStatus.PLANNED:
            raise ExecutionStateError("execution cycle cannot finish as planned")
        _require_reason_code(reason)
        with self._lock:
            current = self._records.get(cycle_id)
            if current is None:
                raise KeyError(cycle_id)
            if current.status in _TERMINAL_STATUSES:
                if current.status is status and current.reason == reason:
                    return current
                raise ExecutionStateError("terminal execution cycle cannot change outcome")
            if current.status is CycleExecutionStatus.OPENED and status is CycleExecutionStatus.OPENED:
                if current.reason == reason:
                    return current
                raise ExecutionStateError("opened execution cycle cannot be opened again")
            updated = ExecutionRecord(
                instance_id=current.instance_id,
                plan=current.plan,
                status=status,
                reason=reason,
                created_at_ms=current.created_at_ms,
                updated_at_ms=time.time_ns() // 1_000_000,
            )
            self._records[cycle_id] = updated
            return updated

    def find(self, instance_id: str, sequence: int) -> ExecutionRecord | None:
        with self._lock:
            cycle_id = self._sequences.get((instance_id, sequence))
            return self._records.get(cycle_id) if cycle_id is not None else None

    def list_recent(self, instance_id: str, limit: int) -> list[ExecutionRecord]:
        with self._lock:
            records = [record for record in self._records.values() if record.instance_id == instance_id]
            records.sort(key=lambda record: record.plan.sequence, reverse=True)
            return records[:limit]

    def recover_incomplete(self) -> int:
        with self._lock:
            pending = [
                record.plan.cycle_id
                for record in self._records.values()
                if record.status in {CycleExecutionStatus.PLANNED, CycleExecutionStatus.OPENED}
            ]
        for cycle_id in pending:
            current = self._records[cycle_id]
            self.finish(
                cycle_id,
                CycleExecutionStatus.UNCERTAIN,
                (
                    "process_restarted_with_open_pair"
                    if current.status is CycleExecutionStatus.OPENED
                    else "process_restarted_before_terminal_result"
                ),
            )
        return len(pending)

    def remove(self, instance_id: str) -> None:
        with self._lock:
            cycle_ids = [
                cycle_id
                for (candidate_id, _sequence), cycle_id in self._sequences.items()
                if candidate_id == instance_id
            ]
            for cycle_id in cycle_ids:
                record = self._records.pop(cycle_id)
                self._sequences.pop((record.instance_id, record.plan.sequence), None)

    def close(self) -> None:
        return None


class SQLiteExecutionJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_cycles (
                cycle_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                total_quote TEXT NOT NULL,
                btc_long_quote TEXT NOT NULL,
                eth_short_quote TEXT NOT NULL,
                allocation_version TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                turnover_quote TEXT NOT NULL DEFAULT '0',
                position_hold_seconds INTEGER NOT NULL DEFAULT 0,
                round_interval_seconds INTEGER NOT NULL DEFAULT 0,
                sizing_mode TEXT NOT NULL DEFAULT 'legacy_fixed',
                strategy_id TEXT NOT NULL DEFAULT 'legacy',
                UNIQUE(instance_id, sequence),
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            )
            """
        )
        self._migrate_plan_columns()
        self._connection.commit()
        self._lock = RLock()

    def begin(self, instance_id: str, plan: PairCyclePlan) -> BeginCycleResult:
        now_ms = time.time_ns() // 1_000_000
        with self._lock, self._connection:
            existing = self._select(instance_id, plan.sequence)
            if existing is not None:
                return BeginCycleResult(existing, False)
            self._connection.execute(
                """
                INSERT INTO execution_cycles(
                    cycle_id, instance_id, sequence, total_quote, btc_long_quote,
                    eth_short_quote, allocation_version, status, reason, created_at_ms, updated_at_ms,
                    turnover_quote, position_hold_seconds, round_interval_seconds, sizing_mode, strategy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.cycle_id,
                    instance_id,
                    plan.sequence,
                    str(plan.total_quote),
                    str(plan.btc_long_quote),
                    str(plan.eth_short_quote),
                    plan.allocation_version,
                    CycleExecutionStatus.PLANNED.value,
                    "prepared_before_submit",
                    now_ms,
                    now_ms,
                    str(plan.turnover_quote),
                    plan.position_hold_seconds,
                    plan.round_interval_seconds,
                    plan.sizing_mode,
                    plan.strategy_id,
                ),
            )
        record = self.find(instance_id, plan.sequence)
        if record is None:
            raise ExecutionStateError("created execution cycle could not be reloaded")
        return BeginCycleResult(record, True)

    def finish(
        self,
        cycle_id: str,
        status: CycleExecutionStatus,
        reason: str,
    ) -> ExecutionRecord:
        if status is CycleExecutionStatus.PLANNED:
            raise ExecutionStateError("execution cycle cannot finish as planned")
        _require_reason_code(reason)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM execution_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            current = self._record(row)
            if current.status in _TERMINAL_STATUSES:
                if current.status is status and current.reason == reason:
                    return current
                raise ExecutionStateError("terminal execution cycle cannot change outcome")
            if current.status is CycleExecutionStatus.OPENED and status is CycleExecutionStatus.OPENED:
                if current.reason == reason:
                    return current
                raise ExecutionStateError("opened execution cycle cannot be opened again")
            self._connection.execute(
                """
                UPDATE execution_cycles
                SET status = ?, reason = ?, updated_at_ms = ?
                WHERE cycle_id = ?
                """,
                (status.value, reason, time.time_ns() // 1_000_000, cycle_id),
            )
        record = self.find(current.instance_id, current.plan.sequence)
        assert record is not None
        return record

    def find(self, instance_id: str, sequence: int) -> ExecutionRecord | None:
        with self._lock:
            return self._select(instance_id, sequence)

    def list_recent(self, instance_id: str, limit: int) -> list[ExecutionRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM execution_cycles
                WHERE instance_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (instance_id, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def recover_incomplete(self) -> int:
        with self._lock, self._connection:
            now_ms = time.time_ns() // 1_000_000
            planned = self._connection.execute(
                """
                UPDATE execution_cycles
                SET status = ?, reason = ?, updated_at_ms = ?
                WHERE status = ?
                """,
                (
                    CycleExecutionStatus.UNCERTAIN.value,
                    "process_restarted_before_terminal_result",
                    now_ms,
                    CycleExecutionStatus.PLANNED.value,
                ),
            ).rowcount
            opened = self._connection.execute(
                """
                UPDATE execution_cycles
                SET status = ?, reason = ?, updated_at_ms = ?
                WHERE status = ?
                """,
                (
                    CycleExecutionStatus.UNCERTAIN.value,
                    "process_restarted_with_open_pair",
                    now_ms,
                    CycleExecutionStatus.OPENED.value,
                ),
            ).rowcount
            return planned + opened

    def remove(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM execution_cycles WHERE instance_id = ?", (instance_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _select(self, instance_id: str, sequence: int) -> ExecutionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM execution_cycles WHERE instance_id = ? AND sequence = ?",
            (instance_id, sequence),
        ).fetchone()
        return self._record(row) if row is not None else None

    def _migrate_plan_columns(self) -> None:
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(execution_cycles)")}
        additions = {
            "turnover_quote": "TEXT NOT NULL DEFAULT '0'",
            "position_hold_seconds": "INTEGER NOT NULL DEFAULT 0",
            "round_interval_seconds": "INTEGER NOT NULL DEFAULT 0",
            "sizing_mode": "TEXT NOT NULL DEFAULT 'legacy_fixed'",
            "strategy_id": "TEXT NOT NULL DEFAULT 'legacy'",
        }
        for column, definition in additions.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE execution_cycles ADD COLUMN {column} {definition}")

    @staticmethod
    def _record(row: tuple[object, ...]) -> ExecutionRecord:
        plan = PairCyclePlan(
            cycle_id=str(row[0]),
            sequence=int(row[2]),
            total_quote=Decimal(str(row[3])),
            btc_long_quote=Decimal(str(row[4])),
            eth_short_quote=Decimal(str(row[5])),
            allocation_version=str(row[6]),
            turnover_quote=(
                Decimal(str(row[11])) if len(row) > 11 and Decimal(str(row[11])) > 0 else Decimal(str(row[3])) * 2
            ),
            position_hold_seconds=int(row[12]) if len(row) > 12 else 0,
            round_interval_seconds=int(row[13]) if len(row) > 13 else 0,
            sizing_mode=str(row[14]) if len(row) > 14 else "legacy_fixed",
            strategy_id=str(row[15]) if len(row) > 15 else "legacy",
        )
        return ExecutionRecord(
            instance_id=str(row[1]),
            plan=plan,
            status=CycleExecutionStatus(str(row[7])),
            reason=str(row[8]),
            created_at_ms=int(row[9]),
            updated_at_ms=int(row[10]),
        )
