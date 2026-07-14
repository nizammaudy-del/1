"""Compatibility shims for Python versions older than 3.10."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint

# `@dataclass(slots=True)` needs 3.10+.
# Use `@dataclass(**DATACLASS_SLOTS)` to get slots where available
# without breaking 3.9.
DATACLASS_SLOTS: dict[str, bool] = {"slots": True} if sys.version_info >= (3, 10) else {}

if sys.version_info >= (3, 10):
    import asyncio
    import importlib.metadata
    from contextlib import aclosing

    def entry_points_group(group: str) -> list[EntryPoint]:
        """Entry points for ``group`` (`entry_points(group=...)`
        needs 3.10+)."""
        # Resolved dynamically so tests can monkeypatch
        # importlib.metadata.entry_points.
        return list(importlib.metadata.entry_points(group=group))

    AsyncLock = asyncio.Lock
    AsyncEvent = asyncio.Event
    AsyncSemaphore = asyncio.Semaphore
    AsyncQueue = asyncio.Queue
    AsyncCondition = asyncio.Condition

else:
    import asyncio
    import collections
    import importlib.metadata
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def aclosing(thing: Any) -> AsyncIterator[Any]:
        try:
            yield thing
        finally:
            await thing.aclose()

    def entry_points_group(group: str) -> list[EntryPoint]:
        """Entry points for ``group`` (`entry_points(group=...)`
        needs 3.10+)."""
        # Resolved dynamically so tests can monkeypatch
        # importlib.metadata.entry_points.
        eps = importlib.metadata.entry_points()
        if isinstance(eps, dict):  # 3.9 returns {group: [EntryPoint, ...]}
            return list(eps.get(group, []))
        return list(eps)

    class _LazyLoopMixin:
        """Defer event-loop binding to first use inside a running loop.

        Python 3.9's asyncio primitives call ``get_event_loop()`` eagerly in
        ``__init__``, which (a) raises when constructed in a sync context with
        no loop set, and (b) binds to a loop that may not be the one the
        primitive is later awaited in. Python 3.10 made binding lazy; these
        subclasses backport that behavior by skipping the eager binding and
        resolving ``self._loop`` at first use via a property.
        """

        _lazy_loop: Any = None

        @property
        def _loop(self) -> Any:
            loop = asyncio.get_running_loop()
            if self._lazy_loop is None:
                self._lazy_loop = loop
            if self._lazy_loop is not loop:
                raise RuntimeError(f"{self!r} is bound to a different event loop")
            return loop

    class AsyncLock(_LazyLoopMixin, asyncio.Lock):
        def __init__(self) -> None:
            # State from 3.9 Lock.__init__, minus the eager loop binding.
            self._waiters = None
            self._locked = False

    class AsyncEvent(_LazyLoopMixin, asyncio.Event):
        def __init__(self) -> None:
            # State from 3.9 Event.__init__, minus the eager loop binding.
            self._waiters = collections.deque()
            self._value = False

    class AsyncSemaphore(_LazyLoopMixin, asyncio.Semaphore):
        def __init__(self, value: int = 1) -> None:
            # State from 3.9 Semaphore.__init__, minus the eager loop binding.
            if value < 0:
                raise ValueError("Semaphore initial value must be >= 0")
            self._value = value
            self._waiters = collections.deque()
            self._wakeup_scheduled = False

    class AsyncCondition(_LazyLoopMixin, asyncio.Condition):
        def __init__(self, lock: Any = None) -> None:
            # State from 3.9 Condition.__init__, minus the eager loop binding
            # (and minus its lock._loop agreement check, which is enforced
            # lazily at await time by _LazyLoopMixin instead).
            if lock is None:
                lock = AsyncLock()
            self._lock = lock
            # Same method re-exports CPython 3.9's Condition.__init__ does.
            self.locked = lock.locked  # type: ignore[method-assign]
            self.acquire = lock.acquire  # type: ignore[method-assign]
            self.release = lock.release  # type: ignore[method-assign]
            self._waiters = collections.deque()

    class AsyncQueue(_LazyLoopMixin, asyncio.Queue):
        def __init__(self, maxsize: int = 0) -> None:
            # State from 3.9 Queue.__init__, minus the eager loop binding.
            self._maxsize = maxsize
            self._getters: collections.deque[Any] = collections.deque()
            self._putters: collections.deque[Any] = collections.deque()
            self._unfinished_tasks = 0
            self._finished = AsyncEvent()
            self._finished.set()
            self._init(maxsize)


__all__ = [
    "DATACLASS_SLOTS",
    "AsyncCondition",
    "AsyncEvent",
    "AsyncLock",
    "AsyncQueue",
    "AsyncSemaphore",
    "aclosing",
    "entry_points_group",
]
