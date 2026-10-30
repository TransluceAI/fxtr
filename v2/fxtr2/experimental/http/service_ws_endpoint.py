"""WebSocket RPC endpoint for stateless services.

Design under
``projects/fxtr_workstation/plans/phase0/part7a_stateless_ws_server/``
(wire protocol ``design/01_wire_protocol.md``, decisions
``open_questions.md``).

One endpoint multiplexes every ws service sharing its url. Wire (one
JSON object per text frame):

    client → server:  {type:"invoke", id, service, method, args, kwargs}
    server → client:  {type:"result", id, value}
                      {type:"error",  id, message}
                      {type:"notify", inbox, payload}

The only server-side state is the **per-connection table**, which dies
with the connection:

1. **Inbox routes** — every inbox uuid seen in a ``["callback", uuid]``
   arg on this connection. ``ctx.notify(addr, payload)`` emits a
   ``notify`` frame for a known uuid, else drops silently (mirrors the
   workstation inbox's silent-drop-when-closed).
2. **Transient services** — a ws impl returns a
   :class:`TransientService` (e.g. the unsubscribe handle from
   ``subscribe``); the codec registers it (minting a connection-scoped
   id) and invokes addressed to that id dispatch back to it.

Each invoke runs as its own task (no head-of-line blocking). No request
timeouts; ordering is ws FIFO (a method that notifies then returns has
its notifies delivered first); no ping, and no ``error_type``
(decisions ``open_questions.md`` Q-O / Q-K).
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from .service_codec import CallbackAddress, TransientService, escape_value, unescape_args
from .service_dispatch import ServiceError, ServiceImpl, invoke_impl

if TYPE_CHECKING:
    from fxtr2.data.entity_ref_scope import FxtrScope

WsEndpoint = Callable[[WebSocket], Awaitable[None]]


class _ConnectionTable:
    """Per-connection routing state (inbox uuids + transient services)."""

    def __init__(self) -> None:
        self.inbox_uuids: set[str] = set()
        self.transients: dict[str, TransientService] = {}

    def register_transient(self, transient: TransientService) -> str:
        """Store a transient service and return its connection-scoped id.

        Args:
            transient: The transient service to register.

        Returns:
            The minted id (invokes addressed to it dispatch back here).
        """
        tid = f"transient-{uuid4().hex}"
        self.transients[tid] = transient
        return tid


class WsServiceContext:
    """``ctx`` handed to a ws service impl: ``scope`` (load entity args),
    ``notify`` (push to an inbox seen in args), ``transient`` (wrap an
    impl as a connection-scoped service to return)."""

    def __init__(
        self,
        scope: FxtrScope,
        table: _ConnectionTable,
        send_frame: Callable[[dict[str, Any]], None],
        *,
        provenanced: bool = False,
    ) -> None:
        self.scope = scope
        self._table = table
        self._send_frame = send_frame
        self._provenanced = provenanced

    def notify(self, addr: CallbackAddress | str, payload: Any) -> None:
        """Push a ``notify`` frame to an inbox seen on this connection.

        Args:
            addr: The target inbox (a ``CallbackAddress`` or its uuid).
            payload: The value to deliver (escaped before sending).

        Silently drops if the inbox uuid was never seen in a callback
        arg on this connection (mirrors the workstation inbox's
        silent-drop-when-closed).
        """
        uuid = addr.uuid if isinstance(addr, CallbackAddress) else addr
        if uuid not in self._table.inbox_uuids:
            return  # client never registered this inbox on this connection — drop
        escaped = escape_value(
            payload,
            register_transient=self._table.register_transient,
            provenanced=self._provenanced,
        )
        self._send_frame({"type": "notify", "inbox": uuid, "payload": escaped})

    def transient(self, impl: ServiceImpl) -> TransientService:
        """Wrap an impl as a connection-scoped service to return.

        Args:
            impl: The transient service impl (callable or method-map).

        Returns:
            A ``TransientService`` the codec registers + encodes on the
            way out.
        """
        return TransientService(impl)


def _collect_callback_uuids(value: Any, out: set[str]) -> None:
    """Walk a decoded value, collecting every ``CallbackAddress`` uuid.

    Args:
        value: The decoded (unescaped) arg/kwarg value to walk.
        out: Accumulator set the found inbox uuids are added to.
    """
    if isinstance(value, CallbackAddress):
        out.add(value.uuid)
    elif isinstance(value, Mapping):
        for v in value.values():
            _collect_callback_uuids(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_callback_uuids(v, out)


def ws_services_endpoint(
    services: Mapping[str, ServiceImpl], *, scope: FxtrScope, provenanced: bool = False
) -> WsEndpoint:
    """Build the ASGI handler for a ws endpoint exposing ``services``.

    Args:
        services: The ``{name: impl}`` map of services multiplexed on
            this ws url.
        scope: The fxtr scope handed to impls via ``ctx``.
        provenanced: When ``True``, layered entity refs in results + notify
            payloads emit the layered form; ``False`` (default) bare.

    Returns:
        An async ``endpoint(websocket)`` ASGI handler owning the
        connection (read/write loops + per-connection table).
    """

    async def endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        send_stream, recv_stream = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=math.inf
        )
        table = _ConnectionTable()

        def send_frame(frame: dict[str, Any]) -> None:
            try:
                send_stream.send_nowait(frame)
            except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
                # Connection draining / gone — drop (e.g. a late notify).
                pass

        ctx = WsServiceContext(scope, table, send_frame, provenanced=provenanced)

        async with anyio.create_task_group() as tg:

            async def _handle_invoke(frame: dict[str, Any]) -> None:
                req_id = frame.get("id")
                try:
                    if frame.get("type") != "invoke":
                        raise ServiceError(f"unexpected frame type: {frame.get('type')!r}")
                    service_name = frame.get("service")
                    method = frame.get("method")
                    if not isinstance(service_name, str) or not isinstance(method, str):
                        raise ServiceError("invoke frame missing 'service'/'method'")
                    args, kwargs = unescape_args(
                        {"args": frame.get("args", []), "kwargs": frame.get("kwargs", {})}
                    )
                    # Learn the inboxes this connection may be notified on.
                    seen: set[str] = set()
                    for v in (*args, *kwargs.values()):
                        _collect_callback_uuids(v, seen)
                    table.inbox_uuids |= seen

                    impl = services.get(service_name)
                    if impl is None:
                        transient = table.transients.get(service_name)
                        if transient is None:
                            raise ServiceError(f"unknown service: {service_name!r}")
                        impl = transient.impl
                    result = await invoke_impl(impl, method, args, kwargs, ctx)
                    value = escape_value(
                        result,
                        register_transient=table.register_transient,
                        provenanced=provenanced,
                    )
                    send_frame({"type": "result", "id": req_id, "value": value})
                except Exception as e:  # noqa: BLE001 — any failure → error frame
                    send_frame({"type": "error", "id": req_id, "message": str(e)})

            async def _read_loop() -> None:
                try:
                    while True:
                        try:
                            text = await websocket.receive_text()
                        except WebSocketDisconnect:
                            return
                        try:
                            frame = json.loads(text)
                        except json.JSONDecodeError as e:
                            send_frame(
                                {"type": "error", "id": None, "message": f"invalid JSON: {e}"}
                            )
                            continue
                        if not isinstance(frame, dict):
                            send_frame(
                                {"type": "error", "id": None, "message": "frame must be an object"}
                            )
                            continue
                        tg.start_soon(_handle_invoke, frame)
                finally:
                    send_stream.close()
                    tg.cancel_scope.cancel()

            async def _write_loop() -> None:
                try:
                    async for frame in recv_stream:
                        try:
                            await websocket.send_text(json.dumps(frame))
                        except WebSocketDisconnect:
                            return
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return
                finally:
                    tg.cancel_scope.cancel()

            tg.start_soon(_read_loop)
            tg.start_soon(_write_loop)

    return endpoint
