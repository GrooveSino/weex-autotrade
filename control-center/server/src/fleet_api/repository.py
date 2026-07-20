from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Protocol

from .models import AccountInstance, LogLine, VolumeStrategy


class DuplicateInstanceError(ValueError):
    pass


class DuplicateStrategyError(ValueError):
    pass


class AccountRepository(Protocol):
    def list(self) -> list[AccountInstance]: ...

    def get(self, instance_id: str) -> AccountInstance | None: ...

    def create(self, instance: AccountInstance) -> AccountInstance: ...

    def replace(self, instance: AccountInstance) -> AccountInstance: ...

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]: ...

    def delete(self, instance_id: str) -> None: ...

    def append_log(self, instance_id: str, line: LogLine) -> None: ...

    def read_logs(self, instance_id: str, limit: int) -> list[LogLine]: ...

    def log_read_count(self, instance_id: str) -> int: ...

    def list_strategies(self) -> list[VolumeStrategy]: ...

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None: ...

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy: ...

    def replace_strategy_and_instances(
        self,
        strategy: VolumeStrategy,
        instances: list[AccountInstance],
    ) -> VolumeStrategy: ...

    def delete_strategy(self, strategy_id: str) -> None: ...

    def close(self) -> None: ...


class InMemoryAccountRepository:
    def __init__(self) -> None:
        self._instances: dict[str, AccountInstance] = {}
        self._logs: dict[str, list[LogLine]] = {}
        self._log_reads: dict[str, int] = {}
        self._strategies: dict[str, VolumeStrategy] = {}
        self._lock = RLock()

    def list(self) -> list[AccountInstance]:
        with self._lock:
            return [instance.model_copy(deep=True) for instance in self._instances.values()]

    def get(self, instance_id: str) -> AccountInstance | None:
        with self._lock:
            instance = self._instances.get(instance_id)
            return instance.model_copy(deep=True) if instance else None

    def create(self, instance: AccountInstance) -> AccountInstance:
        with self._lock:
            if instance.id in self._instances:
                raise DuplicateInstanceError(instance.id)
            self._instances[instance.id] = instance.model_copy(deep=True)
            self._logs[instance.id] = []
            self._log_reads[instance.id] = 0
            return instance.model_copy(deep=True)

    def replace(self, instance: AccountInstance) -> AccountInstance:
        with self._lock:
            if instance.id not in self._instances:
                raise KeyError(instance.id)
            self._instances[instance.id] = instance.model_copy(deep=True)
            return instance.model_copy(deep=True)

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]:
        with self._lock:
            missing = [instance.id for instance in instances if instance.id not in self._instances]
            if missing:
                raise KeyError(missing[0])
            for instance in instances:
                self._instances[instance.id] = instance.model_copy(deep=True)
            return [instance.model_copy(deep=True) for instance in instances]

    def delete(self, instance_id: str) -> None:
        with self._lock:
            self._instances.pop(instance_id, None)
            self._logs.pop(instance_id, None)
            self._log_reads.pop(instance_id, None)

    def append_log(self, instance_id: str, line: LogLine) -> None:
        with self._lock:
            if instance_id not in self._instances:
                raise KeyError(instance_id)
            self._logs[instance_id].append(line.model_copy(deep=True))
            self._logs[instance_id] = self._logs[instance_id][-500:]

    def read_logs(self, instance_id: str, limit: int) -> list[LogLine]:
        with self._lock:
            if instance_id not in self._instances:
                raise KeyError(instance_id)
            self._log_reads[instance_id] += 1
            return [line.model_copy(deep=True) for line in self._logs[instance_id][-limit:]]

    def log_read_count(self, instance_id: str) -> int:
        with self._lock:
            return self._log_reads.get(instance_id, 0)

    def list_strategies(self) -> list[VolumeStrategy]:
        with self._lock:
            return [strategy.model_copy(deep=True) for strategy in self._strategies.values()]

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None:
        with self._lock:
            strategy = self._strategies.get(strategy_id)
            return strategy.model_copy(deep=True) if strategy else None

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy:
        with self._lock:
            if strategy.id in self._strategies:
                raise DuplicateStrategyError(strategy.id)
            self._strategies[strategy.id] = strategy.model_copy(deep=True)
            return strategy.model_copy(deep=True)

    def replace_strategy_and_instances(
        self,
        strategy: VolumeStrategy,
        instances: list[AccountInstance],
    ) -> VolumeStrategy:
        with self._lock:
            if strategy.id not in self._strategies:
                raise KeyError(strategy.id)
            missing = [instance.id for instance in instances if instance.id not in self._instances]
            if missing:
                raise KeyError(missing[0])
            self._strategies[strategy.id] = strategy.model_copy(deep=True)
            for instance in instances:
                self._instances[instance.id] = instance.model_copy(deep=True)
            return strategy.model_copy(deep=True)

    def delete_strategy(self, strategy_id: str) -> None:
        with self._lock:
            self._strategies.pop(strategy_id, None)

    def close(self) -> None:
        return None


