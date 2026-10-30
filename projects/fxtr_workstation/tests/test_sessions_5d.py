"""Stage 5d tests: ``notify`` 3-phase delivery + lifecycle.

End-to-end via ``/client``: a session creates a workstation-minted
callback (inbox), an invoked RPC peer receives the bridged
``ServiceRef[WorkstationSessionEventInbox]`` and exercises ``notify``
/ ``listening`` against it. Successful deliveries land on the session
event log as :class:`CallbackEvent`; failures inside the delivery
pipeline raise to the RPC peer rather than producing a session-side
event.

5d.2 covers the basic round-trip happy path plus a ``listening()``
round-trip. 5d.3 covers refs in payload, lifecycle (close /
session-close drain), slot monotonicity, within-mutation
create+invoke+close, cross-session passing of a callback ref, and
drained-but-alive (peer-held bridge surviving session close). The
6.1 ``subscribe_to`` section covers durable feeds: the inbox
subscribing to an ``EventSource`` (a hub-backed test source), source
events landing as :class:`CallbackEvent`\\ s, close-callback /
session-close triggering the workstation-side unsubscribe, per-source
dedup, and subscribe failures surfacing as ``invoke_error``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import anyio
import pytest
from fxtr2.services.service_dialect import FxtrServiceData
from fxtr2.services.subscription_util import SubscriberHub
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    CallbackEvent,
    CallbackSubscriptionEvent,
    InvokeErrorEvent,
    InvokeResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    SessionEventFrame,
    SessionSnapshotEvent,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto, WorkstationSessionEventInbox
from fxtr_workstation.state import provenanced_key_for
from fxtr_workstation.wire_escape import TAG_ENTITY, TAG_SERVICE

from ._fixtures import (
    CallbackInvokerImpl,
    FailingEventSourceImpl,
    HubEventSourceImpl,
    KeyedSourceImpl,
    Note,
    WatchableHubImpl,
)
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


async def _drain_until(
    client: Any, predicate: Callable[[list[Any]], bool], *, timeout: float = 5.0
) -> list[Any]:
    """Receive frames into a list until ``predicate(seen)`` holds."""
    seen: list[Any] = []
    with anyio.fail_after(timeout):
        while not predicate(seen):
            seen.append(await client.receive())
    return seen


def _has_disconnect_event(frames: list[Any], slot: int) -> bool:
    return any(
        isinstance(f, SessionEventFrame)
        and isinstance(f.event, CallbackSubscriptionEvent)
        and f.event.slot == slot
        and f.event.connected is False
        for f in frames
    )


async def test_notify_round_trip_delivers_callback_event() -> None:
    """Session creates a callback at slot 1 and invokes the RPC peer's
    ``CallbackInvokerImpl.fire(["callback", 1], "hello")``. The peer
    resolves the bridged ref and calls ``notify("hello")``, which
    writes a :class:`CallbackEvent` to the session's event log; the
    invoke returns ``None`` (direct push is fire-and-forget) and
    lands as an :class:`InvokeResultEvent` carrying that ``None``.

    Verifies the basic 5d.2 wiring: ``notify`` runs the shared
    delivery helper, captures the dispatch ``call_scope`` via
    ``get_active_scope()``, and produces a ``CallbackEvent`` with
    the expected slot + payload. Also verifies the receive side's
    ``_closed`` flag is still ``False`` afterward.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # Single mutation: create the callback and invoke
                    # ``fire`` against it. The op walk validates
                    # ``["callback", 1]`` against the just-created slot
                    # via the post-pre-commit lookup.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire",
                                    "args": [["callback", 1], "hello"],
                                },
                            ],
                        )
                    )
                    committed = await client.receive()
                    assert isinstance(committed, MutationCommittedResponse)

                    # Ordering: ``fire`` invokes ``notify`` synchronously
                    # within its own dispatch, so the CallbackEvent
                    # commits first, then the InvokeResultEvent.
                    cb_frame = await client.receive()
                    assert isinstance(cb_frame, SessionEventFrame)
                    cb_event = cb_frame.event
                    assert isinstance(cb_event, CallbackEvent)
                    assert cb_event.slot == 1
                    assert cb_event.payload == "hello"

                    invoke_frame = await client.receive()
                    assert isinstance(invoke_frame, SessionEventFrame)
                    invoke_event = invoke_frame.event
                    assert isinstance(invoke_event, InvokeResultEvent)
                    assert invoke_event.slot == 2
                    assert invoke_event.payload is None

                    # Receive side is still open.
                    session = ws.state.sessions[UUID(sid)]
                    assert 1 in session.open_callbacks
                    assert session.open_callbacks[1].impl._closed is False


async def test_listening_via_invoker_while_open() -> None:
    """Invokes ``is_listening`` against a freshly-created callback;
    expects ``True``. Round-trips the protocol's ``listening`` method
    through the same call-scope-bridge path that ``fire`` exercises,
    minus the session-event delivery."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "is_listening",
                                    "args": [["callback", 1]],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # is_listening doesn't commit a CallbackEvent — only
                    # the invoke result event.
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    assert event.slot == 2
                    assert event.payload is True


async def test_fire_many_delivers_one_callback_event_per_payload() -> None:
    """``fire_many`` calls ``notify`` once per payload in order; each
    successful call appends one :class:`CallbackEvent` to the session
    log. Verifies that successive notifies on the same callback work
    and event seq numbers monotonically increase."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire_many",
                                    # Literal list arg — list-based escape
                                    # codec wraps literal lists as ``[[...]]``.
                                    "args": [["callback", 1], [["a", "b", "c"]]],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    payloads: list[str] = []
                    seqs: list[int] = []
                    for _ in range(3):
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        ev = frame.event
                        assert isinstance(ev, CallbackEvent)
                        assert ev.slot == 1
                        payloads.append(ev.payload)
                        seqs.append(ev.seq)
                    assert payloads == ["a", "b", "c"]
                    assert seqs == sorted(seqs) and len(set(seqs)) == 3

                    invoke_frame = await client.receive()
                    assert isinstance(invoke_frame, SessionEventFrame)
                    ie = invoke_frame.event
                    assert isinstance(ie, InvokeResultEvent)
                    # ``fire_many`` returns the number of pushes made.
                    assert ie.payload == 3


# --- 5d.3: refs in payload -----------------------------------------


