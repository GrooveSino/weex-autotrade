from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal

from .execution import AllocationUnavailable, CycleExecutionStatus, ExecutionStateError
from .models import ExposureSnapshot, InstanceStatus, StrategyStage, StrategyTargetMode, VolumeSnapshot
from .runtime_shared import PollResult, session_projection_verified
from .service import InstanceNotFound, TelemetryUnavailable
from .telemetry import AccountTelemetryContext
from .volume_history import shanghai_day_start_ms


class RuntimePollingMixin:
    async def _poll_one(
        self,
        instance_id: str,
        *,
        propagate: bool = False,
        allow_execution: bool = True,
    ) -> PollResult:
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
                    elif session_projection_verified(session):
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
                return PollResult(False, False)
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
                    return PollResult(False, False)
                if propagate:
                    raise TelemetryUnavailable(f"telemetry unavailable ({failure_type})") from exc
            finally:
                self._record_poll_finished(outcome)
            return PollResult(True, outcome is True)

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
