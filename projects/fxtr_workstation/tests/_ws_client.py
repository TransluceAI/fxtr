"""Test-only WebSocket client helper.

The ``/rpc`` half (:func:`connect_rpc`) is a thin untyped wrapper over
:func:`fxtr_workstation.peer_rpc.open_workstation_rpc` — the
recommended entry point for non-test code. The ``/client`` half
(:class:`ClientWS` / :func:`connect_client`) has no library
counterpart and keeps its own transport (over
``tests/_websocket_util.aconnect_ws``). Lives under ``tests/`` because
it's only useful for exercising the workstation from outside the
package.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx_ws
import msgspec
from fxtr2.rpc.scope_bridge import RPCSession
from fxtr2.services.service_cap_common import UnknownService
from fxtr_workstation.client_protocol import ClientRequest, ServerFrame
from fxtr_workstation.peer_rpc import open_workstation_rpc

from ._websocket_util import aconnect_ws


@asynccontextmanager
async def connect_rpc(
    url: str,
) -> AsyncGenerator[RPCSession[UnknownService]]:
    """Open an RPC session against ``url`` (``ws://…/rpc``).

    Delegates to :func:`fxtr_workstation.peer_rpc.open_workstation_rpc`
    (use that directly in non-test code); this wrapper only widens the
    remote back to :data:`UnknownService`, because many tests exercise
    ``replace_service_type`` explicitly.

    TODO: migrate the tests to the typed helper and remove this.
    """
    async with open_workstation_rpc(url, name="test-client") as session:
        yield dataclasses.replace(
            session,
            remote_service=session.remote_service.replace_service_type(UnknownService),
        )


_SERVER_FRAME_DECODER = msgspec.json.Decoder(ServerFrame)
_REQUEST_ENCODER = msgspec.json.Encoder()


class ClientWS:
    """Test-only thin wrapper around an httpx-ws connection to the
    workstation's ``/client`` endpoint. Knows how to send
    :class:`ClientRequest` and receive :class:`ServerFrame`.
    """

    def __init__(self, ws: httpx_ws.AsyncWebSocketSession) -> None:
        self._ws = ws

    async def send(self, request: ClientRequest) -> None:
        await self._ws.send_text(_REQUEST_ENCODER.encode(request).decode())

    async def send_text(self, text: str) -> None:
        """Send a raw text frame — used for the malformed-input
        tests that bypass the typed encoder."""
        await self._ws.send_text(text)

    async def receive(self) -> ServerFrame:
        text = await self._ws.receive_text()
        return _SERVER_FRAME_DECODER.decode(text)


@asynccontextmanager
async def connect_client(url: str) -> AsyncGenerator[ClientWS]:
    """Open a ``/client`` connection. Yields a :class:`ClientWS`."""
    async with aconnect_ws(url) as ws:
        yield ClientWS(ws)
