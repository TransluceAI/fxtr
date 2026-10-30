"""Stage 3c.3b tests: ``InvokeOp`` + delivery worker.

End-to-end via ``/client``: invoke a workstation-tracked service,
receive an :class:`InvokeResultEvent` (or :class:`InvokeErrorEvent`)
on the session subscription. Covers:

- Happy path with a primitive result.
- Invoke result returning an entity → entity becomes workstation-
  tracked, session takes a refcount.
- Op validation rejections: bad service short_id, bad arg short_id,
  ``["callback", N]`` sentinel.
- Dispatch error → ``InvokeErrorEvent``.
- Close-during-invoke: ``aborted_orphan`` path no-ops cleanly.
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    InvokeErrorEvent,
    InvokeResultEvent,
    LoadResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    SessionEventFrame,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import provenanced_key_for
from fxtr_workstation.wire_escape import TAG_ENTITY, TAG_SERVICE

from ._fixtures import EchoFactoryImpl, EchoImpl, Note, NoteMakerImpl
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


# --- Happy path ----------------------------------------------------


async def test_invoke_echo_delivers_primitive_result() -> None:
    """``echo("hello")`` returns ``"echo:hello"``; the session
    receives an :class:`InvokeResultEvent` carrying the wire-encoded
    result."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["hello"],
                                }
                            ],
                        )
                    )
                    committed = await client.receive()
                    assert isinstance(committed, MutationCommittedResponse)
                    assert committed.seq == 1

                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    assert event.slot == 1
                    assert event.payload == "echo:hello"

                    # No refs in result → no in_flight contributions.
                    session = ws.state.sessions[UUID(sid)]
                    assert session.entity_refcounts == {}
                    assert session.service_refcounts == {}


async def test_invoke_kwargs_delivers_keyword_args_to_method() -> None:
    """``echo_kw(prefix="hi", msg="there")`` shipped via
    :class:`InvokeOp.kwargs` reaches the impl as keyword args and
    returns ``"hi:there"``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo_kw",
                                    "args": [],
                                    "kwargs": {"prefix": "hi", "msg": "there"},
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    assert event.slot == 1
                    assert event.payload == "hi:there"

                    # Sanity: no refs in result.
                    session = ws.state.sessions[UUID(sid)]
                    assert session.entity_refcounts == {}
                    assert session.service_refcounts == {}


async def test_invoke_kwargs_default_factory_when_field_omitted() -> None:
    """A wire payload that omits the ``kwargs`` field entirely (older
    clients pre-stage9) decodes to ``{}`` via msgspec's
    ``default_factory=dict``. Verified via the existing
    ``test_invoke_echo_delivers_primitive_result`` shape: the op dict
    has no ``"kwargs"`` key and the method takes positional args
    only. Combined with the typed-kwargs test above, this confirms
    both shapes round-trip end-to-end."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["hi"],
                                    # No "kwargs" — default_factory kicks in.
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    assert event.payload == "echo:hi"
                    del sid


async def test_invoke_returning_entity_workstation_tracks_it() -> None:
    """A method that returns an :class:`EntityRef` produces an
    :class:`InvokeResultEvent` whose payload is an entity tag, the
    entity becomes workstation-tracked, and the session takes a
    refcount on it."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                maker_ref = await rpc.scope.mount(NoteMakerImpl())
                async with (
                    expose_service(ws_ref, "maker", maker_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "make_note",
                                    "args": ["t", "b"],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)

                    # Payload is an entity tag.
                    payload = event.payload
                    assert isinstance(payload, list)
                    assert payload[0] == TAG_ENTITY
                    note_short_id = payload[1]

                    # Resolves to a workstation-tracked entity.
                    note_fid = ws.state.short_ids.lookup_active_entity(note_short_id)
                    assert note_fid is not None
                    note_key = provenanced_key_for(note_fid)
                    assert note_key in ws.state.entity_records

                    # Session takes a refcount on the entity, and
                    # event_refs[seq] records it.
                    session = ws.state.sessions[UUID(sid)]
                    assert note_key in session.entity_refcounts
                    refs_e, _refs_s = session.event_refs[event.seq]
                    assert note_key in refs_e


# --- Op validation rejections --------------------------------------


async def test_invoke_unknown_service_short_id_rejects() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
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
                            {  # type: ignore[list-item]
                                "kind": "invoke",
                                "slot": 1,
                                "service_short_id": "s99",
                                "method_name": "echo",
                                "args": [],
                            }
                        ],
                    )
                )
                err = await client.receive()
                assert isinstance(err, MutationRejectedResponse)
                assert "unknown or inactive service" in err.message


async def test_invoke_unknown_entity_arg_rejects() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": [["entity", "nonexistent"]],
                                }
                            ],
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "unknown or inactive entity" in err.message


async def test_invoke_callback_arg_rejects() -> None:
    """``["callback", N]`` is reserved for stage 5; the codec rejects
    it during op-validation, so the invoke is rejected atomically."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": [["callback", 0]],
                                }
                            ],
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "callback" in err.message.lower()


