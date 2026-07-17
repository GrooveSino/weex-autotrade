from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.pretty import Pretty

from weex_cli.human_output import render_human
from weex_cli.redaction import redact

console = Console()
error_console = Console(stderr=True)


def emit(payload: Any, *, json_output: bool = False) -> None:
    safe = redact(payload)
    if json_output:
        console.print_json(json.dumps(safe, ensure_ascii=False, default=str))
        return
    if render_human(safe, console):
        return
    console.print(Pretty(safe, expand_all=False))


def emit_error(message: object) -> None:
    error_console.print(f"[red]Error:[/red] {message}")
