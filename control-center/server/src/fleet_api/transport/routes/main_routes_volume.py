"""Explicit account-scoped history imports and turnover-report endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response, status

from fleet_api.bootstrap.main_context import FleetAppContext
from fleet_api.models import AccountTradeVolumeReportResponse
from fleet_api.volume.reports import AccountTradeVolumeReportError


def register_trade_volume_report_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    async def run_account_trade_volume_report(
        instance_id: str,
        lookback_days: list[int],
    ) -> AccountTradeVolumeReportResponse:
        if ctx.selected.adapter == "mock":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="当前页面使用 Mock 数据，无法读取真实 WEEX 成交历史。请切换到已连接的实盘控制平面后重试。",
            )
        instance = ctx.service.get_instance(instance_id)
        try:
            report = await ctx.account_trade_volume_report_service.report(
                instance,
                ctx.vault.get(instance.id),
                lookback_days,
            )
            await ctx.publish_snapshot()
            return report
        except AccountTradeVolumeReportError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{exc.message} 下一步：{exc.action}",
            ) from exc

    @app.post(
        "/api/v1/instances/{instance_id}/trade-volume-report",
        response_model=AccountTradeVolumeReportResponse,
    )
    async def import_account_trade_volume_report(
        instance_id: str,
        lookback_days: Annotated[list[int], Query(min_length=1, max_length=3)],
        response: Response,
    ) -> AccountTradeVolumeReportResponse:
        response.headers["Cache-Control"] = "no-store"
        return await run_account_trade_volume_report(instance_id, lookback_days)

    @app.get(
        "/api/v1/instances/{instance_id}/trade-volume-report",
        response_model=AccountTradeVolumeReportResponse,
        deprecated=True,
    )
    async def legacy_account_trade_volume_report(
        instance_id: str,
        lookback_days: Annotated[list[int], Query(min_length=1, max_length=3)],
        response: Response,
    ) -> AccountTradeVolumeReportResponse:
        response.headers["Cache-Control"] = "no-store"
        return await run_account_trade_volume_report(instance_id, lookback_days)
