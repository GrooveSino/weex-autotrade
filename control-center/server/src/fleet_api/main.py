from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from weex_cli.beta_allocation import HttpBetaAllocationProvider as LiveCampaignBetaAllocationProvider

from .beta_allocation import HttpBetaAllocationProvider
from .beta_source import BetaSourceRuntime, InMemoryBetaSourceStore, SQLiteBetaSourceStore
from .campaign_log import campaign_event_log
from .campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
    SQLiteCampaignJournal,
)
from .command_ledger import CommandReceiptLedger
from .config import ControlPlaneSettings
from .events import InstanceEventBroker
from .execution import (
    AllocationUnavailable,
    CycleExecutionStatus,
    ExecutionJournal,
    ExecutionRecord,
    InMemoryExecutionJournal,
    MockPairedExecutionAdapterFactory,
    PairAllocationProvider,
    PairedCycleCoordinator,
    SQLiteExecutionJournal,
)
from .models import (
    AccountInstance,
    BetaCampaignEvent,
    BetaCampaignExecuteRequest,
    BetaCampaignPreview,
    BetaCampaignPreviewRequest,
    BetaCampaignReconcileRequest,
    BetaCampaignStopRequest,
    BetaCampaignView,
    BetaMarketSnapshot,
    BetaSourceSettings,
    BetaSourceSettingsUpdate,
    BoundStrategyExecutionExecuteRequest,
    BoundStrategyExecutionPreviewRequest,
    BoundStrategyExecutionReconcileRequest,
    BoundStrategyExecutionStopRequest,
    CreateInstanceRequest,
    ExecutionCycleView,
    GlobalStopRequest,
    GlobalStopResult,
    HealthResponse,
    InstanceAction,
    LogBatch,
    LogLine,
    SchedulerMetrics,
    SessionVolumeProjection,
    StrategyAssignmentRequest,
    StrategyAssignmentResult,
    StrategyMonitorSnapshot,
    StrategyRunPage,
    StrategyRunSummary,
    StrategyStage,
    StrategyTargetMode,
    TradingMode,
    UpdateInstanceRequest,
    VolumeSessionCreateRequest,
    VolumeSessionResponse,
    VolumeStrategy,
    VolumeStrategyInput,
)
from .ownership import reset_current_owner_user_id, set_current_owner_user_id
from .repository import AccountRepository, InMemoryAccountRepository, SQLiteAccountRepository
from .runtime import AccountRuntimeManager
from .seed import ensure_mock_volume_baselines, seed_mock_instances
from .service import FleetControlService, FleetError, InstanceNotFound, TelemetryUnavailable, UnsafeOperation
from .strategy import StrategyRunBlocked, StrategyTargetReached, resolve_strategy_run_plan
from .strategy_monitor import StrategyMonitorService
from .telemetry import AccountTelemetryAdapterFactory, MockAccountTelemetryAdapterFactory
from .vault import CredentialVault, EncryptedSQLiteCredentialVault, EphemeralCredentialVault
from .volume_history import InMemoryTradeVolumeLedger, SessionVolumeService, SQLiteTradeVolumeLedger, TradeVolumeLedger
from .weex_readonly import WeexReadonlyAccountTelemetryAdapterFactory


