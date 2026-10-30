"""Stage 8a tests: preserved events.

Covers the wire-side ops (``preserve_events`` / ``unpreserve_events``),
the storage split between ``event_log`` (contiguous unacked suffix)
and ``preserved_events`` (sidecar dict keyed on seq), the unified
retention prune, and the snapshot / debug snapshot wire shape.
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer, expose_entity
from fxtr_workstation.client_protocol import (
    AckResponse,
    LoadResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    SessionEventFrame,
    SessionSnapshotEvent,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import Note
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


# --- Helpers -------------------------------------------------------


async def _load_and_get_result_seq(
    client,
    sub_id: int,
    ent_short_id: str,
    *,
    request_id: int,
    update_id: int,
    ack_seq: int,
    slot: int,
) -> int:
    """Send a load_entity mutation, swallow the committed + result frames,
    and return the result event's seq."""
    await client.send(
        MutateSessionRequest(
            request_id=request_id,
            subscription_id=sub_id,
            ack_seq=ack_seq,
            update_id=update_id,
            patch=[],
            ops=[{"kind": "load_entity", "slot": slot, "short_id": ent_short_id}],  # type: ignore[list-item]
        )
    )
    committed = await client.receive()
    assert isinstance(committed, MutationCommittedResponse)
    frame = await client.receive()
    assert isinstance(frame, SessionEventFrame)
    assert isinstance(frame.event, LoadResultEvent)
    return frame.event.seq


# --- Happy paths ---------------------------------------------------


