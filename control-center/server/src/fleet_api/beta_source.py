from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Protocol

from .beta_allocation import HttpBetaAllocationProvider
from .models import BetaMarketSnapshot, BetaSourceSettings, BetaSourceSettingsUpdate


class BetaSourceStore(Protocol):
    def load(self, fallback: BetaSourceSettings) -> BetaSourceSettings: ...

    def save(self, settings: BetaSourceSettings) -> None: ...

    def close(self) -> None: ...


class InMemoryBetaSourceStore:
    def __init__(self) -> None:
        self._settings: BetaSourceSettings | None = None

    def load(self, fallback: BetaSourceSettings) -> BetaSourceSettings:
        return self._settings or fallback

    def save(self, settings: BetaSourceSettings) -> None:
        self._settings = settings

    def close(self) -> None:
        return None


class SQLiteBetaSourceStore:
    """Persist only the non-secret endpoint and refresh policy alongside the ledger."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_beta_source_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload_json TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )

    def load(self, fallback: BetaSourceSettings) -> BetaSourceSettings:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM fleet_beta_source_settings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            self.save(fallback)
            return fallback
        return BetaSourceSettings.model_validate_json(str(row["payload_json"]))

    def save(self, settings: BetaSourceSettings) -> None:
        payload = settings.model_dump_json(by_alias=True)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO fleet_beta_source_settings (singleton, payload_json, updated_at_ms)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (payload, settings.updated_at_ms),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class BetaSourceRuntime:
    """One serialised source provider shared by telemetry, planning, and the UI."""

    def __init__(
        self,
        store: BetaSourceStore,
        fallback: BetaSourceSettings,
        *,
        provider_factory: Callable[[BetaSourceSettings], HttpBetaAllocationProvider] | None = None,
    ) -> None:
        self._store = store
        self._settings = store.load(fallback)
        self._provider_factory = provider_factory or _provider_from_settings
        self._provider = self._provider_factory(self._settings)
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> BetaSourceSettings:
        return self._settings

    @property
    def last_refresh_error(self) -> str | None:
        value = getattr(self._provider, "last_refresh_error", None)
        return value if isinstance(value, str) else None

    async def get(self, context):
        async with self._lock:
            return await self._provider.get(context)

    async def market_snapshot(self) -> BetaMarketSnapshot:
        async with self._lock:
            return await self._provider.market_snapshot()

    async def refresh(self) -> bool:
        async with self._lock:
            return await self._provider.refresh()

    def seconds_until_refresh(self, maximum_seconds: float) -> float:
        return self._provider.seconds_until_refresh(maximum_seconds)

    async def update(self, payload: BetaSourceSettingsUpdate) -> BetaSourceSettings:
        settings = BetaSourceSettings(
            **payload.model_dump(),
            updated_at_ms=time.time_ns() // 1_000_000,
        )
        replacement = self._provider_factory(settings)
        async with self._lock:
            previous = self._provider
            self._store.save(settings)
            self._settings = settings
            self._provider = replacement
        await previous.aclose()
        return settings

    async def aclose(self) -> None:
        async with self._lock:
            provider = self._provider
        await provider.aclose()


def _provider_from_settings(settings: BetaSourceSettings) -> HttpBetaAllocationProvider:
    return HttpBetaAllocationProvider(
        settings.url,
        timeout_seconds=settings.timeout_seconds,
        cache_seconds=settings.refresh_interval_seconds,
        network_on_demand=not settings.background_refresh_enabled,
    )
