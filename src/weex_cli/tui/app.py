"""Thin Textual application composition for the Campaign TUI."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from textual.app import App

from weex_cli.tui.accounts import (
    DEFAULT_ACCOUNT_FILE,
    DEFAULT_RUNTIME_DIRECTORY,
    AccountLease,
    TuiAccountCatalog,
    load_tui_account_catalog,
)
from weex_cli.tui.runtime import TuiCampaignJournal

from .campaigns import CampaignExecutionMixin
from .contracts import CampaignWorkflow, CatalogLoader, WorkflowFactory
from .navigation import AccountNavigationMixin
from .screens import (
    AccountOverviewScreen,
    AccountSelectionScreen,
    CampaignFormScreen,
    CampaignMonitorScreen,
    CampaignPreviewScreen,
    CampaignResultScreen,
)


class WeexTuiApp(CampaignExecutionMixin, AccountNavigationMixin, App[None]):
    TITLE = "WEEX Beta Campaign"
    CSS = """
    Screen { background: #111417; color: #e8ecef; }
    #page { width: 100%; height: 100%; padding: 1 2; }
    .title { text-style: bold; color: #ffffff; height: 3; }
    .section-title { color: #9ba7b0; height: 2; }
    .panel { border: solid #3b464f; padding: 1 2; margin-bottom: 1; }
    .phrase { border-left: thick #d6a84a; padding: 1 2; margin: 1 0; color: #f2d58d; }
    .error { color: #ff7b72; min-height: 1; }
    .actions { height: 3; margin-top: 1; }
    .actions Button { margin-right: 1; }
    .field { height: 3; align-vertical: middle; }
    .field Label { width: 22; }
    .field Input { width: 18; margin-right: 1; }
    .account-row { width: 100%; height: 3; text-align: left; margin-bottom: 1; }
    #events { border: solid #3b464f; height: 1fr; min-height: 8; }
    #quit-dialog { width: 78; height: auto; max-height: 18; padding: 2; border: heavy #d6a84a; background: #171b1f; }
    """
    BINDINGS = [("ctrl+c", "quit", "安全退出")]

    def __init__(
        self,
        catalog: TuiAccountCatalog,
        *,
        runtime_root: Path = DEFAULT_RUNTIME_DIRECTORY,
        workflow_factory: WorkflowFactory | None = None,
        catalog_loader: CatalogLoader | None = None,
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self.runtime_root = runtime_root
        self.workflow_factory = workflow_factory or self._default_workflow
        self.catalog_loader = catalog_loader or load_tui_account_catalog
        self.selected_account = None
        self.workflow: CampaignWorkflow | None = None
        self.journal: TuiCampaignJournal | None = None
        self.lease: AccountLease | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self.active_campaign = False
        self.stop_event = threading.Event()
        self.stop_confirmation = ""
        self.current_campaign_id = ""
        self._campaign_events: list[dict[str, Any]] = []
        self._campaign_monitor: CampaignMonitorScreen | None = None
        self._old_signal_handlers: dict[int, Any] = {}

    def on_mount(self) -> None:
        self.push_screen(AccountSelectionScreen())
        self._install_signal_handlers()

    def on_unmount(self) -> None:
        self._restore_signal_handlers()
        self._release_lease()


def run_tui(
    accounts_file: Path = DEFAULT_ACCOUNT_FILE,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_DIRECTORY,
) -> None:
    catalog = load_tui_account_catalog(accounts_file)
    WeexTuiApp(catalog, runtime_root=runtime_root).run()


__all__ = [
    "AccountOverviewScreen",
    "AccountSelectionScreen",
    "CampaignFormScreen",
    "CampaignMonitorScreen",
    "CampaignPreviewScreen",
    "CampaignResultScreen",
    "WeexTuiApp",
    "run_tui",
]
