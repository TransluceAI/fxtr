"""Stage 3c.4 cross-cutting tests for refcount + retention behavior.

End-to-end exercises of the invariants the 3c primitives uphold:

- Child entities surfaced by a load result are session-pinned only
  while the result event is retained; once retention drops the
  event past the horizon, refcounts decrement and unowned records
  are released. Same shape applies to services minted from invoke
  results.
- An explicit ``track_entity`` / ``track_service`` pin survives
  retention drops.
- Records sourced via the load / invoke path stay alive when the
  source RPC connection drops, mirroring the 3b.3 RPC-drop test
  but for load-sourced and invoke-sourced records.
- Close-during-in-flight: a session that closes between the
  load- or invoke-spawn and the worker's actual run leaves no
  leaked records (the worker hits ``aborted_silent`` cleanly).
- Cross-session service usage: a service tracked by one session
  is usable from another session over the same workstation.
- Load results walk explicit user-level entity refs (not just
  the implicit ``$fxtr.type`` / ``$fxtr.ctx`` markers).
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    InvokeResultEvent,
    LoadResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    SessionEventFrame,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import provenanced_key_for
from fxtr_workstation.wire_escape import TAG_ENTITY, TAG_SERVICE

from ._fixtures import EchoFactoryImpl, EchoImpl, Note, NoteWithChild, SlowEchoImpl
from ._session_helpers import create_and_subscribe
from ._ws_client import ClientWS, connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _load_and_get_event(
    client: ClientWS, sub_id: int, ent_short_id: str, slot: int = 1
) -> LoadResultEvent:
    """Fire a ``load_entity`` for ``ent_short_id`` and pull out the
    resulting :class:`LoadResultEvent`. Returns the event so the
    caller can inspect ``payload`` / ``seq``."""
    await client.send(
        MutateSessionRequest(
            request_id=10,
            subscription_id=sub_id,
            ack_seq=0,
            update_id=1,
            patch=[],
            ops=[{"kind": "load_entity", "slot": slot, "short_id": ent_short_id}],  # type: ignore[list-item]
        )
    )
    assert isinstance(await client.receive(), MutationCommittedResponse)
    frame = await client.receive()
    assert isinstance(frame, SessionEventFrame)
    event = frame.event
    assert isinstance(event, LoadResultEvent)
    return event


# --- Retention-drop releases child refcounts -----------------------


async def test_child_released_when_retention_drops_load_result() -> None:
    """A load result references children (e.g. ``$fxtr.type``); the
    session pins them via ``event_refs[seq]``. Once the event drops
    past the retention horizon, refcounts decrement; with no other
    holders, the child's record is released and the entity is
    abandoned from ``entities_scope``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    event = await _load_and_get_event(client, sub_id, ent_short_id)
                    result_seq = event.seq

                    # Pull the type-marker FxtrID from the payload.
                    type_tag = event.payload["$fxtr.type"]
                    assert type_tag[0] == TAG_ENTITY
                    type_short_id = type_tag[1]
                    type_fid = ws.state.short_ids.lookup_active_entity(type_short_id)
                    assert type_fid is not None

                    # Pre-drop: type marker is workstation-tracked and
                    # the session pins it via event_refs.
                    session = ws.state.sessions[UUID(sid)]
                    type_key = provenanced_key_for(type_fid)
                    assert type_key in ws.state.entity_records
                    assert type_key in session.entity_refcounts

                    # Advance ack_seq past result_seq → retention prune
                    # drops the event, refcounts decrement.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Type marker is no longer pinned by this session
                    # (no other holders) → record released, entity
                    # abandoned.
                    assert type_key not in session.entity_refcounts
                    assert type_key not in ws.state.entity_records


