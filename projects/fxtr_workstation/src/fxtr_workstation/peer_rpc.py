"""Client-side WebSocket → workstation RPC session helper.

The public client counterpart of :mod:`._ws_transport` (the server-side
Starlette adapter): :func:`open_workstation_rpc` connects to a
workstation's ``/rpc`` endpoint over ``httpx-ws``, layers the same
:class:`~fxtr_sandbox_types.json_object_stream.JSONObjectStream` EOF
discipline over the websocket (the in-band ``["!", "eof"]`` sentinel
that gives half-close semantics on a transport without native
half-close), and opens an fxtr2 RPC scope typed against
:class:`~fxtr_workstation.protocols.WorkstationServiceProto`.

Requires the ``client`` extra (``fxtr_workstation[client]``): the
websocket *client* dependency (``httpx-ws``) is deliberately not a core
dependency of the workstation library — embedding the server does not
require a ws client, so this module is import-on-demand and is not
re-exported from the package ``__init__``.

Typical peer-client flow::

    async with open_workstation_rpc(rpc_url, name="my-tool") as peer:
        ref = await peer.remote_service.get_service_by_external_name("some-name")
        ...  # replace_service_type(...) + resolve through peer.scope

``peer`` is fxtr2's :class:`~fxtr2.rpc.scope_bridge.RPCSession`:
``peer.remote_service`` is the workstation's main service and
``peer.scope`` the mountable scope the connection's bridges live in.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import anyio.abc
import httpx_ws
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.scope_bridge import RPCSession, open_rpc_scope
from fxtr_sandbox_types.json_object_stream import JSONObjectStream

from .protocols import WorkstationServiceProto

__all__ = ["open_workstation_rpc"]


def _break_self_referential_exception_context(exc: BaseException) -> None:
    """Clear a self-referential ``__context__`` link on ``exc``'s chain.

    ``httpx_ws.aconnect_ws`` can occasionally attach a cancellation
    exception as its own :attr:`~BaseException.__context__`, which makes
    anyio loop forever when walking the chain. Same workaround as
    ``fxtr_workstation_mcp.websocket_util`` and the package's
    ``tests/_websocket_util.py`` (each keeps its own copy so their
    dependency sets stay independent).
    """
    e = exc
    while e.__context__:
        if e is e.__context__:
            e.__context__ = None
            break
        e = e.__context__


@asynccontextmanager
async def _httpx_ws_object_stream(
    ws: httpx_ws.AsyncWebSocketSession,
) -> AsyncGenerator[anyio.abc.ObjectStream[JSONTypes]]:
    """Wrap an open httpx-ws session as a JSON ObjectStream.

    One JSON message per text frame; httpx-ws errors map to the anyio
    stream vocabulary (``BrokenResourceError`` on send failure,
    ``EndOfStream`` on receive failure — a clean disconnect and a close
    race look the same to the reader). The stream is closed on context
    exit; the websocket itself is owned by the caller.
    """

    async def _send(item: JSONTypes) -> None:
        try:
            await ws.send_text(json.dumps(item))
        except httpx_ws.HTTPXWSException as e:
            raise anyio.BrokenResourceError from e

    async def _receive() -> JSONTypes:
        try:
            text = await ws.receive_text()
        except httpx_ws.HTTPXWSException:
            # WebSocketDisconnect (clean) or WebSocketNetworkError (race).
            raise anyio.EndOfStream from None
        return json.loads(text)

    stream = JSONObjectStream(_send, _receive)
    try:
        yield stream
    finally:
        await stream.aclose()


@asynccontextmanager
async def open_workstation_rpc(
    rpc_url: str, *, name: str
) -> AsyncGenerator[RPCSession[WorkstationServiceProto]]:
    """Connect to a workstation's ``/rpc`` endpoint as a peer RPC client.

    Opens the websocket, adapts it to an fxtr2 RPC transport (module
    docstring), and enters :func:`~fxtr2.rpc.scope_bridge.open_rpc_scope`
    with no local main service (the workstation never calls back into
    this client's main).

    Args:
        rpc_url: The ``ws://`` / ``wss:`` URL of the workstation's RPC
            endpoint (:attr:`WorkstationServer.rpc_url`).
        name: Diagnostic name for the RPC scope (shows up in scope/task
            names and error reports; pick something identifying the
            caller, e.g. ``"experiment-demo/peer-rpc"``).

    Yields:
        The connection's :class:`~fxtr2.rpc.scope_bridge.RPCSession`
        (``remote_service`` typed as ``WorkstationServiceProto`` +
        ``scope``). Everything bridged through it dies with the context.
    """
    try:
        async with httpx_ws.aconnect_ws(rpc_url) as ws:
            async with _httpx_ws_object_stream(ws) as raw:
                transport = wrap_json_stream(raw)
                async with open_rpc_scope(
                    transport,
                    our_main_service=None,
                    remote_service_type=WorkstationServiceProto,
                    name=name,
                ) as session:
                    yield session
    except BaseException as e:
        _break_self_referential_exception_context(e)
        raise
