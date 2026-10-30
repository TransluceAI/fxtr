"""Pending: an awaitable future for step results."""

from typing import Any, Generator

import anyio


class Pending[R]:
    """A pending result of a step.

    Can be awaited to get the value. Can also be forwarded to other steps
    as an argument, where the runtime will resolve it before dispatch.
    """

    def __init__(self) -> None:
        self._event = anyio.Event()
        self._result: R | None = None
        self._exception: BaseException | None = None
        self._resolved = False

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    def set_result(self, result: R) -> None:
        if self._resolved:
            raise RuntimeError("Pending already resolved")
        self._result = result
        self._resolved = True
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        if self._resolved:
            raise RuntimeError("Pending already resolved")
        self._exception = exc
        self._resolved = True
        self._event.set()

    def __await__(self) -> Generator[Any, Any, R]:
        yield from self._event.wait().__await__()
        if self._exception is not None:
            raise self._exception
        return self._result  # type: ignore[return-value]