def _optional_available_balance(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        balance = Decimal(str(value))
    except Exception:  # noqa: BLE001 - invalid audit metadata is treated as absent.
        return None
    return balance if balance.is_finite() and balance >= 0 else None


def create_app(
    settings: ControlPlaneSettings | None = None,
    adapter_factory: AccountTelemetryAdapterFactory | None = None,
    allocation_provider: PairAllocationProvider | None = None,
    *,
    require_command_id: bool = False,
) -> FastAPI:
    selected = settings or ControlPlaneSettings.load()
    repository: AccountRepository
    vault: CredentialVault
    volume_ledger: TradeVolumeLedger
    execution_journal: ExecutionJournal
    if selected.storage == "sqlite":
        assert selected.master_key is not None
        repository = SQLiteAccountRepository(selected.sqlite_path)
        vault = EncryptedSQLiteCredentialVault(selected.sqlite_path, selected.master_key)
        try:
            vault.verify_all()
            volume_ledger = SQLiteTradeVolumeLedger(selected.sqlite_path)
            try:
                execution_journal = SQLiteExecutionJournal(selected.sqlite_path)
            except Exception:
                volume_ledger.close()
                raise
        except Exception:
            vault.close()
            repository.close()
            raise
        beta_source_store = SQLiteBetaSourceStore(selected.sqlite_path)
    else:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        volume_ledger = InMemoryTradeVolumeLedger()
        execution_journal = InMemoryExecutionJournal()
        beta_source_store = InMemoryBetaSourceStore()
    command_ledger = CommandReceiptLedger(selected.sqlite_path if selected.storage == "sqlite" else None)
    had_persisted_instances = bool(repository.list())
    service = FleetControlService(
        repository,
        vault,
        adapter=selected.adapter,
        mock_cycle_total_quote=selected.mock_cycle_total_quote,
    )
    executor_generation = uuid4().hex
    executor_release_id = os.environ.get("FLEET_EXECUTOR_RELEASE_ID", "dev").strip() or "dev"
    broker = InstanceEventBroker(executor_generation)
    execution_journal.recover_incomplete()
    execution_coordinator: PairedCycleCoordinator | None = None
    selected_allocation_provider: PairAllocationProvider | None = None
    def beta_provider_from_source(source: BetaSourceSettings) -> HttpBetaAllocationProvider:
        return HttpBetaAllocationProvider(
            source.url,
            timeout_seconds=source.timeout_seconds,
            cache_seconds=source.refresh_interval_seconds,
            network_on_demand=not source.background_refresh_enabled,
        )

    beta_source_runtime = BetaSourceRuntime(
        beta_source_store,
        BetaSourceSettings(
            url=selected.beta_ratio_url,
            timeout_seconds=selected.beta_ratio_timeout_seconds,
            refresh_interval_seconds=selected.beta_refresh_interval_seconds,
            background_refresh_enabled=selected.beta_background_refresh_enabled,
            updated_at_ms=time.time_ns() // 1_000_000,
        ),
        provider_factory=beta_provider_from_source,
    )
    campaign_journal = (
        SQLiteCampaignJournal(selected.sqlite_path) if selected.storage == "sqlite" else InMemoryCampaignJournal()
    )
    event_loop: asyncio.AbstractEventLoop | None = None
    session_finalizations: set[str] = set()
    session_finalization_tasks: set[asyncio.Task[None]] = set()

    def latest_bound_record(instance_id: str):
        records = [
            record for record in campaign_journal.list_for_instance(instance_id) if record.metadata.get("strategy_id")
        ]
        return max(records, key=lambda item: item.campaign.created_at_ms) if records else None

    async def finalize_bound_strategy_session(record) -> None:
        metadata = record.metadata
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id or session_id in session_finalizations:
            return
        session = volume_ledger.get_session(session_id)
        if session is None:
            return
        ending_available_balance = _optional_available_balance(
            metadata.get("ending_available_balance_quote")
        )
        if ending_available_balance is not None:
            volume_ledger.update_session(
                session_id,
                ending_available_balance_quote=ending_available_balance,
            )
        if session.status in {"completed", "stopped"} and session.source_complete and not session.stale:
            return
        terminal_status = str(record.status)
        finished_at_ms = int(metadata.get("finished_at_ms") or time.time_ns() // 1_000_000)
        if terminal_status == "uncertain":
            session_volume.mark_uncertain(
                session_id,
                reason=str(metadata.get("reason") or "campaign_outcome_uncertain"),
                finished_at_ms=finished_at_ms,
            )
            await publish_snapshot()
            return
        if terminal_status not in {"completed", "stopped"}:
            return

        session_finalizations.add(session_id)
        volume_ledger.update_session(
            session_id,
            status="verification_pending",
            result=terminal_status,
            result_reason=metadata.get("reason"),
            finished_at_ms=finished_at_ms,
            source_complete=False,
            stale=True,
            pending_sync=True,
        )
        try:
            fills, complete, reason = await runtime.authoritative_session_fills(
                record.instance_id,
                session.started_at_ms,
                finished_at_ms,
            )
            if not complete:
                volume_ledger.update_session(
                    session_id,
                    status="verification_pending",
                    source_complete=False,
                    stale=True,
                    reconciliation_required=True,
                    pending_sync=False,
                    result_reason=f"session_source_incomplete:{reason}"[:160],
                )
                return
            volume_ledger.record_account_fills(record.instance_id, session.mode, fills)
            projection = session_volume.reconcile(session_id, fills, reconciled_at_ms=finished_at_ms)
            if projection["reconciliation_required"]:
                return
            aggregate = volume_ledger.aggregate(record.instance_id, 0)
            session_volume.finalize(
                session_id,
                result=terminal_status,
                reason=str(metadata.get("reason")) if metadata.get("reason") else None,
                finished_at_ms=finished_at_ms,
                final_lifetime_quote_volume=aggregate.lifetime,
                ending_available_balance_quote=ending_available_balance,
            )
        except Exception as exc:
            volume_ledger.update_session(
                session_id,
                status="verification_pending",
                source_complete=False,
                stale=True,
                reconciliation_required=True,
                pending_sync=False,
                result_reason=f"session_reconciliation_failed:{type(exc).__name__.lower()}",
            )
        finally:
            session_finalizations.discard(session_id)
            await publish_snapshot()

    def schedule_session_finalization(record) -> None:
        task = asyncio.create_task(
            finalize_bound_strategy_session(record),
            name=f"fleet-session-finalize-{record.campaign_id}",
        )
        session_finalization_tasks.add(task)
        task.add_done_callback(session_finalization_tasks.discard)

    def notify_campaign_change(_instance_id: str) -> None:
        # The executor owns the Campaign lifecycle; this only projects its
        # durable state into the account list, without submitting any command.
        try:
            record = latest_bound_record(_instance_id)
            if record is not None:
                bound = campaign_manager.get(_instance_id, record.campaign_id)
                service.project_bound_strategy_execution(_instance_id, bound.status.value, bound.reason)
                session_id = record.metadata.get("session_id")
                if isinstance(session_id, str) and volume_ledger.get_session(session_id) is not None:
                    if bound.status.value == "stopping":
                        volume_ledger.update_session(session_id, status="stopping")
                    elif bound.status.value == "uncertain":
                        volume_ledger.update_session(
                            session_id,
                            status="uncertain",
                            uncertain_order_state=True,
                            stale=True,
                            reconciliation_required=True,
                        )
        except Exception:
            pass
        if event_loop is None or not event_loop.is_running():
            return

        def schedule() -> None:
            record = latest_bound_record(_instance_id)
            if record is not None and record.status in {"completed", "stopped", "uncertain"}:
                schedule_session_finalization(record)
            asyncio.create_task(publish_snapshot())

        event_loop.call_soon_threadsafe(schedule)

    def establish_bound_strategy_session(record, started_at_ms: int) -> None:
        """Create the session immediately before worker submission, never on preview."""
        metadata = record.metadata
        if metadata.get("execution_kind") != "bound_strategy":
            return
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        if volume_ledger.get_session(session_id) is not None:
            return
        target = Decimal(str(metadata.get("session_target_quote") or record.campaign.target_turnover_quote))
        SessionVolumeService(volume_ledger).start(
            session_id=session_id,
            account_id=record.instance_id,
            mode="live",
            started_at_ms=started_at_ms,
            target_quote_volume=target,
            maker_only_required=True,
            strategy_id=str(metadata.get("strategy_id")) if metadata.get("strategy_id") else None,
            strategy_name=str(metadata.get("strategy_name")) if metadata.get("strategy_name") else None,
            strategy_version=int(metadata["strategy_version"])
            if metadata.get("strategy_version") is not None
            else None,
            target_mode=str(metadata.get("target_mode") or "incremental"),
            strategy_target_quote_volume=Decimal(str(metadata.get("strategy_target_quote") or target)),
            baseline_lifetime_quote_volume=Decimal(str(metadata.get("baseline_lifetime_quote") or "0")),
            starting_available_balance_quote=_optional_available_balance(
                metadata.get("starting_available_balance_quote")
            ),
        )

    campaign_manager = CampaignWorkerManager(
        selected,
        vault,
        campaign_journal,
        lambda: LiveCampaignBetaAllocationProvider(
            beta_source_runtime.settings.url,
            timeout_seconds=beta_source_runtime.settings.timeout_seconds,
            allow_low_confidence=True,
        ),
        on_change=notify_campaign_change,
        on_execution_claim=establish_bound_strategy_session,
        executor_generation=executor_generation,
    )
    campaign_manager.recover()
    campaign_manager.invalidate_stale_planned_bound_strategy_previews(
        {instance.id: instance.strategy for instance in service.list_instances()},
        reason="executor_startup_strategy_snapshot_stale",
    )
    if selected.adapter == "mock":
        selected_allocation_provider = allocation_provider or beta_source_runtime
        execution_coordinator = PairedCycleCoordinator(
            execution_journal,
            volume_ledger,
            selected_allocation_provider,
            MockPairedExecutionAdapterFactory(),
            total_quote=selected.mock_cycle_total_quote,
        )
    if adapter_factory is not None:
        telemetry_factory = adapter_factory
    elif selected.adapter == "mock":
        telemetry_factory = MockAccountTelemetryAdapterFactory()
    else:
        telemetry_factory = WeexReadonlyAccountTelemetryAdapterFactory(
            volume_ledger,
            request_timeout_ms=selected.weex_request_timeout_ms,
            history_lookback_days=selected.weex_history_lookback_days,
            history_pages_per_poll=selected.weex_history_pages_per_poll,
        )
    runtime = AccountRuntimeManager(
        service,
        telemetry_factory,
        volume_ledger,
        execution_coordinator,
        max_parallel_polls=selected.max_parallel_polls,
        poll_timeout_seconds=selected.account_poll_timeout_seconds,
    )
    session_volume = SessionVolumeService(volume_ledger)
    strategy_monitor = StrategyMonitorService(campaign_journal, volume_ledger, executor_generation)
    strategy_monitor.rebuild_all()
    app_state_campaign_manager = campaign_manager
    if had_persisted_instances:
        ensure_mock_volume_baselines(
            repository.list(),
            volume_ledger,
            time.time_ns() // 1_000_000,
        )
        service.reconcile_after_restart()
    elif selected.seed_demo_data and selected.adapter == "mock":
        seed_mock_instances(
            repository,
            volume_ledger,
            time.time_ns() // 1_000_000,
            selected.mock_cycle_total_quote,
        )

    async def publish_snapshot() -> None:
        await broker.publish(projected_instances(), runtime.metrics(), app_state_campaign_manager.public_snapshot())

    async def poll_loop() -> None:
        while True:
            await asyncio.sleep(selected.poll_interval_seconds)
            if await runtime.poll_all():
                await publish_snapshot()

    async def refresh_beta_state() -> bool:
        available = await beta_source_runtime.refresh()
        changed = 0
        if selected_allocation_provider is beta_source_runtime:
            changed = await runtime.reconcile_beta_availability(
                available,
                beta_source_runtime.last_refresh_error,
            )
        if changed:
            await publish_snapshot()
        return available

    async def beta_refresh_loop() -> None:
        while True:
            interval = beta_source_runtime.settings.refresh_interval_seconds
            await asyncio.sleep(beta_source_runtime.seconds_until_refresh(interval))
            await refresh_beta_state()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal event_loop
        event_loop = asyncio.get_running_loop()
        for record in campaign_journal.list_all():
            if record.metadata.get("strategy_id") and record.status in {"completed", "stopped", "uncertain"}:
                schedule_session_finalization(record)
        beta_task: asyncio.Task[None] | None = None
        if selected.beta_background_refresh_enabled:
            await refresh_beta_state()
            beta_task = asyncio.create_task(beta_refresh_loop(), name="fleet-beta-refresher")
        task = asyncio.create_task(poll_loop(), name="fleet-account-poller")
        try:
            yield
        finally:
            task.cancel()
            if beta_task is not None:
                beta_task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if beta_task is not None:
                with suppress(asyncio.CancelledError):
                    await beta_task
            await runtime.close()
            # Worker shutdown can emit a final terminal callback. Keep the event
            # loop responsive while workers finish, then drain the resulting
            # read-only reconciliation tasks before closing their SQLite ledger.
            await asyncio.to_thread(campaign_manager.close)
            while session_finalization_tasks:
                await asyncio.gather(*tuple(session_finalization_tasks), return_exceptions=True)
            event_loop = None
            await beta_source_runtime.aclose()
            beta_source_store.close()
            execution_journal.close()
            volume_ledger.close()
            vault.close()
            repository.close()
            command_ledger.close()

    app = FastAPI(
        title="WEEX Fleet Control Plane",
        version="0.1.0",
        description=(
            "Private executor for WEEX Fleet. Live bound-strategy execution remains "
            "confirmation-gated, idempotent, POST_ONLY, and subject to manual reconciliation; "
            "the public API proxy never submits exchange commands itself."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(selected.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Fleet-Command-Id"],
    )
    app.state.fleet_service = service
    app.state.fleet_repository = repository
    app.state.credential_vault = vault
    app.state.instance_event_broker = broker
    app.state.account_runtime = runtime
    app.state.trade_volume_ledger = volume_ledger
    app.state.execution_journal = execution_journal
    app.state.execution_coordinator = execution_coordinator
    app.state.pair_allocation_provider = selected_allocation_provider
    app.state.beta_market_provider = beta_source_runtime
    app.state.beta_source_runtime = beta_source_runtime
    app.state.session_volume = session_volume
    app.state.campaign_journal = campaign_journal
    app.state.campaign_manager = campaign_manager
    app.state.executor_generation = executor_generation
    app.state.executor_release_id = executor_release_id
    app.state.command_ledger = command_ledger

    @app.middleware("http")
    async def executor_request_owner(request: Request, call_next):
        # The public API proxy validates the local HttpOnly session and injects
        # this header over a 0600 Unix socket. Direct executor callers are not
        # trusted with an arbitrary browser header.
        if not (require_command_id and selected.local_user_auth_required):
            return await call_next(request)
        if request.url.path in {"/_internal/executor-health", "/api/v1/health"}:
            return await call_next(request)
        user_id = request.headers.get("X-Fleet-User", "").strip()
        allowed_user_characters = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not user_id or len(user_id) > 48 or any(char not in allowed_user_characters for char in user_id):
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "local login is required"})
        token = set_current_owner_user_id(user_id)
        try:
            parts = [part for part in request.url.path.split("/") if part]
            if len(parts) >= 4 and parts[:3] == ["api", "v1", "instances"] and parts[3] != "missing":
                try:
                    service.get_instance(parts[3])
                except InstanceNotFound as exc:
                    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})
            return await call_next(request)
        finally:
            reset_current_owner_user_id(token)

    @app.middleware("http")
    async def idempotent_executor_commands(request: Request, call_next):
        if request.method not in {"POST", "PATCH", "DELETE"} or not request.url.path.startswith("/api/v1/"):
            return await call_next(request)
        command_id = request.headers.get("X-Fleet-Command-Id", "").strip()
        if not command_id:
            if require_command_id:
                return JSONResponse(status_code=400, content={"detail": "X-Fleet-Command-Id is required"})
            return await call_next(request)
        if len(command_id) > 128:
            return JSONResponse(status_code=400, content={"detail": "invalid command id"})
        body = await request.body()
        fingerprint = hashlib.sha256(
            b"\n".join((request.method.encode(), request.url.path.encode(), request.url.query.encode(), body))
        ).hexdigest()
        owner = request.headers.get("X-Fleet-User", "").strip()
        ledger_command_id = f"{owner}:{command_id}" if owner else command_id
        existing = command_ledger.claim(ledger_command_id, fingerprint)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return JSONResponse(
                    status_code=409,
                    content={"detail": "command id conflicts with a different request"},
                )
            return JSONResponse(
                status_code=409,
                content={"detail": "command already accepted; query account or campaign state instead of retrying"},
            )
        response = await call_next(request)
        command_ledger.complete(ledger_command_id)
        return response

    def project_instance_session(instance: AccountInstance) -> AccountInstance:
        active = volume_ledger.active_session(instance.id, instance.mode.value)
        last_run = volume_ledger.latest_terminal_session(instance.id, instance.mode.value)
        compatibility_session = active or last_run
        strategy_target = Decimal(instance.strategy.target_volume_quote)
        progress_source = "ledger"
        progress_updated_at_ms: int | None = None
        strategy_progress = instance.strategy_progress
        if instance.strategy.target_mode is StrategyTargetMode.LIFETIME:
            strategy_verified = Decimal(str(instance.volume.lifetime))
            strategy_remaining = max(strategy_target - strategy_verified, Decimal(0))
            target_reached = instance.volume.complete and strategy_remaining <= 0
        elif active is not None:
            strategy_verified = Decimal(str(active["verified_quote_volume"]))
            strategy_remaining = Decimal(str(active["remaining_quote_volume"]))
            target_reached = False
            # The worker journal is the same durable, transactionally updated
            # source used by execution monitoring.  Its leg_completed values
            # come from reconciled fills; use it immediately when the separate
            # read-only account-history ledger has not caught up yet.
            record = campaign_journal.monitor_record(instance.id, str(active["session_id"]))
            projection = campaign_journal.monitor_projection(record.campaign_id) if record is not None else None
            if projection is not None:
                state = projection.state
                journal_verified = _optional_available_balance(state.get("execution_verified_quote_volume"))
                if journal_verified is not None and journal_verified > strategy_verified:
                    strategy_verified = journal_verified
                    strategy_remaining = max(strategy_target - strategy_verified, Decimal(0))
                    progress_source = "execution_journal"
                progress_updated_at_ms = projection.updated_at_ms
                waits = state.get("active_waits")
                if isinstance(waits, list):
                    primary_wait = next(
                        (
                            item
                            for item in waits
                            if isinstance(item, dict) and item.get("key") in {"hold", "round-gap"}
                        ),
                        None,
                    )
                    if isinstance(primary_wait, dict):
                        deadline = primary_wait.get("deadline_at_ms")
                        try:
                            deadline_at_ms = int(deadline) if deadline is not None else None
                        except (TypeError, ValueError):
                            deadline_at_ms = None
                        stage = (
                            StrategyStage.HOLDING
                            if primary_wait.get("key") == "hold"
                            else StrategyStage.COOLDOWN
                        )
                        strategy_progress = strategy_progress.model_copy(
                            update={"stage": stage, "next_action_at_ms": deadline_at_ms}
                        )
                if progress_source == "ledger" and strategy_verified <= 0:
                    progress_source = "pending"
        else:
            strategy_verified = Decimal(0)
            strategy_remaining = strategy_target
            target_reached = False
            progress_source = "pending"
        return instance.model_copy(
            update={
                "volume": instance.volume.model_copy(
                    update={
                        "session": SessionVolumeProjection.model_validate(compatibility_session)
                        if compatibility_session is not None
                        else None,
                        "active_session": SessionVolumeProjection.model_validate(active)
                        if active is not None
                        else None,
                        "last_run": SessionVolumeProjection.model_validate(last_run) if last_run is not None else None,
                        "lifetime_source_complete": instance.volume.complete,
                        "strategy_target_quote_volume": strategy_target,
                        "strategy_verified_quote_volume": strategy_verified,
                        "strategy_remaining_quote_volume": strategy_remaining,
                        "strategy_target_reached": target_reached,
                        "strategy_progress_source": progress_source,
                        "strategy_progress_updated_at_ms": progress_updated_at_ms,
                    }
                ),
                "strategy_progress": strategy_progress,
            }
        )

    def projected_instances() -> list[AccountInstance]:
        return [project_instance_session(instance) for instance in service.list_instances()]

    def combined_log_updates(instance_id: str, limit: int, after: str | None) -> LogBatch:
        service.get_instance(instance_id)
        system = service.log_updates(instance_id, 500, None).lines
        ranked: list[tuple[int, int, LogLine]] = []
        for index, line in enumerate(system):
            try:
                at_ms = int(datetime.fromisoformat(line.timestamp.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                at_ms = index
            ranked.append((at_ms, index, line))
        rank = len(ranked)
        for record in campaign_journal.list_for_instance(instance_id):
            for event in campaign_journal.events_before(record.campaign_id, None, 500):
                rendered = campaign_event_log(event)
                if rendered is None:
                    continue
                level, message = rendered
                sequence = int(event.get("sequence") or 0)
                at_ms = int(event.get("at_ms") or 0)
                # Releases before the single-journal architecture copied this
                # same rendered row into instance_logs. Prefer the audit row
                # when the legacy copy is adjacent in time.
                ranked = [
                    item
                    for item in ranked
                    if not (item[2].message == message and abs(item[0] - at_ms) <= 2_000)
                ]
                ranked.append(
                    (
                        at_ms,
                        rank,
                        LogLine(
                            id=f"execution:{record.campaign_id}:{sequence}",
                            timestamp=datetime.fromtimestamp(at_ms / 1000, tz=UTC).isoformat(),
                            level=level,
                            message=message,
                        ),
                    )
                )
                rank += 1
        ranked.sort(key=lambda item: (item[0], item[1]))
        combined = [item[2] for item in ranked]
        window = combined[-500:]
        reset = False
        if after is None:
            lines = window[-limit:]
        else:
            cursor_index = next((index for index, line in enumerate(window) if line.id == after), None)
            if cursor_index is None:
                lines = window[-limit:]
                reset = True
            else:
                lines = window[cursor_index + 1 : cursor_index + 1 + limit]
        cursor = lines[-1].id if lines else (None if reset else after)
        return LogBatch(lines=lines, cursor=cursor, reset=reset)

    def strategy_run_plan(instance: AccountInstance):
        try:
            return resolve_strategy_run_plan(
                instance,
                volume_ledger.active_session(instance.id, instance.mode.value),
            )
        except (StrategyRunBlocked, StrategyTargetReached) as exc:
            raise UnsafeOperation(str(exc)) from None

    @app.exception_handler(FleetError)
    async def fleet_error_handler(_request: Request, exc: FleetError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        redacted = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": redacted})

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        journal_metrics = campaign_journal.monitor_metrics()
        stream_metrics = strategy_monitor.metrics()
        ledger_lag = 0
        for record in campaign_journal.list_all():
            session_id = record.metadata.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            try:
                session = volume_ledger.session_projection(session_id)
            except KeyError:
                continue
            if session and session.get("pending_sync"):
                ledger_lag += 1
        return HealthResponse(
            status="degraded" if int(journal_metrics.get("projection_lag") or 0) else "ok",
            adapter=selected.adapter,
            storage=selected.storage,
            live_trading_enabled=selected.live_trading_enabled,
            execution_enabled=selected.adapter == "mock",
            live_campaigns_enabled=selected.adapter == "weex-live" and selected.live_campaigns_enabled,
            bound_strategy_execution_enabled=(
                selected.adapter == "weex-live" and selected.live_campaigns_enabled and selected.live_trading_enabled
            ),
            live_campaign_active_worker_count=campaign_manager.active_worker_count(),
            live_campaign_worker_count=(selected.live_campaign_worker_count if selected.adapter == "weex-live" else 0),
            api_release_id=executor_release_id,
            executor_connected=True,
            executor_generation=executor_generation,
            monitor_projection_lag=int(journal_metrics.get("projection_lag") or 0),
            monitor_ledger_lag=ledger_lag,
            monitor_sse_subscriber_count=stream_metrics["subscriber_count"],
            monitor_sse_reset_count=stream_metrics["reset_count"],
            monitor_transaction_failure_count=int(journal_metrics.get("transaction_failures") or 0),
            monitor_last_event_at_ms=(
                None
                if journal_metrics.get("last_event_at_ms") is None
                else int(journal_metrics["last_event_at_ms"])
            ),
        )

    @app.get("/_internal/executor-health", response_model=HealthResponse, include_in_schema=False)
    def executor_health() -> HealthResponse:
        return health()

    @app.get("/api/v1/commands/{command_id}", response_model=dict[str, str])
    def command_status(command_id: str, request: Request) -> dict[str, str]:
        if not command_id or len(command_id) > 128:
            raise HTTPException(status_code=400, detail="invalid command id")
        owner = request.headers.get("X-Fleet-User", "").strip()
        receipt = command_ledger.get(f"{owner}:{command_id}" if owner else command_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="command not found")
        return {"commandId": receipt.command_id, "status": receipt.status}

    @app.get("/api/v1/runtime/metrics", response_model=SchedulerMetrics)
    def runtime_metrics() -> SchedulerMetrics:
        return runtime.metrics()

    @app.get("/api/v1/beta", response_model=BetaMarketSnapshot)
    async def beta_snapshot() -> BetaMarketSnapshot:
        try:
            return await app.state.beta_market_provider.market_snapshot()
        except AllocationUnavailable as exc:
            raise TelemetryUnavailable(f"beta snapshot unavailable: {exc.reason_code}") from None

    @app.get("/api/v1/beta/source", response_model=BetaSourceSettings)
    def beta_source_settings() -> BetaSourceSettings:
        return beta_source_runtime.settings

    @app.patch("/api/v1/beta/source", response_model=BetaSourceSettings)
    async def update_beta_source_settings(payload: BetaSourceSettingsUpdate) -> BetaSourceSettings:
        updated = await beta_source_runtime.update(payload)
        # The new source is used immediately for telemetry and only for new
        # Live previews; active executions retain their frozen plan snapshot.
        await refresh_beta_state()
        await publish_snapshot()
        return updated

    @app.get("/api/v1/instances", response_model=list[AccountInstance])
    def list_instances() -> list[AccountInstance]:
        return projected_instances()

    @app.get("/api/v1/strategies", response_model=list[VolumeStrategy])
    def list_strategies() -> list[VolumeStrategy]:
        return service.list_strategies()

    @app.post("/api/v1/strategies", response_model=VolumeStrategy, status_code=status.HTTP_201_CREATED)
    def create_strategy(payload: VolumeStrategyInput) -> VolumeStrategy:
        return service.create_strategy(payload)

    @app.patch("/api/v1/strategies/{strategy_id}", response_model=VolumeStrategy)
    async def update_strategy(strategy_id: str, payload: VolumeStrategyInput) -> VolumeStrategy:
        affected = [instance.id for instance in service.list_instances() if instance.strategy_id == strategy_id]
        updated = await asyncio.to_thread(
            campaign_manager.apply_bound_strategy_change,
            affected,
            lambda: service.update_strategy(strategy_id, payload),
            reason="shared_strategy_updated",
        )
        await asyncio.gather(*(runtime.reset_instance(instance_id) for instance_id in affected))
        await publish_snapshot()
        return updated

    @app.delete("/api/v1/strategies/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_strategy(strategy_id: str) -> Response:
        service.delete_strategy(strategy_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/strategies/{strategy_id}/assign",
        response_model=StrategyAssignmentResult,
    )
    async def assign_strategy(
        strategy_id: str,
        payload: StrategyAssignmentRequest,
    ) -> StrategyAssignmentResult:
        instances = await asyncio.to_thread(
            campaign_manager.apply_bound_strategy_change,
            payload.instance_ids,
            lambda: service.assign_strategy(strategy_id, payload.instance_ids),
            reason="strategy_binding_changed",
        )
        await asyncio.gather(*(runtime.reset_instance(instance.id) for instance in instances))
        await publish_snapshot()
        return StrategyAssignmentResult(strategy=service.get_strategy(strategy_id), instances=instances)

    @app.post("/api/v1/instances", response_model=AccountInstance, status_code=status.HTTP_201_CREATED)
    async def create_instance(payload: CreateInstanceRequest) -> AccountInstance:
        created = service.create_instance(payload)
        try:
            complete = selected.adapter == "mock" and created.mode is TradingMode.DEMO
            volume_ledger.set_complete(created.id, complete)
            created = service.set_volume_completeness(created.id, complete)
        except Exception:
            volume_ledger.remove(created.id)
            service.delete_instance(created.id)
            raise
        await publish_snapshot()
        return created

    @app.get("/api/v1/instances/{instance_id}", response_model=AccountInstance)
    def get_instance(instance_id: str) -> AccountInstance:
        return project_instance_session(service.get_instance(instance_id))

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/preview",
        response_model=BetaCampaignPreview,
    )
    async def preview_bound_strategy_execution(
        instance_id: str,
        _payload: BoundStrategyExecutionPreviewRequest,
    ) -> BetaCampaignPreview:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy execution requires a Live account")
        plan = strategy_run_plan(instance)
        session_id = f"session-{uuid4().hex}"
        view = await asyncio.to_thread(
            campaign_manager.preview_bound_strategy,
            instance_id,
            instance.strategy,
            plan.execution_target_quote_volume,
            vault.get(instance_id),
            session_id=session_id,
            target_mode=plan.target_mode.value,
            run_disposition=plan.run_disposition,
            strategy_target_quote=plan.strategy_target_quote_volume,
            baseline_lifetime_quote=plan.baseline_lifetime_quote_volume,
            owner_user_id=instance.owner_user_id,
        )
        return BetaCampaignPreview.model_validate(
            {
                **view.model_dump(),
                "warnings": ["所有订单固定为 POST_ONLY", "本次完成量仅以已核验成交账本为准"],
                "blockers": [],
            }
        )

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/execute",
        response_model=BetaCampaignView,
    )
    async def execute_bound_strategy_execution(
        instance_id: str,
        execution_id: str,
        payload: BoundStrategyExecutionExecuteRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy execution requires a Live account")
        preview = campaign_manager.get(instance_id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        if preview.strategy_id != instance.strategy_id or preview.strategy_version != instance.strategy.version:
            raise UnsafeOperation("bound strategy changed since preview; create a new preview and confirm again")
        try:
            return await asyncio.to_thread(
                campaign_manager.start,
                instance_id,
                execution_id,
                payload.confirmation,
                payload.risk_acknowledged,
                vault.get(instance_id),
            )
        finally:
            await publish_snapshot()

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/stop",
        response_model=BetaCampaignView,
    )
    async def stop_bound_strategy_execution(
        instance_id: str,
        execution_id: str,
        payload: BoundStrategyExecutionStopRequest,
    ) -> BetaCampaignView:
        preview = campaign_manager.get(instance_id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        try:
            return await asyncio.to_thread(campaign_manager.stop, instance_id, execution_id, payload.confirmation)
        finally:
            await publish_snapshot()

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/reconcile",
        response_model=BetaCampaignView,
    )
    async def reconcile_bound_strategy_execution(
        instance_id: str,
        execution_id: str,
        payload: BoundStrategyExecutionReconcileRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy reconciliation requires a Live account")
        preview = campaign_manager.get(instance_id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        try:
            return await asyncio.to_thread(
                campaign_manager.reconcile,
                instance_id,
                execution_id,
                payload.confirmation,
                vault.get(instance_id),
            )
        finally:
            await publish_snapshot()

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-executions",
        response_model=list[BetaCampaignView],
    )
    def list_bound_strategy_executions(instance_id: str) -> list[BetaCampaignView]:
        service.get_instance(instance_id)
        return [view for view in campaign_manager.list(instance_id) if view.strategy_id is not None]

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-runs",
        response_model=StrategyRunPage,
    )
    def list_strategy_runs(
        instance_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=128),
    ) -> StrategyRunPage:
        instance = service.get_instance(instance_id)
        rows, next_cursor = volume_ledger.list_sessions(
            instance.id,
            instance.mode.value,
            limit=limit,
            cursor=cursor,
        )
        items = [
            StrategyRunSummary.model_validate(
                {
                    "session_id": row["session_id"],
                    "strategy_id": row.get("strategy_id"),
                    "strategy_name": row.get("strategy_name"),
                    "strategy_version": row.get("strategy_version"),
                    "target_mode": row["target_mode"],
                    "started_at_ms": row["started_at_ms"],
                    "finished_at_ms": row.get("finished_at_ms"),
                    "status": row["status"],
                    "result": row.get("result"),
                    "result_reason": row.get("result_reason"),
                    "strategy_target_quote_volume": row["strategy_target_quote_volume"],
                    "execution_target_quote_volume": row["target_quote_volume"],
                    "verified_quote_volume": row["verified_quote_volume"],
                    "remaining_quote_volume": row["remaining_quote_volume"],
                    "baseline_lifetime_quote_volume": row["baseline_lifetime_quote_volume"],
                    "final_lifetime_quote_volume": row.get("final_lifetime_quote_volume"),
                    "starting_available_balance_quote": row.get("starting_available_balance_quote"),
                    "ending_available_balance_quote": row.get("ending_available_balance_quote"),
                    "available_balance_change_quote": row.get("available_balance_change_quote"),
                    "source_complete": row["source_complete"],
                    "stale": row["stale"],
                    "reconciliation_required": row["reconciliation_required"],
                }
            )
            for row in rows
        ]
        return StrategyRunPage(items=items, next_cursor=next_cursor)

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}",
        response_model=BetaCampaignView,
    )
    def get_bound_strategy_execution(instance_id: str, execution_id: str) -> BetaCampaignView:
        view = campaign_manager.get(instance_id, execution_id)
        if view.strategy_id is None:
            raise InstanceNotFound("bound strategy execution not found")
        return view

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/events",
        response_model=list[BetaCampaignEvent],
    )
    def bound_strategy_execution_events(instance_id: str, execution_id: str) -> list[BetaCampaignEvent]:
        view = campaign_manager.get(instance_id, execution_id)
        if view.strategy_id is None:
            raise InstanceNotFound("bound strategy execution not found")
        return campaign_manager.events(instance_id, execution_id)

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-monitor",
        response_model=StrategyMonitorSnapshot,
    )
    def strategy_monitor_snapshot(
        instance_id: str,
        session_id: str | None = Query(default=None, alias="sessionId", max_length=128),
        before_sequence: int | None = Query(default=None, alias="beforeSequence", ge=1),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> StrategyMonitorSnapshot:
        instance = service.get_instance(instance_id)
        return strategy_monitor.snapshot(
            instance_id,
            session_id=session_id,
            before_sequence=before_sequence,
            limit=limit,
            owner_user_id=instance.owner_user_id,
        )

    @app.get("/api/v1/instances/{instance_id}/strategy-monitor/events")
    async def strategy_monitor_events(
        instance_id: str,
        request: Request,
        session_id: str | None = Query(default=None, alias="sessionId", max_length=128),
        after: str | None = Query(default=None, max_length=256),
    ) -> StreamingResponse:
        instance = service.get_instance(instance_id)
        owner_user_id = instance.owner_user_id
        requested_cursor = request.headers.get("last-event-id") or after

        async def stream() -> AsyncIterator[str]:
            strategy_monitor.subscriber_opened()
            try:
                heartbeat_at = time.monotonic()
                initial = await asyncio.to_thread(
                    strategy_monitor.snapshot,
                    instance_id,
                    session_id=session_id,
                    limit=200,
                    owner_user_id=owner_user_id,
                )
                parsed = strategy_monitor.parse_cursor(requested_cursor)
                event_type = "snapshot"
                if parsed is not None:
                    generation, campaign_id, sequence = parsed
                    if (
                        generation != executor_generation
                        or campaign_id != initial.execution_id
                        or sequence > initial.projection_sequence
                    ):
                        event_type = "reset"
                        strategy_monitor.reset_recorded()
                last_campaign_id = initial.execution_id
                last_sequence = initial.projection_sequence
                yield _monitor_sse(
                    event_type,
                    initial.cursor,
                    {
                        "type": event_type,
                        "snapshot": initial,
                        "fromSequence": last_sequence,
                        "toSequence": last_sequence,
                    },
                )

                while not await request.is_disconnected():
                    await asyncio.sleep(0.25)
                    record = await asyncio.to_thread(campaign_journal.monitor_record, instance_id, session_id)
                    campaign_id = record.campaign_id if record is not None else None
                    if campaign_id != last_campaign_id:
                        reset = await asyncio.to_thread(
                            strategy_monitor.snapshot,
                            instance_id,
                            session_id=session_id,
                            limit=200,
                            owner_user_id=owner_user_id,
                        )
                        last_campaign_id = reset.execution_id
                        last_sequence = reset.projection_sequence
                        strategy_monitor.reset_recorded()
                        yield _monitor_sse(
                            "reset",
                            reset.cursor,
                            {"type": "reset", "snapshot": reset, "toSequence": last_sequence},
                        )
                        heartbeat_at = time.monotonic()
                        continue
                    journal_sequence = 0
                    projection_sequence = 0
                    if campaign_id is not None:
                        projection, _unused, journal_sequence = await asyncio.to_thread(
                            campaign_journal.monitor_read,
                            campaign_id,
                            None,
                            1,
                        )
                        projection_sequence = projection.projected_sequence if projection is not None else 0
                        if journal_sequence != projection_sequence or journal_sequence - last_sequence > 200:
                            reset = await asyncio.to_thread(
                                strategy_monitor.snapshot,
                                instance_id,
                                session_id=session_id,
                                limit=200,
                                owner_user_id=owner_user_id,
                            )
                            last_sequence = reset.projection_sequence
                            strategy_monitor.reset_recorded()
                            yield _monitor_sse(
                                "reset",
                                reset.cursor,
                                {"type": "reset", "snapshot": reset, "toSequence": last_sequence},
                            )
                            heartbeat_at = time.monotonic()
                            continue
                        if journal_sequence > last_sequence:
                            rows = await asyncio.to_thread(
                                campaign_journal.events_after,
                                campaign_id,
                                last_sequence,
                                200,
                            )
                            if (
                                not rows
                                or int(rows[0].get("sequence") or 0) != last_sequence + 1
                                or int(rows[-1].get("sequence") or 0) != journal_sequence
                            ):
                                reset = await asyncio.to_thread(
                                    strategy_monitor.snapshot,
                                    instance_id,
                                    session_id=session_id,
                                    limit=200,
                                    owner_user_id=owner_user_id,
                                )
                                last_sequence = reset.projection_sequence
                                strategy_monitor.reset_recorded()
                                yield _monitor_sse(
                                    "reset",
                                    reset.cursor,
                                    {"type": "reset", "snapshot": reset, "toSequence": last_sequence},
                                )
                                heartbeat_at = time.monotonic()
                                continue
                            from_sequence = last_sequence + 1
                            last_sequence = journal_sequence
                            delta = await asyncio.to_thread(
                                strategy_monitor.snapshot,
                                instance_id,
                                session_id=session_id,
                                limit=200,
                                event_rows=rows,
                                owner_user_id=owner_user_id,
                            )
                            delta_cursor = strategy_monitor.cursor(campaign_id, last_sequence)
                            yield _monitor_sse(
                                "delta",
                                delta_cursor,
                                {
                                    "type": "delta",
                                    "snapshot": delta,
                                    "fromSequence": from_sequence,
                                    "toSequence": last_sequence,
                                },
                            )
                            heartbeat_at = time.monotonic()
                            continue
                    if time.monotonic() - heartbeat_at >= 5:
                        now_ms = time.time_ns() // 1_000_000
                        cursor = (
                            strategy_monitor.cursor(campaign_id, last_sequence)
                            if campaign_id and last_sequence
                            else None
                        )
                        yield _monitor_sse(
                            "heartbeat",
                            cursor,
                            {
                                "type": "heartbeat",
                                "journalSequence": journal_sequence,
                                "projectionSequence": projection_sequence,
                                "serverTimeMs": now_ms,
                            },
                        )
                        heartbeat_at = time.monotonic()
            finally:
                strategy_monitor.subscriber_closed()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/preview",
        response_model=BetaCampaignPreview,
    )
    async def preview_beta_campaign(instance_id: str, payload: BetaCampaignPreviewRequest) -> BetaCampaignPreview:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("Beta Campaign requires a Live account")
        view = await asyncio.to_thread(
            campaign_manager.preview,
            instance_id,
            payload,
            vault.get(instance_id),
            owner_user_id=instance.owner_user_id,
        )
        return BetaCampaignPreview.model_validate(
            {
                **view.model_dump(),
                "warnings": ["网页端首版仅允许单账号、单 campaign", "所有订单固定为 POST_ONLY"],
                "blockers": [],
            }
        )

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/execute",
        response_model=BetaCampaignView,
    )
    async def execute_beta_campaign(
        instance_id: str,
        campaign_id: str,
        payload: BetaCampaignExecuteRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("Beta Campaign requires a Live account")
        return await asyncio.to_thread(
            campaign_manager.start,
            instance_id,
            campaign_id,
            payload.confirmation,
            payload.risk_acknowledged,
            vault.get(instance_id),
        )

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/stop",
        response_model=BetaCampaignView,
    )
    async def stop_beta_campaign(
        instance_id: str,
        campaign_id: str,
        payload: BetaCampaignStopRequest,
    ) -> BetaCampaignView:
        service.get_instance(instance_id)
        return await asyncio.to_thread(campaign_manager.stop, instance_id, campaign_id, payload.confirmation)

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/reconcile",
        response_model=BetaCampaignView,
    )
    async def reconcile_beta_campaign(
        instance_id: str,
        campaign_id: str,
        payload: BetaCampaignReconcileRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("Beta Campaign reconciliation requires a Live account")
        return await asyncio.to_thread(
            campaign_manager.reconcile,
            instance_id,
            campaign_id,
            payload.confirmation,
            vault.get(instance_id),
        )

    @app.get(
        "/api/v1/instances/{instance_id}/beta-campaigns",
        response_model=list[BetaCampaignView],
    )
    def list_beta_campaigns(instance_id: str) -> list[BetaCampaignView]:
        service.get_instance(instance_id)
        return campaign_manager.list(instance_id)

    @app.get(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}",
        response_model=BetaCampaignView,
    )
    def get_beta_campaign(instance_id: str, campaign_id: str) -> BetaCampaignView:
        return campaign_manager.get(instance_id, campaign_id)

    @app.get(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/events",
        response_model=list[BetaCampaignEvent],
    )
    def beta_campaign_events(instance_id: str, campaign_id: str) -> list[BetaCampaignEvent]:
        return campaign_manager.events(instance_id, campaign_id)

    @app.patch("/api/v1/instances/{instance_id}", response_model=AccountInstance)
    async def update_instance(instance_id: str, payload: UpdateInstanceRequest) -> AccountInstance:
        previous = service.get_instance(instance_id)
        updated = service.update_instance(instance_id, payload)
        await runtime.reset_instance(instance_id)
        history_start_changed = (
            "history_start_at_ms" in payload.model_fields_set
            and payload.history_start_at_ms != previous.history_start_at_ms
        )
        if selected.adapter == "weex-readonly" and (payload.credentials is not None or history_start_changed):
            volume_ledger.remove(instance_id)
            reason = "credentials changed" if payload.credentials is not None else "history start changed"
            updated = service.reset_telemetry_snapshot(instance_id, reason)
        await publish_snapshot()
        return updated

    @app.post("/api/v1/instances/{instance_id}/actions/{action}", response_model=AccountInstance)
    async def apply_action(instance_id: str, action: InstanceAction) -> AccountInstance:
        pending_plan = strategy_run_plan(service.get_instance(instance_id)) if action is InstanceAction.START else None
        try:
            updated = await runtime.apply_action(instance_id, action)
            active_session = volume_ledger.active_session(updated.id, updated.mode.value)
            if action is InstanceAction.START and pending_plan is not None:
                session_volume.start(
                    session_id=f"session-{uuid4().hex}",
                    account_id=updated.id,
                    mode=updated.mode.value,
                    started_at_ms=time.time_ns() // 1_000_000,
                    target_quote_volume=pending_plan.execution_target_quote_volume,
                    maker_only_required=True,
                    strategy_id=updated.strategy.id,
                    strategy_name=updated.strategy.name,
                    strategy_version=updated.strategy.version,
                    target_mode=pending_plan.target_mode.value,
                    strategy_target_quote_volume=pending_plan.strategy_target_quote_volume,
                    baseline_lifetime_quote_volume=pending_plan.baseline_lifetime_quote_volume,
                )
            elif action in {InstanceAction.PAUSE, InstanceAction.STOP} and active_session is not None:
                aggregate = volume_ledger.aggregate(updated.id, 0)
                session_volume.finalize(
                    str(active_session["session_id"]),
                    result="stopped",
                    reason=f"manual_{action.value}",
                    finished_at_ms=time.time_ns() // 1_000_000,
                    final_lifetime_quote_volume=aggregate.lifetime,
                )
            return project_instance_session(updated)
        finally:
            await publish_snapshot()

    @app.post("/api/v1/instances/{instance_id}/positions/close", response_model=AccountInstance)
    async def close_positions(instance_id: str) -> AccountInstance:
        if campaign_journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("cannot close positions while a Beta Campaign is active")
        try:
            return await runtime.close_positions(instance_id)
        finally:
            await publish_snapshot()

    @app.post(
        "/api/v1/instances/{instance_id}/volume-sessions",
        response_model=VolumeSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_volume_session(instance_id: str, payload: VolumeSessionCreateRequest) -> VolumeSessionResponse:
        instance = service.get_instance(instance_id)
        started_at_ms = payload.started_at_ms or (time.time_ns() // 1_000_000)
        projection = session_volume.start(
            session_id=payload.session_id,
            account_id=instance.id,
            mode=instance.mode.value,
            started_at_ms=started_at_ms,
            target_quote_volume=payload.target_quote_volume,
            maker_only_required=payload.maker_only_required,
        )
        await publish_snapshot()
        return VolumeSessionResponse.model_validate(projection)

    def owned_volume_session(session_id: str):
        session = volume_ledger.get_session(session_id)
        if session is None:
            raise InstanceNotFound("volume session not found")
        # The account lookup is also the ownership authorization check.
        service.get_instance(session.account_id)
        return session

    @app.get("/api/v1/volume-sessions/{session_id}", response_model=VolumeSessionResponse)
    def get_volume_session(session_id: str) -> VolumeSessionResponse:
        owned_volume_session(session_id)
        return VolumeSessionResponse.model_validate(session_volume.progress(session_id))

    @app.get("/api/v1/volume-sessions/{session_id}/fills", response_model=list[dict[str, object]])
    def get_volume_session_fills(session_id: str) -> list[dict[str, object]]:
        session = owned_volume_session(session_id)
        return [
            {
                "fill_id": fill.identity,
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "executed_at_ms": fill.executed_at_ms,
                "quote_volume": str(fill.quote_volume),
                "base_quantity": str(fill.base_quantity),
                "side": fill.side,
                "position_side": fill.position_side,
                "position_action": fill.position_action,
                "maker": fill.maker,
                "commission": str(fill.commission),
                "commission_asset": fill.commission_asset,
                "realized_pnl": str(fill.realized_pnl),
                "source": fill.source,
                "authoritative": fill.authoritative,
                "created_at_ms": fill.created_at_ms,
            }
            for fill in volume_ledger.fills_for_account(session.account_id, session.mode, session.started_at_ms)
        ]

    @app.post("/api/v1/volume-sessions/{session_id}/sync", response_model=VolumeSessionResponse)
    async def sync_volume_session(session_id: str) -> VolumeSessionResponse:
        session = owned_volume_session(session_id)
        await runtime.refresh_instance(session.account_id)
        checkpoint = volume_ledger.sync_checkpoint(session.account_id, session.mode) or {}
        volume_ledger.update_session(
            session_id,
            cursor=checkpoint.get("cursor"),
            high_watermark_ms=checkpoint.get("high_watermark_ms"),
        )
        projection = session_volume.progress(session_id)
        await publish_snapshot()
        return VolumeSessionResponse.model_validate(projection)

    @app.post("/api/v1/volume-sessions/{session_id}/reconcile", response_model=VolumeSessionResponse)
    async def reconcile_volume_session(session_id: str) -> VolumeSessionResponse:
        session = owned_volume_session(session_id)
        now_ms = time.time_ns() // 1_000_000
        try:
            fills, complete, reason = await runtime.authoritative_session_fills(
                session.account_id, session.started_at_ms, now_ms
            )
        except Exception:
            volume_ledger.update_session(
                session_id,
                last_reconciliation_at_ms=now_ms,
                source_complete=False,
                stale=True,
                reconciliation_required=True,
                pending_sync=False,
            )
            projection = session_volume.progress(session_id)
        else:
            if complete:
                projection = session_volume.reconcile(session_id, fills, reconciled_at_ms=now_ms)
                refreshed_session = volume_ledger.get_session(session_id)
                if (
                    refreshed_session is not None
                    and refreshed_session.result in {"completed", "stopped"}
                    and not projection["reconciliation_required"]
                ):
                    aggregate = volume_ledger.aggregate(session.account_id, 0)
                    projection = session_volume.finalize(
                        session_id,
                        result=refreshed_session.result,
                        reason=refreshed_session.result_reason,
                        finished_at_ms=refreshed_session.finished_at_ms or now_ms,
                        final_lifetime_quote_volume=aggregate.lifetime,
                    )
            else:
                volume_ledger.update_session(
                    session_id,
                    last_reconciliation_at_ms=now_ms,
                    source_complete=False,
                    stale=True,
                    reconciliation_required=True,
                    pending_sync=False,
                    status="verification_pending",
                    result_reason=f"session_source_incomplete:{reason}"[:160],
                )
                projection = session_volume.progress(session_id)
        await publish_snapshot()
        return VolumeSessionResponse.model_validate(projection)

    @app.get("/api/v1/instances/{instance_id}/volume-history", response_model=dict[str, object])
    def account_volume_history(instance_id: str) -> dict[str, object]:
        instance = service.get_instance(instance_id)
        return volume_ledger.account_summary(instance.id, instance.mode.value)

    @app.post("/api/v1/instances/{instance_id}/refresh", response_model=AccountInstance)
    async def refresh_instance(instance_id: str) -> AccountInstance:
        updated = await runtime.refresh_instance(instance_id)
        await publish_snapshot()
        return updated

    @app.get("/api/v1/instances/{instance_id}/logs", response_model=list[LogLine])
    def instance_logs(instance_id: str, limit: int = Query(default=200, ge=1, le=500)) -> list[LogLine]:
        return combined_log_updates(instance_id, limit, None).lines

    @app.get("/api/v1/instances/{instance_id}/log-updates", response_model=LogBatch)
    def instance_log_updates(
        instance_id: str,
        limit: int = Query(default=200, ge=1, le=500),
        after: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> LogBatch:
        return combined_log_updates(instance_id, limit, after)

    @app.delete("/api/v1/instances/{instance_id}/log-updates", status_code=status.HTTP_204_NO_CONTENT)
    def clear_instance_logs(instance_id: str) -> Response:
        service.clear_logs(instance_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/instances/{instance_id}/executions", response_model=list[ExecutionCycleView])
    def instance_executions(
        instance_id: str,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[ExecutionCycleView]:
        service.get_instance(instance_id)
        return [_execution_cycle_view(record) for record in execution_journal.list_recent(instance_id, limit)]

    @app.delete("/api/v1/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_instance(instance_id: str) -> Response:
        if campaign_journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("stop the active Beta Campaign before deleting the account")
        service.delete_instance(instance_id)
        await runtime.remove_instance(instance_id)
        volume_ledger.remove(instance_id)
        campaign_journal.remove(instance_id)
        await publish_snapshot()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/actions/stop-all", response_model=GlobalStopResult)
    async def stop_all(payload: GlobalStopRequest) -> GlobalStopResult:
        result = await runtime.stop_all(payload.confirmation)
        await publish_snapshot()
        return result

    @app.get("/api/v1/events")
    async def instance_events(request: Request) -> StreamingResponse:
        owner_user_id = request.headers.get("X-Fleet-User", "").strip() or None
        queue = await broker.subscribe(owner_user_id)

        async def stream() -> AsyncIterator[str]:
            try:
                with_owner = set_current_owner_user_id(owner_user_id)
                try:
                    initial_instances = projected_instances()
                finally:
                    reset_current_owner_user_id(with_owner)
                owned_ids = {instance.id for instance in initial_instances}
                campaigns = [
                    item
                    for item in campaign_manager.public_snapshot()
                    if owner_user_id is None
                    or str(item.get("instanceId", item.get("instance_id", ""))) in owned_ids
                ]
                yield broker.sse_message(broker.snapshot_payload(initial_instances, runtime.metrics(), campaigns))
                while not await request.is_disconnected():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                    else:
                        yield broker.sse_message(payload)
            finally:
                await broker.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _monitor_sse(event_type: str, cursor: str | None, payload: dict[str, object]) -> str:
    encoded = json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":"))
    cursor_line = f"id: {cursor}\n" if cursor else ""
    return f"{cursor_line}event: {event_type}\ndata: {encoded}\n\n"


def _execution_cycle_view(record: ExecutionRecord) -> ExecutionCycleView:
    return ExecutionCycleView(
        cycle_id=record.plan.cycle_id,
        sequence=record.plan.sequence,
        status=record.status.value,
        reason=record.reason,
        total_quote=str(record.plan.total_quote),
        turnover_quote=str(record.plan.turnover_quote),
        btc_long_quote=str(record.plan.btc_long_quote),
        eth_short_quote=str(record.plan.eth_short_quote),
        allocation_version=record.plan.allocation_version,
        position_hold_seconds=record.plan.position_hold_seconds,
        round_interval_seconds=record.plan.round_interval_seconds,
        sizing_mode=record.plan.sizing_mode,
        strategy_id=record.plan.strategy_id,
        created_at_ms=record.created_at_ms,
        updated_at_ms=record.updated_at_ms,
        reconciliation_required=record.status is CycleExecutionStatus.UNCERTAIN,
        retry_allowed=False,
    )


def run() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000, reload=False)
