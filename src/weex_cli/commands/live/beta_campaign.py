"""Durable campaign planning and exact-confirmation command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from weex_cli.beta_campaign import (
    DEFAULT_CAMPAIGN_DIRECTORY,
    DEFAULT_CHILD_PLAN_DIRECTORY,
    campaign_execute_command,
    campaign_id_from_confirmation,
)
from weex_cli.beta_campaign.allocation import DEFAULT_BETA_URL, HttpBetaAllocationProvider
from weex_cli.beta_campaign.workflow import (
    BetaCampaignApplication,
    CampaignPreviewRequest,
    CampaignRuntimePaths,
)
from weex_cli.cli_support import gateway_for, invoke, profile_for
from weex_cli.core.errors import ValidationError
from weex_cli.presentation.human import TerminalExecutionProgress
from weex_cli.presentation.output import emit

from .app import app

SECONDS_PER_MINUTE = 60.0


@app.command(
    "beta-campaign",
    help="Authorize once, then run up to 20 bounded pure-Maker Beta sessions from flat boundaries.",
)
def beta_campaign(
    ctx: typer.Context,
    target: Annotated[str, typer.Option(help="Authoritative Maker turnover target in USDT")] = "3000",
    cycle_volume: Annotated[
        str,
        typer.Option(
            "--cycle-volume",
            help="Approximate BTC+ETH opening and closing turnover per flat cycle",
        ),
    ] = "300",
    hold_min_minutes: Annotated[
        float,
        typer.Option(
            "--hold-min",
            min=0,
            max=60,
            metavar="分钟",
            help="Minimum minutes to hold the confirmed open pair",
        ),
    ] = 0.0,
    hold_max_minutes: Annotated[
        float,
        typer.Option(
            "--hold-max",
            min=0,
            max=60,
            metavar="分钟",
            help="Maximum minutes to hold the confirmed open pair",
        ),
    ] = 0.0,
    round_gap_min_minutes: Annotated[
        float,
        typer.Option(
            "--round-gap-min",
            min=0,
            max=60,
            metavar="分钟",
            help="Minimum minutes between confirmed flat cycles",
        ),
    ] = 1.0,
    round_gap_max_minutes: Annotated[
        float,
        typer.Option(
            "--round-gap-max",
            min=0,
            max=60,
            metavar="分钟",
            help="Maximum minutes between confirmed flat cycles",
        ),
    ] = 1.0,
    campaign_id: Annotated[str | None, typer.Option("--campaign-id", "--campaign", hidden=True)] = None,
    execute: Annotated[bool, typer.Option("--execute", help="Execute the reviewed campaign")] = False,
    confirm: Annotated[str, typer.Option(help="Exact campaign phrase printed by the dry run")] = "",
    beta_url: Annotated[str, typer.Option(hidden=True)] = DEFAULT_BETA_URL,
    campaign_directory: Annotated[Path, typer.Option(hidden=True)] = DEFAULT_CAMPAIGN_DIRECTORY,
    child_plan_directory: Annotated[Path, typer.Option(hidden=True)] = DEFAULT_CHILD_PLAN_DIRECTORY,
    progress: Annotated[bool, typer.Option("--progress/--no-progress")] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    profile = profile_for(ctx)
    application = BetaCampaignApplication(
        profile,
        CampaignRuntimePaths(campaigns=campaign_directory, plans=child_plan_directory),
        gateway_factory=lambda: gateway_for(ctx),
        provider_factory=lambda: HttpBetaAllocationProvider(beta_url),
    )
    if execute:
        try:
            confirmed_campaign_id = campaign_id_from_confirmation(confirm)
        except ValidationError as exc:
            raise typer.BadParameter(str(exc), param_hint="--confirm") from exc
        if campaign_id and campaign_id.lower() != confirmed_campaign_id:
            raise typer.BadParameter("--campaign-id does not match --confirm")

        def action() -> dict[str, object]:
            progress_console = Console(stderr=True)
            progress_renderer = TerminalExecutionProgress(progress_console) if progress and not json_output else None
            try:
                return application.execute(
                    confirmation=confirm,
                    campaign_id=confirmed_campaign_id,
                    event_sink=progress_renderer,
                )
            finally:
                if progress_renderer is not None:
                    progress_renderer.close()

        payload = invoke(action)
        emit(payload, json_output=json_output)
        if payload["status"] != "completed":
            raise typer.Exit(1)
        return
    if campaign_id:
        raise typer.BadParameter("--campaign-id is only valid with --execute")
    if confirm:
        raise typer.BadParameter("--confirm is only valid with --execute")

    def make_campaign() -> dict[str, object]:
        payload = application.preview(
            CampaignPreviewRequest(
                target_quote=target,
                cycle_volume=cycle_volume,
                hold_min_seconds=hold_min_minutes * SECONDS_PER_MINUTE,
                hold_max_seconds=hold_max_minutes * SECONDS_PER_MINUTE,
                round_gap_min_seconds=round_gap_min_minutes * SECONDS_PER_MINUTE,
                round_gap_max_seconds=round_gap_max_minutes * SECONDS_PER_MINUTE,
            )
        )
        campaign = application.load(str(payload["campaign"]["campaign_id"])).campaign
        payload["execute_command"] = campaign_execute_command(campaign, profile.path)
        return payload

    emit(invoke(make_campaign), json_output=json_output)
