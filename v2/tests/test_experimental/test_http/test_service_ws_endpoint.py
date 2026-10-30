"""Tests for the stateless ws service endpoint (``service_ws_endpoint``).

Drives the ASGI handler directly through a fake WebSocket (in-memory
streams) — no real socket / port, fully deterministic. The handler only
uses ``accept`` / ``receive_text`` / ``send_text``.
"""

from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from typing import Any, cast

import anyio
import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.service_ws_endpoint import ws_services_endpoint


class FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in backed by anyio streams."""

    def __init__(self) -> None:
        self._in_send, self._in_recv = anyio.create_memory_object_stream[str](math.inf)
        self._out_send, self._out_recv = anyio.create_memory_object_stream[str](math.inf)
        self.client = None

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        try:
            return await self._in_recv.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            raise WebSocketDisconnect(1000) from None

    async def send_text(self, text: str) -> None:
        self._out_send.send_nowait(text)

    # --- test driver side ---
    def client_send(self, frame: dict[str, Any]) -> None:
        self._in_send.send_nowait(json.dumps(frame))

    def client_disconnect(self) -> None:
        self._in_send.close()

    async def client_recv(self, timeout: float = 2.0) -> dict[str, Any]:
        with anyio.fail_after(timeout):
            return json.loads(await self._out_recv.receive())


async def _echo(args, kwargs, ctx):
    return args[0]


async def _notify_n(args, kwargs, ctx):
    cb, n, prefix = args
    for i in range(n):
        ctx.notify(cb, f"{prefix}-{i}")
    return n


async def _subscribe(args, kwargs, ctx):
    async def _unsubscribe(uargs, ukwargs, uctx):
        return "unsubscribed"

    return ctx.transient({"unsubscribe": _unsubscribe})


async def _fail(args, kwargs, ctx):
    raise RuntimeError(args[0])


async def _notify_unknown(args, kwargs, ctx):
    ctx.notify("never-registered", "dropped")
    return "done"


_MAIN_IMPL = {
    "echo": _echo,
    "notify_n": _notify_n,
    "subscribe": _subscribe,
    "fail": _fail,
    "notify_unknown": _notify_unknown,
}


@asynccontextmanager
async def _running_ws():
    async with FxtrScope(name="test_ws_service") as scope:
        endpoint = ws_services_endpoint({"main": _MAIN_IMPL}, scope=scope)
        ws = FakeWebSocket()
        async with anyio.create_task_group() as tg:
            tg.start_soon(endpoint, cast(WebSocket, ws))
            try:
                yield ws
            finally:
                ws.client_disconnect()


@pytest.mark.anyio
async def test_invoke_echo():
    async with _running_ws() as ws:
        ws.client_send(
            {"type": "invoke", "id": "1", "service": "main", "method": "echo", "args": [42]}
        )
        assert await ws.client_recv() == {"type": "result", "id": "1", "value": 42}


@pytest.mark.anyio
async def test_invoke_error():
    async with _running_ws() as ws:
        ws.client_send(
            {"type": "invoke", "id": "2", "service": "main", "method": "fail", "args": ["boom"]}
        )
        frame = await ws.client_recv()
        assert frame == {"type": "error", "id": "2", "message": "boom"}


@pytest.mark.anyio
async def test_unknown_service_errors():
    async with _running_ws() as ws:
        ws.client_send({"type": "invoke", "id": "3", "service": "nope", "method": "x"})
        frame = await ws.client_recv()
        assert frame["type"] == "error"
        assert frame["id"] == "3"
        assert "unknown service" in frame["message"]


@pytest.mark.anyio
async def test_notify_ordered_before_result():
    async with _running_ws() as ws:
        ws.client_send(
            {
                "type": "invoke",
                "id": "4",
                "service": "main",
                "method": "notify_n",
                "args": [["callback", "cb-1"], 3, "msg"],
            }
        )
        # Three notifies, in order, then the terminal result.
        for i in range(3):
            frame = await ws.client_recv()
            assert frame == {"type": "notify", "inbox": "cb-1", "payload": f"msg-{i}"}
        assert await ws.client_recv() == {"type": "result", "id": "4", "value": 3}


@pytest.mark.anyio
async def test_notify_to_unknown_inbox_is_dropped():
    async with _running_ws() as ws:
        ws.client_send({"type": "invoke", "id": "5", "service": "main", "method": "notify_unknown"})
        # No notify frame — the next frame is the terminal result.
        assert await ws.client_recv() == {"type": "result", "id": "5", "value": "done"}


@pytest.mark.anyio
async def test_transient_subscribe_then_invoke():
    async with _running_ws() as ws:
        ws.client_send(
            {
                "type": "invoke",
                "id": "6",
                "service": "main",
                "method": "subscribe",
                "args": [["callback", "cb-2"]],
            }
        )
        frame = await ws.client_recv()
        assert frame["type"] == "result"
        tag, descriptor = frame["value"]
        assert tag == "service"
        assert descriptor["kind"] == "transient"
        transient_id = descriptor["id"]

        # Invoke the connection-scoped transient by its id.
        ws.client_send(
            {"type": "invoke", "id": "7", "service": transient_id, "method": "unsubscribe"}
        )
        assert await ws.client_recv() == {"type": "result", "id": "7", "value": "unsubscribed"}


@pytest.mark.anyio
async def test_malformed_json_errors():
    async with _running_ws() as ws:
        ws._in_send.send_nowait("{not json")
        frame = await ws.client_recv()
        assert frame["type"] == "error"
        assert frame["id"] is None