async def test_notify_with_entity_ref_in_payload() -> None:
    """``fire`` is invoked with payload that's the workstation-tracked
    entity short_id (the wire-codec turns it into a ``FxtrID`` for
    the peer). The peer's ``notify`` re-encodes via the escape codec,
    landing the entity tag inside the :class:`CallbackEvent` payload
    and bumping the session's entity refcount for that event.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire",
                                    "args": [
                                        ["callback", 1],
                                        ["entity", ent_short_id],
                                    ],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    cb_frame = await client.receive()
                    assert isinstance(cb_frame, SessionEventFrame)
                    cb_event = cb_frame.event
                    assert isinstance(cb_event, CallbackEvent)
                    assert cb_event.slot == 1
                    assert cb_event.payload == [TAG_ENTITY, ent_short_id]

                    # Session has a refcount on the entity from this
                    # event; ``event_refs[seq]`` records it so retention
                    # prune knows to decref later.
                    session = ws.state.sessions[UUID(sid)]
                    note_fid = ws.state.short_ids.lookup_active_entity(ent_short_id)
                    assert note_fid is not None
                    note_key = provenanced_key_for(note_fid)
                    assert session.entity_refcounts.get(note_key, 0) >= 1
                    refs_e, _refs_s = session.event_refs[cb_event.seq]
                    assert note_key in refs_e

                    # Drain the trailing invoke result.
                    invoke_frame = await client.receive()
                    assert isinstance(invoke_frame, SessionEventFrame)
                    assert isinstance(invoke_frame.event, InvokeResultEvent)


async def test_notify_with_service_ref_in_payload() -> None:
    """``fire`` is invoked with payload that's a service short_id —
    the bridged ``ServiceRef`` round-trips through ``notify`` and
    re-encodes as ``["service", short_id]`` inside the
    :class:`CallbackEvent` payload, with the session taking a
    refcount on the service for that event.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire",
                                    # Send the invoker's own short_id
                                    # back through as the payload — any
                                    # workstation-tracked service works.
                                    "args": [
                                        ["callback", 1],
                                        ["service", invoker_short_id],
                                    ],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    cb_frame = await client.receive()
                    assert isinstance(cb_frame, SessionEventFrame)
                    cb_event = cb_frame.event
                    assert isinstance(cb_event, CallbackEvent)
                    assert cb_event.payload == [TAG_SERVICE, invoker_short_id]

                    session = ws.state.sessions[UUID(sid)]
                    invoker_key = ws.state.short_ids.lookup_active_service(invoker_short_id)
                    assert invoker_key is not None
                    assert session.service_refcounts.get(invoker_key, 0) >= 1
                    _refs_e, refs_s = session.event_refs[cb_event.seq]
                    assert invoker_key in refs_s

                    # Drain the trailing invoke result.
                    invoke_frame = await client.receive()
                    assert isinstance(invoke_frame, SessionEventFrame)
                    assert isinstance(invoke_frame.event, InvokeResultEvent)


# --- 5d.3: lifecycle ----------------------------------------------


async def test_close_callback_drops_slot_and_marks_closed() -> None:
    """A ``close_callback`` op pops the slot from ``open_callbacks``
    and flips the impl's ``_closed`` flag (so any later
    ``notify`` against the send-side returns ``False`` and
    ``listening()`` returns ``False``)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 1}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    session = ws.state.sessions[UUID(sid)]
                    cb_record = session.open_callbacks[1]
                    assert cb_record.impl._closed is False

                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "close_callback", "slot": 1}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert 1 not in session.open_callbacks
                    assert cb_record.impl._closed is True


async def test_session_close_drains_open_callbacks() -> None:
    """A close-mutation tears down the session and the close sweep
    walks ``open_callbacks``, flipping each impl's ``_closed`` flag
    and emptying the dict before the service-refcount drain runs."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {"kind": "create_callback", "slot": 2},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    session = ws.state.sessions[UUID(sid)]
                    cb1 = session.open_callbacks[1]
                    cb2 = session.open_callbacks[2]

                    # Close the session via a fresh subscription
                    # (``create_and_subscribe``'s exit cleanup), but
                    # do it directly here so we can assert the post-
                    # close state before the helper exits.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert UUID(sid) not in ws.state.sessions
                    assert cb1.impl._closed is True
                    assert cb2.impl._closed is True


async def test_create_callback_slot_monotonicity_rejects_non_monotonic() -> None:
    """``create_callback``'s slot follows the same strict-monotonicity
    rule as ``load_entity`` / ``invoke`` slots: the new slot must be
    strictly greater than every slot already claimed in this or any
    earlier mutation."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    # Claim slot 5 first.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 5}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Try slot 3 → rejected.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 3}],  # type: ignore[list-item]
                        )
                    )
                    rejected = await client.receive()
                    assert isinstance(rejected, MutationRejectedResponse)
                    assert rejected.reason == "validation_failed"

                    # Try slot 5 again (already claimed) → also rejected.
                    await client.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 5}],  # type: ignore[list-item]
                        )
                    )
                    rejected = await client.receive()
                    assert isinstance(rejected, MutationRejectedResponse)
                    assert rejected.reason == "validation_failed"

                    # Slot 6 succeeds.
                    await client.send(
                        MutateSessionRequest(
                            request_id=13,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=4,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 6}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    session = ws.state.sessions[UUID(sid)]
                    assert set(session.open_callbacks.keys()) == {5, 6}


# --- 5d.3: within-mutation create+invoke+close ---------------------


async def test_within_mutation_create_invoke_close_succeeds_with_closed_notify() -> None:
    """A single mutation ``[CreateCallbackOp(1), InvokeOp(fire,
    ["callback", 1]), CloseCallbackOp(1)]`` commits cleanly. The
    commit-phase order is: create attaches → invoke arg-walk
    increfs the cb's send-side as in-flight → close pops the slot
    and flips ``_closed = True`` (and decrefs the open-callback
    contribution; the in-flight ref still holds the record) →
    invoke worker spawns and dispatches.

    By the time the worker's ``fire`` reaches ``notify``, the impl's
    ``_closed`` is already ``True``, so ``notify`` short-circuits to a
    silent no-op and **no** :class:`CallbackEvent` is committed; the
    invoke worker still delivers an :class:`InvokeResultEvent`
    (carrying ``fire``'s ``None``). The within-mutation close makes
    the callback unusable to its own pre-commit-spawned invoke, but
    the pipeline doesn't crash and the underlying service record
    drains cleanly once the worker drops its in-flight ref.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire",
                                    "args": [["callback", 1], "x"],
                                },
                                {"kind": "close_callback", "slot": 1},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Close ran during commit → slot 1 gone, _closed
                    # flipped before the worker dispatched.
                    session = ws.state.sessions[UUID(sid)]
                    assert 1 not in session.open_callbacks

                    invoke_frame = await client.receive()
                    assert isinstance(invoke_frame, SessionEventFrame)
                    invoke_event = invoke_frame.event
                    assert isinstance(invoke_event, InvokeResultEvent)
                    assert invoke_event.slot == 2
                    # ``notify`` saw ``_closed=True`` and silently
                    # dropped; ``fire`` returns ``None`` regardless.
                    assert invoke_event.payload is None

                    # No CallbackEvent committed.
                    assert not any(isinstance(ev, CallbackEvent) for ev in session.event_log)


