"""Tests for ``force_close_session_one_shot``'s overall-timeout
behavior (fix 2 of stage3_mcp_backend_tweaks).

When the workstation accepts the websocket handshake but never
sends a response frame (server hung / partition / etc.), the
function used to hang on its first ``ws.receive_text()`` call
forever. With the ``anyio.fail_after`` wrapper, the overall
timeout fires and we raise :class:`ForceCloseTimedOut` so the
calling tool can surface a clear error message.

The test spins up a minimal hypercorn + Starlette app that
accepts the ``/client`` upgrade and then sleeps forever — no
response frames. Calling ``force_close_session_one_shot`` with
a sub-second timeout against that endpoint must raise
``ForceCloseTimedOut``.
"""

from __future__ import annotations

import socket

import anyio
import hypercorn.asyncio
import hypercorn.config
import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from fxtr_workstation_mcp.ws_connection import (
    ForceCloseTimedOut,
    force_close_session_one_shot,
)

pytestmark = pytest.mark.anyio


def _reserve_free_port(host: str = "127.0.0.1") -> int:
    s = socket.socket()
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


async def _wait_until_listening(host: str, port: int, *, timeout: float = 5.0) -> None:
    deadline = anyio.current_time() + timeout
    while True:
        try:
            stream = await anyio.connect_tcp(host, port)
        except OSError:
            if anyio.current_time() >= deadline:
                raise RuntimeError(f"hang-server did not bind {host}:{port} within {timeout}s")
            await anyio.sleep(0.02)
            continue
        await stream.aclose()
        return


async def _hang_endpoint(websocket: WebSocket) -> None:
    """Accept the upgrade and then sleep indefinitely. No frames
    sent back. Cancellation propagates through anyio when the
    shutdown trigger fires on the hypercorn side."""
    await websocket.accept()
    await anyio.sleep_forever()


async def test_force_close_times_out_when_server_never_responds() -> None:
    host = "127.0.0.1"
    port = _reserve_free_port(host)
    client_url = f"ws://{host}:{port}/client"

    shutdown = anyio.Event()
    app = Starlette(routes=[WebSocketRoute("/client", _hang_endpoint)])
    config = hypercorn.config.Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = None
    config.errorlog = None

    async def _serve() -> None:
        await hypercorn.asyncio.serve(
            app,  # type: ignore[arg-type]
            config,
            shutdown_trigger=shutdown.wait,
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_serve)
        try:
            await _wait_until_listening(host, port)

            # Sub-second timeout so the test finishes quickly.
            with pytest.raises(ForceCloseTimedOut) as exc_info:
                await force_close_session_one_shot(
                    client_url=client_url,
                    session_id="11111111-2222-3333-4444-555555555555",
                    timeout_seconds=0.5,
                )
            msg = str(exc_info.value)
            assert "timed out after 0.5s" in msg, msg
            assert "local toml left as-is" in msg, msg
        finally:
            shutdown.set()


async def test_force_close_default_timeout_signature() -> None:
    """Sanity check: the default ``timeout_seconds`` is 10.0 so
    production callers don't accidentally hit the test-only
    short timeout."""
    import inspect

    sig = inspect.signature(force_close_session_one_shot)
    param = sig.parameters["timeout_seconds"]
    assert param.default == 10.0
