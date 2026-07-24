"""Executor-owned, low-pressure scheduling for authoritative trade-history reads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import AccountInstance, TradingMode
from .runtime import AccountRuntimeManager
from .service import FleetControlService
from .volume_contracts import TradeHistorySyncResult, TradeVolumeLedger

INITIAL_BASELINE = "initial_baseline"
MANUAL_AUDIT = "manual_audit"
ACTIVE_EVENT = "active_event"
ACTIVE_FALLBACK = "active_fallback"
FINAL_SESSION = "final_session"


@dataclass(frozen=True, slots=True)
class SyncScheduleMetrics:
    queued: int
    running: int
    successful_steps: int
    failed_steps: int
    last_success_at_ms: int | None


@dataclass(frozen=True, slots=True)
class _Request:
    instance_id: str
    reason: str
    priority: int
    due_at_ms: int


class TradeHistorySyncScheduler:
    """Schedule one durable source page per turn; never submit exchange orders."""

    def __init__(
        self,
        service: FleetControlService,
        runtime: AccountRuntimeManager,
        ledger: TradeVolumeLedger,
        *,
        is_active: Callable[[AccountInstance], bool],
        active_fallback_seconds: float = 15,
        max_concurrent_requests: int = 1,
    ) -> None:
        if active_fallback_seconds <= 0:
            raise ValueError("active history fallback interval must be positive")
        if max_concurrent_requests < 1:
            raise ValueError("history request concurrency must be positive")
        self._service = service
        self._runtime = runtime
        self._ledger = ledger
        self._is_active = is_active
        self._fallback_ms = int(active_fallback_seconds * 1000)
        self._requests: dict[str, _Request] = {}
        self._global_limit = asyncio.Semaphore(max_concurrent_requests)
        self._proxy_locks: dict[str, asyncio.Lock] = {}
        self._running = 0
        self._successful_steps = 0
        self._failed_steps = 0
        self._last_success_at_ms: int | None = None

    def queue_initial_baseline(self, instance: AccountInstance) -> None:
        if instance.mode is not TradingMode.LIVE:
            return
        checkpoint = self._ledger.sync_checkpoint(instance.id, instance.mode.value) or {}
        if checkpoint.get("initial_baseline_state") in {"queued", "running", "complete", "pending"}:
            return
        self._ledger.save_sync_checkpoint(
            instance.id,
            instance.mode.value,
            cursor=None,
            high_watermark_ms=None,
            pending=True,
            source_complete=False,
            coverage_complete=False,
            stale=False,
            scan_state=None,
            sync_reason=INITIAL_BASELINE,
            next_sync_at_ms=0,
            initial_baseline_state="queued",
        )
        self.request(instance.id, INITIAL_BASELINE)

    def request(self, instance_id: str, reason: str, *, delay_ms: int = 0) -> None:
        priority = _priority(reason)
        due_at_ms = _now_ms() + max(0, delay_ms)
        current = self._requests.get(instance_id)
        if current is None or (priority, -due_at_ms) > (current.priority, -current.due_at_ms):
            self._requests[instance_id] = _Request(instance_id, reason, priority, due_at_ms)
            instance = self._lookup_instance(instance_id)
            if instance is not None and instance.mode is TradingMode.LIVE:
                self._ledger.save_sync_checkpoint(
                    instance.id,
                    instance.mode.value,
                    pending=True,
                    sync_reason=reason,
                    next_sync_at_ms=due_at_ms,
                )

    def bootstrap(self) -> None:
        for instance in self._service.list_instances():
            checkpoint = self._ledger.sync_checkpoint(instance.id, instance.mode.value) or {}
            baseline = checkpoint.get("initial_baseline_state")
            if instance.mode is TradingMode.LIVE and baseline in {"queued", "running"}:
                self.request(instance.id, INITIAL_BASELINE)
            elif self._is_active(instance):
                self.request(instance.id, ACTIVE_FALLBACK)

    def is_active(self, instance: AccountInstance) -> bool:
        return self._is_active(instance)

    async def run_due(self) -> bool:
        self._schedule_active_fallbacks()
        request = self._next_due()
        if request is None:
            return False
        instance = self._lookup_instance(request.instance_id)
        if instance is None:
            return False
        if not self._may_run(instance, request.reason):
            self._mark_ineligible(instance)
            return False
        await self._run_request(instance, request)
        return True

    async def refresh_now(self, instance_id: str) -> TradeHistorySyncResult | None:
        instance = self._service.get_instance(instance_id)
        self.request(instance.id, MANUAL_AUDIT)
        request = self._requests.pop(instance.id)
        return await self._run_request(instance, request)

    def metrics(self) -> SyncScheduleMetrics:
        return SyncScheduleMetrics(
            queued=len(self._requests),
            running=self._running,
            successful_steps=self._successful_steps,
            failed_steps=self._failed_steps,
            last_success_at_ms=self._last_success_at_ms,
        )

    def _schedule_active_fallbacks(self) -> None:
        now_ms = _now_ms()
        for instance in self._service.list_instances():
            if not self._is_active(instance):
                continue
            checkpoint = self._ledger.sync_checkpoint(instance.id, instance.mode.value) or {}
            last = checkpoint.get("updated_at_ms")
            if not isinstance(last, int) or now_ms - last >= self._fallback_ms:
                self.request(instance.id, ACTIVE_FALLBACK)

    def _next_due(self) -> _Request | None:
        now_ms = _now_ms()
        due = [item for item in self._requests.values() if item.due_at_ms <= now_ms]
        if not due:
            return None
        selected = max(due, key=lambda item: (item.priority, -item.due_at_ms, item.instance_id))
        return self._requests.pop(selected.instance_id, None)

    def _lookup_instance(self, instance_id: str) -> AccountInstance | None:
        try:
            return self._service.get_instance(instance_id)
        except Exception:  # Deleted accounts must not retain scheduler work.
            return None

    def _may_run(self, instance: AccountInstance, reason: str) -> bool:
        if instance.mode is not TradingMode.LIVE:
            return False
        if reason in {INITIAL_BASELINE, MANUAL_AUDIT, FINAL_SESSION}:
            return True
        return self._is_active(instance)

    async def _run_request(self, instance: AccountInstance, request: _Request) -> TradeHistorySyncResult | None:
        proxy_lock = self._proxy_locks.setdefault(_proxy_key(instance), asyncio.Lock())
        self._running += 1
        try:
            async with self._global_limit, proxy_lock:
                self._mark_running(instance, request.reason)
                result = await self._runtime.sync_history_step(instance.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed_steps += 1
            self._mark_failure(instance, request.reason)
            return None
        finally:
            self._running -= 1
        if result is None:
            return None
        self._successful_steps += 1
        self._last_success_at_ms = _now_ms()
        self._after_step(instance, request.reason, result)
        return result

    def _mark_running(self, instance: AccountInstance, reason: str) -> None:
        checkpoint = self._ledger.sync_checkpoint(instance.id, instance.mode.value) or {}
        baseline = checkpoint.get("initial_baseline_state")
        self._ledger.save_sync_checkpoint(
            instance.id,
            instance.mode.value,
            sync_reason=reason,
            next_sync_at_ms=None,
            initial_baseline_state="running" if reason == INITIAL_BASELINE else baseline,
        )

    def _mark_failure(self, instance: AccountInstance, reason: str) -> None:
        baseline = "pending" if reason == INITIAL_BASELINE else None
        values: dict[str, object] = {
            "pending": reason != INITIAL_BASELINE and self._is_active(instance),
            "source_complete": False,
            "stale": True,
            "sync_reason": reason,
            "next_sync_at_ms": _now_ms() + self._fallback_ms if self._is_active(instance) else None,
        }
        if baseline is not None:
            values["initial_baseline_state"] = baseline
        self._ledger.save_sync_checkpoint(instance.id, instance.mode.value, **values)

    def _mark_ineligible(self, instance: AccountInstance) -> None:
        """Clear an active-only request after the account reached a quiet state."""
        self._ledger.save_sync_checkpoint(
            instance.id,
            instance.mode.value,
            pending=False,
            sync_reason=None,
            next_sync_at_ms=None,
        )

    def _after_step(self, instance: AccountInstance, reason: str, result: TradeHistorySyncResult) -> None:
        checkpoint = self._ledger.sync_checkpoint(instance.id, instance.mode.value) or {}
        more_pages = result.next_cursor is not None and result.stop_reason == "page_step"
        terminal = result.stop_reason in {"history_exhausted", "source_incomplete", "cursor_loop"}
        if more_pages:
            self.request(instance.id, reason)
        baseline = checkpoint.get("initial_baseline_state")
        values: dict[str, object] = {
            "pending": more_pages,
            "sync_reason": reason,
            "next_sync_at_ms": _now_ms() if more_pages else None,
            "last_success_at_ms": self._last_success_at_ms,
        }
        if reason == INITIAL_BASELINE and terminal:
            values["initial_baseline_state"] = "complete" if result.stop_reason == "history_exhausted" else "pending"
            values["pending"] = False
        elif baseline == "running" and terminal:
            values["initial_baseline_state"] = "complete" if result.stop_reason == "history_exhausted" else "pending"
        self._ledger.save_sync_checkpoint(instance.id, instance.mode.value, **values)


def _priority(reason: str) -> int:
    return {
        # A terminal session sync is the final authoritative accounting pass.
        # It must replace any stale active-event request queued immediately
        # before the worker reached its terminal state.
        FINAL_SESSION: 35,
        ACTIVE_EVENT: 30,
        MANUAL_AUDIT: 20,
        ACTIVE_FALLBACK: 10,
        INITIAL_BASELINE: 1,
    }.get(reason, 0)


def _proxy_key(instance: AccountInstance) -> str:
    return f"{instance.proxy.type.value}:{instance.proxy.host}"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
