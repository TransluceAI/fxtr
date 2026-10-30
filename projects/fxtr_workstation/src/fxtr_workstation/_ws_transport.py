"""Starlette-WebSocket → ``ObjectStream[JSONTypes]`` adapter.

Each WebSocket text frame carries one JSON message. We layer
:class:`fxtr_sandbox_types.json_object_stream.JSONObjectStream` on top of
the WebSocket's send/receive callables to get an in-band EOF sentinel
(``["!", "eof"]``), which gives us proper half-close semantics on a
transport that doesn't support half-close natively. After
``send_eof()``, the local side can keep ``receive``-ing pending frames
from the peer until *they* send their EOF sentinel back.

Lift the result with :func:`fxtr2.rpc.json_serdes.wrap_json_stream` to
get an ``ObjectStream[RPCMessage]`` ready for ``open_rpc_scope``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import anyio.abc
from fxtr2.rpc.json_serdes import JSONTypes
from fxtr_sandbox_types.json_object_stream import JSONObjectStream
from starlette.websockets import WebSocket, WebSocketDisconnect


@asynccontextmanager
async def asgi_websocket_object_stream(
    websocket: WebSocket,
) -> AsyncGenerator[anyio.abc.ObjectStream[JSONTypes]]:
    """Wrap an already-accepted Starlette WebSocket as a JSON ObjectStream.

    Caller must have awaited ``websocket.accept()`` before entering. The
    underlying WebSocket is closed on context exit.
    """
    _WIRE_LOG = os.environ.get("FXTR_WORKSTATION_WIRE_LOG") == "1"

    async def _send(item: JSONTypes) -> None:
        if _WIRE_LOG:
            print(f"[SERVER → CLIENT] {json.dumps(item)}", flush=True)
        try:
            await websocket.send_text(json.dumps(item))
        except WebSocketDisconnect as e:
            raise anyio.BrokenResourceError from e
        except RuntimeError as e:
            raise anyio.BrokenResourceError(str(e)) from e

    async def _receive() -> JSONTypes:
        try:
            text = await websocket.receive_text()
        except WebSocketDisconnect:
            raise anyio.EndOfStream from None
        if _WIRE_LOG:
            print(f"[CLIENT → SERVER] {text}", flush=True)
        return json.loads(text)

    stream = JSONObjectStream(_send, _receive)
    try:
        yield stream
    finally:
        await stream.aclose()
        try:
            await websocket.close()
        except RuntimeError:
            # already closed by peer
            pass