async def test_explicit_pin_keeps_child_alive_through_retention_drop() -> None:
    """Same scenario as above but the session also pins the child
    via :class:`TrackEntityOp` in the mutation that acks past the
    result event. The explicit pin keeps the child's refcount at 1
    after the event-ref contribution is decremented, so the record
    survives and stays in ``tracking_sessions``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    event = await _load_and_get_event(client, sub_id, ent_short_id)
                    result_seq = event.seq

                    type_tag = event.payload["$fxtr.type"]
                    type_short_id = type_tag[1]
                    type_fid = ws.state.short_ids.lookup_active_entity(type_short_id)
                    assert type_fid is not None

                    session = ws.state.sessions[UUID(sid)]
                    type_key = provenanced_key_for(type_fid)
                    assert session.entity_refcounts.get(type_key) == 1  # event-ref only

                    # Mutate: pin type explicitly + ack past result_seq.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "track_entity",
                                    "short_id": type_short_id,
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Refcount stayed at 1 (event-ref dropped, explicit
                    # pin added). Record is still alive and the
                    # session is still in tracking_sessions.
                    assert session.entity_refcounts[type_key] == 1
                    assert type_key in session.tracked_entities
                    tracked = ws.state.entity_records[type_key]
                    assert session.session_id in tracked.tracking_sessions


# --- Cross-source: load-sourced child outlives RPC drop ------------


async def test_load_sourced_child_kept_alive_after_rpc_drops() -> None:
    """A child that became workstation-tracked via a load result
    stays alive after the source RPC connection drops — the session
    is its only holder and the RPC drop only affects the parent's
    explicit registration. Mirrors 3b.3's
    ``test_session_keeps_entity_alive_after_rpc_drops`` but for a
    load-sourced ref."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                type_fid = None
                async with connect_rpc(ws.rpc_url) as rpc:
                    ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                    note = Note(title="t", body="b").store(rpc.scope)
                    async with expose_entity(
                        ws_ref, "note", note.entity_id, rpc.scope
                    ) as ent_short_id:
                        event = await _load_and_get_event(client, sub_id, ent_short_id)
                        type_tag = event.payload["$fxtr.type"]
                        type_fid = ws.state.short_ids.lookup_active_entity(type_tag[1])
                        assert type_fid is not None
                # RPC connection dropped here. Note's registration is
                # gone; only the session's load-sourced refs keep the
                # type marker alive.

                # Type marker is still tracked because the session has
                # a refcount on it via event_refs[seq].
                type_key = provenanced_key_for(type_fid)
                assert type_key in ws.state.entity_records
                session = ws.state.sessions[UUID(sid)]
                assert type_key in session.entity_refcounts


# --- Close-during-in-flight (load) ----------------------------------


async def test_close_during_in_flight_load_leaves_no_leaks() -> None:
    """Send a load_entity mutation followed immediately by a close
    mutation. The load worker's ``deliver_load_result_to_slot``
    races with session close: either it runs first (and session-close
    drains the refcounts cleanly) or the close runs first (and the
    worker's phase-1 session-alive check short-circuits). In either
    case, no entity records minted by the load remain tracked after
    teardown."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # Send the load.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "load_entity", "slot": 1, "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    # Immediately close — the load worker may or may
                    # not have run yet (race).
                    assert isinstance(await client.receive(), MutationCommittedResponse)
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
                    # Drain everything from the wire so we know all
                    # events have been processed by the time we
                    # inspect state.
                    seen_committed_close = False
                    while not seen_committed_close:
                        frame = await client.receive()
                        if isinstance(frame, MutationCommittedResponse) and frame.request_id == 11:
                            seen_committed_close = True

                    # Give the load worker a chance to finish if it
                    # was still in flight when the close drained.
                    # ``checkpoint`` yields once; that should be
                    # enough since load is sync after load_cbor.
                    await anyio.sleep(0.05)

                    # Session is gone.
                    assert UUID(sid) not in ws.state.sessions
                    # The note's registration on RPC keeps the note
                    # itself tracked, but no children should be
                    # workstation-tracked: either the worker hit
                    # ``aborted_silent`` (no records minted) or it
                    # ran first and session-close drained the refs.
                    # Walk all records: only the note (which has an
                    # RPC registration) should remain.
                    remaining = set(ws.state.entity_records)
                    assert remaining == {provenanced_key_for(note.entity_id)}


# --- Service-from-invoke: refcount + retention ---------------------


async def _invoke_make_echo_and_get_service_short_id(
    client: ClientWS, sub_id: int, factory_short_id: str
) -> tuple[InvokeResultEvent, str]:
    """Send ``invoke make_echo`` and return the resulting
    :class:`InvokeResultEvent` plus the new service's short_id."""
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
    assert isinstance(payload, list) and payload[0] == TAG_SERVICE
    return event, payload[1]


