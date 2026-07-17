from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from weex_cli.adaptive_executor import VenueOrder
from weex_cli.adaptive_maker import MarketSnapshot, Side
from weex_cli.errors import ValidationError


@dataclass(frozen=True)
class SimulationConfig:
    initial_mid: float = 62_500.0
    tick_size: float = 0.1
    step_ms: int = 50
    queue_factor: float = 0.35
    base_depth: float = 0.018
    base_flow_per_sec: float = 0.032
    drift_change_probability: float = 0.035

    def __post_init__(self) -> None:
        values = (
            self.initial_mid,
            self.tick_size,
            self.queue_factor,
            self.base_depth,
            self.base_flow_per_sec,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValidationError("simulation values must be finite and positive")
        if self.step_ms <= 0 or not 0 <= self.drift_change_probability <= 1:
            raise ValidationError("simulation step or drift probability is invalid")


class SimulatedMakerVenue:
    """Seeded level-one simulator where accepted resting orders can only fill as Maker."""

    def __init__(
        self,
        seed: int,
        config: SimulationConfig | None = None,
        *,
        fault: str | None = None,
    ) -> None:
        self.config = config or SimulationConfig()
        self._random = random.Random(seed)
        self._now_ms = 0
        self._position = 0.0
        self._mid_ticks = round(self.config.initial_mid / self.config.tick_size)
        self._drift = self._random.choice((-1, 0, 0, 0, 1))
        self._orders: dict[str, VenueOrder] = {}
        self._active_order_id: str | None = None
        self._next_order_id = 1
        self._fault = fault
        self._fetch_count = 0
        self._snapshot = self._make_snapshot()

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def snapshot(self) -> MarketSnapshot:
        return self._snapshot

    def position_quantity(self) -> float:
        return self._position

    def wait_for_submission_slot(self) -> None:
        return None

    def submit_post_only(
        self,
        side: Side,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> VenueOrder:
        if quantity <= 0 or price <= 0 or not math.isfinite(quantity) or not math.isfinite(price):
            raise ValidationError("simulated order quantity and price must be positive")
        if self._active_order_id is not None:
            raise ValidationError("simulator permits only one active order")
        if client_order_id in {order.client_order_id for order in self._orders.values()}:
            raise ValidationError("client order ID must be unique")

        post_only = price < self._snapshot.ask if side == "buy" else price > self._snapshot.bid
        order_id = f"sim-{self._next_order_id:06d}"
        self._next_order_id += 1
        if not post_only:
            order = VenueOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                side=side,
                price=price,
                quantity=quantity,
                filled_quantity=0.0,
                cumulative_quote=0.0,
                status="rejected",
                post_only=True,
                maker=None,
            )
            self._orders[order_id] = order
            return order

        at_book = math.isclose(price, self._snapshot.bid if side == "buy" else self._snapshot.ask)
        displayed = self._snapshot.bid_size if side == "buy" else self._snapshot.ask_size
        queue_ahead = displayed * self.config.queue_factor * self._random.uniform(0.75, 1.25) if at_book else 0.0
        order = VenueOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            side=side,
            price=price,
            quantity=quantity,
            filled_quantity=0.0,
            cumulative_quote=0.0,
            status="new",
            post_only=True,
            maker=None,
            queue_ahead=queue_ahead,
        )
        self._orders[order_id] = order
        self._active_order_id = order_id
        return order

    def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        order = self._get_order(order_id, client_order_id)
        self._fetch_count += 1
        if self._fault == "unknown" and self._fetch_count >= 1 and order.status in {"new", "partially_filled"}:
            return replace(order, status="unknown")
        return order

    def cancel_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        order = self._get_order(order_id, client_order_id)
        if self._fault == "unconfirmed_cancel" and order.status in {"new", "partially_filled"}:
            return order
        if order.status in {"new", "partially_filled"}:
            order = replace(order, status="canceled")
            self._orders[order_id] = order
            self._active_order_id = None
        return order

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValidationError("simulation time cannot move backwards")
        remaining = milliseconds
        while remaining > 0:
            step = min(remaining, self.config.step_ms)
            self._match_active(step)
            self._now_ms += step
            self._evolve_market()
            remaining -= step

    def _get_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        order = self._orders.get(order_id)
        if order is None or order.client_order_id != client_order_id:
            raise ValidationError("simulated order not found")
        return order

    def _match_active(self, step_ms: int) -> None:
        if self._active_order_id is None:
            return
        order = self._orders[self._active_order_id]
        if order.status not in {"new", "partially_filled"}:
            self._active_order_id = None
            return

        competitive = order.price >= self._snapshot.bid if order.side == "buy" else order.price <= self._snapshot.ask
        if not competitive:
            return
        opposing_flow = self._snapshot.sell_flow_per_sec if order.side == "buy" else self._snapshot.buy_flow_per_sec
        available = opposing_flow * (step_ms / 1000) * self._random.uniform(0.75, 1.25)
        queue_consumed = min(order.queue_ahead, available)
        queue_ahead = order.queue_ahead - queue_consumed
        available -= queue_consumed
        if available <= 0:
            self._orders[order.order_id] = replace(order, queue_ahead=queue_ahead)
            return

        remaining = order.quantity - order.filled_quantity
        filled = min(remaining, available)
        total_filled = order.filled_quantity + filled
        cumulative_quote = order.cumulative_quote + filled * order.price
        status = "filled" if total_filled >= order.quantity - 1e-12 else "partially_filled"
        maker = self._fault != "taker_fill"
        updated = replace(
            order,
            filled_quantity=total_filled,
            cumulative_quote=cumulative_quote,
            status=status,
            maker=maker,
            queue_ahead=queue_ahead,
        )
        self._orders[order.order_id] = updated
        self._position += filled if order.side == "buy" else -filled
        if abs(self._position) < 1e-12:
            self._position = 0.0
        if status == "filled":
            self._active_order_id = None

    def _evolve_market(self) -> None:
        if self._random.random() < self.config.drift_change_probability:
            self._drift = self._random.choice((-1, -1, 0, 0, 0, 1, 1))
        move_roll = self._random.random()
        move = 0
        if move_roll < 0.18:
            move = self._drift
        elif move_roll < 0.26:
            move = self._random.choice((-1, 1))
        self._mid_ticks = max(10, self._mid_ticks + move)
        self._snapshot = self._make_snapshot()

    def _make_snapshot(self) -> MarketSnapshot:
        spread_ticks = 2 if self._random.random() < 0.22 else 1
        half = spread_ticks / 2
        bid_ticks = math.floor(self._mid_ticks - half)
        ask_ticks = bid_ticks + spread_ticks
        depth_skew = self._random.uniform(-0.35, 0.35)
        flow_skew = self._random.uniform(-0.45, 0.45) + 0.08 * self._drift
        bid_size = self.config.base_depth * (1 + depth_skew) * self._random.uniform(0.8, 1.2)
        ask_size = self.config.base_depth * (1 - depth_skew) * self._random.uniform(0.8, 1.2)
        buy_flow = self.config.base_flow_per_sec * (1 + flow_skew) * self._random.uniform(0.8, 1.2)
        sell_flow = self.config.base_flow_per_sec * (1 - flow_skew) * self._random.uniform(0.8, 1.2)
        return MarketSnapshot(
            timestamp_ms=self._now_ms,
            bid=bid_ticks * self.config.tick_size,
            ask=ask_ticks * self.config.tick_size,
            bid_size=max(1e-6, bid_size),
            ask_size=max(1e-6, ask_size),
            buy_flow_per_sec=max(1e-6, buy_flow),
            sell_flow_per_sec=max(1e-6, sell_flow),
            volatility_ticks=0.7 + abs(self._drift) * 0.7 + self._random.random() * 0.6,
            tick_size=self.config.tick_size,
        )
