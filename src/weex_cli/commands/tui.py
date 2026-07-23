from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from weex_cli.cli_support import app_context, invoke
from weex_cli.tui_accounts import DEFAULT_ACCOUNT_FILE
from weex_cli.tui_app import run_tui


def tui(
    ctx: typer.Context,
    accounts_file: Annotated[
        Path,
        typer.Option("--accounts-file", help="项目内的 TUI 多账户 TOML 文件"),
    ] = DEFAULT_ACCOUNT_FILE,
) -> None:
    """启动多账户 Live Beta Campaign 终端界面。"""
    context = app_context(ctx)
    if context.env_file is not None or context.profile_file is not None:
        raise typer.BadParameter("tui uses --accounts-file; do not combine it with --env-file or --profile")
    invoke(lambda: run_tui(accounts_file))
