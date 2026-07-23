from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from weex_cli.beta_allocation import DEFAULT_BETA_URL, HttpBetaAllocationProvider
from weex_cli.beta_campaign import (
    DEFAULT_CAMPAIGN_DIRECTORY,
    DEFAULT_CHILD_PLAN_DIRECTORY,
    campaign_execute_command,
    campaign_id_from_confirmation,
)
from weex_cli.beta_campaign_workflow import (
    BetaCampaignApplication,
    CampaignPreviewRequest,
    CampaignRuntimePaths,
)
from weex_cli.beta_volume import (
    DEFAULT_PLAN_DIRECTORY,
    BetaVolumePlanStore,
    beta_volume_confirmation,
    beta_volume_recovery_confirmation,
    observed_recovery_quantity,
)
from weex_cli.beta_volume_workflow import BetaVolumeApplication, BetaVolumePlanRequest
from weex_cli.cli_support import app_context, gateway_for, invoke, profile_for, settings_for
from weex_cli.errors import ValidationError
from weex_cli.human_output import TerminalExecutionProgress, render_execution_event, render_live_volume_event
from weex_cli.live_volume import (
    DEFAULT_PLAN_DIRECTORY as DEFAULT_LIVE_VOLUME_PLAN_DIRECTORY,
)
from weex_cli.live_volume import (
    LiveMakerVolumePlan,
    LiveMakerVolumePlanStore,
    LiveMakerVolumeService,
    live_maker_volume_confirmation,
    plan_payload,
)
from weex_cli.output import emit
from weex_cli.safety import require_execution

