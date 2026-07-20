from __future__ import annotations

import ccxt
import pytest

from weex_cli.reliability import ReadRetryPolicy, retry_read


def test_retry_read_recovers_with_exponential_capped_backoff() -> None:
    calls = 0
    delays: list[float] = []
    events: list[dict[str, object]] = []

    def reader() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise ccxt.RequestTimeout("temporary timeout")
        return "ready"

    result = retry_read(
        reader,
        operation="positions",
        policy=ReadRetryPolicy(attempts=5, initial_delay_seconds=1, max_delay_seconds=2),
        sleep=delays.append,
        retry_sink=lambda event: events.append(dict(event)),
    )

    assert result == "ready"
    assert calls == 4
    assert delays == [1, 2, 2]
    assert [event["next_attempt"] for event in events] == [2, 3, 4]


def test_retry_read_exhaustion_reraises_last_network_error() -> None:
    calls = 0

    def reader() -> None:
        nonlocal calls
        calls += 1
        raise ccxt.NetworkError("still unavailable")

    with pytest.raises(ccxt.NetworkError, match="still unavailable"):
        retry_read(
            reader,
            operation="orders",
            policy=ReadRetryPolicy(attempts=3, initial_delay_seconds=0),
            sleep=lambda _: None,
        )

    assert calls == 3


def test_retry_read_does_not_hide_validation_or_programming_errors() -> None:
    calls = 0

    def reader() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("malformed response")

    with pytest.raises(ValueError, match="malformed response"):
        retry_read(reader, operation="positions", sleep=lambda _: None)

    assert calls == 1
