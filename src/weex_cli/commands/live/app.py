"""Shared Typer application for explicitly gated live workflows."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Plan and run explicitly gated live workflows.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
