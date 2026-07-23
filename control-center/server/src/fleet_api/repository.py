from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Protocol

from .models import AccountInstance, LogLine, VolumeStrategy
from .ownership import LEGACY_OWNER_USER_ID, current_owner_user_id


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

    def clear_logs(self, instance_id: str) -> None: ...

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
            owner = current_owner_user_id()
            return [
                instance.model_copy(deep=True)
                for instance in self._instances.values()
                if owner is None or instance.owner_user_id == owner
            ]

    def get(self, instance_id: str) -> AccountInstance | None:
        with self._lock:
            instance = self._instances.get(instance_id)
            owner = current_owner_user_id()
            if instance is None or (owner is not None and instance.owner_user_id != owner):
                return None
            return instance.model_copy(deep=True)

    def create(self, instance: AccountInstance) -> AccountInstance:
        with self._lock:
            owner = current_owner_user_id()
            if owner is not None and instance.owner_user_id != owner:
                raise PermissionError("cannot create an instance for another user")
            if instance.id in self._instances:
                raise DuplicateInstanceError(instance.id)
            self._instances[instance.id] = instance.model_copy(deep=True)
            self._logs[instance.id] = []
            self._log_reads[instance.id] = 0
            return instance.model_copy(deep=True)

    def replace(self, instance: AccountInstance) -> AccountInstance:
        with self._lock:
            current = self._instances.get(instance.id)
            owner = current_owner_user_id()
            if current is None or (owner is not None and current.owner_user_id != owner):
                raise KeyError(instance.id)
            self._instances[instance.id] = instance.model_copy(deep=True)
            return instance.model_copy(deep=True)

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]:
        with self._lock:
            owner = current_owner_user_id()
            missing = [
                instance.id
                for instance in instances
                if instance.id not in self._instances
                or (owner is not None and self._instances[instance.id].owner_user_id != owner)
            ]
            if missing:
                raise KeyError(missing[0])
            for instance in instances:
                self._instances[instance.id] = instance.model_copy(deep=True)
            return [instance.model_copy(deep=True) for instance in instances]

    def delete(self, instance_id: str) -> None:
        with self._lock:
            instance = self._instances.get(instance_id)
            owner = current_owner_user_id()
            if instance is None or (owner is not None and instance.owner_user_id != owner):
                return
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

    def clear_logs(self, instance_id: str) -> None:
        with self._lock:
            if instance_id not in self._instances:
                raise KeyError(instance_id)
            self._logs[instance_id] = []

    def log_read_count(self, instance_id: str) -> int:
        with self._lock:
            return self._log_reads.get(instance_id, 0)

    def list_strategies(self) -> list[VolumeStrategy]:
        with self._lock:
            owner = current_owner_user_id()
            return [
                strategy.model_copy(deep=True)
                for strategy in self._strategies.values()
                if owner is None or strategy.owner_user_id == owner
            ]

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None:
        with self._lock:
            strategy = self._strategies.get(strategy_id)
            owner = current_owner_user_id()
            if strategy is None or (owner is not None and strategy.owner_user_id != owner):
                return None
            return strategy.model_copy(deep=True)

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy:
        with self._lock:
            owner = current_owner_user_id()
            if owner is not None and strategy.owner_user_id != owner:
                raise PermissionError("cannot create a strategy for another user")
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
            owner = current_owner_user_id()
            current = self._strategies.get(strategy.id)
            if current is None or (owner is not None and current.owner_user_id != owner):
                raise KeyError(strategy.id)
            missing = [
                instance.id
                for instance in instances
                if instance.id not in self._instances
                or (owner is not None and self._instances[instance.id].owner_user_id != owner)
            ]
            if missing:
                raise KeyError(missing[0])
            self._strategies[strategy.id] = strategy.model_copy(deep=True)
            for instance in instances:
                self._instances[instance.id] = instance.model_copy(deep=True)
            return strategy.model_copy(deep=True)

    def delete_strategy(self, strategy_id: str) -> None:
        with self._lock:
            strategy = self._strategies.get(strategy_id)
            owner = current_owner_user_id()
            if strategy is not None and (owner is None or strategy.owner_user_id == owner):
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
                owner_user_id TEXT NOT NULL DEFAULT 'gg',
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
                owner_user_id TEXT NOT NULL DEFAULT 'gg',
                payload TEXT NOT NULL
            );
            """
        )
        self._ensure_owner_columns()
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_instances_owner ON instances(owner_user_id)")
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_strategies_owner ON strategies(owner_user_id)")
        self._connection.commit()
        self._lock = RLock()
        self._migrate_embedded_strategies()

    @staticmethod
    def _instance(payload: str) -> AccountInstance:
        return AccountInstance.model_validate(_upgrade_instance_payload(json.loads(payload)))

    @staticmethod
    def _line(payload: str) -> LogLine:
        return LogLine.model_validate_json(payload)

    def _ensure_owner_columns(self) -> None:
        for table in ("instances", "strategies"):
            columns = {str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if "owner_user_id" not in columns:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT '{LEGACY_OWNER_USER_ID}'"
                )
            # Existing data predates local users and is deliberately assigned
            # to gg as a one-time ownership migration.
            self._connection.execute(
                f"UPDATE {table} SET owner_user_id = ? WHERE owner_user_id IS NULL OR owner_user_id = ''",
                (LEGACY_OWNER_USER_ID,),
            )

    @staticmethod
    def _owner() -> str | None:
        return current_owner_user_id()

    @staticmethod
    def _owner_sql(column: str = "owner_user_id") -> tuple[str, tuple[object, ...]]:
        owner = current_owner_user_id()
        return ("", ()) if owner is None else (f" AND {column} = ?", (owner,))

    def list(self) -> list[AccountInstance]:
        owner_clause, owner_params = self._owner_sql()
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM instances WHERE 1 = 1{owner_clause} ORDER BY rowid",
                owner_params,
            ).fetchall()
        return [self._instance(row[0]) for row in rows]

    def get(self, instance_id: str) -> AccountInstance | None:
        owner_clause, owner_params = self._owner_sql()
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload FROM instances WHERE id = ?{owner_clause}",
                (instance_id, *owner_params),
            ).fetchone()
        return self._instance(row[0]) if row else None

    def create(self, instance: AccountInstance) -> AccountInstance:
        owner = self._owner()
        if owner is not None and instance.owner_user_id != owner:
            raise PermissionError("cannot create an instance for another user")
        payload = instance.model_dump_json(by_alias=True)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO instances(id, owner_user_id, payload) VALUES (?, ?, ?)",
                        (instance.id, instance.owner_user_id, payload),
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
        owner_clause, owner_params = self._owner_sql()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE instances SET payload = ?, owner_user_id = ? WHERE id = ?{owner_clause}",
                (payload, instance.owner_user_id, instance.id, *owner_params),
            )
            if cursor.rowcount != 1:
                raise KeyError(instance.id)
        return instance.model_copy(deep=True)

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]:
        owner_clause, owner_params = self._owner_sql()
        with self._lock, self._connection:
            for instance in instances:
                cursor = self._connection.execute(
                    f"UPDATE instances SET payload = ?, owner_user_id = ? WHERE id = ?{owner_clause}",
                    (instance.model_dump_json(by_alias=True), instance.owner_user_id, instance.id, *owner_params),
                )
                if cursor.rowcount != 1:
                    raise KeyError(instance.id)
        return [instance.model_copy(deep=True) for instance in instances]

    def delete(self, instance_id: str) -> None:
        owner_clause, owner_params = self._owner_sql()
        with self._lock, self._connection:
            self._connection.execute(f"DELETE FROM instances WHERE id = ?{owner_clause}", (instance_id, *owner_params))

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

    def clear_logs(self, instance_id: str) -> None:
        with self._lock, self._connection:
            if self.get(instance_id) is None:
                raise KeyError(instance_id)
            self._connection.execute(
                "DELETE FROM instance_logs WHERE instance_id = ?",
                (instance_id,),
            )

    def log_read_count(self, instance_id: str) -> int:
        if self.get(instance_id) is None:
            return 0
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
        owner_clause, owner_params = self._owner_sql()
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM strategies WHERE 1 = 1{owner_clause} ORDER BY rowid",
                owner_params,
            ).fetchall()
        return [self._strategy(row[0]) for row in rows]

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None:
        owner_clause, owner_params = self._owner_sql()
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload FROM strategies WHERE id = ?{owner_clause}",
                (strategy_id, *owner_params),
            ).fetchone()
        return self._strategy(row[0]) if row else None

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy:
        owner = self._owner()
        if owner is not None and strategy.owner_user_id != owner:
            raise PermissionError("cannot create a strategy for another user")
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO strategies(id, owner_user_id, payload) VALUES (?, ?, ?)",
                        (strategy.id, strategy.owner_user_id, strategy.model_dump_json(by_alias=True)),
                    )
            except sqlite3.IntegrityError as exc:
                raise DuplicateStrategyError(strategy.id) from exc
        return strategy.model_copy(deep=True)

    def replace_strategy_and_instances(
        self,
        strategy: VolumeStrategy,
        instances: list[AccountInstance],
    ) -> VolumeStrategy:
        owner_clause, owner_params = self._owner_sql()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE strategies SET payload = ?, owner_user_id = ? WHERE id = ?{owner_clause}",
                (strategy.model_dump_json(by_alias=True), strategy.owner_user_id, strategy.id, *owner_params),
            )
            if cursor.rowcount != 1:
                raise KeyError(strategy.id)
            for instance in instances:
                cursor = self._connection.execute(
                    f"UPDATE instances SET payload = ?, owner_user_id = ? WHERE id = ?{owner_clause}",
                    (instance.model_dump_json(by_alias=True), instance.owner_user_id, instance.id, *owner_params),
                )
                if cursor.rowcount != 1:
                    raise KeyError(instance.id)
        return strategy.model_copy(deep=True)

    def delete_strategy(self, strategy_id: str) -> None:
        owner_clause, owner_params = self._owner_sql()
        with self._lock, self._connection:
            self._connection.execute(f"DELETE FROM strategies WHERE id = ?{owner_clause}", (strategy_id, *owner_params))

    def _migrate_embedded_strategies(self) -> None:
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT id, owner_user_id, payload FROM instances ORDER BY rowid"
            ).fetchall()
            for instance_id, owner_user_id, payload in rows:
                instance = self._instance(payload)
                instance = instance.model_copy(
                    update={"owner_user_id": owner_user_id or LEGACY_OWNER_USER_ID},
                    deep=True,
                )
                strategy = instance.strategy.model_copy(update={"owner_user_id": instance.owner_user_id}, deep=True)
                instance = instance.model_copy(update={"strategy": strategy}, deep=True)
                self._connection.execute(
                    "INSERT OR IGNORE INTO strategies(id, owner_user_id, payload) VALUES (?, ?, ?)",
                    (instance.strategy.id, instance.owner_user_id, instance.strategy.model_dump_json(by_alias=True)),
                )
                normalized = instance.model_dump_json(by_alias=True)
                if normalized != payload:
                    self._connection.execute(
                        "UPDATE instances SET owner_user_id = ?, payload = ? WHERE id = ?",
                        (instance.owner_user_id, normalized, instance_id),
                    )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _upgrade_instance_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    volume = payload.get("volume")
    if isinstance(volume, dict):
        for key in ("session", "activeSession", "active_session", "lastRun", "last_run"):
            projection = volume.get(key)
            if not isinstance(projection, dict):
                continue
            strategy_target = projection.get(
                "strategyTargetQuoteVolume",
                projection.get("strategy_target_quote_volume"),
            )
            if strategy_target is not None:
                continue
            target = projection.get("targetQuoteVolume", projection.get("target_quote_volume"))
            if target is not None:
                projection["strategyTargetQuoteVolume"] = target
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
