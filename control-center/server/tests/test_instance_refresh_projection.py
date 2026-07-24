from decimal import Decimal

from fastapi.testclient import TestClient

from fleet_api.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.volume_history import NormalizedTradeFill

from .test_api_support import create_payload, strategy_payload


def test_repeated_refresh_never_replaces_terminal_run_progress_with_zero() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        strategy_request = strategy_payload(target="500")
        strategy_request["targetMode"] = "incremental"
        strategy = api.post("/api/v1/strategies", json=strategy_request).json()
        payload = create_payload()
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        ledger = app.state.trade_volume_ledger
        ledger.create_session(
            "terminal-refresh-run",
            instance["id"],
            instance["mode"],
            1_000,
            Decimal("500"),
            strategy_id=strategy["id"],
            strategy_name=strategy["name"],
            strategy_version=strategy["version"],
            target_mode="incremental",
            strategy_target_quote_volume=Decimal("500"),
        )
        ledger.record_account_fills(
            instance["id"],
            instance["mode"],
            (
                NormalizedTradeFill(
                    identity="terminal-refresh-fill",
                    executed_at_ms=1_500,
                    quote_volume=Decimal("67.2638"),
                    symbol="BTCUSDT",
                    authoritative=True,
                ),
            ),
        )
        ledger.update_session(
            "terminal-refresh-run",
            verified_quote_volume=Decimal("67.2638"),
            status="stopped",
            result="stopped",
            finished_at_ms=2_000,
            source_complete=True,
            stale=False,
            pending_sync=False,
            audit_status="verified",
        )

        snapshots = [api.get(f"/api/v1/instances/{instance['id']}").json()]
        snapshots.extend(
            api.post(f"/api/v1/instances/{instance['id']}/refresh").json()
            for _ in range(3)
        )
        snapshots.append(api.get(f"/api/v1/instances/{instance['id']}").json())

    assert [row["volume"]["strategyVerifiedQuoteVolume"] for row in snapshots] == ["67.2638"] * 5
    assert all(row["volume"]["strategyProgressSource"] == "ledger" for row in snapshots)


def test_terminal_progress_does_not_leak_after_strategy_binding_changes() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        first_request = strategy_payload(name="first", target="500")
        first_request["targetMode"] = "incremental"
        second_request = strategy_payload(name="second", target="800")
        second_request["targetMode"] = "incremental"
        first = api.post("/api/v1/strategies", json=first_request).json()
        second = api.post("/api/v1/strategies", json=second_request).json()
        payload = create_payload()
        payload["strategyId"] = first["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        ledger = app.state.trade_volume_ledger
        ledger.create_session(
            "old-strategy-run",
            instance["id"],
            instance["mode"],
            1_000,
            Decimal("500"),
            strategy_id=first["id"],
            strategy_version=first["version"],
        )
        ledger.update_session(
            "old-strategy-run",
            verified_quote_volume=Decimal("67.2638"),
            status="stopped",
            finished_at_ms=2_000,
        )

        assigned = api.post(
            f"/api/v1/strategies/{second['id']}/assign",
            json={"instanceIds": [instance["id"]]},
        )
        projected = api.get(f"/api/v1/instances/{instance['id']}").json()

    assert assigned.status_code == 200
    assert projected["volume"]["strategyVerifiedQuoteVolume"] == "0"
    assert projected["volume"]["strategyTargetQuoteVolume"] == "800"
