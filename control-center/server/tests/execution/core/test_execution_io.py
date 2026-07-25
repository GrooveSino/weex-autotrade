from __future__ import annotations

from threading import Event

from fleet_api.execution.runtime.execution_io import NORMAL_IO_PRIORITY, BoundedGateway, ExecutionIoBudget


class _Gateway:
    available = "100"

    def __init__(self) -> None:
        self.forks: list[_Gateway] = []

    def fork(self) -> _Gateway:
        child = _Gateway()
        self.forks.append(child)
        return child


class _RecordingBudget:
    def __init__(self) -> None:
        self.emergency_calls: list[bool] = []

    def call(self, operation, /, *args, emergency: bool, **kwargs):  # type: ignore[no-untyped-def]
        self.emergency_calls.append(emergency)
        return operation(*args, **kwargs)


def test_gateway_wrapper_forwards_non_private_assignments_to_the_wrapped_gateway() -> None:
    raw = _Gateway()
    wrapper = BoundedGateway(raw, ExecutionIoBudget(max_normal=1, max_emergency=1), Event())

    wrapper.available = "99.75"

    assert raw.available == "99.75"


def test_shared_stream_fork_can_ignore_the_creating_actor_stop_state() -> None:
    raw = _Gateway()
    stopped = Event()
    stopped.set()
    budget = _RecordingBudget()
    wrapper = BoundedGateway(raw, budget, stopped)

    child = wrapper.fork(priority=NORMAL_IO_PRIORITY)

    assert isinstance(child, BoundedGateway)
    assert budget.emergency_calls == [False]
