"""Transient Rich progress display for concurrent terminal waits."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from weex_cli.execution_progress import ExecutionProgressProjector, TimelinePresentation
from weex_cli.presentation.i18n import text

from .execution import render_execution_event
from .shared import duration as _duration


@dataclass
class _ActiveWait:
    label: str
    elapsed_seconds: float
    remaining_seconds: float | None
    updated_at: float
    detail: str = ""


def _render_progress_presentation(presentation: TimelinePresentation, console: Console) -> None:
    styles = {"info": "cyan", "success": "green", "warn": "yellow", "error": "red"}
    style = styles.get(presentation.level, "cyan")
    detail = f"  {presentation.detail}" if presentation.detail else ""
    console.print(f"[{style}]{presentation.title}[/{style}]{detail}")


class TerminalExecutionProgress:
    """Render concurrent execution waits as one transient, live terminal area."""

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(
        self,
        console: Console,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        interactive: bool | None = None,
        auto_refresh: bool = True,
    ) -> None:
        self.console = console
        self.monotonic = monotonic
        self.interactive = console.is_terminal if interactive is None else interactive
        self._lock = threading.RLock()
        self._live_lifecycle_lock = threading.Lock()
        self._waits: dict[str, _ActiveWait] = {}
        self._projector = ExecutionProgressProjector()
        self._live = Live(
            self,
            console=console,
            refresh_per_second=8,
            transient=True,
            auto_refresh=auto_refresh,
        )
        self._live_started = False

    def __call__(self, event: Mapping[str, Any]) -> None:
        if not self.interactive:
            render_execution_event(event, self.console)
            return
        with self._lock:
            presentation = self._projector.apply(event, at_ms=int(self.monotonic() * 1000))
            self._waits = {
                wait.key: _ActiveWait(
                    label=wait.label,
                    elapsed_seconds=wait.elapsed_ms / 1000,
                    remaining_seconds=(wait.remaining_ms / 1000 if wait.remaining_ms is not None else None),
                    updated_at=self.monotonic(),
                    detail=wait.detail,
                )
                for wait in self._projector.active_waits.values()
            }
        self._sync_live()
        if presentation is not None:
            _render_progress_presentation(presentation, self.console)

    def close(self) -> None:
        if not self.interactive:
            return
        with self._live_lifecycle_lock:
            with self._lock:
                self._waits.clear()
                self._projector.active_waits.clear()
                should_stop = self._live_started
                self._live_started = False
            if should_stop:
                self._live.stop()

    def refresh(self) -> None:
        if not self.interactive:
            return
        with self._live_lifecycle_lock:
            with self._lock:
                should_refresh = self._live_started
            if should_refresh:
                self._live.refresh()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        with self._lock:
            waits = tuple(self._waits.values())
        now = self.monotonic()
        frame = self._FRAMES[int(now * 8) % len(self._FRAMES)]
        for wait in waits:
            delta = max(0.0, now - wait.updated_at)
            elapsed = wait.elapsed_seconds + delta
            remaining = max(0.0, wait.remaining_seconds - delta) if wait.remaining_seconds is not None else None
            line = Text()
            line.append(f"{frame} ", style="bold cyan")
            line.append(wait.label, style="cyan")
            line.append(
                f"  {text('已等待', 'elapsed')} {_duration(elapsed * 1000, milliseconds=True)}",
                style="dim",
            )
            if remaining is not None:
                line.append(
                    f" / {text('剩余', 'remaining')} {_duration(remaining * 1000, milliseconds=True)}",
                    style="dim",
                )
            if wait.detail:
                line.append(f" / {wait.detail}", style="dim")
            yield line

    def _sync_live(self) -> None:
        with self._live_lifecycle_lock:
            with self._lock:
                has_waits = bool(self._waits)
                was_started = self._live_started
                if has_waits and not was_started:
                    self._live_started = True
                elif not has_waits and was_started:
                    self._live_started = False
            if has_waits:
                if not was_started:
                    self._live.start(refresh=True)
                else:
                    self._live.update(self, refresh=True)
            elif was_started:
                self._live.stop()
