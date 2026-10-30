"""Test-local wrapper around ``httpx_ws.aconnect_ws``.

``httpx_ws.aconnect_ws`` can occasionally attach a cancellation exception
as its own :attr:`~BaseException.__context__`, which makes anyio loop
when walking the chain. :func:`aconnect_ws` wraps the upstream async
context manager and clears such self-references before re-raising.

Duplicated alongside ``fxtr_workstation_mcp.websocket_util`` so the core
workstation library does not need ``httpx-ws`` at runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx_ws


def _break_self_referential_exception_context(exc: BaseException) -> None:
    e = exc
    while e.__context__:
        if e is e.__context__:
            e.__context__ = None
            break
        e = e.__context__


@asynccontextmanager
async def aconnect_ws(*args: Any, **kwargs: Any) -> AsyncIterator[httpx_ws.AsyncWebSocketSession]:
    """Same interface as :func:`httpx_ws.aconnect_ws`, with exception-chain patching on exit."""
    try:
        async with httpx_ws.aconnect_ws(*args, **kwargs) as ws:
            yield ws
    except BaseException as e:
        _break_self_referential_exception_context(e)
        raise
