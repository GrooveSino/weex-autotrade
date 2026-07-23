"""HTTP middleware and app-state wiring for Fleet."""

from __future__ import annotations

import hashlib

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from .main_context import FleetAppContext
from .ownership import reset_current_owner_user_id, set_current_owner_user_id
from .service import InstanceNotFound


def install_http_support(app: FastAPI, ctx: FleetAppContext) -> None:
    app.state.fleet_service = ctx.service
    app.state.fleet_repository = ctx.repository
    app.state.credential_vault = ctx.vault
    app.state.instance_event_broker = ctx.broker
    app.state.account_runtime = ctx.runtime
    app.state.trade_volume_ledger = ctx.volume_ledger
    app.state.execution_journal = ctx.execution_journal
    app.state.execution_coordinator = ctx.execution_coordinator
    app.state.pair_allocation_provider = ctx.selected_allocation_provider
    app.state.beta_market_provider = ctx.beta_source_runtime
    app.state.beta_source_runtime = ctx.beta_source_runtime
    app.state.session_volume = ctx.session_volume
    app.state.campaign_journal = ctx.campaign_journal
    app.state.campaign_manager = ctx.campaign_manager
    app.state.bound_strategy_recovery = ctx.bound_strategy_recovery
    app.state.executor_generation = ctx.executor_generation
    app.state.executor_release_id = ctx.executor_release_id
    app.state.command_ledger = ctx.command_ledger

    @app.middleware("http")
    async def executor_request_owner(request: Request, call_next):
        if not (ctx.require_command_id and ctx.selected.local_user_auth_required):
            return await call_next(request)
        if request.url.path in {"/_internal/executor-health", "/api/v1/health"}:
            return await call_next(request)
        user_id = request.headers.get("X-Fleet-User", "").strip()
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not user_id or len(user_id) > 48 or any(char not in allowed for char in user_id):
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "local login is required"})
        token = set_current_owner_user_id(user_id)
        try:
            parts = [part for part in request.url.path.split("/") if part]
            if len(parts) >= 4 and parts[:3] == ["api", "v1", "instances"] and parts[3] != "missing":
                try:
                    ctx.service.get_instance(parts[3])
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
            if ctx.require_command_id:
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
        existing = ctx.command_ledger.claim(ledger_command_id, fingerprint)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return JSONResponse(status_code=409, content={"detail": "command id conflicts with a different request"})
            return JSONResponse(
                status_code=409,
                content={"detail": "command already accepted; query account or campaign state instead of retrying"},
            )
        response = await call_next(request)
        ctx.command_ledger.complete(ledger_command_id)
        return response