async def test_service_released_when_retention_drops_invoke_result() -> None:
    """Mirror of :func:`test_child_released_when_retention_drops_load_result`
    but for a service surfaced by an invoke result. After the result
    event passes the retention horizon, the session's refcount on the
    new service drops; with no other holders the record is released
    and its worker exits."""
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
                    event, new_svc_short_id = await _invoke_make_echo_and_get_service_short_id(
                        client, sub_id, factory_short_id
                    )
                    new_key = ws.state.short_ids.lookup_active_service(new_svc_short_id)
                    assert new_key is not None

                    # Pre-drop: service is workstation-tracked and the
                    # session pins it via event_refs.
                    session = ws.state.sessions[UUID(sid)]
                    assert new_key in ws.state.service_records
                    assert new_key in session.service_refcounts

                    # Advance ack_seq past the result event → retention
                    # prune drops it, the session's event-ref decrements.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=event.seq,
                            update_id=2,
                            patch=[],
                            ops=[],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Service has no other holders → record released,
                    # worker shut down. ``maybe_release_service_record``
                    # sets ``shutdown_event``; the worker drains the
                    # service_scope and removes the record.
                    await anyio.sleep(0.05)  # let the worker exit
                    assert new_key not in session.service_refcounts
                    assert new_key not in ws.state.service_records


# Note: test C from stage3post1_tests.md ("service-from-invoke
# survives source RPC drop") is intentionally NOT implemented.
# Unlike entities — which are *copied* into ``entities_scope`` and
# therefore truly independent of their source RPC connection — a
# service minted from an invoke result is just a workstation-side
# bridge pointing at a remote ``IRHandle`` imported from that RPC
# connection. The actual impl object (e.g. ``EchoImpl`` from
# ``EchoFactoryImpl.make_echo``) lives in the registering peer's
# process; the workstation has only a chain of leases through the
# RPC connection that it would have to call back over. When the
# session pins such a service, the workstation's lease on the
# remote handle is what *keeps the RPC connection alive* (the
# 3b.3 graceful-drain rule). Dropping the connection while the
# session still pins the new service therefore blocks RPC drain
# until session-close releases the refcount — there is no path
# where the service "outlives" the RPC. Mirroring the entity test
# was a misreading of the lifetime model on our part; recorded
# here so we don't try to reintroduce it.