# --- Dispatch error → InvokeErrorEvent ------------------------------


async def test_invoke_dispatch_error_delivers_invoke_error_event() -> None:
    """``boom()`` raises; the worker delivers an
    :class:`InvokeErrorEvent` with the *full traceback* (category +
    frames), not just the exception's ``repr``.

    ``boom`` runs on a peer RPC connection, so its ``RuntimeError``
    crosses the wire as an :class:`IRException` carrying the formatted
    traceback in ``traceback_info``. The delivered message must surface
    that — the exception type (``RuntimeError``) and a real
    ``Traceback`` header — neither of which appears in ``repr`` of the
    wire-decoded ``IRException`` (which shows only its bare message).
    Regression for stage6 Issue 2 (see the part6_adjustments plan)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                maker_ref = await rpc.scope.mount(NoteMakerImpl())
                async with (
                    expose_service(ws_ref, "maker", maker_ref, rpc.scope) as svc_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "boom",
                                    "args": [],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeErrorEvent)
                    assert event.slot == 1
                    assert "boom" in event.message
                    # Full traceback, not repr: the exception type and a
                    # Traceback header survive (repr of the wire-decoded
                    # IRException shows neither).
                    assert "RuntimeError" in event.message, event.message
                    assert "Traceback (most recent call last)" in event.message, event.message

                    # No leftover refs.
                    session = ws.state.sessions[UUID(sid)]
                    assert session.entity_refcounts == {}
                    assert session.service_refcounts == {}


# --- Invoke arg passing through ------------------------------------


async def test_invoke_with_entity_arg_passes_it_through() -> None:
    """Pass an entity short_id as an arg; the dispatched method
    sees the FxtrID. (Echo doesn't use it, but the wire decode +
    arg lift through call_scope must not fail.)"""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
                    expose_entity(ws_ref, "note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    # Echo doesn't accept entity args, but op-validation
                    # should accept the arg shape (the codec resolves the
                    # short_id; the dispatch will then fail because echo
                    # doesn't take a FxtrID — that becomes an
                    # InvokeErrorEvent, not an op-validation error).
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": [["entity", ent_short_id]],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    # Either result or error — but op-validation passed,
                    # which is the point of this test.
                    assert isinstance(frame.event, (InvokeResultEvent, InvokeErrorEvent))


# --- Service-from-result + service-arg + chained invoke ------------


async def test_invoke_returning_service_workstation_tracks_it() -> None:
    """A method that returns a ``ServiceRef`` produces an
    :class:`InvokeResultEvent` whose payload is a service tag, the
    new service becomes workstation-tracked (record minted, worker
    spawned, bridge_ready up), and the calling session takes a
    refcount on it via ``event_refs``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                factory_ref = await rpc.scope.mount(EchoFactoryImpl())
                async with (
                    expose_service(ws_ref, "factory", factory_ref, rpc.scope) as factory_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # Invoke make_echo → returns ServiceRef[EchoProto].
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": factory_short_id,
                                    "method_name": "make_echo",
                                    "args": [],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)

                    payload = event.payload
                    assert isinstance(payload, list)
                    assert payload[0] == TAG_SERVICE
                    new_svc_short_id = payload[1]

                    # Workstation-tracked: short_id resolves to a
                    # service_key that's in service_records, bridge_ready
                    # is set (worker spawned + bridged), no bridge_error.
                    new_key = ws.state.short_ids.lookup_active_service(new_svc_short_id)
                    assert new_key is not None
                    assert new_key in ws.state.service_records
                    record = ws.state.service_records[new_key]
                    assert record.bridge_ready.is_set()
                    assert record.bridge_error is None

                    # Session takes a refcount on the new service via event_refs.
                    session = ws.state.sessions[UUID(sid)]
                    assert new_key in session.service_refcounts
                    _refs_e, refs_s = session.event_refs[event.seq]
                    assert new_key in refs_s


