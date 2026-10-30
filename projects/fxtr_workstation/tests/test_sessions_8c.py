"""Stage 8c tests: ``LoadEntitySilentOp`` and ``LoadResultSilentEvent``.

Side effects identical to :class:`LoadEntityOp` (children tracked,
session refcounted, retention horizon decrements normally); only the
result event differs (no payload). Failures still deliver
:class:`LoadErrorEvent` regardless of silent flag.
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer, expose_entity
from fxtr_workstation.client_protocol import (
    AckResponse,
    LoadResultEvent,
    LoadResultSilentEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    SessionEventFrame,
    SessionSnapshotEvent,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import Note
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


# --- Happy path ----------------------------------------------------


async def test_silent_load_delivers_payload_less_event() -> None:
    """A LoadEntitySilentOp commits and delivers a LoadResultSilentEvent
    with no payload field. Side effects (children tracked, refcount on
    the requesting session) match the non-silent variant."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
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
                                {"kind": "load_entity_silent", "slot": 1, "short_id": ent_short_id}  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, LoadResultSilentEvent)
                    assert frame.event.slot == 1
                    # Wire shape: only `kind` / `seq` / `slot` — no payload.
                    assert not hasattr(frame.event, "payload")

                    session = ws.state.sessions[UUID(sid)]
                    # Children entities became workstation-tracked and
                    # the session took refcounts on them — same as the
                    # non-silent path.
                    assert session.entity_refcounts
                    assert frame.event.seq in session.event_refs


async def test_silent_and_loud_load_have_equivalent_refcount_side_effects() -> None:
    """Run a non-silent load on session A and a silent load on session B
    against the same entity; assert their entity_refcounts dicts are
    structurally identical (same keys, same counts)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client_a,
                connect_client(ws.client_url) as client_b,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client_a) as (sid_a, sub_a),
                    create_and_subscribe(client_b) as (sid_b, sub_b),
                ):
                    # Session A: non-silent load.
                    await client_a.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_a,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "load_entity", "slot": 1, "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client_a.receive(), MutationCommittedResponse)
                    a_frame = await client_a.receive()
                    assert isinstance(a_frame, SessionEventFrame)
                    assert isinstance(a_frame.event, LoadResultEvent)

                    # Session B: silent load.
                    await client_b.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_b,
                            ack_seq=0,
                            update_id=2,
                            patch=[],
                            ops=[
                                {
                                    "kind": "load_entity_silent",
                                    "slot": 1,
                                    "short_id": ent_short_id,
                                }  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client_b.receive(), MutationCommittedResponse)
                    b_frame = await client_b.receive()
                    assert isinstance(b_frame, SessionEventFrame)
                    assert isinstance(b_frame.event, LoadResultSilentEvent)

                    sess_a = ws.state.sessions[UUID(sid_a)]
                    sess_b = ws.state.sessions[UUID(sid_b)]
                    # Same keys, same counts — silent and non-silent
                    # incref the exact same set of children.
                    assert dict(sess_a.entity_refcounts) == dict(sess_b.entity_refcounts)


async def test_silent_load_refcount_drops_on_retention_advance() -> None:
    """A LoadResultSilentEvent rides retention identically to the
    non-silent variant: when ack_seq advances past it, the
    event_refs entry decrements and child refcounts drop."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
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
                                {
                                    "kind": "load_entity_silent",
                                    "slot": 1,
                                    "short_id": ent_short_id,
                                }  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, LoadResultSilentEvent)
                    result_seq = frame.event.seq
                    session = ws.state.sessions[UUID(sid)]
                    assert session.entity_refcounts

                    # Ack past it.
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
                    assert result_seq not in session.event_refs
                    assert session.entity_refcounts == {}


# --- Mixing in one mutation ----------------------------------------


async def test_mixed_silent_and_loud_loads_in_one_mutation() -> None:
    """``[LoadEntityOp, LoadEntitySilentOp]`` in a single mutation:
    slot monotonicity covers both uniformly; each delivers its own
    flavor of result event."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
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
                                {"kind": "load_entity", "slot": 1, "short_id": ent_short_id},  # type: ignore[list-item]
                                {
                                    "kind": "load_entity_silent",
                                    "slot": 2,
                                    "short_id": ent_short_id,
                                },  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Two result events arrive in some order — match by
                    # slot.
                    saw_loud = False
                    saw_silent = False
                    while not (saw_loud and saw_silent):
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        if isinstance(frame.event, LoadResultEvent):
                            assert frame.event.slot == 1
                            saw_loud = True
                        elif isinstance(frame.event, LoadResultSilentEvent):
                            assert frame.event.slot == 2
                            saw_silent = True


async def test_silent_load_unknown_short_id_rejected() -> None:
    """Op-validation rejects an unknown short_id for the silent variant
    just like for the loud one."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (_sid, sub_id):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {
                                    "kind": "load_entity_silent",
                                    "slot": 1,
                                    "short_id": "no-such-thing",
                                }  # type: ignore[list-item]
                            ],
                        )
                    )
                    err = await client.receive()
                    from fxtr_workstation.client_protocol import MutationRejectedResponse

                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "unknown or inactive" in err.message


# --- Preservation interop ------------------------------------------


async def test_silent_load_event_is_preservable() -> None:
    """A LoadResultSilentEvent rides preserve / unpreserve identically
    to LoadResultEvent — exercises the 8a + 8c interop."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
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
                                {
                                    "kind": "load_entity_silent",
                                    "slot": 1,
                                    "short_id": ent_short_id,
                                }  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, LoadResultSilentEvent)
                    silent_seq = frame.event.seq
                    session = ws.state.sessions[UUID(sid)]
                    children = set(session.entity_refcounts.keys())

                    # Preserve, ack past.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=silent_seq,
                            update_id=2,
                            patch=[],
                            ops=[
                                {"kind": "preserve_events", "seqs": [silent_seq]}  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert silent_seq in session.preserved_events
                    assert set(session.entity_refcounts.keys()) == children

                    # Resubscribe and confirm the silent event is in
                    # the snapshot's preserved_events bucket.
                    await client.send(SubscribeSessionRequest(request_id=20, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=20)
                    snap = await client.receive()
                    assert isinstance(snap, SessionSnapshotEvent)
                    [preserved] = [e for e in snap.preserved_events if e.seq == silent_seq]
                    assert isinstance(preserved, LoadResultSilentEvent)
                    assert preserved.slot == 1
