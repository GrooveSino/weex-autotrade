from __future__ import annotations

from types import SimpleNamespace

from fleet_api.execution.runtime.execution_recovery import (
    boundary_state,
    recovery_delay_ms,
    recovery_due,
    recovery_metadata,
)


def _record(*, status: str = "recovering", metadata: dict[str, object] | None = None):
    return SimpleNamespace(status=status, metadata=metadata or {})


def test_recovery_backoff_is_bounded() -> None:
    assert [recovery_delay_ms(value) for value in range(1, 8)] == [1_000, 2_000, 5_000, 10_000, 30_000, 30_000, 30_000]


def test_only_recovering_records_become_due() -> None:
    assert recovery_due(_record(), 1_000)
    assert not recovery_due(_record(status="stopped"), 1_000)
    assert not recovery_due(_record(metadata={"next_recovery_check_at_ms": 2_000}), 1_000)


def test_owned_boundary_requires_matching_side_and_bounded_quantity() -> None:
    record = _record(
        metadata={
            "execution_ownership": {
                "legs": {
                    "BTC": {"position_side": "long", "owned_quantity": "0.001", "amount_step": "0.0001"},
                    "ETH": {"position_side": "short", "owned_quantity": "0.02", "amount_step": "0.001"},
                }
            }
        }
    )
    boundary = {
        "flat": False,
        "blocking_positions": [
            {"symbol": "BTC", "side": "long", "quantity": "0.001"},
            {"symbol": "ETH", "side": "short", "quantity": "0.02"},
        ],
    }
    assert boundary_state(record, boundary) == "owned_exposure"
    boundary["blocking_positions"][0]["quantity"] = "0.01"
    assert boundary_state(record, boundary) == "external_exposure"


def test_flat_boundary_never_requires_ownership() -> None:
    assert boundary_state(_record(), {"flat": True}) == "flat"


def test_recovery_metadata_advances_attempt_and_deadline() -> None:
    metadata = recovery_metadata(
        _record(metadata={"recovery_attempt": 2}),
        now_ms=10_000,
        state="waiting_read",
        boundary="unknown",
        reason="timeout",
    )
    assert metadata["recovery_attempt"] == 3
    assert metadata["next_recovery_check_at_ms"] == 15_000