async def test_explicit_pin_keeps_service_alive_through_retention_drop() -> None:
    """Mirror of :func:`test_explicit_pin_keeps_child_alive_through_retention_drop`
    but for a service: the same mutation that acks past the invoke
    result event also pins the new service via :class:`TrackServiceOp`.
    Refcount stays ≥ 1 (event-ref dropped, explicit pin added); record
    survives."""
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
                    event, new_svc_short_id = await _invoke_make_echo_and_get_service_short_id(
                        client, sub_id, factory_short_id
                    )
                    new_key = ws.state.short_ids.lookup_active_service(new_svc_short_id)
                    assert new_key is not None

                    session = ws.state.sessions[UUID(sid)]
                    assert session.service_refcounts.get(new_key) == 1

                    # Mutate: pin the new service explicitly + ack past
                    # the result event.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=event.seq,
                            update_id=2,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "track_service",
                                    "short_id": new_svc_short_id,
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Refcount stayed at 1 (event-ref dropped, explicit
                    # pin added). Record alive, session still tracks.
                    assert session.service_refcounts[new_key] == 1
                    assert new_key in session.tracked_services
                    tracked = ws.state.service_records[new_key]
                    assert session.session_id in tracked.tracking_sessions


# --- Close-during-invoke -------------------------------------------


async def test_close_during_in_flight_invoke_leaves_no_leaks() -> None:
    """Send an invoke whose dispatch is artificially slow, immediately
    follow with a close mutation. The worker may run before, during,
    or after the close — in all cases no service records minted by the
    invoke survive teardown.

    Note: this test exercises the ``aborted_orphan`` shape (close runs
    before phase-3 commit) most of the time; the precise sub-case is
    scheduler-dependent and not asserted."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                slow_ref = await rpc.scope.mount(SlowEchoImpl())
                async with (
                    expose_service(ws_ref, "slow", slow_ref, rpc.scope) as svc_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    pre_records = set(ws.state.service_records)

                    # Send a slow invoke.
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
                                    "args": ["x", 0.2],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Immediately close the session.
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
                    seen_close = False
                    while not seen_close:
                        frame = await client.receive()
                        if isinstance(frame, MutationCommittedResponse) and frame.request_id == 11:
                            seen_close = True

                    # Wait long enough for the slow invoke to have run
                    # to completion.
                    await anyio.sleep(0.4)

                    # Session is gone, no new service records linger.
                    assert UUID(sid) not in ws.state.sessions
                    assert set(ws.state.service_records) == pre_records


# --- Cross-session service usage -----------------------------------


async def test_service_tracked_by_one_session_invokable_by_another() -> None:
    """Session A tracks a workstation service via :class:`TrackServiceOp`.
    Session B (on a different ``/client`` connection) invokes that
    service by short_id. Verifies the workstation acts as a shared
    service registry; sessions don't have to "own" a service to use
    it — they just need its short_id."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client_a,
                connect_client(ws.client_url) as client_b,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
                    create_and_subscribe(client_a) as (_sid_a, sub_a),
                    create_and_subscribe(client_b) as (_sid_b, sub_b),
                ):
                    # Session A pins the service.
                    await client_a.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_a,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "track_service",
                                    "short_id": svc_short_id,
                                }
                            ],
                        )
                    )
                    assert isinstance(await client_a.receive(), MutationCommittedResponse)

                    # Session B invokes the same service.
                    await client_b.send(
                        MutateSessionRequest(
                            request_id=20,
                            subscription_id=sub_b,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["shared"],
                                }
                            ],
                        )
                    )
                    assert isinstance(await client_b.receive(), MutationCommittedResponse)
                    frame = await client_b.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, InvokeResultEvent)
                    assert event.payload == "echo:shared"


# --- Load result with explicit user-level child ref ----------------


async def test_load_result_walks_explicit_user_level_child_ref() -> None:
    """The pre-existing 3c.4 child-tracking tests ride on the implicit
    ``$fxtr.type`` / ``$fxtr.ctx`` markers. This test uses a real
    user-level child reference: :class:`NoteWithChild` holds an
    :class:`EntityRef[Note]` field. Loading the parent must surface
    the child's ref in the wire payload, mint a workstation record
    for it, and refcount it on the session."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                child_ref = Note(title="child", body="").store(rpc.scope)
                parent_ref = NoteWithChild(title="parent", child=child_ref).store(rpc.scope)
                async with (
                    expose_entity(
                        ws_ref, "parent", parent_ref.entity_id, rpc.scope
                    ) as parent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    event = await _load_and_get_event(client, sub_id, parent_short_id)

                    # Payload has a "child" field that decodes as an
                    # entity tag.
                    payload = event.payload
                    assert isinstance(payload, dict)
                    child_tag = payload["child"]
                    assert isinstance(child_tag, list)
                    assert child_tag[0] == TAG_ENTITY
                    child_short_id = child_tag[1]

                    # The child is now workstation-tracked.
                    child_fid = ws.state.short_ids.lookup_active_entity(child_short_id)
                    assert child_fid is not None
                    assert child_fid == child_ref.entity_id
                    child_key = provenanced_key_for(child_fid)
                    assert child_key in ws.state.entity_records

                    # The session has a refcount on the child via
                    # event_refs[seq].
                    session = ws.state.sessions[UUID(sid)]
                    assert child_key in session.entity_refcounts
                    refs_e, _refs_s = session.event_refs[event.seq]
                    assert child_key in refs_e
