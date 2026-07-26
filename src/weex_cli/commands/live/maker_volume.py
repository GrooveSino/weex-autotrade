"""Exact-confirmation command for alternating flat-to-flat live Maker volume."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from weex_cli.cli_support import app_context, gateway_for, invoke, profile_for, settings_for
from weex_cli.core.safety import require_execution
from weex_cli.presentation.human import render_live_volume_event
from weex_cli.presentation.output import emit
from weex_cli.volume.live import (
    DEFAULT_PLAN_DIRECTORY as DEFAULT_LIVE_VOLUME_PLAN_DIRECTORY,
)
from weex_cli.volume.live import (
    LiveMakerVolumePlan,
    LiveMakerVolumePlanStore,
    LiveMakerVolumeService,
    live_maker_volume_confirmation,
    plan_payload,
)

from .app import app


@app.command(
    "maker-volume",
    help="Plan by default; execute an alternating flat-to-flat pure-Maker volume session with --execute.",
)
def maker_volume(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Option(help="Contract base asset", rich_help_panel="Plan")] = "BTC",
    target: Annotated[
        str, typer.Option(help="Authoritative Maker turnover target in USDT", rich_help_panel="Plan")
    ] = "5000",
    round_quote: Annotated[
        str,
        typer.Option(
            "--round",
            help="Approximate opening + closing turnover per flat-to-flat round",
            rich_help_panel="Plan",
        ),
    ] = "500",
    timeout: Annotated[
        int,
        typer.Option(min=1, help="Maximum time for each adaptive Maker attempt", rich_help_panel="Plan"),
    ] = 120,
    leverage: Annotated[
        int,
        typer.Option(
            min=1,
            max=125,
            help="Already configured isolated leverage; used only for funding checks",
            rich_help_panel="Plan",
        ),
    ] = 1,
    recovery_attempts: Annotated[
        int,
        typer.Option(
            min=1,
            max=10,
            help="Maximum confirmed Maker close attempts after partial fills",
            rich_help_panel="Recovery",
        ),
    ] = 3,
    max_empty_rounds: Annotated[
        int,
        typer.Option(
            min=0,
            max=20,
            help="Consecutive no-fill round attempts allowed before stopping",
            rich_help_panel="Recovery",
        ),
    ] = 3,
    cooldown: Annotated[
        float,
        typer.Option(min=0, max=300, help="Seconds between flat rounds", rich_help_panel="Recovery"),
    ] = 1.0,
    plan_id: Annotated[
        str | None,
        typer.Option("--plan-id", "--plan", help="Reviewed plan to execute", rich_help_panel="Execution gate"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Enable execution of the reviewed plan", rich_help_panel="Execution gate"),
    ] = False,
    confirm: Annotated[
        str, typer.Option(help="Exact phrase printed by the plan", rich_help_panel="Execution gate")
    ] = "",
    progress: Annotated[
        bool,
        typer.Option("--progress/--no-progress", help="Show concise execution events", rich_help_panel="Output"),
    ] = True,
    plan_directory: Annotated[Path, typer.Option(hidden=True)] = DEFAULT_LIVE_VOLUME_PLAN_DIRECTORY,
    json_output: Annotated[
        bool, typer.Option("--json", help="Stable machine-readable output", rich_help_panel="Output")
    ] = False,
) -> None:
    store = LiveMakerVolumePlanStore(plan_directory)
    if execute:
        if not plan_id:
            raise typer.BadParameter("--plan-id is required with --execute")

        def action() -> dict[str, object]:
            plan = store.load(plan_id)
            require_execution(
                execute=True,
                supplied=confirm,
                expected=live_maker_volume_confirmation(plan),
                mode="live",
                settings=settings_for(ctx),
            )
            if app_context(ctx).profile_file is not None:
                profile_for(ctx).require_maker_execution()
            gateway = gateway_for(ctx)
            progress_console = Console(stderr=True)
            event_sink = (
                (lambda event: render_live_volume_event(event, progress_console))
                if progress and not json_output
                else None
            )
            return LiveMakerVolumeService(gateway, store, event_sink=event_sink).execute(plan)

        payload = invoke(action)
        emit(payload, json_output=json_output)
        if payload["status"] != "completed":
            raise typer.Exit(1)
        return
    if plan_id:
        raise typer.BadParameter("--plan-id is only valid with --execute")

    def make_plan() -> dict[str, object]:
        gateway = gateway_for(ctx)
        plan = LiveMakerVolumePlan.create(
            gateway,
            symbol=symbol,
            target_quote=target,
            round_quote=round_quote,
            timeout_seconds=timeout,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=cooldown,
            leverage=leverage,
        )
        path = store.create(plan)
        return plan_payload(plan, path)

    emit(invoke(make_plan), json_output=json_output)
