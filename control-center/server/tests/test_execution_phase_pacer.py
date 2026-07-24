from __future__ import annotations

from fleet_api.execution_phase_pacer import ExecutionPhasePacer


class FakeClock:
    def __init__(self) -> None:
        self.value = 10.0

    def monotonic(self) -> float:
        return self.value

    def now_ms(self) -> int:
        return 1_000_000 + int(self.value * 1_000)


class FakeStop:
    def __init__(self, clock: FakeClock, *, stopped: bool = False) -> None:
        self.clock = clock
        self.stopped = stopped

    def wait(self, seconds: float) -> bool:
        if not self.stopped:
            self.clock.value += seconds
        return self.stopped


def test_many_accounts_receive_noncolliding_idempotent_phase_slots() -> None:
    clock = FakeClock()
    pacer = ExecutionPhasePacer(
        minimum_gap_seconds=5,
        jitter_max_seconds=15,
        monotonic=clock.monotonic,
        now_ms=clock.now_ms,
        randbelow=lambda upper: upper - 1,
    )
    events: list[dict[str, object]] = []

    for index in range(50):
        assert pacer.wait(
            f"execution-{index}:plan:1:open",
            phase="open",
            round_number=1,
            stop_event=FakeStop(clock),  # type: ignore[arg-type]
            event_sink=events.append,
        )

    deadlines = [int(event["deadline_at_ms"]) for event in events if event["event"] == "phase_pacing_started"]
    assert len(deadlines) == 50
    assert all(right - left >= 5_000 for left, right in zip(deadlines, deadlines[1:], strict=False))
    before = len(events)
    assert pacer.wait(
        "execution-0:plan:1:open",
        phase="open",
        round_number=1,
        stop_event=FakeStop(clock),  # type: ignore[arg-type]
        event_sink=events.append,
    )
    assert len(events) == before


def test_stop_interrupts_pacing_without_completing_the_slot() -> None:
    clock = FakeClock()
    pacer = ExecutionPhasePacer(
        minimum_gap_seconds=5,
        jitter_max_seconds=15,
        monotonic=clock.monotonic,
        now_ms=clock.now_ms,
        randbelow=lambda upper: upper - 1,
    )
    events: list[dict[str, object]] = []

    assert not pacer.wait(
        "execution:plan:2:close",
        phase="close",
        round_number=2,
        stop_event=FakeStop(clock, stopped=True),  # type: ignore[arg-type]
        event_sink=events.append,
    )
    assert [event["event"] for event in events] == ["phase_pacing_started", "phase_pacing_cancelled"]
