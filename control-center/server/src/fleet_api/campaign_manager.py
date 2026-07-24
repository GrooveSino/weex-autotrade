from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from threading import RLock
from typing import Any

from weex_cli.beta_allocation import BetaUnavailable, HttpBetaAllocationProvider
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignStore,
    inspect_live_account,
    live_profile_fingerprint,
)

from .campaign_contracts import CampaignJournal, CampaignRecord, _AccountLease
from .campaign_events import _sanitize_event, _view
from .campaign_helpers import (
    _available_quote_from_readiness,
    _preview_metadata,
)
from .campaign_manager_actor import CampaignActorRuntimeMixin
from .campaign_manager_bound import CampaignBoundStrategyMixin
from .campaign_manager_cleanup import CampaignCleanupMixin
from .campaign_manager_worker import CampaignWorkerRuntimeMixin
from .config import ControlPlaneSettings
from .execution_capacity import ExecutionCapacity, ExecutionCapacitySnapshot
from .execution_io import ExecutionIoBudget, ExecutionIoSnapshot
from .execution_phase_pacer import ExecutionPhasePacer
from .fleet_write_coordinator import FleetWriteCoordinator
from .market_data_hub import MarketDataHub
from .models import (
    BetaCampaignEvent,
    BetaCampaignPreview,
    BetaCampaignPreviewRequest,
    BetaCampaignStatus,
    BetaCampaignView,
)
from .ownership import LEGACY_OWNER_USER_ID
from .private_order_stream_pool import PrivateOrderStreamPool
from .service import BetaSourceUnavailable, UnsafeOperation
from .vault import CredentialMaterial, CredentialVault
class CampaignWorkerManager(
    CampaignBoundStrategyMixin,
    CampaignCleanupMixin,
    CampaignActorRuntimeMixin,
    CampaignWorkerRuntimeMixin,
):
    def __init__(
        self,
        settings: ControlPlaneSettings,
        vault: CredentialVault,
        journal: CampaignJournal,
        beta_provider_factory: Callable[[], HttpBetaAllocationProvider],
        *,
        on_change: Callable[[str], None] | None = None,
        on_progress: Callable[[str, Mapping[str, Any]], None] | None = None,
        on_execution_claim: Callable[[CampaignRecord, int], None] | None = None,
        executor_generation: str = "local",
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.journal = journal
        self.beta_provider_factory = beta_provider_factory
        self.on_change = on_change or (lambda _instance_id: None)
        self.on_progress = on_progress or (lambda _instance_id, _event: None)
        self.on_execution_claim = on_execution_claim or (lambda _record, _started_at_ms: None)
        self.executor_generation = executor_generation
        self.capacity = ExecutionCapacity(
            max_active_executions=settings.max_active_executions,
            max_normal_phases=settings.normal_phase_max_concurrency,
            phase_start_rate_per_second=settings.normal_phase_starts_per_second,
            per_proxy_gap_seconds=settings.normal_phase_proxy_gap_seconds,
            stable_jitter_seconds=settings.execution_phase_jitter_seconds,
        )
        self.phase_pacer = ExecutionPhasePacer(capacity=self.capacity)
        self.io_budget = ExecutionIoBudget(
            max_normal=settings.execution_io_normal_capacity,
            max_emergency=settings.execution_io_emergency_capacity,
        )
        self.write_coordinator = FleetWriteCoordinator()
        self.market_data_hub = MarketDataHub()
        self.private_order_stream_pool = PrivateOrderStreamPool()
        # The legacy worker remains only as a compatibility fallback.  Actor
        # mode owns logical concurrency through ``ExecutionCapacity`` instead.
        legacy_workers = 1 if settings.async_actor_runtime_enabled else settings.live_campaign_worker_count
        self._executor = ThreadPoolExecutor(
            max_workers=legacy_workers, thread_name_prefix="weex-campaign"
        )
        self._stops: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._leases: dict[str, _AccountLease] = {}
        self._starting: set[str] = set()
        self._cleaning: set[str] = set()
        self._closing = False
        self._lock = RLock()
        self._create_actor_runtime()

    def recover(self) -> int:
        count = self.journal.recover_incomplete()
        for record in self.journal.list_all():
            self._notify(record.instance_id)
        return count

    def preview(
        self,
        instance_id: str,
        request: BetaCampaignPreviewRequest,
        material: CredentialMaterial | None,
        *,
        owner_user_id: str = LEGACY_OWNER_USER_ID,
    ) -> BetaCampaignPreview:
        self._require_live_gate()
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        if self.journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("this account already has an active Beta Campaign")
        self._recover_uncertain_before_preview(instance_id, material)
        profile, gateway = self._profile_and_gateway(material)
        provider = self.beta_provider_factory()
        try:
            try:
                allocation = provider.get()
            except BetaUnavailable as exc:
                raise BetaSourceUnavailable(f"final beta source unavailable: {exc}") from None
            campaign = BetaVolumeCampaign.create(
                gateway,
                allocation,
                profile_fingerprint=live_profile_fingerprint(profile),
                target_turnover_quote=request.target_quote,
                round_turnover_quote=request.cycle_volume,
                hold_min_seconds=request.hold_min_seconds,
                hold_max_seconds=request.hold_max_seconds,
                round_gap_min_seconds=request.round_gap_min_seconds,
                round_gap_max_seconds=request.round_gap_max_seconds,
            )
            opening_notional = min(campaign.round_turnover_quote, campaign.target_turnover_quote) / Decimal(2)
            required = opening_notional / Decimal(campaign.max_auto_leverage) * campaign.margin_buffer
            readiness = inspect_live_account(
                gateway,
                required,
                opening_notional=opening_notional,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
            )
            available = _available_quote_from_readiness(readiness)
            blockers: list[str] = []
            if not readiness.get("available_sufficient", False):
                blockers.append("available_balance_insufficient")
            if (
                readiness.get("active_position_count", 0)
                or readiness.get("regular_order_count", 0)
                or readiness.get("trigger_order_count", 0)
            ):
                blockers.append("account_is_not_flat")
            if blockers:
                raise UnsafeOperation(f"campaign preview blocked: {','.join(blockers)}")
            metadata = _preview_metadata(campaign, available, readiness)
            metadata["owner_user_id"] = owner_user_id
            self.journal.create(instance_id, campaign, metadata)
            BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance_id).create(campaign)
            return _view(self.journal.get(campaign.campaign_id), include_events=False)  # type: ignore[arg-type]
        finally:
            gateway.close()

    def start(
        self,
        instance_id: str,
        campaign_id: str,
        confirmation: str,
        risk_acknowledged: bool,
        material: CredentialMaterial | None,
    ) -> BetaCampaignView:
        self._require_live_gate()
        if not risk_acknowledged:
            raise UnsafeOperation("risk acknowledgement is required")
        record = self._require_record(instance_id, campaign_id)
        if record.status != BetaCampaignStatus.PLANNED.value:
            raise UnsafeOperation("campaign is not in planned state")
        if record.campaign.schema_version not in {2, 3, 4}:
            raise UnsafeOperation("execution schema is not executable; create a new preview")
        with self._lock:
            if self._closing:
                raise UnsafeOperation("campaign manager is shutting down")
            if campaign_id in self._starting or campaign_id in self._futures:
                raise UnsafeOperation("campaign is already starting or running")
            if not self.capacity.admit(campaign_id):
                raise UnsafeOperation(
                    f"Fleet active execution capacity is full ({self.capacity.snapshot().max_active_executions})"
                )
            self._starting.add(campaign_id)
        lease: _AccountLease | None = None
        submitted = False
        try:
            if int(time.time() * 1000) >= record.campaign.expires_at_ms:
                self.journal.update(
                    campaign_id,
                    status=BetaCampaignStatus.STOPPED.value,
                    finished_at_ms=int(time.time() * 1000),
                    reason="launch_aborted:authorization_expired",
                )
                raise UnsafeOperation("campaign authorization has expired")
            if confirmation != str(record.metadata["confirmation"]):
                raise UnsafeOperation("exact campaign confirmation does not match")
            if material is None:
                raise UnsafeOperation("account credentials are unavailable")
            lease = _AccountLease(
                self.settings.campaign_data_directory,
                material.api_key.get_secret_value(),
                instance_id,
                campaign_id,
            )
            lease.acquire()
            try:
                starting_available_balance = self._verify_execution_boundary(record, material)
            except Exception as exc:
                reason = f"launch_aborted:execution_boundary:{type(exc).__name__.lower()}"
                self.journal.update(
                    campaign_id,
                    status=BetaCampaignStatus.STOPPED.value,
                    finished_at_ms=int(time.time() * 1000),
                    reason=reason,
                )
                event = _sanitize_event({"event": "launch_aborted", "reason": reason})
                event["sequence"] = self._append_monitor_event(record, event)
                self._notify(instance_id)
                raise UnsafeOperation("启动条件已变化，请重新确认") from exc
            self.journal.update(
                campaign_id,
                starting_available_balance_quote=str(starting_available_balance),
            )
            stop = threading.Event()
            with self._lock:
                started_at_ms = int(time.time() * 1000)
                if not self.journal.claim_execution(campaign_id, started_at_ms=started_at_ms):
                    raise UnsafeOperation("campaign was already claimed by another worker")
                claimed = self._require_record(instance_id, campaign_id)
                try:
                    self.on_execution_claim(claimed, started_at_ms)
                except Exception as exc:
                    self.journal.update(
                        campaign_id,
                        status=BetaCampaignStatus.STOPPED.value,
                        finished_at_ms=int(time.time() * 1000),
                        reason=f"launch_aborted:execution_claim_callback_failed:{type(exc).__name__.lower()}",
                    )
                    raise UnsafeOperation("execution could not establish its local ledger session") from exc
                self._stops[campaign_id] = stop
                self._leases[campaign_id] = lease
                claimed = self._require_record(instance_id, campaign_id)
                try:
                    if self.settings.async_actor_runtime_enabled:
                        self._start_actor(claimed, material, stop)
                    else:
                        future = self._executor.submit(self._run, claimed, material, stop)
                        self._futures[campaign_id] = future
                except Exception as exc:
                    self._stops.pop(campaign_id, None)
                    self._leases.pop(campaign_id, None)
                    self.journal.update(
                        campaign_id,
                        status=BetaCampaignStatus.STOPPED.value,
                        finished_at_ms=int(time.time() * 1000),
                        reason=f"launch_aborted:worker_submit_failed:{type(exc).__name__.lower()}",
                    )
                    raise UnsafeOperation("campaign worker could not be started; start can be prepared again") from exc
                submitted = True
        finally:
            with self._lock:
                self._starting.discard(campaign_id)
            if lease is not None and not submitted:
                lease.release()
            if not submitted:
                self.capacity.release_execution(campaign_id)
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id), include_events=False)  # type: ignore[arg-type]

    def stop(self, instance_id: str, campaign_id: str, confirmation: str) -> BetaCampaignView:
        record = self._require_record(instance_id, campaign_id)
        if record.status not in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
            raise UnsafeOperation("campaign is not running")
        if confirmation != str(record.metadata["stop_confirmation"]):
            raise UnsafeOperation("exact stop confirmation does not match")
        with self._lock:
            event = self._stops.get(campaign_id)
            if event is None:
                raise UnsafeOperation("campaign worker is not available")
            event.set()
            if self.settings.async_actor_runtime_enabled:
                self._actor_runtime.stop(campaign_id)
            self.journal.update(campaign_id, status=BetaCampaignStatus.STOPPING.value, reason="stop_requested")
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id), include_events=False)  # type: ignore[arg-type]

    def get(self, instance_id: str, campaign_id: str) -> BetaCampaignView:
        return _view(self._require_record(instance_id, campaign_id))

    def list(self, instance_id: str) -> list[BetaCampaignView]:
        return [_view(record, include_events=False) for record in self.journal.list_for_instance(instance_id)]

    def events(self, instance_id: str, campaign_id: str) -> list[BetaCampaignEvent]:
        record = self._require_record(instance_id, campaign_id)
        return [BetaCampaignEvent.model_validate(event) for event in record.events]

    def public_snapshot(self) -> list[dict[str, Any]]:
        if not hasattr(self.journal, "list_all"):
            return []
        return [
            _view(record, include_events=False).model_dump(mode="json", by_alias=True)
            for record in self.journal.list_all()
        ]  # type: ignore[attr-defined]

    def active_worker_count(self) -> int:
        """Return work currently owned by this process, not executor capacity."""
        with self._lock:
            active_futures = {campaign_id for campaign_id, future in self._futures.items() if not future.done()}
            active_actors = {campaign_id for campaign_id, future in self._actor_futures.items() if not future.done()}
            return len(self._starting | active_futures | active_actors)
    def capacity_snapshot(self) -> ExecutionCapacitySnapshot:
        return self.phase_pacer.snapshot()

    def io_snapshot(self) -> ExecutionIoSnapshot:
        return self.io_budget.snapshot()

    def has_active_worker(self, instance_id: str) -> bool:
        """Report whether this process still owns executable work for an account."""
        with self._lock:
            campaign_ids = set(self._starting)
            campaign_ids.update(campaign_id for campaign_id, future in self._futures.items() if not future.done())
            campaign_ids.update(campaign_id for campaign_id, future in self._actor_futures.items() if not future.done())
        return any(
            (record := self.journal.get(campaign_id)) is not None and record.instance_id == instance_id
            for campaign_id in campaign_ids
        )

    def close(self) -> None:
        with self._lock:
            self._closing = True
            for stop in self._stops.values():
                stop.set()
        self._actor_runtime.close()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.private_order_stream_pool.close()
        self.market_data_hub.close()
        self.write_coordinator.close()
        self.journal.close()
