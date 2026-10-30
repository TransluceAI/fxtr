"""Test-only helpers: open workstation ``/rpc`` and ``/client``
connections from a distinct peer (i.e. not the MCP itself).

- :func:`connect_peer_rpc` — registers entities / services into the
  workstation. Used by stage-2+ integration tests that need data
  the MCP can ``lookup_entity`` etc.
- :class:`PeerClient` + :func:`connect_peer_client` — minimal
  ``/client`` subscriber. Stage-3a tests use it to inspect the
  session state from outside the MCP without going through the
  workstation's RPC API.

Mirrors the workstation-side test helpers at
``projects/fxtr_workstation/tests/_ws_client.py`` — kept local here
so this package's tests don't need to import from the workstation's
tests/ tree.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator

import anyio
import anyio.abc
import httpx_ws
import msgspec
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.scope_bridge import RPCSession, open_rpc_scope
from fxtr_sandbox_types.json_object_stream import JSONObjectStream
from fxtr_workstation.client_protocol import ClientRequest, ServerFrame
from fxtr_workstation.protocols import WorkstationServiceProto

from fxtr_workstation_mcp.websocket_util import aconnect_ws

_SERVER_FRAME_DECODER = msgspec.json.Decoder(ServerFrame)
_REQUEST_ENCODER = msgspec.json.Encoder()


@contextlib.asynccontextmanager
async def connect_peer_rpc(rpc_url: str) -> AsyncGenerator[RPCSession[WorkstationServiceProto]]:
    """Open a workstation ``/rpc`` connection as a peer.

    Yields an :class:`RPCSession` with the workstation's main
    service typed as :class:`WorkstationServiceProto`, ready for
    ``register_service`` / ``copy_entity`` calls (typically via
    the ``expose_*`` async-CM helpers).
    """
    async with aconnect_ws(rpc_url) as ws:
        async with _httpx_ws_object_stream(ws) as raw_stream:
            transport = wrap_json_stream(raw_stream)
            async with open_rpc_scope(
                transport,
                our_main_service=None,
                remote_service_type=WorkstationServiceProto,
                name="test-peer/rpc",
            ) as session:
                yield session


class PeerClient:
    """Minimal ``/client`` connection wrapper for tests. Sends typed
    :class:`ClientRequest`s and decodes :class:`ServerFrame`s."""

    def __init__(self, ws: httpx_ws.AsyncWebSocketSession) -> None:
        self._ws = ws

    async def send(self, request: ClientRequest) -> None:
        await self._ws.send_text(_REQUEST_ENCODER.encode(request).decode())

    async def receive(self) -> ServerFrame:
        text = await self._ws.receive_text()
        return _SERVER_FRAME_DECODER.decode(text)


@contextlib.asynccontextmanager
async def connect_peer_client(client_url: str) -> AsyncGenerator[PeerClient]:
    """Open a workstation ``/client`` connection as a peer."""
    async with aconnect_ws(client_url) as ws:
        yield PeerClient(ws)


@contextlib.asynccontextmanager
async def _httpx_ws_object_stream(
    ws: httpx_ws.AsyncWebSocketSession,
) -> AsyncGenerator[anyio.abc.ObjectStream[JSONTypes]]:
    async def _send(item: JSONTypes) -> None:
        try:
            await ws.send_text(json.dumps(item))
        except httpx_ws.HTTPXWSException as e:
            raise anyio.BrokenResourceError from e

    async def _receive() -> JSONTypes:
        try:
            text = await ws.receive_text()
        except httpx_ws.HTTPXWSException:
            raise anyio.EndOfStream from None
        return json.loads(text)

    stream = JSONObjectStream(_send, _receive)
    try:
        yield stream
    finally:
        await stream.aclose()
