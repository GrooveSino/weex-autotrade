from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from weex_cli.cli_support import gateway_for, invoke
from weex_cli.execution.venues import DemoAdaptiveMakerVenue
from weex_cli.presentation.output import emit

from . import volume

app = typer.Typer(
    help="Run safe Demo maker workflows with practical defaults.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("run", help="Complete one flat-to-flat Maker volume target.")
def run(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Contract base asset")] = "BTC",
    target: Annotated[str, typer.Option(help="Two-sided volume target in SUSDT")] = "10000",
    fills: Annotated[int, typer.Option(min=2, help="Successful open/close legs")] = 10,
    max_position: Annotated[str, typer.Option(help="Maximum open notional in SUSDT")] = "1200",
    timeout: Annotated[int, typer.Option(min=1, help="Per-leg timeout in seconds")] = 120,
    execute: Annotated[bool, typer.Option("--execute", help="Execute after exact confirmation")] = False,
    confirm: Annotated[str, typer.Option(help="Exact phrase shown by the dry run")] = "",
    report: Annotated[bool, typer.Option("--report/--no-report", help="Write a Markdown audit report")] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Stable machine-readable output")] = False,
    poll_interval: Annotated[float, typer.Option(hidden=True, min=0.2, max=10.0)] = 1.0,
) -> None:
    volume.maker(
        ctx,
        symbol=symbol,
        target=target,
        fills=fills,
        max_position=max_position,
        timeout=timeout,
        poll_interval=poll_interval,
        execute=execute,
        confirm=confirm,
        report=report,
        baseline_seconds=None,
        json_output=json_output,
    )


@app.command("soak", help="Repeat the flat-to-flat target and stop on the first unsafe round.")
def soak(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Contract base asset")] = "BTC",
    target: Annotated[str, typer.Option(help="Per-round volume target in SUSDT")] = "10000",
    fills: Annotated[int, typer.Option(min=2, help="Successful open/close legs per round")] = 10,
    rounds: Annotated[int, typer.Option(min=2, max=10, help="Flat-to-flat rounds")] = 3,
    max_position: Annotated[str, typer.Option(help="Maximum open notional in SUSDT")] = "1200",
    timeout: Annotated[int, typer.Option(min=1, help="Per-leg timeout in seconds")] = 120,
    execute: Annotated[bool, typer.Option("--execute", help="Execute after exact confirmation")] = False,
    confirm: Annotated[str, typer.Option(help="Exact phrase shown by the dry run")] = "",
    report: Annotated[bool, typer.Option("--report/--no-report", help="Write a Markdown audit report")] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Stable machine-readable output")] = False,
    poll_interval: Annotated[float, typer.Option(hidden=True, min=0.2, max=10.0)] = 1.0,
) -> None:
    volume.soak(
        ctx,
        symbol=symbol,
        target=target,
        fills=fills,
        rounds=rounds,
        max_position=max_position,
        timeout=timeout,
        poll_interval=poll_interval,
        execute=execute,
        confirm=confirm,
        report=report,
        json_output=json_output,
    )


@app.command("flatten", help="Close the current Demo long with pure Maker orders.")
def flatten(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Contract base asset")] = "BTC",
    quantity: Annotated[str | None, typer.Option(help="Override the detected position quantity")] = None,
    max_position: Annotated[str, typer.Option(help="Maximum position notional in SUSDT")] = "1200",
    timeout: Annotated[int, typer.Option(min=1, help="Per-leg timeout in seconds")] = 120,
    execute: Annotated[bool, typer.Option("--execute", help="Execute after exact confirmation")] = False,
    confirm: Annotated[str, typer.Option(help="Exact phrase shown by the dry run")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Stable machine-readable output")] = False,
) -> None:
    selected_quantity = quantity
    if selected_quantity is None:
        detected = invoke(lambda: DemoAdaptiveMakerVenue(gateway_for(ctx), symbol).position_quantity())
        if detected <= 0:
            emit(
                {
                    "view": "message",
                    "status": "ok",
                    "message": f"{symbol.upper()} is already flat. No order was submitted.",
                },
                json_output=json_output,
            )
            return
        selected_quantity = _decimal_text(detected)
    volume.flatten(
        ctx,
        symbol=symbol,
        quantity=selected_quantity,
        max_position=max_position,
        timeout=timeout,
        execute=execute,
        confirm=confirm,
        json_output=json_output,
    )


def _decimal_text(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")
