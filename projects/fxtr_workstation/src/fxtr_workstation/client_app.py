"""ASGI route handler for the workstation's ``/client`` endpoint.

One :func:`client_endpoint` task per WebSocket. The handler:

1. Accepts the WS and registers a :class:`ClientConnectionRecord`
   (which owns the per-connection bounded send queue and cancel scope).
2. Spawns two sibling tasks under a task group:
   - **read loop** — pulls JSON request frames off the WS, parses
     them, dispatches to subscribe / unsubscribe handlers, and
     enqueues an ``ack`` (or ``error``) response.
   - **write loop** — drains the per-connection send queue and
     forwards each :class:`ServerFrame` as a WS text frame.
3. When either loop exits — peer disconnect, parse error past our
   tolerance, overflow-triggered cancel, server shutdown — both are
   torn down via the surrounding cancel scope.

See ``plans/phase0/part1_ws_server/stage2_client_listing.md`` for
the protocol design.
"""

from __future__ import annotations

import logging
from uuid import UUID

import anyio
import anyio.streams.memory
from starlette.websockets import WebSocket, WebSocketDisconnect

from .client_protocol import (
    AckResponse,
    ClientRequest,
    CreateSessionRequest,
    ErrorResponse,
    GetCallbackServiceRequest,
    GetCallbackServiceResponse,
    LoadEntityErrorResponse,
    LoadEntityRequest,
    MutateSessionRequest,
    MutationRejectedResponse,
    MutationRejectReason,
    ProtocolError,
    ResourceSnapshotEvent,
    ServerFrame,
    SessionCreatedResponse,
    SubscribeRequest,
    SubscribeSessionDebugRequest,
    SubscribeSessionRequest,
    UnsubscribeRequest,
    parse_text,
    serialize_to_text,
)
from .state import (
    ClientConnectionRecord,
    ResourceSubscription,
    SessionDebugSubscription,
    SessionSubscription,
    WorkstationState,
    _build_session_debug_snapshot,
    _PendingMutation,
    _project_record,
    direct_load_worker,
)

_log = logging.getLogger(__name__)


def _peer_addr(websocket: WebSocket) -> str | None:
    client = websocket.client
    if client is None:
        return None
    return f"{client.host}:{client.port}"


async def client_endpoint(websocket: WebSocket) -> None:
    """``/client`` ASGI handler. Owns the connection's lifetime."""
    state: WorkstationState = websocket.app.state.workstation_state
    await websocket.accept()
    async with state.add_client_connection(_peer_addr(websocket)) as (
        record,
        receive_stream,
    ):
        async with anyio.create_task_group() as tg:

            async def _run_read() -> None:
                try:
                    await _read_loop(websocket, state, record)
                finally:
                    tg.cancel_scope.cancel()

            async def _run_write() -> None:
                try:
                    await _write_loop(websocket, receive_stream)
                finally:
                    tg.cancel_scope.cancel()

            tg.start_soon(_run_read)
            tg.start_soon(_run_write)


async def _read_loop(
    websocket: WebSocket,
    state: WorkstationState,
    record: ClientConnectionRecord,
) -> None:
    """Pull request frames from the WS, dispatch, ack/error each.

    A protocol error on a single frame surfaces as an
    :class:`ErrorResponse`; the connection stays open. The loop only
    exits when the WS itself disconnects, or via cancellation.
    """
    while True:
        try:
            text = await websocket.receive_text()
        except WebSocketDisconnect:
            return

        # Parse — protocol errors get an error response, not a
        # connection close. We don't have a parsed request_id on
        # parse failure, so use 0; the client correlates by
        # request_id where possible but otherwise sees the message
        # text.
        try:
            request = parse_text(text)
        except ProtocolError as e:
            record.send_nowait(ErrorResponse(request_id=0, message=str(e)))
            continue

        # Dispatch — handler exceptions become error responses
        # tagged with the request_id.
        try:
            _dispatch(state, record, request)
        except Exception as e:
            record.send_nowait(ErrorResponse(request_id=request.request_id, message=str(e)))


async def _write_loop(
    websocket: WebSocket,
    receive_stream: anyio.streams.memory.MemoryObjectReceiveStream[ServerFrame],
) -> None:
    """Drain the per-connection send queue into the WebSocket.

    Exits when the send stream is closed (sender side dropped) or
    when the WebSocket fails to send. Either way the surrounding
    ``client_endpoint`` task group cancels the read loop.
    """
    try:
        async for frame in receive_stream:
            try:
                await websocket.send_text(serialize_to_text(frame))
            except WebSocketDisconnect:
                return
    except (anyio.EndOfStream, anyio.ClosedResourceError):
        return


