from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .beta_allocation import HttpBetaAllocationProvider
from .campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
    SQLiteCampaignJournal,
)
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
    BetaCampaignStopRequest,
    BetaCampaignView,
    BetaMarketSnapshot,
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
    StrategyTargetMode,
    TradingMode,
    UpdateInstanceRequest,
    VolumeSessionCreateRequest,
    VolumeSessionResponse,
    VolumeStrategy,
    VolumeStrategyInput,
)
from .repository import AccountRepository, InMemoryAccountRepository, SQLiteAccountRepository
from .runtime import AccountRuntimeManager
from .seed import ensure_mock_volume_baselines, seed_mock_instances
from .service import FleetControlService, FleetError, InstanceNotFound, TelemetryUnavailable, UnsafeOperation
from .telemetry import AccountTelemetryAdapterFactory, MockAccountTelemetryAdapterFactory
from .vault import CredentialVault, EncryptedSQLiteCredentialVault, EphemeralCredentialVault
from .volume_history import InMemoryTradeVolumeLedger, SessionVolumeService, SQLiteTradeVolumeLedger, TradeVolumeLedger
from .weex_readonly import WeexReadonlyAccountTelemetryAdapterFactory


def create_app(
    settings: ControlPlaneSettings | None = None,
    adapter_factory: AccountTelemetryAdapterFactory | None = None,
    allocation_provider: PairAllocationProvider | None = None,
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
    else:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        volume_ledger = InMemoryTradeVolumeLedger()
        execution_journal = InMemoryExecutionJournal()
    had_persisted_instances = bool(repository.list())
    service = FleetControlService(
        repository,
        vault,
        adapter=selected.adapter,
        mock_cycle_total_quote=selected.mock_cycle_total_quote,
    )
    broker = InstanceEventBroker()
    execution_journal.recover_incomplete()
    execution_coordinator: PairedCycleCoordinator | None = None
    selected_allocation_provider: PairAllocationProvider | None = None
    beta_market_provider = HttpBetaAllocationProvider(
        selected.beta_ratio_url,
        timeout_seconds=selected.beta_ratio_timeout_seconds,
        cache_seconds=selected.beta_refresh_interval_seconds,
        network_on_demand=not selected.beta_background_refresh_enabled,
    )
    campaign_journal = (
        SQLiteCampaignJournal(selected.sqlite_path) if selected.storage == "sqlite" else InMemoryCampaignJournal()
    )
    event_loop: asyncio.AbstractEventLoop | None = None

    def notify_campaign_change(_instance_id: str) -> None:
        if event_loop is None or not event_loop.is_running():
            return
        event_loop.call_soon_threadsafe(lambda: asyncio.create_task(publish_snapshot()))

    campaign_manager = CampaignWorkerManager(
        selected,
        vault,
        campaign_journal,
        lambda: HttpBetaAllocationProvider(
            selected.beta_ratio_url,
            timeout_seconds=selected.beta_ratio_timeout_seconds,
            allow_low_confidence=True,
        ),
        on_change=notify_campaign_change,
    )
    campaign_manager.recover()
    if selected.adapter == "mock":
        selected_allocation_provider = allocation_provider or beta_market_provider
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
        available = await beta_market_provider.refresh()
        changed = 0
        if selected_allocation_provider is beta_market_provider:
            changed = await runtime.reconcile_beta_availability(
                available,
                getattr(beta_market_provider, "last_refresh_error", None),
            )
        if changed:
            await publish_snapshot()
        return available

    async def beta_refresh_loop() -> None:
        while True:
            await asyncio.sleep(beta_market_provider.seconds_until_refresh(selected.beta_refresh_interval_seconds))
            await refresh_beta_state()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal event_loop
        event_loop = asyncio.get_running_loop()
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
            campaign_manager.close()
            event_loop = None
            if selected_allocation_provider is not beta_market_provider:
                await beta_market_provider.aclose()
            execution_journal.close()
            volume_ledger.close()
            vault.close()
            repository.close()

    app = FastAPI(
        title="WEEX Fleet Control Plane",
        version="0.1.0",
        description="Mock execution or WEEX read-only telemetry. This process cannot submit live WEEX orders.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(selected.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
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
    app.state.beta_market_provider = beta_market_provider
    app.state.session_volume = session_volume
    app.state.campaign_journal = campaign_journal
    app.state.campaign_manager = campaign_manager

    def project_instance_session(instance: AccountInstance) -> AccountInstance:
        projection = volume_ledger.latest_session(instance.id, instance.mode.value)
        if projection is None:
            return instance
        return instance.model_copy(
            update={
                "volume": instance.volume.model_copy(
                    update={"session": SessionVolumeProjection.model_validate(projection)}
                )
            }
        )

    def projected_instances() -> list[AccountInstance]:
        return [project_instance_session(instance) for instance in service.list_instances()]

    @app.exception_handler(FleetError)
    async def fleet_error_handler(_request: Request, exc: FleetError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        redacted = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": redacted})

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            adapter=selected.adapter,
            storage=selected.storage,
            live_trading_enabled=selected.live_trading_enabled,
            execution_enabled=selected.adapter == "mock",
            live_campaigns_enabled=selected.adapter == "weex-live" and selected.live_campaigns_enabled,
            live_campaign_worker_count=(selected.live_campaign_worker_count if selected.adapter == "weex-live" else 0),
        )

    @app.get("/api/v1/runtime/metrics", response_model=SchedulerMetrics)
    def runtime_metrics() -> SchedulerMetrics:
        return runtime.metrics()

    @app.get("/api/v1/beta", response_model=BetaMarketSnapshot)
    async def beta_snapshot() -> BetaMarketSnapshot:
        try:
            return await app.state.beta_market_provider.market_snapshot()
        except AllocationUnavailable as exc:
            raise TelemetryUnavailable(f"beta snapshot unavailable: {exc.reason_code}") from None

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
        updated = service.update_strategy(strategy_id, payload)
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
        instances = service.assign_strategy(strategy_id, payload.instance_ids)
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
        try:
            updated = await runtime.apply_action(instance_id, action)
            latest_session = volume_ledger.latest_session(updated.id, updated.mode.value)
            if (
                action is InstanceAction.START
                and updated.strategy.target_mode is StrategyTargetMode.INCREMENTAL
                and (latest_session is None or latest_session.get("status") == "completed")
            ):
                session_volume.start(
                    session_id=f"session-{uuid4().hex}",
                    account_id=updated.id,
                    mode=updated.mode.value,
                    started_at_ms=time.time_ns() // 1_000_000,
                    target_quote_volume=updated.strategy.target_volume_quote,
                    maker_only_required=True,
                )
            elif action is InstanceAction.START and latest_session is not None:
                volume_ledger.update_session(str(latest_session["session_id"]), status="running")
            elif action in {InstanceAction.PAUSE, InstanceAction.STOP} and latest_session is not None:
                volume_ledger.update_session(str(latest_session["session_id"]), status=action.value)
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

    @app.get("/api/v1/volume-sessions/{session_id}", response_model=VolumeSessionResponse)
    def get_volume_session(session_id: str) -> VolumeSessionResponse:
        try:
            return VolumeSessionResponse.model_validate(session_volume.progress(session_id))
        except KeyError:
            raise InstanceNotFound("volume session not found") from None

    @app.get("/api/v1/volume-sessions/{session_id}/fills", response_model=list[dict[str, object]])
    def get_volume_session_fills(session_id: str) -> list[dict[str, object]]:
        session = volume_ledger.get_session(session_id)
        if session is None:
            raise InstanceNotFound("volume session not found")
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
        session = volume_ledger.get_session(session_id)
        if session is None:
            raise InstanceNotFound("volume session not found")
        await runtime.refresh_instance(session.account_id)
        checkpoint = volume_ledger.sync_checkpoint(session.account_id, session.mode) or {}
        volume_ledger.update_session(
            session_id,
            last_sync_at_ms=time.time_ns() // 1_000_000,
            cursor=checkpoint.get("cursor"),
            high_watermark_ms=checkpoint.get("high_watermark_ms"),
            pending_sync=bool(checkpoint.get("pending", False)),
            source_complete=bool(checkpoint.get("source_complete", False)),
            stale=bool(checkpoint.get("stale", True)),
        )
        projection = session_volume.progress(session_id)
        await publish_snapshot()
        return VolumeSessionResponse.model_validate(projection)

    @app.post("/api/v1/volume-sessions/{session_id}/reconcile", response_model=VolumeSessionResponse)
    async def reconcile_volume_session(session_id: str) -> VolumeSessionResponse:
        session = volume_ledger.get_session(session_id)
        if session is None:
            raise InstanceNotFound("volume session not found")
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
            else:
                volume_ledger.update_session(
                    session_id,
                    last_reconciliation_at_ms=now_ms,
                    source_complete=False,
                    stale=True,
                    reconciliation_required=True,
                    pending_sync=False,
                    status=f"uncertain:{reason}",
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
        return service.logs(instance_id, limit)

    @app.get("/api/v1/instances/{instance_id}/log-updates", response_model=LogBatch)
    def instance_log_updates(
        instance_id: str,
        limit: int = Query(default=200, ge=1, le=500),
        after: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> LogBatch:
        return service.log_updates(instance_id, limit, after)

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
        queue = await broker.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                yield broker.sse_message(
                    broker.snapshot_payload(
                        projected_instances(), runtime.metrics(), campaign_manager.public_snapshot()
                    )
                )
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


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("fleet_api.main:app", host="127.0.0.1", port=8000, reload=False)
