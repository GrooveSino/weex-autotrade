"""Campaign execution lifecycle, journal events, and controlled stop requests."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from textual import work

from weex_cli.beta_campaign import live_profile_fingerprint
from weex_cli.core.errors import SafetyError
from weex_cli.tui.runtime import boundary_is_flat

from .screens import CampaignMonitorScreen, CampaignPreviewScreen, CampaignResultScreen
from .support import _result_metrics, _safe_error


class CampaignExecutionMixin:
    def show_preview(self, payload: Mapping[str, Any]) -> None:
        self.push_screen(CampaignPreviewScreen(payload))

    def validate_preview_for_execution(self, payload: Mapping[str, Any]) -> None:
        campaign = cast(Mapping[str, Any], payload["campaign"])
        if int(time.time() * 1000) >= int(campaign["expires_at_ms"]):
            raise SafetyError("campaign plan has expired")
        if self.require_journal().unresolved_uncertain():
            raise SafetyError("uncertain campaign must be reconciled before execution")
        account = self.selected_account
        if account is None:
            raise SafetyError("no account is selected")
        current_catalog = self.catalog_loader(self.catalog.path)
        current = current_catalog.get(account.account_id)
        if not current.enabled:
            raise SafetyError("selected account was disabled after preview")
        current_profile = current.live_profile(current_catalog.path, current_catalog.safety)
        current_profile.require_maker_execution()
        if not current_profile.settings.live_trading_enabled:
            raise SafetyError("live trading is disabled; set WEEX_LIVE_TRADING_ENABLED=true before starting the TUI")
        if live_profile_fingerprint(current_profile) != str(campaign["profile_fingerprint"]):
            raise SafetyError("account credentials or proxy changed after preview")
        record = self.require_workflow().load(str(campaign["campaign_id"]))
        if record.state != "planned":
            raise SafetyError("campaign is no longer in planned state")

    def start_campaign(self, payload: Mapping[str, Any]) -> None:
        campaign = cast(Mapping[str, Any], payload["campaign"])
        self.active_campaign = True
        self.stop_event.clear()
        self.stop_confirmation = str(payload["stop_confirm"])
        self.current_campaign_id = str(campaign["campaign_id"])
        self._campaign_events = []
        self.record_campaign_event(
            {
                "event": "tui_campaign_console_opened",
                "campaign_id": self.current_campaign_id,
                "message": "任务已进入队列；等待控制台渲染后启动后台 worker",
            }
        )
        self.push_screen(CampaignMonitorScreen(payload))
        # Starting the worker only after the monitor has rendered prevents the
        # first preflight event from being lost while Textual switches screens.
        self.call_after_refresh(
            self._launch_campaign_worker, str(payload["confirm"]), self.current_campaign_id, payload
        )

    def _launch_campaign_worker(self, confirmation: str, campaign_id: str, payload: Mapping[str, Any]) -> None:
        if not self.active_campaign or campaign_id != self.current_campaign_id:
            return
        self.run_campaign(confirmation, campaign_id, payload)

    @work(thread=True, exclusive=True, group="live-campaign")
    def run_campaign(self, confirmation: str, campaign_id: str, payload: Mapping[str, Any]) -> None:
        def event_sink(event: Mapping[str, Any]) -> None:
            stored = self.require_journal().append_event(campaign_id, event)
            self.call_from_thread(self.apply_campaign_event, stored)

        execution_started = False
        stage = "worker_start"
        try:
            event_sink({"event": "tui_worker_started", "campaign_id": campaign_id})
            stage = "authorization_validation"
            event_sink({"event": "tui_execution_validation_started", "campaign_id": campaign_id})
            self.validate_preview_for_execution(payload)
            event_sink({"event": "tui_execution_validation_completed", "campaign_id": campaign_id})

            stage = "exchange_preflight"
            event_sink({"event": "tui_execution_preflight_started", "campaign_id": campaign_id})
            snapshot = self.require_workflow().account_boundary()
            if not boundary_is_flat(snapshot):
                raise SafetyError("account changed after preview and is no longer flat")
            event_sink({"event": "tui_execution_preflight_completed", "campaign_id": campaign_id})

            stage = "campaign_execution"
            execution_started = True
            result = self.require_workflow().execute(
                confirmation=confirmation,
                campaign_id=campaign_id,
                event_sink=event_sink,
                stop_requested=self.stop_event.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - a started live worker fails closed as uncertain
            reason = f"tui_{stage}_failed:{type(exc).__name__.lower()}"
            status = "uncertain" if execution_started else "stopped"
            result = {
                "schema_version": 1,
                "kind": "beta_volume_campaign_execution",
                "mode": "live",
                "status": status,
                "reason": reason,
                "campaign_id": campaign_id,
                "retry_allowed": False,
            }
            try:
                record = self.require_workflow().load(campaign_id)
                store = getattr(self.require_workflow(), "campaign_store", None)
                if store is not None:
                    store.save(record.campaign, state=status, result=result)
            except Exception:  # noqa: BLE001 - keep original journal available for manual inspection
                pass
            event_sink(
                {
                    "event": "tui_execution_failed",
                    "campaign_id": campaign_id,
                    "stage": stage,
                    "reason": reason,
                    "message": _safe_error(exc),
                }
            )
            event_sink({"event": f"campaign_{status}", "reason": reason, "campaign_id": campaign_id})
        else:
            event_sink(
                {
                    "event": "tui_execution_result_received",
                    "campaign_id": campaign_id,
                    "status": str(result.get("status") or "unknown"),
                    "reason": result.get("reason"),
                }
            )
        self.call_from_thread(self.finish_campaign, result)

    def record_campaign_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not self.current_campaign_id:
            raise SafetyError("no active campaign is selected")
        stored = self.require_journal().append_event(self.current_campaign_id, event)
        self.apply_campaign_event(stored)
        return stored

    def apply_campaign_event(self, event: Mapping[str, Any]) -> None:
        self._campaign_events.append(dict(event))
        monitor = self._campaign_monitor
        if monitor is not None and monitor.is_mounted:
            monitor.apply_event(event)

    def bind_campaign_monitor(self, monitor: CampaignMonitorScreen) -> None:
        self._campaign_monitor = monitor
        for event in self._campaign_events:
            monitor.apply_event(event)

    def unbind_campaign_monitor(self, monitor: CampaignMonitorScreen) -> None:
        if self._campaign_monitor is monitor:
            self._campaign_monitor = None

    def finish_campaign(self, result: Mapping[str, Any]) -> None:
        self.record_campaign_event(
            {
                "event": "tui_worker_finished",
                "campaign_id": self.current_campaign_id,
                "status": str(result.get("status") or "unknown"),
                "reason": result.get("reason"),
            }
        )
        self.active_campaign = False
        self.stop_confirmation = ""
        enriched = dict(result)
        events = self.require_journal().events(self.current_campaign_id, limit=10_000)
        enriched["tui_events"] = events
        enriched["tui_metrics"] = _result_metrics(result, events)
        self.switch_screen(CampaignResultScreen(enriched))

    def request_safe_stop(self, confirmation: str) -> None:
        if not self.active_campaign:
            raise SafetyError("no campaign is running")
        if confirmation != self.stop_confirmation:
            raise SafetyError("safe stop confirmation does not match exactly")
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        workflow = self.require_workflow()
        try:
            record = workflow.load(self.current_campaign_id)
            store = getattr(workflow, "campaign_store", None)
            if store is not None and record.state == "executing":
                store.save(record.campaign, state="stopping", result=record.result)
        except Exception:  # noqa: BLE001 - stop flag remains authoritative in this process
            pass
        self.record_campaign_event(
            {"event": "stop_requested", "campaign_id": self.current_campaign_id},
        )
        if self._campaign_monitor is not None and self._campaign_monitor.is_mounted:
            self._campaign_monitor.show_stopping()