async def test_preserve_keeps_event_alive_past_horizon() -> None:
    """Preserve a load_result event in the same mutation that acks past
    it; verify the event survives in ``preserved_events``, child entity
    refcounts persist, and a fresh subscriber sees the preserved event
    in the snapshot's ``events`` list with ``seq <= last_state.ack_seq``.
    """
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    session = ws.state.sessions[UUID(sid)]
                    children = set(session.entity_refcounts.keys())
                    assert children, "load_result should pin at least the type-marker child"

                    # Mutation 2: ack past the result event AND preserve it.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Server-side: event moved to sidecar, refcounts persist.
                    assert result_seq in session.preserved_events
                    assert all(e.seq != result_seq for e in session.event_log)
                    assert result_seq in session.event_refs
                    assert set(session.entity_refcounts.keys()) == children

                    # Fresh subscriber sees the preserved event in
                    # the snapshot's preserved_events bucket; its seq
                    # is at or below last_state.ack_seq by definition.
                    await client.send(SubscribeSessionRequest(request_id=20, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=20)
                    snap = await client.receive()
                    assert isinstance(snap, SessionSnapshotEvent)
                    preserved_seqs = [e.seq for e in snap.preserved_events]
                    assert result_seq in preserved_seqs
                    assert result_seq <= snap.last_state.ack_seq
                    assert all(e.seq > snap.last_state.ack_seq for e in snap.subsequent_events)
                    # The preserved event itself is the LoadResultEvent.
                    [preserved] = [e for e in snap.preserved_events if e.seq == result_seq]
                    assert isinstance(preserved, LoadResultEvent)


async def test_unpreserve_drops_event_and_decrefs() -> None:
    """Preserve, then unpreserve in a later mutation. Event drops out of
    ``preserved_events``; ``event_refs[seq]`` is dropped and child
    refcounts decrement (releasing children with no other holders)."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    session = ws.state.sessions[UUID(sid)]

                    # Preserve.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert result_seq in session.preserved_events
                    assert session.entity_refcounts  # children still pinned

                    # Unpreserve.
                    await client.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=session.last_state_seq,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "unpreserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert result_seq not in session.preserved_events
                    assert result_seq not in session.event_refs
                    # No other holders; refcount dict drains to empty.
                    assert session.entity_refcounts == {}


async def test_within_mutation_preserve_and_unpreserve_nets_to_drop() -> None:
    """``[Preserve(seq), Unpreserve(seq)]`` in one mutation: working set
    ends unchanged, but the event still rides ``event_log`` retention
    (which drops it because ack_seq covers it). End state matches the
    no-preserve baseline."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    session = ws.state.sessions[UUID(sid)]

                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[
                                {"kind": "preserve_events", "seqs": [result_seq]},  # type: ignore[list-item]
                                {"kind": "unpreserve_events", "seqs": [result_seq]},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Net: nothing preserved, retention drops the event.
                    assert session.preserved_events == {}
                    assert result_seq not in session.event_refs
                    assert all(e.seq != result_seq for e in session.event_log)
                    assert session.entity_refcounts == {}


async def test_multi_mutation_preserve_lifecycle() -> None:
    """Preserve in M1; multiple M2..Mn keep state moving without
    touching the preserved event; unpreserve in Mn+1 drops it."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    session = ws.state.sessions[UUID(sid)]

                    # Preserve.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    refcount_after_preserve = dict(session.entity_refcounts)
                    assert refcount_after_preserve

                    # Three no-op mutations advance state without mentioning
                    # the preserved event. It must keep its event_refs intact.
                    for i, req in enumerate([12, 13, 14]):
                        await client.send(
                            MutateSessionRequest(
                                request_id=req,
                                subscription_id=sub_id,
                                ack_seq=session.last_state_seq,
                                update_id=10 + i,
                                patch=[],
                                ops=[],
                            )
                        )
                        assert isinstance(await client.receive(), MutationCommittedResponse)
                        assert result_seq in session.preserved_events
                        assert dict(session.entity_refcounts) == refcount_after_preserve

                    # Now unpreserve. Drops + decref.
                    await client.send(
                        MutateSessionRequest(
                            request_id=15,
                            subscription_id=sub_id,
                            ack_seq=session.last_state_seq,
                            update_id=20,
                            patch=[],
                            ops=[{"kind": "unpreserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert result_seq not in session.preserved_events
                    assert result_seq not in session.event_refs
                    assert session.entity_refcounts == {}


async def test_batched_preserve_and_unpreserve() -> None:
    """A single PreserveEventsOp can carry multiple seqs; same for
    UnpreserveEventsOp. End-to-end."""
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
                    # Two loads in one mutation → two load_result events
                    # share the unacked suffix.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "load_entity", "slot": 1, "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "load_entity", "slot": 2, "short_id": ent_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    result_seqs: list[int] = []
                    while len(result_seqs) < 2:
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        assert isinstance(frame.event, LoadResultEvent)
                        result_seqs.append(frame.event.seq)
                    seq_a, seq_b = sorted(result_seqs)
                    session = ws.state.sessions[UUID(sid)]

                    # Single mutation, batched preserve op covering both.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=max(seq_a, seq_b),
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [seq_a, seq_b]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert seq_a in session.preserved_events
                    assert seq_b in session.preserved_events
                    assert all(e.seq not in (seq_a, seq_b) for e in session.event_log)

                    # Batched unpreserve in the other order.
                    await client.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=session.last_state_seq,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "unpreserve_events", "seqs": [seq_b, seq_a]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    assert session.preserved_events == {}


# --- Rejections ----------------------------------------------------


async def test_preserve_rejects_state_update() -> None:
    """The state_update event itself can't be preserved.

    State_updates aren't stored in ``event_log`` (3-post-2 invariant)
    — they're authoritative through ``last_state_seq`` /
    ``last_state_ack_seq`` / ``state``. Trying to preserve a
    state_update's seq surfaces as "not in the session's event log",
    same as any other unknown seq.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (_sid, sub_id):
                    # Mutation 1 commits a no-op state_update at seq=1.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Try to preserve seq=1 (the state_update). Reject.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [1]}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "not in the session's event log" in err.message


async def test_preserve_rejects_unacked_event() -> None:
    """A preserve target whose seq is still in the unacked suffix
    (i.e. ``seq > new ack_seq``) is rejected — it would be a no-op
    since retention won't drop it this mutation anyway."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    # Try to preserve without acking past the result.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq - 1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "not being acknowledged" in err.message


async def test_preserve_rejects_unknown_seq() -> None:
    """Preserve targeting a seq that's never been emitted is rejected."""
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
                            ops=[{"kind": "preserve_events", "seqs": [999]}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "not in the session's event log" in err.message


async def test_preserve_rejects_already_preserved() -> None:
    """Preserve targeting a seq already in ``preserved_events`` is rejected."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    session = ws.state.sessions[UUID(sid)]
                    # Preserve once.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    # Preserve again — reject.
                    await client.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=session.last_state_seq,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "already preserved" in err.message


async def test_unpreserve_rejects_non_preserved() -> None:
    """Unpreserve a seq that's not currently preserved is rejected."""
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
                            ops=[{"kind": "unpreserve_events", "seqs": [42]}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "not currently preserved" in err.message


async def test_duplicate_seqs_within_op_rejected() -> None:
    """The same seq appearing twice in one op's ``seqs`` rejects."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq, result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert err.reason == "validation_failed"
                    assert "duplicate" in err.message


# --- Debug snapshot ------------------------------------------------


async def test_close_drains_preserved_events() -> None:
    """Session close clears ``preserved_events`` and decref's their
    refcount contributions via the standard close drain."""
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
                    result_seq = await _load_and_get_result_seq(
                        client,
                        sub_id,
                        ent_short_id,
                        request_id=10,
                        update_id=1,
                        ack_seq=0,
                        slot=1,
                    )
                    session = ws.state.sessions[UUID(sid)]

                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=result_seq,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "preserve_events", "seqs": [result_seq]}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    children = set(session.entity_refcounts.keys())
                    assert children
                    type_key = next(iter(children))

                    # Close the session.
                    await client.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=session.last_state_seq,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    # Session is gone from the registry.
                    assert UUID(sid) not in ws.state.sessions
                    # The child entities pinned only via the preserved
                    # event are no longer tracked.
                    assert type_key not in ws.state.entity_records
