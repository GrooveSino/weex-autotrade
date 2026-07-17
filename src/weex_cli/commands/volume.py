from __future__ import annotations

import typer

from weex_cli.adaptive_volume import (
    AdaptiveMakerVolumeService,
    DemoMakerSoakService,
    MakerFlattenService,
    MakerSoakPlan,
    maker_flatten_confirmation,
    maker_soak_confirmation,
)
from weex_cli.cli_support import gateway_for, invoke, settings_for
from weex_cli.maker_benchmark import BenchmarkConfig, run_benchmark
from weex_cli.maker_run_report import write_maker_run_report, write_maker_soak_report
from weex_cli.maker_volume import MakerVolumePlan, maker_volume_confirmation
from weex_cli.models import decimal_value
from weex_cli.output import emit
from weex_cli.safety import require_execution

app = typer.Typer(help="Run bounded Demo maker-volume workflows.", no_args_is_help=True)


@app.command("flatten")
def flatten(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    quantity: str = typer.Option(...),
    max_position: str = typer.Option(...),
    timeout: int = typer.Option(120, min=1),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    normalized_quantity = decimal_value(quantity, name="quantity")
    normalized_max = decimal_value(max_position, name="max_position")
    assert normalized_quantity is not None and normalized_max is not None
    phrase = maker_flatten_confirmation(
        symbol=symbol,
        quantity=normalized_quantity,
        max_position_quote=normalized_max,
        timeout_seconds=timeout,
    )
    if not execute:
        emit(
            {
                "status": "dry_run",
                "mode": "demo",
                "action": "maker_flatten",
                "symbol": symbol.upper(),
                "quantity": str(normalized_quantity),
                "max_position": str(normalized_max),
                "timeout": timeout,
                "confirm": phrase,
            },
            json_output=json_output,
        )
        return

    def action():
        require_execution(
            execute=True,
            supplied=confirm,
            expected=phrase,
            mode="demo",
            settings=settings_for(ctx),
        )
        return MakerFlattenService(gateway_for(ctx)).run(
            symbol=symbol,
            quantity=normalized_quantity,
            max_position_quote=normalized_max,
            timeout_seconds=timeout,
        )

    payload = invoke(action)
    emit(payload, json_output=json_output)
    if payload["status"] != "completed":
        raise typer.Exit(1)


@app.command("benchmark")
def benchmark(
    target: float = typer.Option(10_000, min=1, help="Two-sided quote-volume target in USDT"),
    cycles: int = typer.Option(5, min=5, help="Required complete open/close cycles per trial"),
    train_trials: int = typer.Option(15, min=5, help="Seeded trials used only for parameter selection"),
    validation_trials: int = typer.Option(15, min=5, help="Independent seeded acceptance trials"),
    seed: int = typer.Option(20_260_717, help="First deterministic training seed"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Search and validate a pure-Maker policy without credentials or network access."""
    payload = invoke(
        lambda: run_benchmark(
            BenchmarkConfig(
                target_quote=target,
                cycles=cycles,
                train_trials=train_trials,
                validation_trials=validation_trials,
                seed=seed,
            )
        )
    )
    emit(payload, json_output=json_output)
    if payload["status"] != "passed":
        raise typer.Exit(1)


@app.command("maker")
def maker(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    target: str = typer.Option(..., help="Batch quote-volume target in SUSDT"),
    fills: int = typer.Option(..., min=2, help="Even number of successful maker fills"),
    max_position: str = typer.Option(..., help="Maximum opening position notional in SUSDT"),
    timeout: int = typer.Option(120, min=1, help="Per-order fill timeout in seconds"),
    poll_interval: float = typer.Option(1.0, min=0.2, max=10.0),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    report: bool = typer.Option(False, "--report", help="Write a credential-free Markdown run report"),
    baseline_seconds: float | None = typer.Option(None, min=0, help="Optional elapsed-time comparison baseline"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    plan = invoke(
        lambda: MakerVolumePlan.create(
            symbol=symbol,
            target_quote=target,
            fills=fills,
            max_position_quote=max_position,
            timeout_seconds=timeout,
            poll_interval_seconds=poll_interval,
        )
    )
    phrase = maker_volume_confirmation(plan)
    if not execute:
        emit(
            {
                "status": "dry_run",
                "plan": plan.as_dict(),
                "confirm": phrase,
                "safety": {
                    "demo_only": True,
                    "post_only": True,
                    "one_active_order": True,
                    "no_submission_retry_after_uncertainty": True,
                    "stop_on_cancel_rejection_partial_or_timeout": True,
                    "starting_and_final_position_must_be_flat": True,
                },
            },
            json_output=json_output,
        )
        return

    def action():
        require_execution(
            execute=True,
            supplied=confirm,
            expected=phrase,
            mode="demo",
            settings=settings_for(ctx),
        )
        return AdaptiveMakerVolumeService(gateway_for(ctx)).run(plan)

    payload = invoke(action)
    if report:
        path = write_maker_run_report(payload, baseline_seconds=baseline_seconds)
        payload = {**payload, "report_path": str(path)}
    emit(payload, json_output=json_output)
    if payload["status"] != "completed":
        raise typer.Exit(1)


@app.command("soak")
def soak(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    target: str = typer.Option(..., help="Per-round quote-volume target in SUSDT"),
    fills: int = typer.Option(..., min=2, help="Even number of successful Maker legs per round"),
    rounds: int = typer.Option(..., min=2, max=10, help="Complete flat-to-flat rounds"),
    max_position: str = typer.Option(..., help="Maximum opening position notional in SUSDT"),
    timeout: int = typer.Option(120, min=1, help="Per-leg timeout in seconds"),
    poll_interval: float = typer.Option(1.0, min=0.2, max=10.0),
    execute: bool = typer.Option(False, "--execute"),
    confirm: str = typer.Option(""),
    report: bool = typer.Option(False, "--report", help="Write a credential-free Markdown soak report"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    volume_plan = invoke(
        lambda: MakerVolumePlan.create(
            symbol=symbol,
            target_quote=target,
            fills=fills,
            max_position_quote=max_position,
            timeout_seconds=timeout,
            poll_interval_seconds=poll_interval,
        )
    )
    plan = invoke(lambda: MakerSoakPlan(volume_plan, rounds))
    phrase = maker_soak_confirmation(plan)
    if not execute:
        emit(
            {
                "status": "dry_run",
                "plan": plan.as_dict(),
                "confirm": phrase,
                "safety": {
                    "demo_only": True,
                    "post_only": True,
                    "flat_before_and_after_every_round": True,
                    "stop_on_first_failed_or_uncertain_round": True,
                    "no_automatic_cleanup_or_submission_retry": True,
                    "inter_round_submit_cooldown_seconds": 10.1,
                },
            },
            json_output=json_output,
        )
        return

    def action():
        require_execution(
            execute=True,
            supplied=confirm,
            expected=phrase,
            mode="demo",
            settings=settings_for(ctx),
        )
        return DemoMakerSoakService(gateway_for(ctx)).run(plan)

    payload = invoke(action)
    if report:
        path = write_maker_soak_report(payload)
        payload = {**payload, "report_path": str(path)}
    emit(payload, json_output=json_output)
    if payload["status"] != "completed":
        raise typer.Exit(1)