def _dispatch(
    state: WorkstationState,
    record: ClientConnectionRecord,
    request: ClientRequest,
) -> None:
    """Sync — runs from within the read loop, no awaits."""
    if isinstance(request, SubscribeRequest):
        _handle_subscribe(state, record, request)
        return
    if isinstance(request, UnsubscribeRequest):
        _handle_unsubscribe(state, record, request)
        return
    if isinstance(request, CreateSessionRequest):
        _handle_create_session(state, record, request)
        return
    if isinstance(request, SubscribeSessionRequest):
        _handle_subscribe_session(state, record, request)
        return
    if isinstance(request, SubscribeSessionDebugRequest):
        _handle_subscribe_session_debug(state, record, request)
        return
    if isinstance(request, MutateSessionRequest):
        _handle_mutate_session(state, record, request)
        return
    if isinstance(request, LoadEntityRequest):
        _handle_load_entity(state, record, request)
        return
    if isinstance(request, GetCallbackServiceRequest):
        _handle_get_callback_service(state, record, request)
        return
    raise AssertionError(f"unhandled request type: {type(request).__name__}")  # pragma: no cover


def _handle_subscribe(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: SubscribeRequest,
) -> None:
    # Per the ID convention, the request_id doubles as the new
    # subscription_id.
    sub_id = req.request_id
    if sub_id in record.subscriptions:
        raise ValueError(f"subscription id {sub_id} is already in use on this connection")
    state._register_subscription(
        record, sub_id, ResourceSubscription(resource=req.resource, debug=req.debug)
    )
    # Ack first, then immediate snapshot. Both go through the same
    # send queue in order; a slow client falling behind here trips
    # overflow on whichever frame fills the buffer.
    record.send_nowait(AckResponse(request_id=req.request_id))
    snapshot_data = [
        _project_record(req.resource, rec, req.debug) for rec in state.build_snapshot(req.resource)
    ]
    record.send_nowait(ResourceSnapshotEvent(subscription_id=sub_id, data=snapshot_data))


def _handle_unsubscribe(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: UnsubscribeRequest,
) -> None:
    if req.subscription_id not in record.subscriptions:
        raise ValueError(f"no active subscription with id {req.subscription_id} on this connection")
    state._unregister_subscription(record, req.subscription_id)
    record.send_nowait(AckResponse(request_id=req.request_id))


# --- Session handlers (stage 3a) -------------------------------------


def _parse_session_uuid(raw: str) -> UUID:
    """Validate the wire-side ``session_id`` string. Raises
    :class:`ValueError` (which the read loop turns into an
    :class:`ErrorResponse`) on a malformed UUID."""
    try:
        return UUID(raw)
    except ValueError as e:
        raise ValueError(f"invalid session_id {raw!r}: {e}") from e


def _handle_create_session(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: CreateSessionRequest,
) -> None:
    session = state.create_session(req.metadata)
    record.send_nowait(
        SessionCreatedResponse(request_id=req.request_id, session_id=str(session.session_id))
    )


def _handle_subscribe_session(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: SubscribeSessionRequest,
) -> None:
    sub_id = req.request_id
    if sub_id in record.subscriptions:
        raise ValueError(f"subscription id {sub_id} is already in use on this connection")
    session_id = _parse_session_uuid(req.session_id)
    if session_id not in state.sessions:
        raise ValueError(f"no session with id {session_id}")
    state._register_subscription(record, sub_id, SessionSubscription(session_id=session_id))
    record.send_nowait(AckResponse(request_id=req.request_id))
    record.send_nowait(state.build_session_snapshot(sub_id, session_id))
    # After the snapshot: surface any service the session already holds
    # that is currently broken (out-of-band side channel, re-derived
    # from current state — covers "broke before I subscribed").
    state.announce_broken_services_to_subscription(record, sub_id, session_id)


def _handle_subscribe_session_debug(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: SubscribeSessionDebugRequest,
) -> None:
    """Subscribe to a session's debug stream (stage 4b).

    Mirrors :func:`_handle_subscribe_session` shape: validate
    ``session_id``, allocate a :class:`SessionDebugSubscription` on
    the per-connection subscription dict (request_id doubles as
    subscription_id), ack, then push an initial
    :class:`SessionDebugSnapshotEvent`. Subsequent snapshots are
    pushed by the refcount/slot-counter hooks in
    :mod:`fxtr_workstation.state`.
    """
    sub_id = req.request_id
    if sub_id in record.subscriptions:
        raise ValueError(f"subscription id {sub_id} is already in use on this connection")
    session_id = _parse_session_uuid(req.session_id)
    session = state.sessions.get(session_id)
    if session is None or session.closed:
        raise ValueError(f"no session with id {session_id}")
    state._register_subscription(record, sub_id, SessionDebugSubscription(session_id=session_id))
    record.send_nowait(AckResponse(request_id=req.request_id))
    record.send_nowait(_build_session_debug_snapshot(state, session, sub_id))


