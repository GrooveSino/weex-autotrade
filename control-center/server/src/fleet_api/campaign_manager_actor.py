"""Actor-runtime ownership and short-lived exchange environments for Campaigns."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import ExitStack
from decimal import Decimal
from hashlib import sha256
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaignStore, LiveBetaVolumeCampaignService, live_profile_fingerprint
from weex_cli.beta_volume import BetaVolumePlanStore, LiveBetaVolumeService
from weex_cli.live_websocket import WeexPrivateOrderStream, WeexPublicOrderBookStream

from .async_execution_orchestrator import AsyncExecutionOrchestrator
from .campaign_actor_models import CampaignPhaseEnvironment
from .campaign_actor_phases import CampaignActorPhases
from .campaign_actor_program import CampaignActorProgram
from .campaign_contracts import CampaignRecord
from .campaign_events import _sanitize_event, submission_attempted
from .campaign_helpers import _campaign_result_metrics, _worker_exception_reason
from .campaign_monitor_publish import publish_monitor_event
from .execution_actor_state import ExecutionActorState
from .execution_io import BoundedGateway
from .models import BetaCampaignStatus
from .vault import CredentialMaterial


class CampaignActorRuntimeMixin:
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
    ) -> Future[None]:
        phase_factory = CampaignActorPhases(
            lambda phase: self._actor_environment(record, material, stop, phase),
            is_stopping=stop.is_set,
        )
        proxy_key = self._proxy_key(material)
        program = CampaignActorProgram(
            record.campaign,
            phase_factory,
            proxy_key=proxy_key,
            on_result=lambda result: self._actor_completed(record, material, result),
            on_failure=lambda error: self._actor_failed(record, error),
        )
        future = self._actor_runtime.start(record.campaign_id, record.instance_id, program)
        with self._lock:
            self._actor_futures[record.campaign_id] = future
        future.add_done_callback(lambda _future: self._release_actor(record.campaign_id, record.instance_id))
        return future

    def _actor_environment(
        self,
        record: CampaignRecord,
        material: CredentialMaterial,
        stop: threading.Event,
        phase: str,
    ) -> CampaignPhaseEnvironment:
        profile, raw_gateway = self._profile_and_gateway(material)
        gateway = BoundedGateway(raw_gateway, self.io_budget, stop)
        lanes: dict[str, BoundedGateway] = {}
        leases = ExitStack()
        try:
            lanes = {"BTC": gateway.fork(), "ETH": gateway.fork()}
            provider = self.beta_provider_factory()
            event_sink = self._actor_event_sink(record)
            campaign_store = BetaVolumeCampaignStore(self.settings.campaign_data_directory / record.instance_id)
            child_store = BetaVolumePlanStore(self.settings.campaign_data_directory / record.instance_id / "plans")
            market_data, order_updates = self._phase_streams(
                leases,
                phase=phase,
                profile=profile,
                gateway=gateway,
                instance_id=record.instance_id,
                proxy_key=self._proxy_key(material),
            )
            campaign_service = LiveBetaVolumeCampaignService(
                gateway,
                provider,
                campaign_store,
                child_store,
                profile_fingerprint=live_profile_fingerprint(profile),
                event_sink=event_sink,
                lane_gateways=lanes,
                market_data=market_data,
                order_updates=order_updates,
                stop_requested=stop.is_set,
            )
            volume_service = LiveBetaVolumeService(
                gateway,
                provider,
                child_store,
                event_sink=event_sink,
                lane_gateways=lanes,
                market_data=market_data,
                order_updates=order_updates,
                stop_requested=stop.is_set,
            )
            return CampaignPhaseEnvironment(
                campaign_service,
                volume_service,
                lambda: _close_actor_environment(leases, gateway, lanes),
            )
        except Exception:
            leases.close()
            _close_environment(gateway, lanes)
            raise

    def _phase_streams(
        self,
        leases: ExitStack,
        *,
        phase: str,
        profile: Any,
        gateway: BoundedGateway,
        instance_id: str,
        proxy_key: str,
    ) -> tuple[Any | None, Any | None]:
        if not self.settings.live_campaign_websockets_enabled or phase not in {"open", "close", "safe_stop"}:
            return None, None
        market_data = leases.enter_context(
            self.market_data_hub.lease(
                proxy_key,
                lambda: _open_public_market_stream(gateway.fork(), profile.proxy_url),
            )
        )
        order_updates = leases.enter_context(
            self.private_order_stream_pool.lease(
                instance_id,
                lambda: _open_private_order_stream(profile.settings.require_credentials(), profile.proxy_url),
            )
        )
        return market_data, order_updates

    def _actor_event_sink(self, record: CampaignRecord) -> Callable[[Mapping[str, Any]], None]:
        def sink(payload: Mapping[str, Any]) -> None:
            try:
                event = _sanitize_event(dict(payload))
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
        ending_available = _ending_available_quote(result)
        if ending_available is None:
            ending_available = self._read_ending_available(material)
        self.journal.update(
            record.campaign_id,
            status=status,
            result=result,
            finished_at_ms=_now_ms(),
            phase="finished",
            generated_quote=result.get("executed_quote_volume", "0"),
            remaining_quote=result.get("remaining_quote", "0"),
            excess_quote=result.get("excess_quote", "0"),
            reason=result.get("reason"),
            ending_available_balance_quote=ending_available,
            **_campaign_result_metrics(result),
        )

    def _actor_failed(self, record: CampaignRecord, error: Exception) -> None:
        reason = _worker_exception_reason(error)
        latest = self.journal.get(record.campaign_id) or record
        attempted = submission_attempted(latest)
        status = BetaCampaignStatus.RECOVERING.value if attempted else BetaCampaignStatus.STOPPED.value
        stored_reason = reason if attempted else f"launch_aborted:{reason}"
        self.journal.update(record.campaign_id, status=status, finished_at_ms=_now_ms(), reason=stored_reason)
        event = _sanitize_event(
            {
                "event": "campaign_recovering" if attempted else "launch_aborted",
                "error": type(error).__name__,
                "reason": stored_reason,
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

    def actor_runtime_snapshot(self):  # type: ignore[no-untyped-def]
        return self._actor_runtime.snapshot()

    def connection_snapshots(self):  # type: ignore[no-untyped-def]
        return self.market_data_hub.snapshot(), self.private_order_stream_pool.snapshot()

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


def _ending_available_quote(result: Mapping[str, Any]) -> str | None:
    boundary = result.get("final_boundary")
    if isinstance(boundary, Mapping) and boundary.get("available_quote") is not None:
        try:
            value = Decimal(str(boundary["available_quote"]))
        except Exception:
            return None
        return format(value, "f") if value.is_finite() else None
    return None


def _close_environment(gateway: BoundedGateway, lanes: Mapping[str, BoundedGateway]) -> None:
    for lane in lanes.values():
        lane.close()
    gateway.close()


def _close_actor_environment(leases: ExitStack, gateway: BoundedGateway, lanes: Mapping[str, BoundedGateway]) -> None:
    leases.close()
    _close_environment(gateway, lanes)


class _PublicMarketStream:
    """Own the snapshot gateway for a public stream shared by one proxy key."""

    def __init__(self, snapshot_gateway: BoundedGateway, proxy_url: str | None) -> None:
        self._snapshot_gateway = snapshot_gateway
        self._stream = WeexPublicOrderBookStream(snapshot_gateway, proxy_url=proxy_url)
        self._stream.start()

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return self._stream.order_book(symbol, limit)

    def close(self) -> None:
        self._stream.close()
        self._snapshot_gateway.close()


def _open_public_market_stream(snapshot_gateway: BoundedGateway, proxy_url: str | None) -> _PublicMarketStream:
    return _PublicMarketStream(snapshot_gateway, proxy_url)


def _open_private_order_stream(credentials: Any, proxy_url: str | None) -> WeexPrivateOrderStream:
    stream = WeexPrivateOrderStream(credentials, proxy_url=proxy_url)
    stream.start()
    return stream


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
