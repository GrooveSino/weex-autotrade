import threading

from fleet_api.account_boundary_reader import AccountBoundaryReader
from fleet_api.execution_io import ExecutionIoBudget


class ConcurrentBoundaryGateway:
    def __init__(self, barrier: threading.Barrier, closed: list[str], lane: str = "root") -> None:
        self._barrier = barrier
        self._closed = closed
        self._lane = lane

    def fork(self) -> "ConcurrentBoundaryGateway":
        lane = f"lane-{len(self._closed)}-{id(self)}"
        return ConcurrentBoundaryGateway(self._barrier, self._closed, lane)

    def account_balance_rows(self, mode: str) -> list[dict[str, str]]:
        assert mode == "live"
        self._barrier.wait(timeout=2)
        return [{"asset": "USDT", "availableBalance": "321.50"}]

    def positions(self, mode: str, symbol: str) -> list[dict[str, object]]:
        assert mode == "live"
        self._barrier.wait(timeout=2)
        if symbol == "BTC":
            return [{"size": "0.001", "side": "long", "markPrice": "65000", "id": "private"}]
        return []

    def open_orders(self, symbol: str, *, mode: str) -> list[dict[str, str]]:
        assert mode == "live"
        return [{"id": "private"}] if symbol == "ETH" else []

    def algo_orders(self, symbol: str) -> dict[str, list[dict[str, str]]]:
        return {"data": [{"id": "private"}]} if symbol == "BTC" else {"data": []}

    def close(self) -> None:
        self._closed.append(self._lane)


def test_account_boundary_reader_runs_balance_and_symbol_lanes_concurrently() -> None:
    closed: list[str] = []
    budget = ExecutionIoBudget(max_normal=8, max_emergency=2)
    reader = AccountBoundaryReader(budget, max_workers=3)
    try:
        boundary = reader.read(ConcurrentBoundaryGateway(threading.Barrier(3), closed))  # type: ignore[arg-type]
    finally:
        reader.close()

    assert boundary["available_quote"] == "321.5"
    assert boundary["position_count"] == 1
    assert boundary["regular_order_count"] == 1
    assert boundary["trigger_order_count"] == 1
    assert boundary["flat"] is False
    assert boundary["blocking_positions"] == [
        {
            "symbol": "BTC",
            "side": "long",
            "quantity": "0.001",
            "approximate_quote": "65",
        }
    ]
    assert budget.snapshot().peak_normal >= 3
    assert len(closed) == 2


def test_account_boundary_reader_rejects_a_missing_usdt_balance() -> None:
    class MissingUsdtGateway(ConcurrentBoundaryGateway):
        def account_balance_rows(self, mode: str) -> list[dict[str, str]]:
            assert mode == "live"
            self._barrier.wait(timeout=2)
            return [{"asset": "BTC", "availableBalance": "1"}]

    reader = AccountBoundaryReader(ExecutionIoBudget(max_normal=8, max_emergency=2), max_workers=3)
    try:
        try:
            reader.read(MissingUsdtGateway(threading.Barrier(3), []))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - assert the public validation boundary
            assert "no USDT row" in str(exc)
        else:
            raise AssertionError("missing USDT balance must fail closed")
    finally:
        reader.close()
