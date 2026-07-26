"""Account selection, navigation, workflow construction, and signal lifecycle."""

from __future__ import annotations

import signal
from typing import Any

from weex_cli.beta_campaign.allocation import HttpBetaAllocationProvider
from weex_cli.beta_campaign.workflow import BetaCampaignApplication, CampaignRuntimePaths
from weex_cli.core.errors import SafetyError
from weex_cli.tui.accounts import AccountInUseError, AccountLease, TuiAccount, TuiAccountCatalog
from weex_cli.tui.runtime import TuiCampaignJournal

from .contracts import CampaignWorkflow
from .screens import AccountOverviewScreen, AccountSelectionScreen, SafeQuitScreen
from .support import _safe_error


class AccountNavigationMixin:
    def select_account(self, account_id: str) -> None:
        account = self.catalog.get(account_id)
        if not account.enabled:
            self.notify("账户已禁用", severity="warning")
            return
        lease = AccountLease(account.account_id, self.runtime_root)
        try:
            lease.acquire()
        except AccountInUseError:
            self.notify("该账户正在另一个终端中使用", severity="error")
            return
        try:
            paths = CampaignRuntimePaths.for_account(self.runtime_root, account.account_id)
            workflow = self.workflow_factory(account, self.catalog, paths)
            workflow.mark_interrupted_uncertain()
        except Exception as exc:  # noqa: BLE001 - no private request has run yet
            lease.release()
            self.notify(_safe_error(exc), severity="error")
            return
        self.lease = lease
        self.selected_account = account
        self.workflow = workflow
        self.journal = TuiCampaignJournal(paths)
        self.push_screen(AccountOverviewScreen())

    def refresh_catalog(self) -> None:
        try:
            self.catalog = self.catalog_loader(self.catalog.path)
        except Exception as exc:  # noqa: BLE001
            self.notify(_safe_error(exc), severity="error")
            return
        self.switch_screen(AccountSelectionScreen())

    def show_overview(self) -> None:
        self.switch_screen(AccountOverviewScreen())

    def leave_account(self) -> None:
        if self.active_campaign:
            self.push_screen(SafeQuitScreen())
            return
        self._release_lease()
        self.selected_account = None
        self.workflow = None
        self.journal = None
        self.switch_screen(AccountSelectionScreen())

    def action_quit(self) -> None:
        if self.active_campaign:
            if not isinstance(self.screen, SafeQuitScreen):
                self.push_screen(SafeQuitScreen())
            return
        self._release_lease()
        self.exit()

    def require_workflow(self) -> CampaignWorkflow:
        if self.workflow is None:
            raise SafetyError("no account workflow is active")
        return self.workflow

    def require_journal(self) -> TuiCampaignJournal:
        if self.journal is None:
            raise SafetyError("no account journal is active")
        return self.journal

    def _default_workflow(
        self,
        account: TuiAccount,
        catalog: TuiAccountCatalog,
        paths: CampaignRuntimePaths,
    ) -> CampaignWorkflow:
        profile = account.live_profile(catalog.path, catalog.safety)
        return BetaCampaignApplication(
            profile,
            paths,
            provider_factory=lambda: HttpBetaAllocationProvider(catalog.beta_url),
        )

    def _release_lease(self) -> None:
        if self.lease is not None:
            self.lease.release()
            self.lease = None

    def _install_signal_handlers(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_signal_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle_signal)
            except (ValueError, OSError):
                continue

    def _restore_signal_handlers(self) -> None:
        for signal_number, handler in self._old_signal_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (ValueError, OSError):
                continue
        self._old_signal_handlers.clear()

    def _handle_signal(self, signal_number: int, frame: Any) -> None:
        if self.active_campaign:
            self.call_later(self.push_screen, SafeQuitScreen())
        else:
            self.call_later(self.action_quit)
