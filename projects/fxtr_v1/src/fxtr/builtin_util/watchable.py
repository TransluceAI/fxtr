from __future__ import annotations

import abc
import contextlib
from typing import Callable, Iterator

import anyio

from fxtr.object_model import core


class Watchable(
    core.FxtrCallTarget,
    interface_id="Watchable^1",
    namespace_id="!builtin.util",
    stateless=True,
):
    @abc.abstractmethod
    async def poll(self) -> tuple[core.SendableValue, Watchable] | None:
        """Gets the current state of the watchable, if it is active.

        Returns: Either a tuple (current_value, next_watchable) or None. If None, this means the
        watchable is no longer active and will not produce any more values. Otherwise, the first
        value is the current value of the watchable, and the second value is a new watchable that
        can be polled to get an updated value. (The new watchable will block until the next value
        is available after the current value.)
        """
        ...


class _WatchablePublisher:
    def __init__(self):
        self._next_evt = anyio.Event()
        self._first_evt = self._next_evt
        self._last_value: tuple[core.SendableValue, Watchable] | None = None
        self._closed = False

    def initial_watchable(self) -> Watchable:
        """Get a watchable that will return any value including the first."""
        return _EvtWatchable(self._first_evt, self)

    def publish(self, value: core.SendableValue) -> None:
        """Publish a value to the watchable."""
        if self._closed:
            raise ValueError("Watchable is closed")
        last_next_evt = self._next_evt
        self._next_evt = anyio.Event()
        self._last_value = (value, _EvtWatchable(self._next_evt, self))
        last_next_evt.set()

    def close(self) -> None:
        """Close the watchable."""
        self._last_value = None
        self._next_evt.set()
        self._closed = True


class _EvtWatchable(Watchable):
    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, evt: anyio.Event, publisher: _WatchablePublisher):
        self._evt = evt
        self._publisher = publisher

    async def poll(self) -> tuple[core.SendableValue, Watchable] | None:
        await self._evt.wait()
        return self._publisher._last_value  # pyright: ignore[reportPrivateUsage]


@contextlib.contextmanager
def publishing_watchable() -> Iterator[tuple[Callable[[core.SendableValue], None], Watchable]]:
    """Create a watchable that we can publish values to in a scope.

    Yields:
        A tuple (publish_func, watchable). The publish_func is a function that can be used to
        publish values to the watchable. The watchable is a watchable that can be polled to get the
        published values.
    """
    publisher = _WatchablePublisher()
    try:
        yield (publisher.publish, publisher.initial_watchable())
    finally:
        publisher.close()
