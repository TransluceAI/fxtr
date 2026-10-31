"""Resource lifetime helpers for model and context policy sessions."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import anyio


class _AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


@asynccontextmanager
async def closing_session[T: _AsyncCloseable](session: T) -> AsyncIterator[T]:
    """Close a session on every exit, allowing async cleanup during cancellation."""
    try:
        yield session
    finally:
        with anyio.CancelScope(shield=True):
            await session.aclose()