app = typer.Typer(
    help="Plan and run explicitly gated live workflows.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

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
    campaign_id: Annotated[
        str | None,
        typer.Option("--campaign-id", "--campaign", hidden=True),
    ] = None,
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


@app.command(
    "beta-volume",
    help="Plan by default; execute reviewed parallel BTC-long / ETH-short pure-Maker cycles with --execute.",
)
def beta_volume(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Option(help="Opening + closing turnover target in USDT", rich_help_panel="Plan")
    ] = "5000",
    round_quote: Annotated[
        str,
        typer.Option(
            "--round",
            help="Approximate combined BTC+ETH opening and closing turnover per flat cycle",
            rich_help_panel="Plan",
        ),
    ] = "500",
    max_position: Annotated[str, typer.Option(help="Hard notional limit for either open leg", hidden=True)] = "1200",
    timeout: Annotated[int, typer.Option(min=1, help="Maximum time allowed for each Maker leg", hidden=True)] = 240,
    recovery_attempts: Annotated[
        int,
        typer.Option(min=1, max=10, help="Maximum Maker close attempts per lane", hidden=True),
    ] = 3,
    max_empty_rounds: Annotated[
        int,
        typer.Option(min=0, max=20, help="Consecutive no-fill paired cycles allowed", hidden=True),
    ] = 3,
    cooldown: Annotated[
        float,
        typer.Option(min=0, max=300, help="Seconds between flat paired cycles", hidden=True),
    ] = 1.0,
    leverage: Annotated[
        str,
        typer.Option(
            help="Automatic per-cycle leverage, or a fixed integer override",
            metavar="auto|1..125",
            rich_help_panel="Plan",
        ),
    ] = "auto",
    plan_id: Annotated[
        str | None,
        typer.Option("--plan-id", "--plan", help="Reviewed plan to execute", rich_help_panel="Execution gate"),
    ] = None,
    execute: Annotated[
        bool, typer.Option("--execute", help="Enable execution of the reviewed plan", rich_help_panel="Execution gate")
    ] = False,
    confirm: Annotated[
        str, typer.Option(help="Exact phrase printed by the plan", rich_help_panel="Execution gate")
    ] = "",
    progress: Annotated[
        bool,
        typer.Option("--progress/--no-progress", help="Show concise execution events", rich_help_panel="Output"),
    ] = True,
    beta_url: Annotated[str, typer.Option(help="Authoritative Beta endpoint", hidden=True)] = DEFAULT_BETA_URL,
    allow_low_confidence_beta: Annotated[
        bool,
        typer.Option(
            "--allow-low-confidence-beta",
            hidden=True,
        ),
    ] = False,
    plan_directory: Annotated[Path, typer.Option(hidden=True)] = DEFAULT_PLAN_DIRECTORY,
    json_output: Annotated[
        bool, typer.Option("--json", help="Stable machine-readable output", rich_help_panel="Output")
    ] = False,
    recover: Annotated[
        bool,
        typer.Option("--recover", help="Recover one observed BTC/ETH position with pure Maker"),
    ] = False,
    recover_symbol: Annotated[
        str,
        typer.Option("--recover-symbol", help="BTC or ETH position to recover", hidden=True),
    ] = "BTC",
) -> None:
    gateway = gateway_for(ctx)
    store = BetaVolumePlanStore(plan_directory)
    application = BetaVolumeApplication(gateway, store)

    if recover:
        if not plan_id:
            raise typer.BadParameter("--plan-id is required with --recover")
        normalized_symbol = recover_symbol.upper()

        def recovery_snapshot() -> dict[str, object]:
            plan, state = store.load(plan_id or "")
            if state not in {"uncertain", "stopped", "recovery_uncertain"}:
                raise typer.BadParameter("the Beta plan is not in a recoverable state")
            side = "long" if normalized_symbol == "BTC" else "short"
            quantity = observed_recovery_quantity(gateway, normalized_symbol, side)
            if quantity <= 0:
                return {
                    "schema_version": 1,
                    "kind": "beta_volume_recovery_plan",
                    "status": "already_flat",
                    "plan_id": plan.plan_id,
                    "symbol": normalized_symbol,
                    "position_side": side,
                    "quantity": "0",
                }
            phrase = beta_volume_recovery_confirmation(plan, normalized_symbol, side, quantity)
            return {
                "schema_version": 1,
                "kind": "beta_volume_recovery_plan",
                "status": "dry_run",
                "mode": "live",
                "plan_id": plan.plan_id,
                "symbol": normalized_symbol,
                "position_side": side,
                "quantity": str(quantity),
                "time_in_force": "POST_ONLY",
                "confirm": phrase,
                "execute_command": (
                    f"./weex live beta-volume --recover --plan {plan.plan_id} "
                    f"--recover-symbol {normalized_symbol} "
                    f"--plan-directory {shlex.quote(str(plan_directory))} "
                    f"--confirm {shlex.quote(phrase)} --execute"
                ),
            }

        if not execute:
            emit(invoke(recovery_snapshot), json_output=json_output)
            return

        def recover_action() -> dict[str, object]:
            plan, state = store.load(plan_id or "")
            if state not in {"uncertain", "stopped", "recovery_uncertain"}:
                raise typer.BadParameter("the Beta plan is not in a recoverable state")
            side = "long" if normalized_symbol == "BTC" else "short"
            quantity = observed_recovery_quantity(gateway, normalized_symbol, side)
            expected = beta_volume_recovery_confirmation(plan, normalized_symbol, side, quantity)
            require_execution(
                execute=True,
                supplied=confirm,
                expected=expected,
                mode="live",
                settings=settings_for(ctx),
            )
            if app_context(ctx).profile_file is not None:
                profile_for(ctx).require_maker_execution()
            progress_console = Console(stderr=True)
            event_sink = (
                (lambda event: render_execution_event(event, progress_console))
                if progress and not json_output
                else None
            )
            return application.recover_plan(plan, normalized_symbol, quantity, event_sink=event_sink)

        payload = invoke(recover_action)
        emit(payload, json_output=json_output)
        if payload.get("status") != "completed":
            raise typer.Exit(1)
        return

    if execute:
        if not plan_id:
            raise typer.BadParameter("--plan-id is required with --execute")

        def action() -> dict[str, object]:
            plan = application.load_plan(plan_id)
            expected = beta_volume_confirmation(plan)
            require_execution(
                execute=True,
                supplied=confirm,
                expected=expected,
                mode="live",
                settings=settings_for(ctx),
            )
            if app_context(ctx).profile_file is not None:
                profile_for(ctx).require_maker_execution()
            provider = HttpBetaAllocationProvider(beta_url)
            progress_console = Console(stderr=True)
            event_sink = (
                (lambda event: render_execution_event(event, progress_console))
                if progress and not json_output
                else None
            )
            return application.execute_plan(plan, provider, event_sink=event_sink)

        payload = invoke(action)
        emit(payload, json_output=json_output)
        if payload["status"] != "completed":
            raise typer.Exit(1)
        return

    if plan_id:
        raise typer.BadParameter("--plan-id is only valid with --execute")

    def make_plan() -> dict[str, object]:
        provider = HttpBetaAllocationProvider(beta_url, allow_low_confidence=allow_low_confidence_beta)
        return application.create_plan(
            BetaVolumePlanRequest(
                target_turnover_quote=target,
                round_turnover_quote=round_quote,
                max_position_quote=max_position,
                timeout_seconds=timeout,
                recovery_attempts=recovery_attempts,
                max_empty_rounds=max_empty_rounds,
                cooldown_seconds=cooldown,
                leverage=leverage,
            ),
            provider,
        )

    payload = invoke(make_plan)
    emit(payload, json_output=json_output)