# --- 5d.3: cross-session passing -----------------------------------


async def test_cross_session_callback_routes_to_originating_session() -> None:
    """Two sessions A and B share one wsclient connection. A creates a
    callback at slot 1 and ``return_ref(["callback", 1])``s it through
    an invoke, surfacing the cb's send-side as ``["service",
    cb_short_id]`` in A's invoke result. B then ``track_service``s
    the cb (so it survives if A drops its own holds) and invokes
    ``fire(["service", cb_short_id], "from-b")``.

    The peer's ``notify`` lands a :class:`CallbackEvent` in **A**'s
    event log (because the impl carries A's ``session_id``), not B's.
    B sees only the :class:`InvokeResultEvent` for ``fire``. This
    pins down the routing assertion that the basic round-trip
    doesn't cover: ``notify`` follows the cb's originating session,
    not the invoking session.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client_a,
                connect_client(ws.client_url) as client_b,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client_a) as (a_sid, a_sub),
                    create_and_subscribe(client_b) as (b_sid, b_sub),
                ):
                    # A creates the cb and round-trips it through
                    # ``return_ref`` to surface the send-side
                    # short_id.
                    await client_a.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=a_sub,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "return_ref",
                                    "args": [["callback", 1]],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client_a.receive(), MutationCommittedResponse)
                    a_invoke_frame = await client_a.receive()
                    assert isinstance(a_invoke_frame, SessionEventFrame)
                    a_invoke_event = a_invoke_frame.event
                    assert isinstance(a_invoke_event, InvokeResultEvent)
                    # ``return_ref`` returns the same ref it got;
                    # encoded as a service tag.
                    payload = a_invoke_event.payload
                    assert isinstance(payload, list) and payload[0] == TAG_SERVICE
                    cb_short_id = payload[1]

                    # Sanity-check: cb_short_id matches the workstation's
                    # internal record.
                    cb_record = ws.state.sessions[UUID(a_sid)].open_callbacks[1]
                    assert cb_record.service_short_id == cb_short_id

                    # B pins the cb (so dropping A's open-callback /
                    # event-retention contributions can't release it
                    # before B fires) and then fires it.
                    await client_b.send(
                        MutateSessionRequest(
                            request_id=20,
                            subscription_id=b_sub,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "track_service", "short_id": cb_short_id},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire",
                                    "args": [
                                        ["service", cb_short_id],
                                        "from-b",
                                    ],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client_b.receive(), MutationCommittedResponse)

                    # B's stream: only the invoke result. No
                    # CallbackEvent — the cb routes to A.
                    b_invoke_frame = await client_b.receive()
                    assert isinstance(b_invoke_frame, SessionEventFrame)
                    b_invoke_event = b_invoke_frame.event
                    assert isinstance(b_invoke_event, InvokeResultEvent)
                    assert b_invoke_event.payload is None

                    # A's stream: the CallbackEvent.
                    a_cb_frame = await client_a.receive()
                    assert isinstance(a_cb_frame, SessionEventFrame)
                    a_cb_event = a_cb_frame.event
                    assert isinstance(a_cb_event, CallbackEvent)
                    assert a_cb_event.slot == 1
                    assert a_cb_event.payload == "from-b"

                    # B's session log is CallbackEvent-free.
                    b_session = ws.state.sessions[UUID(b_sid)]
                    assert not any(isinstance(ev, CallbackEvent) for ev in b_session.event_log)


# --- 5d.3: drained-but-alive ---------------------------------------


async def test_drained_but_alive_peer_bridge_survives_session_close() -> None:
    """The cb's send-side ``service_scope`` stays alive while an
    external RPC peer holds a bridge to it, even after every
    workstation-side tracker drops.

    Sequence:

    - Session creates cb at slot 1.
    - Peer pulls the cb into its own scope via
      ``WorkstationServiceProto.get_service(cb_short_id)``. From now
      on the peer holds an independent bridge.
    - Session closes the cb (``close_callback``) and then closes
      itself. The workstation's ``service_records`` drops the cb's
      entry; the short_id deactivates.
    - Peer's resolved proxy is still usable. ``notify("after")``
      silently no-ops (impl was flipped ``_closed=True`` by the
      close-callback path); ``listening()`` returns ``False``.
    - When the peer eventually drops its scope (handled by
      ``connect_rpc`` exit), the underlying ``service_scope`` drains.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                ws_proxy = await ws_ref.resolve(rpc.scope)

                async with create_and_subscribe(client) as (sid, sub_id):
                    # Create the cb.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 1}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    session = ws.state.sessions[UUID(sid)]
                    cb_record = session.open_callbacks[1]
                    cb_short_id = cb_record.service_short_id
                    cb_service_key = cb_record.service_key

                    # Peer pulls the cb into its scope. From now on
                    # the bridge is held independently of any
                    # workstation-side tracker.
                    cb_ref = await ws_proxy.get_service(cb_short_id)
                    cb_proxy = await cb_ref.replace_service_type(
                        WorkstationSessionEventInbox
                    ).resolve(rpc.scope)

                    # While still open: listening returns True.
                    assert await cb_proxy.listening() is True

                    # Close the cb.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "close_callback", "slot": 1}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # After close: impl is _closed; cb still tracked
                    # because the session record still references the
                    # send-side via the (now-dropped) open-callback
                    # contribution? Let's see the actual state.
                    # The open-callback contribution decrefs in commit;
                    # if no other contributions exist, the cb's
                    # service_records entry is gone.
                    assert cb_record.impl._closed is True

                    # Close the session entirely.
                    await client.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=2,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert UUID(sid) not in ws.state.sessions

                    # Workstation registry has dropped the cb (the
                    # drained-but-alive surface): the record is
                    # popped from ``service_records`` and the short_id
                    # is no longer active.
                    assert cb_service_key not in ws.state.service_records
                    assert ws.state.short_ids.lookup_active_service(cb_short_id) is None

                    # But the peer's bridge is still alive — listening
                    # / notify still dispatch successfully; the impl is
                    # _closed so listening is ``False`` and notify is a
                    # silent no-op.
                    assert await cb_proxy.listening() is False
                    assert await cb_proxy.notify("after") is None


