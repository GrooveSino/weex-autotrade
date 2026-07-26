"""Reviewed Beta volume planning, execution, and owned-exposure recovery command."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from weex_cli.beta_campaign.allocation import DEFAULT_BETA_URL, HttpBetaAllocationProvider
from weex_cli.beta_volume import (
    DEFAULT_PLAN_DIRECTORY,
    BetaVolumePlanStore,
    beta_volume_confirmation,
    beta_volume_recovery_confirmation,
    observed_recovery_quantity,
)
from weex_cli.beta_volume.workflow import BetaVolumeApplication, BetaVolumePlanRequest
from weex_cli.cli_support import app_context, gateway_for, invoke, profile_for, settings_for
from weex_cli.core.safety import require_execution
from weex_cli.presentation.human import render_execution_event
from weex_cli.presentation.output import emit

from .app import app


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
    allow_low_confidence_beta: Annotated[bool, typer.Option("--allow-low-confidence-beta", hidden=True)] = False,
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
        _recover(
            ctx,
            application,
            store,
            gateway,
            plan_id,
            recover_symbol,
            execute,
            confirm,
            progress,
            json_output,
            plan_directory,
        )
        return
    if execute:
        if not plan_id:
            raise typer.BadParameter("--plan-id is required with --execute")

        def action() -> dict[str, object]:
            plan = application.load_plan(plan_id)
            require_execution(
                execute=True,
                supplied=confirm,
                expected=beta_volume_confirmation(plan),
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

    emit(invoke(make_plan), json_output=json_output)


def _recover(
    ctx: typer.Context,
    application: BetaVolumeApplication,
    store: BetaVolumePlanStore,
    gateway: object,
    plan_id: str | None,
    recover_symbol: str,
    execute: bool,
    confirm: str,
    progress: bool,
    json_output: bool,
    plan_directory: Path,
) -> None:
    if not plan_id:
        raise typer.BadParameter("--plan-id is required with --recover")
    normalized_symbol = recover_symbol.upper()

    def recovery_snapshot() -> dict[str, object]:
        plan, state = store.load(plan_id)
        if state not in {"uncertain", "stopped", "recovery_uncertain"}:
            raise typer.BadParameter("the Beta plan is not in a recoverable state")
        side = "long" if normalized_symbol == "BTC" else "short"
        quantity = observed_recovery_quantity(gateway, normalized_symbol, side)  # type: ignore[arg-type]
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
        plan, state = store.load(plan_id)
        if state not in {"uncertain", "stopped", "recovery_uncertain"}:
            raise typer.BadParameter("the Beta plan is not in a recoverable state")
        side = "long" if normalized_symbol == "BTC" else "short"
        quantity = observed_recovery_quantity(gateway, normalized_symbol, side)  # type: ignore[arg-type]
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
            (lambda event: render_execution_event(event, progress_console)) if progress and not json_output else None
        )
        return application.recover_plan(plan, normalized_symbol, quantity, event_sink=event_sink)

    payload = invoke(recover_action)
    emit(payload, json_output=json_output)
    if payload.get("status") != "completed":
        raise typer.Exit(1)