def _handle_load_entity(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: LoadEntityRequest,
) -> None:
    """Out-of-band direct entity load (stage 8b).

    Sync — resolves ``short_id`` against the workstation's active
    entity allocator. On miss replies immediately with
    :class:`LoadEntityErrorResponse`. On hit spawns
    :func:`direct_load_worker` on ``state.background_tg`` to do the
    async load + escape + reply work; the worker writes its
    response directly back to ``record.send_nowait``. No session
    linkage; ``request_id`` is the only correlation handle.
    """
    fxtr_id = state.short_ids.lookup_active_entity(req.short_id)
    if fxtr_id is None:
        record.send_nowait(
            LoadEntityErrorResponse(
                request_id=req.request_id,
                message=f"unknown or inactive entity short id {req.short_id!r}",
            )
        )
        return
    state.background_tg.start_soon(direct_load_worker, state, record, req.request_id, fxtr_id)


def _handle_get_callback_service(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: GetCallbackServiceRequest,
) -> None:
    """Look up the workstation short_id of a session-owned
    callback's send-side service (stage 10).

    Sync. Validates ``subscription_id`` is a
    :class:`SessionSubscription` on this connection, then resolves
    ``slot`` against ``session.open_callbacks``. Errors via the
    standard read-loop ``ErrorResponse`` path on:

    - subscription_id not bound on this connection;
    - subscription is not a :class:`SessionSubscription`;
    - referenced session has been removed (race against close);
    - slot is not currently in ``open_callbacks``.

    On success, replies with :class:`GetCallbackServiceResponse`
    carrying :attr:`CallbackRecord.service_short_id`.
    """
    sub = record.subscriptions.get(req.subscription_id)
    if not isinstance(sub, SessionSubscription):
        raise ValueError(
            f"no active session subscription with id {req.subscription_id} on this connection"
        )
    session = state.sessions.get(sub.session_id)
    if session is None:
        raise ValueError(f"session {sub.session_id} no longer exists")
    cb_record = session.open_callbacks.get(req.slot)
    if cb_record is None:
        raise ValueError(f"no open callback at slot {req.slot} in session {sub.session_id}")
    record.send_nowait(
        GetCallbackServiceResponse(
            request_id=req.request_id,
            subscription_id=req.subscription_id,
            slot=req.slot,
            service_short_id=cb_record.service_short_id,
        )
    )


def _handle_mutate_session(
    state: WorkstationState,
    record: ClientConnectionRecord,
    req: MutateSessionRequest,
) -> None:
    """Validate the subscription handle locally, then enqueue the
    request onto the target session's mutation queue. The session's
    worker drains the queue sequentially and runs the full pipeline
    (validation, apply, broadcast, response) via
    :meth:`WorkstationState.apply_mutation`.

    All rejection paths surface as :class:`MutationRejectedResponse`
    on the connection's send queue; this function returns ``None``
    on every code path.
    """

    def _reject(reason: MutationRejectReason, message: str = "") -> None:
        record.send_nowait(
            MutationRejectedResponse(request_id=req.request_id, reason=reason, message=message)
        )

    sub = record.subscriptions.get(req.subscription_id)
    if not isinstance(sub, SessionSubscription):
        _reject(
            "validation_failed",
            f"no active session subscription with id {req.subscription_id} on this connection",
        )
        return

    send_stream = state.session_mutation_queues.get(sub.session_id)
    if send_stream is None:
        _reject("session_gone", f"no session with id {sub.session_id}")
        return

    pending = _PendingMutation(
        mutator_connection=record,
        mutator_subscription_id=req.subscription_id,
        request_id=req.request_id,
        ack_seq=req.ack_seq,
        update_id=req.update_id,
        patch=req.patch,
        ops=req.ops,
    )
    try:
        send_stream.send_nowait(pending)
    except anyio.WouldBlock:
        _reject("session_busy", "session mutation queue is full; retry shortly")
    except anyio.ClosedResourceError:
        # Race: the session was closed between our ``get`` above and
        # the ``send_nowait``. Surface the same rejection as the
        # missing-entry case.
        _reject("session_gone", f"no session with id {sub.session_id}")