# --- 6.1: subscribe_to durable feeds --------------------------------


@dataclasses.dataclass
class _FeedHarness:
    """Plumbing for the ``subscribe_to`` tests: a workstation, one
    client session with a callback open at slot 1, and a hub-backed
    event source (plus an always-failing one) exposed by the RPC
    peer."""

    ws: WorkstationServer
    client: Any
    hub: SubscriberHub[FxtrServiceData]
    sid: str
    sub_id: int
    cb_short_id: str
    src_short_id: str
    failing_src_short_id: str


@asynccontextmanager
async def _feed_harness() -> AsyncGenerator[_FeedHarness]:
    async with WorkstationServer() as ws:
        async with (
            connect_rpc(ws.rpc_url) as rpc,
            connect_client(ws.client_url) as client,
        ):
            ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
            async with anyio.create_task_group() as source_tg:
                try:
                    hub = SubscriberHub[FxtrServiceData](task_group=source_tg)
                    source_ref = await rpc.scope.mount(HubEventSourceImpl(hub))
                    failing_ref = await rpc.scope.mount(FailingEventSourceImpl())
                    async with (
                        expose_service(ws_ref, "source", source_ref, rpc.scope) as src_short_id,
                        expose_service(
                            ws_ref, "failing-source", failing_ref, rpc.scope
                        ) as failing_src_short_id,
                        create_and_subscribe(client) as (sid, sub_id),
                    ):
                        await client.send(
                            MutateSessionRequest(
                                request_id=10,
                                subscription_id=sub_id,
                                ack_seq=0,
                                update_id=1,
                                patch=[],
                                ops=[{"kind": "create_callback", "slot": 1}],  # type: ignore[list-item]
                            )
                        )
                        assert isinstance(await client.receive(), MutationCommittedResponse)
                        cb_short_id = (
                            ws.state.sessions[UUID(sid)].open_callbacks[1].service_short_id
                        )
                        yield _FeedHarness(
                            ws=ws,
                            client=client,
                            hub=hub,
                            sid=sid,
                            sub_id=sub_id,
                            cb_short_id=cb_short_id,
                            src_short_id=src_short_id,
                            failing_src_short_id=failing_src_short_id,
                        )
                finally:
                    # Tear down any hub workers still alive (the
                    # session-close path inside the CMs above already
                    # unsubscribed in the normal case).
                    source_tg.cancel_scope.cancel()


async def _subscribe_inbox_to_source(
    h: _FeedHarness,
    *,
    request_id: int,
    ack_seq: int,
    update_id: int,
    slot: int,
    src_short_id: str | None = None,
) -> InvokeResultEvent | InvokeErrorEvent:
    """Invoke ``subscribe_to`` on the slot-1 inbox's send-side service
    and return the resulting invoke event (result or error).

    On success the workstation emits a ``connected=True``
    :class:`CallbackSubscriptionEvent` on the inbox's slot *ahead* of
    the subscribe_to invoke's own result, so drain that first. On
    failure (subscribe raised) no connected event is emitted — the
    invoke error arrives directly.
    """
    await h.client.send(
        MutateSessionRequest(
            request_id=request_id,
            subscription_id=h.sub_id,
            ack_seq=ack_seq,
            update_id=update_id,
            patch=[],
            ops=[
                {  # type: ignore[list-item]
                    "kind": "invoke",
                    "slot": slot,
                    "service_short_id": h.cb_short_id,
                    "method_name": "subscribe_to",
                    "args": [["service", src_short_id or h.src_short_id]],
                },
            ],
        )
    )
    assert isinstance(await h.client.receive(), MutationCommittedResponse)
    frame = await h.client.receive()
    assert isinstance(frame, SessionEventFrame)
    event = frame.event
    if isinstance(event, CallbackSubscriptionEvent):
        # Connected transition lands ahead of the invoke result.
        assert event.connected is True
        assert event.slot == 1
        assert event.reason is None
        frame = await h.client.receive()
        assert isinstance(frame, SessionEventFrame)
        event = frame.event
    assert isinstance(event, (InvokeResultEvent, InvokeErrorEvent))
    assert event.slot == slot
    return event


