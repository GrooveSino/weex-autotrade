from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from .execution import CancelOrdersOutcome, CycleExecutionStatus, ExecutionStateError
from .models import AccountInstance, GlobalStopResult, InstanceAction, InstanceStatus, StrategyStage, StrategyTargetMode
from .runtime_shared import GlobalStopAccountResult, session_projection_verified
from .service import InstanceNotFound, TelemetryUnavailable, UnsafeOperation
from .telemetry import AccountTelemetryContext
from .volume_history import NormalizedTradeFill, shanghai_day_start_ms


class RuntimeControlMixin:
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
                elif session_projection_verified(session):
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

    async def _stop_one(self, instance_id: str) -> GlobalStopAccountResult:
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            try:
                before = self._service.get_instance(instance_id)
            except InstanceNotFound:
                return GlobalStopAccountResult(False, False, False)
            was_active = before.status is not InstanceStatus.STOPPED
            if before.status is InstanceStatus.STOPPED and before.runtime.last_stop_verified_at_ms is not None:
                return GlobalStopAccountResult(False, False, False)
            updated = self._service.apply_action(instance_id, InstanceAction.STOP)
            outcome = await self._cancel_active_orders(updated)
            if not outcome.verified:
                self._service.record_order_cancel_failure(
                    instance_id,
                    origin="global_stop",
                    reason=outcome.reason,
                )
                return GlobalStopAccountResult(False, False, True)
            self._service.record_order_cancel_verified(
                instance_id,
                canceled_count=outcome.canceled_count,
                reason=outcome.reason,
                marks_stop_verified=True,
            )
            self._service.record_global_stop(instance_id)
            return GlobalStopAccountResult(was_active, True, False)

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