async def test_invoke_with_service_arg_dispatches_via_arg() -> None:
    """``echo_via(echo_ref, msg)`` resolves the passed-in ServiceRef
    arg via the call_scope's ``include`` chain and forwards the
    call. Verifies the per-arg ``arg_service_scopes`` plumbing in
    :func:`invoke_worker`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                factory_ref = await rpc.scope.mount(EchoFactoryImpl())
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "factory", factory_ref, rpc.scope) as factory_short_id,
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as echo_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": factory_short_id,
                                    "method_name": "echo_via",
                                    "args": [
                                        ["service", echo_short_id],
                                        "world",
                                    ],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    assert event.payload == "echo:world"


async def test_chained_invoke_via_service_returned_from_invoke() -> None:
    """Invoke ``make_echo`` to get a new short_id, then in a follow-up
    mutation invoke ``echo`` on that new short_id. Exercises the
    full A → G chain: a service returned from one invoke is
    workstation-tracked and immediately usable as the target of a
    later invoke."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                factory_ref = await rpc.scope.mount(EchoFactoryImpl())
                async with (
                    expose_service(ws_ref, "factory", factory_ref, rpc.scope) as factory_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    # First invoke: make_echo → ServiceRef.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": factory_short_id,
                                    "method_name": "make_echo",
                                    "args": [],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    new_short = event.payload[1]

                    # Second invoke: echo on the freshly-tracked
                    # service. Keep ``ack_seq=0`` so the first result
                    # event stays inside the retention horizon —
                    # otherwise its ``event_refs`` decrement would
                    # release the new service before this invoke runs.
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
                                    "service_short_id": new_short,
                                    "method_name": "echo",
                                    "args": ["chain"],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame2 = await client.receive()
                    assert isinstance(frame2, SessionEventFrame)
                    assert isinstance(frame2.event, InvokeResultEvent)
                    assert frame2.event.payload == "echo:chain"


# --- Multi-op slot rules + mixed load+invoke -----------------------


async def test_multiple_invokes_and_load_in_one_mutation_share_slot_rule() -> None:
    """A single mutation with two invoke ops + a load_entity op
    requires strictly-monotonic slots across all of them. Verifies
    the running ``max_slot_claimed`` works across both kinds."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
                    expose_entity(ws_ref, "note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    # Reject: invoke at slot 2, load at slot 2 (same).
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["a"],
                                },
                                {  # type: ignore[list-item]
                                    "kind": "load_entity",
                                    "slot": 2,
                                    "short_id": ent_short_id,
                                },
                            ],
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "strictly greater" in err.message

                    # Accept: invoke at slot 2, load at slot 3, invoke at 4.
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
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["a"],
                                },
                                {  # type: ignore[list-item]
                                    "kind": "load_entity",
                                    "slot": 3,
                                    "short_id": ent_short_id,
                                },
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 4,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["b"],
                                },
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Drain the three result events. Order is up to
                    # the task scheduler; just verify slots seen.
                    slots: set[int] = set()
                    for _ in range(3):
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        assert isinstance(frame.event, (InvokeResultEvent, LoadResultEvent))
                        slots.add(frame.event.slot)
                    assert slots == {2, 3, 4}


# --- Result with mixed entity + service ---------------------------


async def test_invoke_result_with_entity_and_service_pair() -> None:
    """``make_pair`` returns ``{"note": EntityRef, "echo": ServiceRef}``
    in a single dict result. Both refs become workstation-tracked
    and the session takes a refcount on each."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                factory_ref = await rpc.scope.mount(EchoFactoryImpl())
                async with (
                    expose_service(ws_ref, "factory", factory_ref, rpc.scope) as factory_short_id,
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
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": factory_short_id,
                                    "method_name": "make_pair",
                                    "args": [],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)

                    payload = event.payload
                    assert isinstance(payload, dict)
                    note_tag = payload["note"]
                    echo_tag = payload["echo"]
                    assert note_tag[0] == TAG_ENTITY
                    assert echo_tag[0] == TAG_SERVICE

                    note_fid = ws.state.short_ids.lookup_active_entity(note_tag[1])
                    echo_key = ws.state.short_ids.lookup_active_service(echo_tag[1])
                    assert note_fid is not None
                    assert echo_key is not None

                    session = ws.state.sessions[UUID(sid)]
                    refs_e, refs_s = session.event_refs[event.seq]
                    assert provenanced_key_for(note_fid) in refs_e
                    assert echo_key in refs_s


# --- LoadResultEvent / SessionEventFrame import for the multi-op test --

# (LoadResultEvent imported via `from fxtr_workstation.client_protocol`)