async def test_subscribe_to_delivers_source_events_as_callback_events() -> None:
    """The inbox subscribes to an ``EventSource`` via ``subscribe_to``
    (a normal invoke against its send-side service); every value the
    source publishes afterwards lands in the session event log as a
    :class:`CallbackEvent` on the inbox's slot — receipt is identical
    to direct-push ``notify``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            event = await _subscribe_inbox_to_source(
                h, request_id=11, ack_seq=1, update_id=2, slot=2
            )
            assert isinstance(event, InvokeResultEvent)
            assert event.payload is None
            assert h.hub.live_subscription_count == 1

            for i, payload in enumerate(["evt-1", "evt-2"]):
                h.hub.publish(payload)
                frame = await h.client.receive()
                assert isinstance(frame, SessionEventFrame)
                cb_event = frame.event
                assert isinstance(cb_event, CallbackEvent), f"event {i}: {cb_event!r}"
                assert cb_event.slot == 1
                assert cb_event.payload == payload


async def test_close_callback_unsubscribes_from_source() -> None:
    """Closing the callback slot triggers the workstation-side
    unsubscribe: the source's hub drops the subscription, and later
    publishes deliver nothing to the session."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await _subscribe_inbox_to_source(h, request_id=11, ack_seq=1, update_id=2, slot=2)
            h.hub.publish("before-close")
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            assert isinstance(frame.event, CallbackEvent)

            await h.client.send(
                MutateSessionRequest(
                    request_id=12,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=3,
                    patch=[],
                    ops=[{"kind": "close_callback", "slot": 1}],  # type: ignore[list-item]
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)

            # The workstation's parker unsubscribes from the source.
            await _wait_until(lambda: h.hub.live_subscription_count == 0)

            # A later publish delivers nothing: the session's event
            # seq stays put.
            session = h.ws.state.sessions[UUID(h.sid)]
            seq_before = session.event_seq
            h.hub.publish("after-close")
            await anyio.sleep(0.15)
            assert session.event_seq == seq_before


async def test_session_close_unsubscribes_from_source() -> None:
    """Closing the whole session sweeps the open callback, which wakes
    the subscription parker → unsubscribe on the source."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await _subscribe_inbox_to_source(h, request_id=11, ack_seq=1, update_id=2, slot=2)
            assert h.hub.live_subscription_count == 1

            await h.client.send(
                MutateSessionRequest(
                    request_id=12,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=3,
                    patch=[],
                    ops=[{"kind": "close"}],  # type: ignore[list-item]
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)
            assert UUID(h.sid) not in h.ws.state.sessions

            await _wait_until(lambda: h.hub.live_subscription_count == 0)


async def test_close_callback_handle_drop_alone_tears_down_source_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the workstation's best-effort ``unsubscribe`` never
    runs (timeout forced to 0), the parker's forceful subscription-
    scope teardown releases the source's ``UnsubscribeHandle`` — and
    that handle drop alone tears the source-side subscription down
    promptly, with no delivery needed. (Pre-redesign, a missed
    unsubscribe left a zombie subscription until the next ``on_event``
    failed.)"""
    monkeypatch.setattr("fxtr_workstation.callback_impl.UNSUBSCRIBE_TIMEOUT_SECONDS", 0.0)
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await _subscribe_inbox_to_source(h, request_id=11, ack_seq=1, update_id=2, slot=2)
            assert h.hub.live_subscription_count == 1

            await h.client.send(
                MutateSessionRequest(
                    request_id=12,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=3,
                    patch=[],
                    ops=[{"kind": "close_callback", "slot": 1}],  # type: ignore[list-item]
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)

            # No unsubscribe dispatch happened; the hub side finds out
            # purely via the dropped handle.
            await _wait_until(lambda: h.hub.live_subscription_count == 0)


async def test_subscribe_to_second_live_subscription_rejected() -> None:
    """An inbox permits at most one *live* subscription. A second
    ``subscribe_to`` while one is live raises (surfacing as an
    ``invoke_error``) — including a repeat for the **same** source: an
    inbox is expected to be subscribed exactly once. The first
    subscription stays live and intact."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            event_1 = await _subscribe_inbox_to_source(
                h, request_id=11, ack_seq=1, update_id=2, slot=2
            )
            assert isinstance(event_1, InvokeResultEvent)
            assert h.hub.live_subscription_count == 1

            # Second subscribe to the *same* source → error (no longer a
            # silent no-op). No connected event precedes it.
            await h.client.send(
                MutateSessionRequest(
                    request_id=12,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=3,
                    patch=[],
                    ops=[
                        {  # type: ignore[list-item]
                            "kind": "invoke",
                            "slot": 3,
                            "service_short_id": h.cb_short_id,
                            "method_name": "subscribe_to",
                            "args": [["service", h.src_short_id]],
                        },
                    ],
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            err = frame.event
            assert isinstance(err, InvokeErrorEvent)
            assert err.slot == 3
            assert "exactly one source" in err.message

            # The first subscription is untouched: one hub subscription,
            # still delivering.
            assert h.hub.live_subscription_count == 1
            h.hub.publish("still-live")
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            assert isinstance(frame.event, CallbackEvent)
            assert frame.event.payload == "still-live"


async def test_subscribe_to_failure_surfaces_as_invoke_error() -> None:
    """A source whose ``subscribe`` raises fails the parker before it
    signals readiness; the error propagates out of the ``subscribe_to``
    dispatch as an ``invoke_error`` on the invoking slot, the
    subscription is not retained, and the inbox stays usable."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            event = await _subscribe_inbox_to_source(
                h,
                request_id=11,
                ack_seq=1,
                update_id=2,
                slot=2,
                src_short_id=h.failing_src_short_id,
            )
            assert isinstance(event, InvokeErrorEvent)
            assert "rejects all subscribers" in event.message

            # The failed subscription was not retained...
            impl = h.ws.state.sessions[UUID(h.sid)].open_callbacks[1].impl
            assert impl._subscriptions == []

            # ...and the inbox still accepts a subscription to a
            # working source afterwards.
            event_2 = await _subscribe_inbox_to_source(
                h, request_id=12, ack_seq=1, update_id=3, slot=3
            )
            assert isinstance(event_2, InvokeResultEvent)
            h.hub.publish("recovered")
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            assert isinstance(frame.event, CallbackEvent)
            assert frame.event.payload == "recovered"


# --- 6.1 chunk 3.5: callback_invoke (invoke-on-callback-slot) --------


async def test_callback_invoke_subscribes_inbox_to_source() -> None:
    """``callback_invoke`` targets the session's own callback by slot —
    no ``GetCallbackServiceRequest`` round trip. The ``subscribe_to``
    dispatch + delivery flow is identical to the short-id-targeted
    invoke."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await h.client.send(
                MutateSessionRequest(
                    request_id=11,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=2,
                    patch=[],
                    ops=[
                        {  # type: ignore[list-item]
                            "kind": "callback_invoke",
                            "slot": 2,
                            "callback_slot": 1,
                            "method_name": "subscribe_to",
                            "args": [["service", h.src_short_id]],
                        },
                    ],
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)
            # Connected transition lands ahead of the invoke result.
            conn_frame = await h.client.receive()
            assert isinstance(conn_frame, SessionEventFrame)
            assert isinstance(conn_frame.event, CallbackSubscriptionEvent)
            assert conn_frame.event.slot == 1
            assert conn_frame.event.connected is True
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            event = frame.event
            assert isinstance(event, InvokeResultEvent)
            assert event.slot == 2
            assert event.payload is None
            assert h.hub.live_subscription_count == 1

            h.hub.publish("via-callback-invoke")
            cb_frame = await h.client.receive()
            assert isinstance(cb_frame, SessionEventFrame)
            cb_event = cb_frame.event
            assert isinstance(cb_event, CallbackEvent)
            assert cb_event.slot == 1
            assert cb_event.payload == "via-callback-invoke"


async def test_same_mutation_create_and_callback_invoke() -> None:
    """``[create_callback(N), callback_invoke(target=N, subscribe_to)]``
    rides a single mutation: the async pre-commit phase brings the
    callback up before the invoke worker spawns, so the target resolves
    against ``pending_creates`` exactly like an arg-side
    ``["callback", N]`` reference."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await h.client.send(
                MutateSessionRequest(
                    request_id=11,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=2,
                    patch=[],
                    ops=[
                        {"kind": "create_callback", "slot": 2},  # type: ignore[list-item]
                        {  # type: ignore[list-item]
                            "kind": "callback_invoke",
                            "slot": 3,
                            "callback_slot": 2,
                            "method_name": "subscribe_to",
                            "args": [["service", h.src_short_id]],
                        },
                    ],
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)
            # Connected transition for the fresh slot-2 inbox lands ahead
            # of the invoke result.
            conn_frame = await h.client.receive()
            assert isinstance(conn_frame, SessionEventFrame)
            assert isinstance(conn_frame.event, CallbackSubscriptionEvent)
            assert conn_frame.event.slot == 2
            assert conn_frame.event.connected is True
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            event = frame.event
            assert isinstance(event, InvokeResultEvent)
            assert event.slot == 3
            assert event.payload is None

            # The fresh slot-2 inbox is subscribed; events land on its
            # slot.
            h.hub.publish("one-mutation")
            cb_frame = await h.client.receive()
            assert isinstance(cb_frame, SessionEventFrame)
            cb_event = cb_frame.event
            assert isinstance(cb_event, CallbackEvent)
            assert cb_event.slot == 2
            assert cb_event.payload == "one-mutation"


async def test_callback_invoke_unknown_slot_rejected() -> None:
    """A ``callback_invoke`` whose ``callback_slot`` has no open
    callback rejects the whole mutation as ``validation_failed``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await h.client.send(
                MutateSessionRequest(
                    request_id=11,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=2,
                    patch=[],
                    ops=[
                        {  # type: ignore[list-item]
                            "kind": "callback_invoke",
                            "slot": 2,
                            "callback_slot": 99,
                            "method_name": "listening",
                            "args": [],
                        },
                    ],
                )
            )
            rejected = await h.client.receive()
            assert isinstance(rejected, MutationRejectedResponse)
            assert rejected.reason == "validation_failed"
            assert "99" in rejected.message


async def test_callback_invoke_after_close_in_same_mutation_rejected() -> None:
    """The ops walk is positional: a ``close_callback`` earlier in the
    same mutation removes the slot from the working set, so a
    ``callback_invoke`` targeting it afterwards rejects — and the
    rejection leaves the callback open (whole-mutation atomicity)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await h.client.send(
                MutateSessionRequest(
                    request_id=11,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=2,
                    patch=[],
                    ops=[
                        {"kind": "close_callback", "slot": 1},  # type: ignore[list-item]
                        {  # type: ignore[list-item]
                            "kind": "callback_invoke",
                            "slot": 2,
                            "callback_slot": 1,
                            "method_name": "listening",
                            "args": [],
                        },
                    ],
                )
            )
            rejected = await h.client.receive()
            assert isinstance(rejected, MutationRejectedResponse)
            assert rejected.reason == "validation_failed"
            # Atomic rejection: the close didn't happen either.
            session = h.ws.state.sessions[UUID(h.sid)]
            assert 1 in session.open_callbacks
            assert session.open_callbacks[1].impl._closed is False


async def test_callback_invoke_then_close_same_mutation() -> None:
    """``[callback_invoke(target=N), close_callback(N)]`` commits: the
    in-flight target pin (claimed before the close runs in the commit
    phase) keeps the send-side service alive for the dispatch, which
    then observes the already-closed inbox (``listening`` → False)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await h.client.send(
                MutateSessionRequest(
                    request_id=11,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=2,
                    patch=[],
                    ops=[
                        {  # type: ignore[list-item]
                            "kind": "callback_invoke",
                            "slot": 2,
                            "callback_slot": 1,
                            "method_name": "listening",
                            "args": [],
                        },
                        {"kind": "close_callback", "slot": 1},  # type: ignore[list-item]
                    ],
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)
            session = h.ws.state.sessions[UUID(h.sid)]
            assert 1 not in session.open_callbacks

            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            event = frame.event
            assert isinstance(event, InvokeResultEvent)
            assert event.slot == 2
            # The dispatch ran after the commit-phase close flipped
            # ``_closed``.
            assert event.payload is False


# --- 6.4post1: subscription connection-state transitions -------------


async def _create_inbox_slot1(client: Any, ws: WorkstationServer, sub_id: int, sid: str) -> str:
    """Create a callback inbox at slot 1; return its send-side short_id."""
    await client.send(
        MutateSessionRequest(
            request_id=10,
            subscription_id=sub_id,
            ack_seq=0,
            update_id=1,
            patch=[],
            ops=[{"kind": "create_callback", "slot": 1}],  # type: ignore[list-item]
        )
    )
    assert isinstance(await client.receive(), MutationCommittedResponse)
    return ws.state.sessions[UUID(sid)].open_callbacks[1].service_short_id


async def test_successful_subscribe_emits_connected_before_invoke_result() -> None:
    """A successful ``subscribe_to`` appends a ``connected=True``
    :class:`CallbackSubscriptionEvent` (slot = the inbox slot,
    ``reason=None``) to the owning session's log, ordered *ahead* of the
    subscribe_to invoke's own :class:`InvokeResultEvent`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await h.client.send(
                MutateSessionRequest(
                    request_id=11,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=2,
                    patch=[],
                    ops=[
                        {  # type: ignore[list-item]
                            "kind": "invoke",
                            "slot": 2,
                            "service_short_id": h.cb_short_id,
                            "method_name": "subscribe_to",
                            "args": [["service", h.src_short_id]],
                        },
                    ],
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)

            connected_frame = await h.client.receive()
            assert isinstance(connected_frame, SessionEventFrame)
            connected = connected_frame.event
            assert isinstance(connected, CallbackSubscriptionEvent)
            assert connected.slot == 1
            assert connected.connected is True
            assert connected.reason is None

            result_frame = await h.client.receive()
            assert isinstance(result_frame, SessionEventFrame)
            assert isinstance(result_frame.event, InvokeResultEvent)
            assert result_frame.event.slot == 2


async def test_subscription_source_break_emits_disconnect_event_with_reason() -> None:
    """When the source backing a live inbox subscription breaks
    ungracefully (its registering peer's ``/rpc`` connection drops), the
    workstation's keep-alive lease on the subscription fires: the owning
    session receives a ``connected=False`` :class:`CallbackSubscriptionEvent`
    carrying a non-empty break ``reason``, and the dead subscription
    tears down (its parker exits, ``done``)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    cb_short_id = await _create_inbox_slot1(client, ws, sub_id, sid)

                    with pytest.raises(BaseException):  # noqa: PT012, B017
                        async with connect_rpc(ws.rpc_url) as peer:
                            async with anyio.create_task_group() as source_tg:
                                hub = SubscriberHub[FxtrServiceData](task_group=source_tg)
                                source_ref = await peer.scope.mount(HubEventSourceImpl(hub))
                                workstation = await peer.remote_service.replace_service_type(
                                    WorkstationServiceProto
                                ).resolve(peer.scope)
                                src_short_id, _rcb = await workstation.register_service(
                                    "source", source_ref
                                )
                                await client.send(
                                    MutateSessionRequest(
                                        request_id=11,
                                        subscription_id=sub_id,
                                        ack_seq=0,
                                        update_id=2,
                                        patch=[],
                                        ops=[
                                            {  # type: ignore[list-item]
                                                "kind": "invoke",
                                                "slot": 2,
                                                "service_short_id": cb_short_id,
                                                "method_name": "subscribe_to",
                                                "args": [["service", src_short_id]],
                                            },
                                        ],
                                    )
                                )
                                assert isinstance(await client.receive(), MutationCommittedResponse)
                                seen = await _drain_until(
                                    client,
                                    lambda s: any(
                                        isinstance(f, SessionEventFrame)
                                        and isinstance(f.event, InvokeResultEvent)
                                        and f.event.slot == 2
                                        for f in s
                                    ),
                                )
                                assert any(
                                    isinstance(f, SessionEventFrame)
                                    and isinstance(f.event, CallbackSubscriptionEvent)
                                    and f.event.connected is True
                                    for f in seen
                                )
                                assert hub.live_subscription_count == 1
                                # Crash the peer ungracefully: the source's
                                # grant is revoked, breaking the inbox
                                # subscription's keep-alive lease.
                                raise RuntimeError("crash source peer")

                    seen = await _drain_until(client, lambda s: _has_disconnect_event(s, 1))
                    broken = next(
                        f.event
                        for f in seen
                        if isinstance(f, SessionEventFrame)
                        and isinstance(f.event, CallbackSubscriptionEvent)
                        and f.event.connected is False
                    )
                    assert broken.slot == 1
                    assert broken.reason  # non-empty break reason

                    # The dead subscription tore itself down.
                    impl = ws.state.sessions[UUID(sid)].open_callbacks[1].impl
                    await _wait_until(lambda: all(e.done for e in impl._subscriptions))


async def test_resubscribe_after_break_reconnects() -> None:
    """A broken subscription no longer counts as live, so the inbox can
    ``subscribe_to`` a fresh source — emitting a new ``connected=True``
    transition (broken → connected)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    cb_short_id = await _create_inbox_slot1(client, ws, sub_id, sid)

                    # First source: subscribe, then crash it.
                    with pytest.raises(BaseException):  # noqa: PT012, B017
                        async with connect_rpc(ws.rpc_url) as peer:
                            async with anyio.create_task_group() as source_tg:
                                hub = SubscriberHub[FxtrServiceData](task_group=source_tg)
                                source_ref = await peer.scope.mount(HubEventSourceImpl(hub))
                                workstation = await peer.remote_service.replace_service_type(
                                    WorkstationServiceProto
                                ).resolve(peer.scope)
                                src_short_id, _rcb = await workstation.register_service(
                                    "source", source_ref
                                )
                                await client.send(
                                    MutateSessionRequest(
                                        request_id=11,
                                        subscription_id=sub_id,
                                        ack_seq=0,
                                        update_id=2,
                                        patch=[],
                                        ops=[
                                            {  # type: ignore[list-item]
                                                "kind": "invoke",
                                                "slot": 2,
                                                "service_short_id": cb_short_id,
                                                "method_name": "subscribe_to",
                                                "args": [["service", src_short_id]],
                                            },
                                        ],
                                    )
                                )
                                assert isinstance(await client.receive(), MutationCommittedResponse)
                                await _drain_until(
                                    client,
                                    lambda s: any(
                                        isinstance(f, SessionEventFrame)
                                        and isinstance(f.event, InvokeResultEvent)
                                        and f.event.slot == 2
                                        for f in s
                                    ),
                                )
                                raise RuntimeError("crash first source")

                    await _drain_until(client, lambda s: _has_disconnect_event(s, 1))
                    impl = ws.state.sessions[UUID(sid)].open_callbacks[1].impl
                    await _wait_until(lambda: all(e.done for e in impl._subscriptions))

                    # Second source: a fresh subscribe is allowed (the
                    # broken/done entry doesn't count as live) and emits a
                    # new connected transition. Crash it too at the end so
                    # there's no graceful-unsubscribe-against-dead-source
                    # teardown.
                    with pytest.raises(BaseException):  # noqa: PT012, B017
                        async with connect_rpc(ws.rpc_url) as peer2:
                            async with anyio.create_task_group() as source_tg2:
                                hub2 = SubscriberHub[FxtrServiceData](task_group=source_tg2)
                                source_ref2 = await peer2.scope.mount(HubEventSourceImpl(hub2))
                                workstation2 = await peer2.remote_service.replace_service_type(
                                    WorkstationServiceProto
                                ).resolve(peer2.scope)
                                src2_short_id, _rcb2 = await workstation2.register_service(
                                    "source-2", source_ref2
                                )
                                await client.send(
                                    MutateSessionRequest(
                                        request_id=21,
                                        subscription_id=sub_id,
                                        ack_seq=0,
                                        update_id=3,
                                        patch=[],
                                        ops=[
                                            {  # type: ignore[list-item]
                                                "kind": "invoke",
                                                "slot": 3,
                                                "service_short_id": cb_short_id,
                                                "method_name": "subscribe_to",
                                                "args": [["service", src2_short_id]],
                                            },
                                        ],
                                    )
                                )
                                assert isinstance(await client.receive(), MutationCommittedResponse)
                                seen = await _drain_until(
                                    client,
                                    lambda s: any(
                                        isinstance(f, SessionEventFrame)
                                        and isinstance(f.event, InvokeResultEvent)
                                        and f.event.slot == 3
                                        for f in s
                                    ),
                                )
                                reconnected = [
                                    f.event
                                    for f in seen
                                    if isinstance(f, SessionEventFrame)
                                    and isinstance(f.event, CallbackSubscriptionEvent)
                                    and f.event.connected is True
                                ]
                                assert reconnected and reconnected[-1].slot == 1
                                assert hub2.live_subscription_count == 1
                                raise RuntimeError("crash second source")

                    # The second subscription also breaks → another
                    # connected=False on slot 1.
                    await _drain_until(client, lambda s: _has_disconnect_event(s, 1))


async def test_orderly_close_emits_no_disconnect_event() -> None:
    """An orderly ``close_callback`` unsubscribes from the source but
    does **not** emit a ``connected=False`` transition — a graceful close
    is a different signal from a break."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            await _subscribe_inbox_to_source(h, request_id=11, ack_seq=1, update_id=2, slot=2)
            h.hub.publish("before-close")
            frame = await h.client.receive()
            assert isinstance(frame, SessionEventFrame)
            assert isinstance(frame.event, CallbackEvent)

            await h.client.send(
                MutateSessionRequest(
                    request_id=12,
                    subscription_id=h.sub_id,
                    ack_seq=1,
                    update_id=3,
                    patch=[],
                    ops=[{"kind": "close_callback", "slot": 1}],  # type: ignore[list-item]
                )
            )
            assert isinstance(await h.client.receive(), MutationCommittedResponse)
            await _wait_until(lambda: h.hub.live_subscription_count == 0)

            # Give any spurious break event time to arrive, then assert
            # none did: the session event log holds no connected=False
            # transition.
            await anyio.sleep(0.15)
            session = h.ws.state.sessions[UUID(h.sid)]
            assert not any(
                isinstance(ev, CallbackSubscriptionEvent) and ev.connected is False
                for ev in session.event_log
            )


async def test_subscription_connected_event_rides_snapshot() -> None:
    """The ``connected=True`` transition is a normal seq'd
    :class:`NonStateSessionEvent`: an un-acked transition replays through
    a fresh subscription's snapshot ``subsequent_events`` so a client
    that attaches later can reduce the current connection state."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with _feed_harness() as h:
            # Subscribe (ack_seq stays 0, so the connected event is never
            # acked and survives in the unacked suffix).
            await _subscribe_inbox_to_source(h, request_id=11, ack_seq=0, update_id=2, slot=2)

            # A fresh subscription to the same session gets a snapshot
            # whose subsequent_events include the connected transition.
            await h.client.send(SubscribeSessionRequest(request_id=200, session_id=h.sid))
            seen = await _drain_until(
                h.client, lambda s: any(isinstance(f, SessionSnapshotEvent) for f in s)
            )
            snapshot = next(f for f in seen if isinstance(f, SessionSnapshotEvent))
            connected_events = [
                ev
                for ev in snapshot.subsequent_events
                if isinstance(ev, CallbackSubscriptionEvent) and ev.connected is True
            ]
            assert connected_events
            assert connected_events[-1].slot == 1


# --- 6.4post1 chunk 4: subscribe_to(method=...) generalization -------


async def test_subscribe_to_custom_method_name() -> None:
    """``subscribe_to(source, method="watch")`` dispatches the source's
    ``watch`` method (the source is not an ``EventSource`` subclass and
    has no ``subscribe`` method); events flow into the inbox as
    :class:`CallbackEvent`\\ s exactly like the default path, and a
    successful subscribe still emits ``connected=True``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                async with anyio.create_task_group() as source_tg:
                    try:
                        hub = SubscriberHub[FxtrServiceData](task_group=source_tg)
                        source_ref = await rpc.scope.mount(WatchableHubImpl(hub))
                        async with (
                            expose_service(
                                ws_ref, "watchable", source_ref, rpc.scope
                            ) as src_short_id,
                            create_and_subscribe(client) as (sid, sub_id),
                        ):
                            cb_short_id = await _create_inbox_slot1(client, ws, sub_id, sid)
                            # subscribe_to with a non-default method name.
                            await client.send(
                                MutateSessionRequest(
                                    request_id=11,
                                    subscription_id=sub_id,
                                    ack_seq=0,
                                    update_id=2,
                                    patch=[],
                                    ops=[
                                        {  # type: ignore[list-item]
                                            "kind": "invoke",
                                            "slot": 2,
                                            "service_short_id": cb_short_id,
                                            "method_name": "subscribe_to",
                                            "args": [
                                                ["service", src_short_id],
                                                "watch",
                                            ],
                                        },
                                    ],
                                )
                            )
                            assert isinstance(await client.receive(), MutationCommittedResponse)
                            seen = await _drain_until(
                                client,
                                lambda s: any(
                                    isinstance(f, SessionEventFrame)
                                    and isinstance(f.event, InvokeResultEvent)
                                    and f.event.slot == 2
                                    for f in s
                                ),
                            )
                            # connected=True transition emitted on success.
                            assert any(
                                isinstance(f, SessionEventFrame)
                                and isinstance(f.event, CallbackSubscriptionEvent)
                                and f.event.connected is True
                                for f in seen
                            )
                            assert hub.live_subscription_count == 1

                            # Events delivered through the watch subscription
                            # land as CallbackEvents on the inbox slot.
                            hub.publish("via-watch")
                            frame = await client.receive()
                            assert isinstance(frame, SessionEventFrame)
                            assert isinstance(frame.event, CallbackEvent)
                            assert frame.event.slot == 1
                            assert frame.event.payload == "via-watch"
                    finally:
                        source_tg.cancel_scope.cancel()


async def test_subscribe_to_forwards_extra_kwargs() -> None:
    """``subscribe_to(source, method, **kwargs)`` forwards the extra
    keyword args to the source's subscription method after the
    subscriber ref — ``watch_key(subscriber, *, key, depth)`` receives
    them through its own typed decode — and the subscription behaves
    exactly like the default path (connected transition, events as
    callback events)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                async with anyio.create_task_group() as source_tg:
                    try:
                        hub = SubscriberHub[FxtrServiceData](task_group=source_tg)
                        source = KeyedSourceImpl(hub)
                        source_ref = await rpc.scope.mount(source)
                        async with (
                            expose_service(
                                ws_ref, "keyed-source", source_ref, rpc.scope
                            ) as src_short_id,
                            create_and_subscribe(client) as (sid, sub_id),
                        ):
                            cb_short_id = await _create_inbox_slot1(client, ws, sub_id, sid)
                            await client.send(
                                MutateSessionRequest(
                                    request_id=11,
                                    subscription_id=sub_id,
                                    ack_seq=0,
                                    update_id=2,
                                    patch=[],
                                    ops=[
                                        {  # type: ignore[list-item]
                                            "kind": "invoke",
                                            "slot": 2,
                                            "service_short_id": cb_short_id,
                                            "method_name": "subscribe_to",
                                            "args": [
                                                ["service", src_short_id],
                                                "watch_key",
                                            ],
                                            "kwargs": {"key": "topic-a", "depth": 3},
                                        },
                                    ],
                                )
                            )
                            assert isinstance(await client.receive(), MutationCommittedResponse)
                            seen = await _drain_until(
                                client,
                                lambda s: any(
                                    isinstance(f, SessionEventFrame)
                                    and isinstance(f.event, InvokeResultEvent)
                                    and f.event.slot == 2
                                    for f in s
                                ),
                            )
                            assert any(
                                isinstance(f, SessionEventFrame)
                                and isinstance(f.event, CallbackSubscriptionEvent)
                                and f.event.connected is True
                                for f in seen
                            )
                            # The source's typed decode received the
                            # forwarded args verbatim.
                            assert source.seen == [("topic-a", 3)]
                            assert hub.live_subscription_count == 1

                            hub.publish("via-keyed")
                            frame = await client.receive()
                            assert isinstance(frame, SessionEventFrame)
                            assert isinstance(frame.event, CallbackEvent)
                            assert frame.event.slot == 1
                            assert frame.event.payload == "via-keyed"
                    finally:
                        source_tg.cancel_scope.cancel()
