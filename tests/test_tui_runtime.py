from __future__ import annotations

import json
from pathlib import Path

from weex_cli.beta_campaign_workflow import CampaignRuntimePaths
from weex_cli.tui_runtime import TuiCampaignJournal, reconciliation_confirmation


def test_event_journal_is_private_and_redacts_sensitive_values(tmp_path: Path) -> None:
    journal = TuiCampaignJournal(CampaignRuntimePaths(tmp_path / "campaigns", tmp_path / "plans"))

    journal.append_event(
        "wc-1234567890",
        {
            "event": "request_failed",
            "api_secret": "must-not-appear",
            "detail": "https://user:password@proxy.example:8080 api_key=also-secret",
        },
    )

    path = tmp_path / "campaigns" / "wc-1234567890.events.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text(encoding="utf-8")
    assert "must-not-appear" not in raw
    assert "also-secret" not in raw
    assert "user:password" not in raw
    row = json.loads(raw)
    assert row["api_secret"] == "[REDACTED]"
    assert "[REDACTED]" in row["detail"]


def test_reconciliation_phrase_is_campaign_specific() -> None:
    assert reconciliation_confirmation("wc-1234567890") == (
        "RECONCILE WEEX LIVE BETA-CAMPAIGN WC-1234567890 ACCOUNT_FLAT NO_ORDERS"
    )
