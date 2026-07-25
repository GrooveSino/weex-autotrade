"""Bounded, observable exchange I/O leases for Fleet execution workers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Any, Protocol, TypeVar


@dataclass(frozen=True, slots=True)
class ExecutionIoSnapshot:
    active_normal: int
    max_normal: int
    active_emergency: int
    max_emergency: int
    peak_normal: int
    peak_emergency: int


class _PrioritySource(Protocol):
    def is_set(self) -> bool: ...


class _NormalPriority:
    def is_set(self) -> bool:
        return False


NORMAL_IO_PRIORITY = _NormalPriority()


T = TypeVar("T")


class ExecutionIoBudget:
    """Limit blocking remote calls without ever dropping a mutation request."""

    def __init__(self, *, max_normal: int, max_emergency: int) -> None:
        if max_normal < 1 or max_emergency < 1:
            raise ValueError("execution I/O capacities must be positive")
        self._normal = BoundedSemaphore(max_normal)
        self._emergency = BoundedSemaphore(max_emergency)
        self._max_normal = max_normal
        self._max_emergency = max_emergency
        self._lock = Lock()
        self._active_normal = 0
        self._active_emergency = 0
        self._peak_normal = 0
        self._peak_emergency = 0

    @contextmanager
    def lease(self, *, emergency: bool) -> Iterator[None]:
        semaphore = self._emergency if emergency else self._normal
        semaphore.acquire()
        self._enter(emergency)
        try:
            yield
        finally:
            self._leave(emergency)
            semaphore.release()

    def call(self, operation: Callable[..., T], /, *args: Any, emergency: bool, **kwargs: Any) -> T:
        with self.lease(emergency=emergency):
            return operation(*args, **kwargs)

    def snapshot(self) -> ExecutionIoSnapshot:
        with self._lock:
            return ExecutionIoSnapshot(
                active_normal=self._active_normal,
                max_normal=self._max_normal,
                active_emergency=self._active_emergency,
                max_emergency=self._max_emergency,
                peak_normal=self._peak_normal,
                peak_emergency=self._peak_emergency,
            )

    def _enter(self, emergency: bool) -> None:
        with self._lock:
            if emergency:
                self._active_emergency += 1
                self._peak_emergency = max(self._peak_emergency, self._active_emergency)
            else:
                self._active_normal += 1
                self._peak_normal = max(self._peak_normal, self._active_normal)

    def _leave(self, emergency: bool) -> None:
        with self._lock:
            if emergency:
                self._active_emergency -= 1
            else:
                self._active_normal -= 1


class BoundedGateway:
    """Duck-typed gateway proxy that applies the shared I/O budget to calls."""

    def __init__(self, gateway: Any, budget: ExecutionIoBudget, emergency: _PrioritySource) -> None:
        object.__setattr__(self, "_gateway", gateway)
        object.__setattr__(self, "_budget", budget)
        object.__setattr__(self, "_emergency", emergency)

    def fork(self, *, priority: _PrioritySource | None = None) -> BoundedGateway:
        source = priority or self._emergency
        gateway = self._budget.call(self._gateway.fork, emergency=source.is_set())
        return BoundedGateway(gateway, self._budget, source)

    def close(self) -> None:
        close = getattr(self._gateway, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        member = getattr(self._gateway, name)
        if not callable(member):
            return member

        def invoke(*args: Any, **kwargs: Any) -> Any:
            return self._budget.call(member, *args, emergency=self._emergency.is_set(), **kwargs)

        return invoke

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._gateway, name, value)
