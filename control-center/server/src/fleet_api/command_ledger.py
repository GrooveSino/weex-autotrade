from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    fingerprint: str
    status: str


class CommandReceiptLedger:
    """Stores only command fingerprints, never request bodies or exchange payloads."""

    def __init__(self, sqlite_path: Path | None = None) -> None:
        self._lock = RLock()
        self._memory: dict[str, CommandReceipt] | None = None
        self._connection: sqlite3.Connection | None = None
        if sqlite_path is None:
            self._memory = {}
            return
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(sqlite_path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fleet_command_receipts (
                command_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                completed_at_ms INTEGER
            )
            """
        )
        self._connection.commit()

    def claim(self, command_id: str, fingerprint: str) -> CommandReceipt | None:
        with self._lock:
            existing = self.get(command_id)
            if existing is not None:
                return existing
            receipt = CommandReceipt(command_id, fingerprint, "accepted")
            if self._memory is not None:
                self._memory[command_id] = receipt
                return None
            assert self._connection is not None
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO fleet_command_receipts("
                "command_id, fingerprint, status, created_at_ms) VALUES (?, ?, ?, ?)",
                (command_id, fingerprint, receipt.status, int(time.time() * 1000)),
            )
            self._connection.commit()
            if inserted.rowcount == 0:
                return self.get(command_id)
            return None

    def complete(self, command_id: str) -> None:
        with self._lock:
            existing = self.get(command_id)
            if existing is None:
                return
            receipt = CommandReceipt(command_id, existing.fingerprint, "completed")
            if self._memory is not None:
                self._memory[command_id] = receipt
                return
            assert self._connection is not None
            self._connection.execute(
                "UPDATE fleet_command_receipts SET status = ?, completed_at_ms = ? WHERE command_id = ?",
                (receipt.status, int(time.time() * 1000), command_id),
            )
            self._connection.commit()

    def get(self, command_id: str) -> CommandReceipt | None:
        if self._memory is not None:
            return self._memory.get(command_id)
        assert self._connection is not None
        row = self._connection.execute(
            "SELECT command_id, fingerprint, status FROM fleet_command_receipts WHERE command_id = ?", (command_id,)
        ).fetchone()
        return CommandReceipt(*row) if row is not None else None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