class SQLiteAccountRepository:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS instances (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS instance_logs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_instance_logs_instance_seq
                ON instance_logs(instance_id, seq DESC);
            CREATE TABLE IF NOT EXISTS instance_log_reads (
                instance_id TEXT PRIMARY KEY,
                reads INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            """
        )
        self._connection.commit()
        self._lock = RLock()
        self._migrate_embedded_strategies()

    @staticmethod
    def _instance(payload: str) -> AccountInstance:
        return AccountInstance.model_validate(_upgrade_instance_payload(json.loads(payload)))

    @staticmethod
    def _line(payload: str) -> LogLine:
        return LogLine.model_validate_json(payload)

    def list(self) -> list[AccountInstance]:
        with self._lock:
            rows = self._connection.execute("SELECT payload FROM instances ORDER BY rowid").fetchall()
        return [self._instance(row[0]) for row in rows]

    def get(self, instance_id: str) -> AccountInstance | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
        return self._instance(row[0]) if row else None

    def create(self, instance: AccountInstance) -> AccountInstance:
        payload = instance.model_dump_json(by_alias=True)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO instances(id, payload) VALUES (?, ?)",
                        (instance.id, payload),
                    )
                    self._connection.execute(
                        "INSERT INTO instance_log_reads(instance_id, reads) VALUES (?, 0)",
                        (instance.id,),
                    )
            except sqlite3.IntegrityError as exc:
                raise DuplicateInstanceError(instance.id) from exc
        return instance.model_copy(deep=True)

    def replace(self, instance: AccountInstance) -> AccountInstance:
        payload = instance.model_dump_json(by_alias=True)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE instances SET payload = ? WHERE id = ?",
                (payload, instance.id),
            )
            if cursor.rowcount != 1:
                raise KeyError(instance.id)
        return instance.model_copy(deep=True)

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]:
        with self._lock, self._connection:
            for instance in instances:
                cursor = self._connection.execute(
                    "UPDATE instances SET payload = ? WHERE id = ?",
                    (instance.model_dump_json(by_alias=True), instance.id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(instance.id)
        return [instance.model_copy(deep=True) for instance in instances]

    def delete(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM instances WHERE id = ?", (instance_id,))

    def append_log(self, instance_id: str, line: LogLine) -> None:
        with self._lock, self._connection:
            if self.get(instance_id) is None:
                raise KeyError(instance_id)
            self._connection.execute(
                "INSERT INTO instance_logs(instance_id, payload) VALUES (?, ?)",
                (instance_id, line.model_dump_json(by_alias=True)),
            )
            self._connection.execute(
                """
                DELETE FROM instance_logs
                WHERE instance_id = ?
                  AND seq NOT IN (
                    SELECT seq FROM instance_logs
                    WHERE instance_id = ?
                    ORDER BY seq DESC
                    LIMIT 500
                  )
                """,
                (instance_id, instance_id),
            )

    def read_logs(self, instance_id: str, limit: int) -> list[LogLine]:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE instance_log_reads SET reads = reads + 1 WHERE instance_id = ?",
                (instance_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(instance_id)
            rows = self._connection.execute(
                "SELECT payload FROM instance_logs WHERE instance_id = ? ORDER BY seq DESC LIMIT ?",
                (instance_id, limit),
            ).fetchall()
        return [self._line(row[0]) for row in reversed(rows)]

    def log_read_count(self, instance_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT reads FROM instance_log_reads WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _strategy(payload: str) -> VolumeStrategy:
        return VolumeStrategy.model_validate_json(payload)

    def list_strategies(self) -> list[VolumeStrategy]:
        with self._lock:
            rows = self._connection.execute("SELECT payload FROM strategies ORDER BY rowid").fetchall()
        return [self._strategy(row[0]) for row in rows]

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
        return self._strategy(row[0]) if row else None

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy:
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO strategies(id, payload) VALUES (?, ?)",
                        (strategy.id, strategy.model_dump_json(by_alias=True)),
                    )
            except sqlite3.IntegrityError as exc:
                raise DuplicateStrategyError(strategy.id) from exc
        return strategy.model_copy(deep=True)

    def replace_strategy_and_instances(
        self,
        strategy: VolumeStrategy,
        instances: list[AccountInstance],
    ) -> VolumeStrategy:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE strategies SET payload = ? WHERE id = ?",
                (strategy.model_dump_json(by_alias=True), strategy.id),
            )
            if cursor.rowcount != 1:
                raise KeyError(strategy.id)
            for instance in instances:
                cursor = self._connection.execute(
                    "UPDATE instances SET payload = ? WHERE id = ?",
                    (instance.model_dump_json(by_alias=True), instance.id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(instance.id)
        return strategy.model_copy(deep=True)

    def delete_strategy(self, strategy_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))

    def _migrate_embedded_strategies(self) -> None:
        with self._lock, self._connection:
            rows = self._connection.execute("SELECT id, payload FROM instances ORDER BY rowid").fetchall()
            for instance_id, payload in rows:
                instance = self._instance(payload)
                self._connection.execute(
                    "INSERT OR IGNORE INTO strategies(id, payload) VALUES (?, ?)",
                    (instance.strategy.id, instance.strategy.model_dump_json(by_alias=True)),
                )
                normalized = instance.model_dump_json(by_alias=True)
                if normalized != payload:
                    self._connection.execute(
                        "UPDATE instances SET payload = ? WHERE id = ?",
                        (normalized, instance_id),
                    )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _upgrade_instance_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    strategy = payload.get("strategy")
    if not isinstance(strategy, dict):
        return payload
    strategy_id = strategy.get("id")
    if strategy_id and not payload.get("strategyId"):
        payload["strategyId"] = strategy_id
    if "roundTurnoverQuoteMin" in strategy:
        return payload

    minimum = strategy.pop("btcOpenQuoteMin", None)
    maximum = strategy.pop("btcOpenQuoteMax", None)
    reference_beta = strategy.pop("referenceEthRatio", "0.25")
    if minimum is None or maximum is None:
        return payload
    beta = Decimal(str(reference_beta))
    multiplier = (Decimal(1) + beta) * Decimal(2)
    strategy["roundTurnoverQuoteMin"] = str(Decimal(str(minimum)) * multiplier)
    strategy["roundTurnoverQuoteMax"] = str(Decimal(str(maximum)) * multiplier)
    return payload
