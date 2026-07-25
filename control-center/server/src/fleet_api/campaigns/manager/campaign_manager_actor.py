"""Actor-runtime ownership and short-lived exchange environments for Campaigns."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from hashlib import sha256
from typing import Any
from uuid import uuid4

from weex_cli.beta_volume import BetaVolumePlanStore

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.campaigns.actors.campaign_actor_models import (
    CampaignActorContext,
    OpenCycle,
    cycle_plan_from_ownership,
)
from fleet_api.campaigns.actors.campaign_actor_phases import CampaignActorPhases
from fleet_api.campaigns.actors.campaign_actor_program import CampaignActorProgram
from fleet_api.campaigns.actors.campaign_actor_resources import CampaignActorResourceMixin
from fleet_api.campaigns.actors.campaign_recovery_program import CampaignRecoveryProgram
from fleet_api.campaigns.actors.projection.condition_state import persist_condition_projection
from fleet_api.campaigns.core.campaign_contracts import CampaignRecord
from fleet_api.campaigns.core.campaign_events import _sanitize_event, submission_attempted
from fleet_api.campaigns.core.campaign_helpers import _campaign_result_metrics, _worker_exception_reason
from fleet_api.campaigns.manager.helpers.actor_values import accounting_checkpoint, ending_available_quote, now_ms
from fleet_api.execution.runtime.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.execution.runtime.execution_actor_state import ExecutionActorState
from fleet_api.models import BetaCampaignStatus
from fleet_api.monitoring.campaign_monitor_publish import append_monitor_event_direct, publish_monitor_event


class CampaignActorRuntimeMixin(CampaignActorResourceMixin):
    """Use one async actor per account while retaining the durable Campaign journal."""

    def _create_actor_runtime(self) -> None:
        self._actor_runtime = AsyncExecutionOrchestrator(
            self.capacity,
            normal_workers=self.settings.execution_io_normal_capacity,
            emergency_workers=self.settings.execution_io_emergency_capacity,
            state_sink=self._on_actor_state,
        )
        self._actor_futures: dict[str, Future[None]] = {}

    def _start_actor(
        self,
        record: CampaignRecord,
        material: CredentialMaterial,
        stop: threading.Event,
        *,
        resume_context: CampaignActorContext | None = None,
    ) -> Future[None]:
        phase_factory = CampaignActorPhases(
            lambda phase: self._actor_environment(record, material, stop, phase),
            is_stopping=stop.is_set,
            ownership_sink=lambda opened, state: self._persist_actor_ownership(record, opened, state),
        )
        proxy_key = self._proxy_key(material)
        program = CampaignActorProgram(
            record.campaign,
            phase_factory,
            proxy_key=proxy_key,
            shared_market=self.public_market_snapshot_service,
            on_result=lambda result: self._actor_completed(record, material, result),
            on_failure=lambda error: self._actor_failed(record, error),
            on_event=self._actor_event_sink(record),
            resume_context=resume_context,
        )
        future = self._actor_runtime.start(record.campaign_id, record.instance_id, program)
        with self._lock:
            self._actor_futures[record.campaign_id] = future
        future.add_done_callback(lambda _future: self._release_actor(record.campaign_id, record.instance_id))
        return future

    def _start_recovery_actor(
        self,
        record: CampaignRecord,
        material: CredentialMaterial,
        stop: threading.Event,
    ) -> Future[None]:
        opened = self._recovery_open_cycle(record)
        phases = CampaignActorPhases(
            lambda phase: self._actor_environment(record, material, stop, phase),
            is_stopping=lambda: True,
            ownership_sink=lambda cycle, state: self._persist_actor_ownership(record, cycle, state),
        )
        program = CampaignRecoveryProgram(
            phases,
            opened,
            on_result=lambda result: self._actor_completed(record, material, result),
            on_failure=lambda error: self._actor_failed(record, error),
        )
        future = self._actor_runtime.start(record.campaign_id, record.instance_id, program)
        with self._lock:
            self._actor_futures[record.campaign_id] = future
        future.add_done_callback(lambda _future: self._release_actor(record.campaign_id, record.instance_id))
        return future

    def _recovery_open_cycle(self, record: CampaignRecord) -> OpenCycle:
        ownership = record.metadata.get("execution_ownership")
        if not isinstance(ownership, Mapping) or ownership.get("state") not in {"opened", "uncertain", "closed"}:
            raise ValueError("execution ownership is unavailable")
        plan_id = str(ownership.get("plan_id") or "")
        child_store = BetaVolumePlanStore(self.settings.campaign_data_directory / record.instance_id / "plans")
        child = child_store.load_record(plan_id).plan
        legs = ownership.get("legs") if isinstance(ownership.get("legs"), Mapping) else {}
        summaries = []
        for symbol in ("BTC", "ETH"):
            leg = legs.get(symbol) if isinstance(legs, Mapping) else None
            if not isinstance(leg, Mapping):
                continue
            summaries.append(
                {
                    "symbol": symbol,
                    "action": "open",
                    "position_side": str(leg.get("position_side") or ""),
                    "executed_quantity": str(leg.get("owned_quantity") or "0"),
                    "accounting_verified": True,
                    "quote_volume": "0",
                }
            )
        context = CampaignActorContext(
            child=child,
            run_number=1,
            execution_started_at_ms=int(record.metadata.get("started_at_ms") or record.campaign.created_at_ms),
            round_number=int(ownership.get("round") or 1),
        )
        return OpenCycle(
            context=context,
            preflight={},
            btc_plan=child.btc,
            eth_plan=child.eth,
            sizing={"opening_notional_quote": "0"},
            selected_leverage=record.campaign.leverage,
            leverage_state={},
            open_summaries=summaries,
            lane_stops={},
            started_at_ms=int(ownership.get("updated_at_ms") or now_ms()),
            hold_seconds=0,
            execution_plan=cycle_plan_from_ownership(ownership, child),
        )

    def _persist_actor_ownership(self, record: CampaignRecord, opened: Any, state: str) -> None:
        """Persist the execution-owned quantity before a later phase may fail."""
        from weex_cli.beta_volume import _owned_position_quantity

        legs: dict[str, dict[str, str]] = {}
        summaries = opened.context.summaries or opened.open_summaries
        for symbol, plan in (("BTC", opened.btc_plan), ("ETH", opened.eth_plan)):
            legs[symbol] = {
                "position_side": plan.position_side,
                "planned_quantity": str(plan.quantity),
                "owned_quantity": str(_owned_position_quantity(summaries, symbol, plan.position_side)),
                "amount_step": str(plan.amount_step),
            }
        payload = {
            "plan_id": opened.context.child.plan_id,
            "round": opened.context.round_number,
            "attempt": opened.context.attempt_number,
            "state": state,
            "legs": legs,
            "cycle_plan": opened.plan.as_dict(),
            "opening_notional_quote": str(opened.sizing.get("opening_notional_quote") or "0"),
            "planned_turnover_quote": str(opened.sizing.get("planned_turnover_quote") or "0"),
            "completed_quote": str(opened.context.child_total_quote),
            "accounting_checkpoint": accounting_checkpoint(opened.context.summaries),
            "beta_version": str(opened.sizing.get("beta_version") or ""),
            "beta_as_of_ms": str(opened.sizing.get("beta_as_of_ms") or ""),
            "updated_at_ms": now_ms(),
        }
        if state != "planned":
            self.write_coordinator.critical(
                lambda: self.journal.update(record.campaign_id, execution_ownership=payload)
            )
            return
        event = _sanitize_event(
            {
                "event": "cycle_plan_created",
                "round": opened.context.round_number,
                "attempt": opened.context.attempt_number,
                "desired_quote": payload["planned_turnover_quote"],
                "target_quote": str(opened.context.child.target_turnover_quote),
                "remaining_quote": str(
                    max(opened.context.child.target_turnover_quote - opened.context.child_total_quote, 0)
                ),
                "opening_notional_quote": payload["opening_notional_quote"],
                "beta_version": payload["beta_version"],
            }
        )

        def persist() -> int:
            self.journal.update(record.campaign_id, execution_ownership=payload)
            return append_monitor_event_direct(self, record, event)

        event["sequence"] = self.write_coordinator.critical(persist)
        self._notify_progress(record.instance_id, event)

    def _actor_event_sink(self, record: CampaignRecord) -> Callable[[Mapping[str, Any]], None]:
        def sink(payload: Mapping[str, Any]) -> None:
            try:
                event = _sanitize_event(dict(payload))
                persist_condition_projection(self, record, event)
                publish_monitor_event(self, record, event)
            except Exception:
                # Execution telemetry can be replayed from the exchange and
                # ledger; it must not interrupt a live order state machine.
                return

        return sink

    def _on_actor_state(self, state: ExecutionActorState) -> None:
        try:
            record = self.journal.get(state.execution_id)
            if record is None:
                return
            phase_queue = state.phase_queue
            event = _sanitize_event(
                {
                    "event": "actor_lifecycle",
                    "phase": state.phase,
                    "reason": state.reason,
                    "deadline_at_ms": state.wait_deadline_at_ms,
                    "queue_phase": None if phase_queue is None else phase_queue.phase,
                    "queue_position": None if phase_queue is None else phase_queue.queue_position,
                    "estimated_start_at_ms": None if phase_queue is None else phase_queue.estimated_start_at_ms,
                    "queue_constraint": None if phase_queue is None else phase_queue.constraint,
                    "source": "actor",
                }
            )
            event["sequence"] = self._append_monitor_event(record, event)
            self.journal.update(state.execution_id, phase=state.phase)
            self._notify_progress(record.instance_id, event)
            self._notify(record.instance_id)
        except Exception:
            return

    def _actor_completed(
        self,
        record: CampaignRecord,
        material: CredentialMaterial,
        result: dict[str, Any],
    ) -> None:
        status = str(result.get("status") or BetaCampaignStatus.UNCERTAIN.value)
        latest = self.journal.get(record.campaign_id) or record
        if status == BetaCampaignStatus.UNCERTAIN.value:
            status = (
                BetaCampaignStatus.RECOVERING.value
                if submission_attempted(latest)
                else BetaCampaignStatus.STOPPED.value
            )
            result = {
                **result,
                "status": status,
                "reason": str(result.get("reason") or "execution_outcome_uncertain"),
            }
        ending_available = ending_available_quote(result)
        if ending_available is None:
            ending_available = self._read_ending_available(material)
        self.journal.update(
            record.campaign_id,
            status=status,
            result=result,
            finished_at_ms=now_ms(),
            phase="finished",
            generated_quote=result.get("executed_quote_volume", "0"),
            remaining_quote=result.get("remaining_quote", "0"),
            excess_quote=result.get("excess_quote", "0"),
            reason=result.get("reason"),
            ending_available_balance_quote=ending_available,
            **_campaign_result_metrics(result),
        )

    def _actor_failed(self, record: CampaignRecord, error: Exception) -> None:
        error_id = uuid4().hex[:12]
        logging.getLogger(__name__).error(
            "campaign actor failed error_id=%s campaign_id=%s",
            error_id,
            record.campaign_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        reason = _worker_exception_reason(error)
        latest = self.journal.get(record.campaign_id) or record
        attempted = submission_attempted(latest)
        status = BetaCampaignStatus.RECOVERING.value if attempted else BetaCampaignStatus.STOPPED.value
        stored_reason = reason if attempted else f"launch_aborted:{reason}"
        self.journal.update(
            record.campaign_id,
            status=status,
            finished_at_ms=now_ms(),
            reason=stored_reason,
            error_id=error_id,
            failure_phase="actor",
        )
        event = _sanitize_event(
            {
                "event": "campaign_recovering" if attempted else "launch_aborted",
                "error": type(error).__name__,
                "reason": stored_reason,
                "error_id": error_id,
            }
        )
        event["sequence"] = self._append_monitor_event(record, event)
        self._notify_progress(record.instance_id, event)

    def _release_actor(self, campaign_id: str, instance_id: str) -> None:
        with self._lock:
            self._actor_futures.pop(campaign_id, None)
            self._stops.pop(campaign_id, None)
            lease = self._leases.pop(campaign_id, None)
        if lease is not None:
            lease.release()
        self._notify(instance_id)

    def actor_state(self, campaign_id: str) -> ExecutionActorState | None:
        return self._actor_runtime.state(campaign_id)

    def actor_runtime_snapshot(self):
        return self._actor_runtime.snapshot()  # type: ignore[no-untyped-def]

    def connection_snapshots(self):  # type: ignore[no-untyped-def]
        return self.public_market_snapshot_service.snapshot(), self.private_order_stream_pool.snapshot()

    def collect_connections(self) -> None:
        self.private_order_stream_pool.collect()

    def _read_ending_available(self, material: CredentialMaterial) -> str | None:
        gateway = None
        try:
            _, gateway = self._profile_and_gateway(material)
            rows = gateway.account_balance_rows("live")
            for row in rows:
                if str(row.get("asset") or "").upper() == "USDT":
                    return str(row.get("availableBalance") or row.get("available") or "") or None
        except Exception:
            return None
        finally:
            if gateway is not None:
                gateway.close()
        return None

    def _proxy_key(self, material: CredentialMaterial) -> str:
        secret = material.proxy_url.get_secret_value() if material.proxy_url is not None else ""
        return "direct" if not secret else f"proxy-{sha256(secret.encode('utf-8')).hexdigest()[:16]}"
