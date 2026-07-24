from __future__ import annotations

from threading import Event

from fleet_api.execution_io import BoundedGateway, ExecutionIoBudget


class _Gateway:
    available = "100"


def test_gateway_wrapper_forwards_non_private_assignments_to_the_wrapped_gateway() -> None:
    raw = _Gateway()
    wrapper = BoundedGateway(raw, ExecutionIoBudget(max_normal=1, max_emergency=1), Event())

    wrapper.available = "99.75"

    assert raw.available == "99.75"
