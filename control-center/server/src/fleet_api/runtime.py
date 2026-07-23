from __future__ import annotations

import asyncio
import time
from threading import RLock

from .execution import PairedCycleCoordinator
from .models import SchedulerMetrics
from .runtime_control import RuntimeControlMixin
from .runtime_polling import RuntimePollingMixin
from .telemetry import AccountTelemetryAdapter, AccountTelemetryAdapterFactory
from .volume_history import TradeVolumeLedger


class AccountRuntimeManager(RuntimeControlMixin, RuntimePollingMixin):
    """Concurrently polls isolated account adapters and coordinates scheduled Mock cycles."""

    def __init__(
        self,
        service: FleetControlService,
        adapter_factory: AccountTelemetryAdapterFactory,
        volume_ledger: TradeVolumeLedger,
        execution_coordinator: PairedCycleCoordinator | None = None,
        *,
        max_parallel_polls: int,
        poll_timeout_seconds: float,
    ) -> None:
        self._service = service
        self._adapter_factory = adapter_factory
        self._volume_ledger = volume_ledger
        self._execution_coordinator = execution_coordinator
        self._max_parallel_polls = max_parallel_polls
        self._semaphore = asyncio.Semaphore(max_parallel_polls)
        self._poll_timeout_seconds = poll_timeout_seconds
        self._adapters: dict[str, AccountTelemetryAdapter] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._metrics_lock = RLock()
        self._active_polls = 0
        self._max_observed_parallelism = 0
        self._poll_rounds = 0
        self._accounts_polled = 0
        self._successful_polls = 0
        self._failed_polls = 0
        self._last_round_account_count = 0
        self._last_round_succeeded = 0
        self._last_round_failed = 0
        self._last_round_started_at_ms: int | None = None
        self._last_round_completed_at_ms: int | None = None
        self._last_round_duration_ms: int | None = None

    async def poll_all(self) -> bool:
        instance_ids = [instance.id for instance in self._service.list_instances()]
        if not instance_ids:
            return False
        round_started_at_ms = time.time_ns() // 1_000_000
        round_started = time.perf_counter()
        results = await asyncio.gather(*(self._poll_one(instance_id) for instance_id in instance_ids))
        completed_at_ms = time.time_ns() // 1_000_000
        with self._metrics_lock:
            self._poll_rounds += 1
            self._last_round_account_count = len(instance_ids)
            self._last_round_succeeded = sum(result.successful for result in results)
            self._last_round_failed = sum(result.processed and not result.successful for result in results)
            self._last_round_started_at_ms = round_started_at_ms
            self._last_round_completed_at_ms = completed_at_ms
            self._last_round_duration_ms = self._duration_ms(round_started)
        return any(result.processed for result in results)

    def metrics(self) -> SchedulerMetrics:
        with self._metrics_lock:
            return SchedulerMetrics(
                max_parallel_polls=self._max_parallel_polls,
                active_polls=self._active_polls,
                max_observed_parallelism=self._max_observed_parallelism,
                poll_rounds=self._poll_rounds,
                accounts_polled=self._accounts_polled,
                successful_polls=self._successful_polls,
                failed_polls=self._failed_polls,
                last_round_account_count=self._last_round_account_count,
                last_round_succeeded=self._last_round_succeeded,
                last_round_failed=self._last_round_failed,
                last_round_started_at_ms=self._last_round_started_at_ms,
                last_round_completed_at_ms=self._last_round_completed_at_ms,
                last_round_duration_ms=self._last_round_duration_ms,
            )
