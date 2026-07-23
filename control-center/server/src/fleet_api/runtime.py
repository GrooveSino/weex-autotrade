from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import RLock

from .execution import (
    AllocationUnavailable,
    CancelOrdersOutcome,
    CycleExecutionStatus,
    ExecutionStateError,
    PairedCycleCoordinator,
)
from .models import (
    AccountInstance,
    ExposureSnapshot,
    GlobalStopResult,
    InstanceAction,
    InstanceStatus,
    SchedulerMetrics,
    StrategyStage,
    StrategyTargetMode,
    VolumeSnapshot,
)
from .service import FleetControlService, InstanceNotFound, TelemetryUnavailable, UnsafeOperation
from .telemetry import (
    AccountTelemetryAdapter,
    AccountTelemetryAdapterFactory,
    AccountTelemetryContext,
)
from .volume_history import NormalizedTradeFill, TradeVolumeLedger, shanghai_day_start_ms


@dataclass(frozen=True, slots=True)
class _PollResult:
    processed: bool
    successful: bool


@dataclass(frozen=True, slots=True)
class _GlobalStopAccountResult:
    stopped: bool
    cancel_verified: bool
    cancel_failed: bool


class AccountRuntimeManager:
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

    async def refresh_instance(self, instance_id: str) -> AccountInstance:
        result = await self._poll_one(instance_id, propagate=True, allow_execution=False)
        if result.successful:
            self._service.record_refresh_success(instance_id)
        return self._service.get_instance(instance_id)

    async def authoritative_session_fills(
        self,
        instance_id: str,
        started_at_ms: int,
        end_ms: int,
    ) -> tuple[tuple[NormalizedTradeFill, ...], bool, str]:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            instance = self._service.get_instance(instance_id)
            adapter = self._adapters.get(instance_id)
            if adapter is None:
                adapter = self._adapter_factory.create(instance_id)
                self._adapters[instance_id] = adapter
            context = AccountTelemetryContext(
                instance=instance,
                credentials=self._service.vault.get(instance_id),
            )
            reader = getattr(adapter, "authoritative_fills", None)
            if reader is None:
                fills = self._volume_ledger.fills_for_account(instance_id, instance.mode.value, started_at_ms)
                return fills, True, "local_authoritative_adapter"
            return await asyncio.wait_for(
                reader(context, start_ms=started_at_ms, end_ms=end_ms),
                timeout=self._poll_timeout_seconds,
            )

    async def apply_action(self, instance_id: str, action: InstanceAction) -> AccountInstance:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            if action is InstanceAction.START:
                return self._service.apply_action(instance_id, action)
            before = self._service.get_instance(instance_id)
            if (
                action is InstanceAction.STOP
                and before.status is InstanceStatus.STOPPED
                and before.runtime.last_stop_verified_at_ms is not None
            ):
                # This exact inactive state was already cancellation-verified.
                # Avoid duplicate network work and duplicate audit records from
                # repeated clicks or overlapping browser requests.
                return before
            updated = self._service.apply_action(instance_id, action)
            outcome = await self._cancel_active_orders(updated)
            if not outcome.verified:
                self._service.record_order_cancel_failure(
                    instance_id,
                    origin=f"manual_{action.value}",
                    reason=outcome.reason,
                )
                raise UnsafeOperation(f"active order cancellation could not be verified ({outcome.reason})")
            self._service.record_order_cancel_verified(
                instance_id,
                canceled_count=outcome.canceled_count,
                reason=outcome.reason,
                marks_stop_verified=action is InstanceAction.STOP,
            )
            return self._service.get_instance(instance_id)

    async def close_positions(self, instance_id: str) -> AccountInstance:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            instance = self._service.get_instance(instance_id)
            if instance.status is InstanceStatus.RUNNING:
                raise UnsafeOperation("stop or pause the strategy before closing positions")
            if instance.exposure.btc_long <= 0 and instance.exposure.eth_short <= 0:
                raise UnsafeOperation("the instance has no open positions")
            coordinator = self._execution_coordinator
            if coordinator is None:
                raise UnsafeOperation("position close execution is unavailable in WEEX read-only mode")

            cancel = await self._cancel_active_orders(instance)
            if not cancel.verified:
                self._service.record_order_cancel_failure(
                    instance_id,
                    origin="manual_position_close",
                    reason=cancel.reason,
                )
                raise UnsafeOperation(f"active order cancellation could not be verified ({cancel.reason})")
            self._service.record_order_cancel_verified(
                instance_id,
                canceled_count=cancel.canceled_count,
                reason=cancel.reason,
            )
            current = self._service.get_instance(instance_id)
            context = AccountTelemetryContext(
                instance=current,
                credentials=self._service.vault.get(instance_id),
            )
            try:
                result = await asyncio.wait_for(
                    coordinator.close_positions(context),
                    timeout=self._poll_timeout_seconds,
                )
            except TimeoutError:
                reason = "position_close_timeout"
                self._service.record_execution_failure(instance_id, "uncertain", reason)
                raise UnsafeOperation("position close outcome is uncertain (timeout)") from None
            except ExecutionStateError as exc:
                self._service.record_execution_failure(
                    instance_id,
                    "rejected",
                    "position_state_mismatch",
                )
                raise UnsafeOperation(str(exc)) from None
            except Exception as exc:
                reason = f"position_close_exception:{type(exc).__name__.lower()}"[:80]
                self._service.record_execution_failure(instance_id, "uncertain", reason)
                raise UnsafeOperation(f"position close outcome is uncertain ({reason})") from None

            if result.outcome.status is not CycleExecutionStatus.COMPLETED:
                self._service.record_execution_failure(
                    instance_id,
                    result.outcome.status.value,
                    result.outcome.reason,
                )
                raise UnsafeOperation(f"position close failed ({result.outcome.status.value}:{result.outcome.reason})")

            now_ms = time.time_ns() // 1_000_000
            aggregate = self._volume_ledger.aggregate(instance_id, shanghai_day_start_ms(now_ms))
            strategy_generated = None
            if current.strategy.target_mode is StrategyTargetMode.INCREMENTAL:
                session = self._volume_ledger.latest_session(instance_id, current.mode.value)
                if session is None:
                    strategy_generated = current.strategy_progress.generated_volume_quote + result.closed_quote
                elif _session_projection_verified(session):
                    strategy_generated = Decimal(str(session["verified_quote_volume"]))
            return self._service.record_positions_closed(
                instance_id,
                result,
                aggregate,
                strategy_generated_volume_quote=strategy_generated,
            )

    async def stop_all(self, confirmation: str) -> GlobalStopResult:
        self._service.validate_global_stop_confirmation(confirmation)
        instance_ids = [instance.id for instance in self._service.list_instances()]
        results = await asyncio.gather(*(self._stop_one(instance_id) for instance_id in instance_ids))
        return GlobalStopResult(
            stopped=sum(result.stopped for result in results),
            cancel_verified=sum(result.cancel_verified for result in results),
            cancel_failed=sum(result.cancel_failed for result in results),
        )

    async def reconcile_beta_availability(
        self,
        available: bool,
        reason_code: str | None = None,
    ) -> int:
        if self._execution_coordinator is None:
            return 0
        instance_ids = [instance.id for instance in self._service.list_instances()]
        if available:
            changed = await asyncio.gather(*(self._resume_beta_pause(instance_id) for instance_id in instance_ids))
        else:
            reason = reason_code or "beta_unavailable"
            changed = await asyncio.gather(*(self._pause_for_beta(instance_id, reason) for instance_id in instance_ids))
        return sum(changed)

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

    async def reset_instance(self, instance_id: str) -> None:
        await self._remove_adapter(instance_id)
        if self._execution_coordinator is not None:
            await self._execution_coordinator.reset_instance(instance_id)

    async def remove_instance(self, instance_id: str) -> None:
        await self._remove_adapter(instance_id)
        if self._execution_coordinator is not None:
            await self._execution_coordinator.remove_instance(instance_id)
        self._locks.pop(instance_id, None)

    async def close(self) -> None:
        adapters = tuple(self._adapters.values())
        self._adapters.clear()
        self._locks.clear()
        await asyncio.gather(*(adapter.aclose() for adapter in adapters), return_exceptions=True)
        if self._execution_coordinator is not None:
            await self._execution_coordinator.close()

    async def _stop_one(self, instance_id: str) -> _GlobalStopAccountResult:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            try:
                before = self._service.get_instance(instance_id)
            except InstanceNotFound:
                return _GlobalStopAccountResult(False, False, False)
            was_active = before.status is not InstanceStatus.STOPPED
            if before.status is InstanceStatus.STOPPED and before.runtime.last_stop_verified_at_ms is not None:
                return _GlobalStopAccountResult(False, False, False)
            updated = self._service.apply_action(instance_id, InstanceAction.STOP)
            outcome = await self._cancel_active_orders(updated)
            if not outcome.verified:
                self._service.record_order_cancel_failure(
                    instance_id,
                    origin="global_stop",
                    reason=outcome.reason,
                )
                return _GlobalStopAccountResult(False, False, True)
            self._service.record_order_cancel_verified(
                instance_id,
                canceled_count=outcome.canceled_count,
                reason=outcome.reason,
                marks_stop_verified=True,
            )
            self._service.record_global_stop(instance_id)
            return _GlobalStopAccountResult(was_active, True, False)

    async def _pause_for_beta(self, instance_id: str, reason_code: str) -> bool:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            return await self._pause_for_beta_locked(instance_id, reason_code)

    async def _pause_for_beta_locked(self, instance_id: str, reason_code: str) -> bool:
        try:
            instance = self._service.get_instance(instance_id)
        except InstanceNotFound:
            return False
        if instance.status is not InstanceStatus.RUNNING:
            return False
        paused = self._service.pause_for_beta(instance_id, reason_code)
        outcome = await self._cancel_active_orders(paused)
        if outcome.verified:
            self._service.record_order_cancel_verified(
                instance_id,
                canceled_count=outcome.canceled_count,
                reason=outcome.reason,
            )
        else:
            self._service.record_order_cancel_failure(
                instance_id,
                origin="beta_pause",
                reason=outcome.reason,
            )
        return True

    async def _resume_beta_pause(self, instance_id: str) -> bool:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            try:
                before = self._service.get_instance(instance_id)
            except InstanceNotFound:
                return False
            updated = self._service.resume_beta_pause(instance_id)
            return updated != before

    async def _cancel_active_orders(self, instance: AccountInstance) -> CancelOrdersOutcome:
        coordinator = self._execution_coordinator
        if coordinator is None:
            return CancelOrdersOutcome(True, 0, "execution_disabled")
        context = AccountTelemetryContext(
            instance=instance,
            credentials=self._service.vault.get(instance.id),
        )
        try:
            return await asyncio.wait_for(
                coordinator.cancel_active_orders(context),
                timeout=self._poll_timeout_seconds,
            )
        except TimeoutError:
            return CancelOrdersOutcome(False, 0, "cancel_timeout")
        except Exception as exc:
            failure_type = type(exc).__name__.lower()
            reason = f"cancel_exception:{failure_type}"[:80]
            return CancelOrdersOutcome(False, 0, reason)

    @staticmethod
    def _is_beta_system_pause(instance: AccountInstance) -> bool:
        reason = instance.strategy_progress.system_pause_reason
        return instance.status is InstanceStatus.PAUSED and reason is not None and reason.startswith("beta:")

    async def _poll_one(
        self,
        instance_id: str,
        *,
        propagate: bool = False,
        allow_execution: bool = True,
    ) -> _PollResult:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock, self._semaphore:
            poll_started_at_ms = time.time_ns() // 1_000_000
            poll_started = time.perf_counter()
            outcome: bool | None = None
            self._record_poll_started()
            try:
                instance = self._service.get_instance(instance_id)
                adapter = self._adapters.get(instance_id)
                if adapter is None:
                    adapter = self._adapter_factory.create(instance_id)
                    self._adapters[instance_id] = adapter
                credentials = self._service.vault.get(instance_id)
                context = AccountTelemetryContext(instance=instance, credentials=credentials)
                telemetry = await asyncio.wait_for(
                    adapter.collect(context),
                    timeout=self._poll_timeout_seconds,
                )
                execution_failure: tuple[str, str] | None = None
                execution_result = None
                manual_pair_closed = False
                skip_execution = False
                coordinator = self._execution_coordinator
                if (
                    allow_execution
                    and coordinator is not None
                    and instance.strategy_progress.stage is StrategyStage.HOLDING
                    and instance.status in {InstanceStatus.RUNNING, InstanceStatus.PAUSED}
                ):
                    expected_btc = instance.exposure.btc_long > 0
                    expected_eth = instance.exposure.eth_short > 0
                    actual_btc = telemetry.exposure.btc_long > 0
                    actual_eth = telemetry.exposure.eth_short > 0
                    if not expected_btc or not expected_eth:
                        paused = self._service.pause_for_position_mismatch(
                            instance_id,
                            "holding_projection_invalid",
                        )
                        cancel = await self._cancel_active_orders(paused)
                        if cancel.verified:
                            self._service.record_order_cancel_verified(
                                instance_id,
                                canceled_count=cancel.canceled_count,
                                reason=cancel.reason,
                            )
                        else:
                            self._service.record_order_cancel_failure(
                                instance_id,
                                origin="position_mismatch",
                                reason=cancel.reason,
                            )
                        skip_execution = True
                    elif not actual_btc and not actual_eth:
                        cancel = await self._cancel_active_orders(instance)
                        if not cancel.verified:
                            self._service.record_order_cancel_failure(
                                instance_id,
                                origin="manual_pair_close",
                                reason=cancel.reason,
                            )
                        else:
                            self._service.record_order_cancel_verified(
                                instance_id,
                                canceled_count=cancel.canceled_count,
                                reason=cancel.reason,
                            )
                            try:
                                execution_result = await coordinator.reconcile_manual_pair_closed(context)
                            except ExecutionStateError:
                                self._service.pause_for_position_mismatch(
                                    instance_id,
                                    "manual_close_cycle_unmatched",
                                )
                            else:
                                manual_pair_closed = True
                                telemetry = replace(
                                    telemetry,
                                    exposure=ExposureSnapshot(),
                                    cycle_completed=execution_result.record.plan.sequence,
                                    phase="检测到人工双腿平仓，正在核对成交历史",
                                )
                        skip_execution = True
                    elif actual_btc != actual_eth:
                        reason = "eth_leg_missing" if actual_btc else "btc_leg_missing"
                        paused = self._service.pause_for_position_mismatch(instance_id, reason)
                        cancel = await self._cancel_active_orders(paused)
                        if cancel.verified:
                            self._service.record_order_cancel_verified(
                                instance_id,
                                canceled_count=cancel.canceled_count,
                                reason=cancel.reason,
                            )
                        else:
                            self._service.record_order_cancel_failure(
                                instance_id,
                                origin="position_mismatch",
                                reason=cancel.reason,
                            )
                        skip_execution = True
                    instance = self._service.get_instance(instance_id)
                    if skip_execution and not manual_pair_closed:
                        telemetry = replace(telemetry, phase=instance.phase)

                if allow_execution and coordinator is not None and not skip_execution:
                    if self._is_beta_system_pause(instance):
                        try:
                            await coordinator.check_allocation(context)
                        except AllocationUnavailable as exc:
                            telemetry = replace(
                                telemetry,
                                phase=f"Beta 服务仍不可用，系统保持暂停：{exc.reason_code}",
                            )
                        else:
                            instance = self._service.resume_beta_pause(instance_id)
                            context = AccountTelemetryContext(instance=instance, credentials=credentials)
                            telemetry = replace(telemetry, phase=instance.phase)
                    elif instance.status is InstanceStatus.RUNNING:
                        try:
                            execution_result = await coordinator.execute_next(context)
                        except AllocationUnavailable as exc:
                            await self._pause_for_beta_locked(instance_id, exc.reason_code)
                            instance = self._service.get_instance(instance_id)
                            telemetry = replace(
                                telemetry,
                                phase=f"Beta 服务异常，系统已暂停：{exc.reason_code}",
                            )
                        else:
                            if execution_result is None:
                                if instance.strategy_progress.stage is StrategyStage.COOLDOWN:
                                    telemetry = replace(telemetry, phase="轮次间隔等待中")
                            else:
                                record = execution_result.record
                                if record.status is CycleExecutionStatus.OPENED:
                                    telemetry = replace(
                                        telemetry,
                                        exposure=ExposureSnapshot(
                                            btc_long=float(record.plan.btc_long_quote),
                                            eth_short=float(record.plan.eth_short_quote),
                                        ),
                                        phase="BTC 多 / ETH 空已开仓，等待平仓",
                                    )
                                elif record.status is CycleExecutionStatus.COMPLETED:
                                    telemetry = replace(
                                        telemetry,
                                        exposure=ExposureSnapshot(),
                                        cycle_completed=record.plan.sequence,
                                        phase="Mock BTC 多 / ETH 空周期已平仓",
                                    )
                                else:
                                    execution_failure = (record.status.value, record.reason)
                aggregate = self._volume_ledger.aggregate(
                    instance_id,
                    shanghai_day_start_ms(time.time_ns() // 1_000_000),
                )
                ledger_is_authoritative = aggregate.fill_count > 0 or instance.volume.lifetime == 0
                if ledger_is_authoritative:
                    telemetry = replace(
                        telemetry,
                        volume=VolumeSnapshot(
                            lifetime=float(aggregate.lifetime),
                            today=float(aggregate.today),
                            complete=aggregate.complete,
                            session=self._volume_ledger.latest_session(instance_id, instance.mode.value),
                        ),
                    )
                strategy_generated = None
                if (
                    instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
                    and instance.strategy_progress.started_at_ms is not None
                ):
                    session = self._volume_ledger.latest_session(instance_id, instance.mode.value)
                    if session is None:
                        strategy_generated = self._volume_ledger.aggregate(
                            instance_id,
                            instance.strategy_progress.started_at_ms,
                        ).today
                    elif _session_projection_verified(session):
                        strategy_generated = Decimal(str(session["verified_quote_volume"]))
                poll_completed_at_ms = time.time_ns() // 1_000_000
                self._service.apply_telemetry(
                    instance_id,
                    telemetry,
                    poll_started_at_ms=poll_started_at_ms,
                    poll_completed_at_ms=poll_completed_at_ms,
                    poll_duration_ms=self._duration_ms(poll_started),
                    strategy_generated_volume_quote=strategy_generated,
                )
                if execution_result is not None and execution_result.record.status in {
                    CycleExecutionStatus.OPENED,
                    CycleExecutionStatus.COMPLETED,
                }:
                    self._service.record_strategy_execution(
                        instance_id,
                        execution_result.record,
                        submitted=execution_result.submitted,
                    )
                    if manual_pair_closed:
                        self._service.record_manual_pair_close(instance_id)
                if execution_failure is not None:
                    self._service.record_execution_failure(instance_id, *execution_failure)
                outcome = True
            except InstanceNotFound:
                if propagate:
                    raise
                return _PollResult(False, False)
            except Exception as exc:
                outcome = False
                failure_type = type(exc).__name__
                # CCXT clients can retain a poisoned connection after a
                # completed transport/parser failure. Recreate it for the next
                # poll instead of repeatedly reusing a known-bad client. Do
                # not close a thread-backed request that the outer timeout
                # cancelled but could not actually interrupt.
                if failure_type != "TimeoutError":
                    failed_adapter = self._adapters.pop(instance_id, None)
                    if failed_adapter is not None:
                        with suppress(Exception):
                            await failed_adapter.aclose()
                try:
                    self._service.record_runtime_failure(
                        instance_id,
                        failure_type,
                        poll_started_at_ms=poll_started_at_ms,
                        poll_failed_at_ms=time.time_ns() // 1_000_000,
                        poll_duration_ms=self._duration_ms(poll_started),
                    )
                except InstanceNotFound:
                    outcome = None
                    if propagate:
                        raise
                    return _PollResult(False, False)
                if propagate:
                    raise TelemetryUnavailable(f"telemetry unavailable ({failure_type})") from exc
            finally:
                self._record_poll_finished(outcome)
            return _PollResult(True, outcome is True)

    def _record_poll_started(self) -> None:
        with self._metrics_lock:
            self._active_polls += 1
            self._accounts_polled += 1
            self._max_observed_parallelism = max(self._max_observed_parallelism, self._active_polls)

    def _record_poll_finished(self, outcome: bool | None) -> None:
        with self._metrics_lock:
            self._active_polls -= 1
            if outcome is True:
                self._successful_polls += 1
            elif outcome is False:
                self._failed_polls += 1

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1_000))

    async def _remove_adapter(self, instance_id: str) -> None:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            adapter = self._adapters.pop(instance_id, None)
            if adapter is not None:
                await asyncio.gather(adapter.aclose(), return_exceptions=True)


def _session_projection_verified(session: dict[str, object]) -> bool:
    return (
        session.get("source_complete") is True
        and session.get("stale") is False
        and session.get("reconciliation_required") is False
        and session.get("pending_sync") is False
        and session.get("uncertain_order_state") is False
    )
