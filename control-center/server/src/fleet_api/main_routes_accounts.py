from __future__ import annotations

import asyncio

from fastapi import FastAPI, Response, status

from .instance_projection import project_instance_session
from .main_context import FleetAppContext
from .models import (
    AccountInstance,
    CreateInstanceRequest,
    StrategyAssignmentRequest,
    StrategyAssignmentResult,
    TradingMode,
    VolumeStrategy,
    VolumeStrategyInput,
)


def register_account_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    selected = ctx.selected
    service = ctx.service
    volume_ledger = ctx.volume_ledger
    runtime = ctx.runtime
    campaign_manager = ctx.campaign_manager
    strategy_monitor = ctx.strategy_monitor
    publish_snapshot = ctx.publish_snapshot
    projected_instances = ctx.projected_instances
    trade_history_scheduler = ctx.trade_history_scheduler
    strategy_run_lifecycle = ctx.strategy_run_lifecycle

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
            trade_history_scheduler.queue_initial_baseline(created)
        except Exception:
            volume_ledger.remove(created.id)
            service.delete_instance(created.id)
            raise
        await publish_snapshot()
        return created

    @app.get("/api/v1/instances/{instance_id}", response_model=AccountInstance)
    def get_instance(instance_id: str) -> AccountInstance:
        return project_instance_session(
            service.get_instance(instance_id),
            volume_ledger,
            strategy_monitor,
            strategy_run_lifecycle,
        )
