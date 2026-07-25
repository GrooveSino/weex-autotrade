"""Async actors that keep timer waits out of exchange and campaign threads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from threading import Event, Lock, Thread
from typing import TypeVar

from fleet_api.execution.runtime.execution_actor_state import ActorPhase, ActorPhaseQueue, ExecutionActorState
from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity, PhaseQueueState, PhaseReservation

T = TypeVar("T")
ActorProgram = Callable[["ExecutionActor"], Awaitable[None]]
StateSink = Callable[[ExecutionActorState], None]


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeSnapshot:
    actor_count: int
    event_loop_delay_p99_ms: int
    normal_worker_capacity: int
    emergency_worker_capacity: int


class ExecutionActor:
    """One serial command stream for an account, backed by an asyncio Task."""

    def __init__(
        self,
        execution_id: str,
        account_id: str,
        capacity: ExecutionCapacity,
        normal_pool: ThreadPoolExecutor,
        emergency_pool: ThreadPoolExecutor,
        state_sink: StateSink,
    ) -> None:
        now_ms = _now_ms()
        self.execution_id = execution_id
        self.account_id = account_id
        self.stop_event = Event()
        self._capacity = capacity
        self._normal_pool = normal_pool
        self._emergency_pool = emergency_pool
        self._state_sink = state_sink
        self._state = ExecutionActorState(execution_id, account_id, "admitted", now_ms)
        self._state_lock = Lock()

    def snapshot(self) -> ExecutionActorState:
        with self._state_lock:
            return self._state

    def request_stop(self) -> None:
        self.stop_event.set()
        if self.snapshot().phase not in {"completed", "stopped", "failed"}:
            self.transition("stopping", reason="stop_requested")

    def transition(
        self,
        phase: ActorPhase,
        *,
        deadline_at_ms: int | None = None,
        phase_queue: ActorPhaseQueue | None = None,
        reason: str | None = None,
    ) -> None:
        with self._state_lock:
            if (
                self._state.phase == phase
                and self._state.wait_deadline_at_ms == deadline_at_ms
                and self._state.phase_queue == phase_queue
                and self._state.reason == reason
            ):
                return
            self._state = replace(
                self._state,
                phase=phase,
                updated_at_ms=_now_ms(),
                wait_deadline_at_ms=deadline_at_ms,
                phase_queue=phase_queue,
                reason=reason,
            )
            state = self._state
        # Monitoring is intentionally observational. A failed SQLite/SSE
        # projection must never rewrite an order lifecycle into recovery.
        with suppress(Exception):
            self._state_sink(state)

    async def sleep_until(
        self,
        deadline_at_ms: int,
        *,
        phase: ActorPhase,
        reason: str | None = None,
    ) -> bool:
        self.transition(phase, deadline_at_ms=deadline_at_ms, reason=reason)
        while not self.stop_event.is_set():
            delay = max(0.0, (deadline_at_ms - _now_ms()) / 1_000)
            if delay <= 0:
                self.transition(phase)
                return True
            await asyncio.sleep(min(delay, 0.25))
        return False

    async def wait_for_normal_phase(
        self,
        phase: str,
        *,
        proxy_key: str,
        round_number: int,
        attempt_number: int | None = None,
    ) -> PhaseReservation | None:
        queue_number = attempt_number if attempt_number is not None else round_number
        key = f"{self.execution_id}:{queue_number}:{phase}"
        while not self.stop_event.is_set():
            reservation = self._capacity.try_start_phase(key, proxy_key=proxy_key)
            if reservation is not None:
                self.transition("opening" if phase == "open" else "closing")
                return reservation
            queued = self._capacity.queue_state(key)
            if queued is not None:
                self._publish_queue(phase, queued, proxy_key)
            await asyncio.sleep(self._capacity.next_phase_wake_seconds(key))
        self._capacity.cancel_phase(key)
        return None

    def finish_normal_phase(self, reservation: PhaseReservation | None) -> None:
        if reservation is not None:
            self._capacity.finish_phase(reservation.key)

    async def run_blocking(
        self,
        operation: Callable[..., T],
        /,
        *args: object,
        emergency: bool = False,
        **kwargs: object,
    ) -> T:
        loop = asyncio.get_running_loop()
        pool = self._emergency_pool if emergency else self._normal_pool
        return await loop.run_in_executor(pool, partial(operation, *args, **kwargs))

    def _publish_queue(self, phase: str, queued: PhaseQueueState, proxy_key: str) -> None:
        constraint = self._capacity.queue_constraint(queued.key)
        existing = self.snapshot()
        previous = existing.phase_queue
        # Capacity probes run every 250ms.  Only persist a revised prediction
        # when its displayed meaning changed; otherwise 200 waiting Actors
        # would generate a journal/SSE heartbeat storm.
        if (
            existing.phase == "phase_queued"
            and previous is not None
            and previous.phase == phase
            and previous.queue_position == queued.queue_position
            and previous.constraint == constraint
            and abs(previous.estimated_start_at_ms - queued.estimated_start_at_ms) < 1_000
        ):
            return
        self.transition(
            "phase_queued",
            deadline_at_ms=queued.estimated_start_at_ms,
            phase_queue=ActorPhaseQueue(
                phase=phase,
                queue_position=queued.queue_position,
                estimated_start_at_ms=queued.estimated_start_at_ms,
                proxy_key=proxy_key,
                constraint=constraint,
            ),
        )


class AsyncExecutionOrchestrator:
    """Hosts up to the admitted logical tasks on one event-loop thread.

    Programs remain responsible for their own exact order semantics.  This
    runtime only owns serial lifecycle execution, cancellable timer waits and
    bounded synchronous phase calls.
    """

    def __init__(
        self,
        capacity: ExecutionCapacity,
        *,
        normal_workers: int,
        emergency_workers: int,
        state_sink: StateSink | None = None,
    ) -> None:
        self._capacity = capacity
        self._normal_workers = normal_workers
        self._emergency_workers = emergency_workers
        self._state_sink = state_sink or (lambda _state: None)
        self._normal_pool = ThreadPoolExecutor(max_workers=normal_workers, thread_name_prefix="fleet-io")
        self._emergency_pool = ThreadPoolExecutor(max_workers=emergency_workers, thread_name_prefix="fleet-safe")
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="fleet-actors", daemon=True)
        self._actors: dict[str, ExecutionActor] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = Lock()
        self._loop_delays_ms: list[int] = []
        self._closed = False
        self._thread.start()
        self._probe_future = asyncio.run_coroutine_threadsafe(self._measure_event_loop(), self._loop)

    def start(self, execution_id: str, account_id: str, program: ActorProgram) -> Future[None]:
        with self._lock:
            if self._closed:
                raise RuntimeError("execution orchestrator is closed")
            if execution_id in self._actors:
                raise RuntimeError("execution actor already exists")
            actor = ExecutionActor(
                execution_id,
                account_id,
                self._capacity,
                self._normal_pool,
                self._emergency_pool,
                self._state_sink,
            )
            self._actors[execution_id] = actor
        # Persist the explicit admission before any preparation work begins.
        # This lets the UI distinguish an accepted actor from an execution that
        # has already reached a normal opening/closing slot.
        with suppress(Exception):
            self._state_sink(actor.snapshot())
        return asyncio.run_coroutine_threadsafe(self._launch(actor, program), self._loop)

    def stop(self, execution_id: str) -> bool:
        with self._lock:
            actor = self._actors.get(execution_id)
        if actor is None:
            return False
        actor.request_stop()
        self._loop.call_soon_threadsafe(lambda: None)
        return True

    def has_actor(self, execution_id: str) -> bool:
        with self._lock:
            return execution_id in self._actors

    def active_count(self) -> int:
        with self._lock:
            return len(self._actors)

    def state(self, execution_id: str) -> ExecutionActorState | None:
        with self._lock:
            actor = self._actors.get(execution_id)
        return actor.snapshot() if actor is not None else None

    def snapshot(self) -> ExecutionRuntimeSnapshot:
        with self._lock:
            delays = sorted(self._loop_delays_ms)
            p99 = delays[int((len(delays) - 1) * 0.99)] if delays else 0
            return ExecutionRuntimeSnapshot(
                actor_count=len(self._actors),
                event_loop_delay_p99_ms=p99,
                normal_worker_capacity=self._normal_workers,
                emergency_worker_capacity=self._emergency_workers,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            actors = tuple(self._actors.values())
        for actor in actors:
            actor.request_stop()
        self._probe_future.cancel()
        with suppress(Exception):
            self._probe_future.result(timeout=1)
        # Do not cancel actor tasks here.  Cancellation skips the program's
        # cooperative safe-stop branch while a blocking exchange call can
        # still be executing in a worker thread.  Every admitted actor owns
        # its own stop/cleanup sequence, so shutdown waits for that sequence
        # to reach a terminal state before tearing down its I/O pools.
        future = asyncio.run_coroutine_threadsafe(self._drain_all(), self._loop)
        future.result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._normal_pool.shutdown(wait=True, cancel_futures=False)
        self._emergency_pool.shutdown(wait=True, cancel_futures=False)

    async def _launch(self, actor: ExecutionActor, program: ActorProgram) -> None:
        task = asyncio.create_task(self._run_actor(actor, program))
        with self._lock:
            self._tasks[actor.execution_id] = task
        await task

    async def _run_actor(self, actor: ExecutionActor, program: ActorProgram) -> None:
        try:
            await program(actor)
        except asyncio.CancelledError:
            actor.transition("stopped", reason="orchestrator_closed")
            raise
        except Exception as exc:  # Caller owns durable failure classification.
            actor.transition("failed", reason=f"actor_exception:{type(exc).__name__.lower()}")
        finally:
            self._capacity.release_execution(actor.execution_id)
            with self._lock:
                self._tasks.pop(actor.execution_id, None)
                self._actors.pop(actor.execution_id, None)

    async def _drain_all(self) -> None:
        while True:
            with self._lock:
                tasks = tuple(self._tasks.values())
                actor_count = len(self._actors)
            if not tasks and actor_count == 0:
                return
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                continue
            # ``start`` may have registered an actor immediately before
            # shutdown while its launch coroutine is still queued.
            await asyncio.sleep(0)

    async def _measure_event_loop(self) -> None:
        expected = time.monotonic()
        while True:
            await asyncio.sleep(0.05)
            current = time.monotonic()
            delay = max(0, int((current - expected - 0.05) * 1_000))
            with self._lock:
                self._loop_delays_ms = (self._loop_delays_ms + [delay])[-200:]
            expected = current

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
