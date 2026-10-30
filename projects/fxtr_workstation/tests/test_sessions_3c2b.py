"""Stage 3c.2b tests: ``LoadEntityOp`` + delivery worker.

End-to-end via the ``/client`` wire, plus a couple of unit tests
that exercise edge cases (load_cbor failure, aborted_silent on a
closed session). The codec round-trip itself is covered in
``test_wire_escape.py``; here we focus on:

- Op validation (slot rules, unknown short_id).
- Successful load with primitive entity and with child refs (which
  become workstation-tracked).
- Refcount bookkeeping: result events bump session refcounts; the
  session record's ``event_refs`` sidecar lands.
- Error path: ``load_cbor`` failure → ``LoadErrorEvent``.
- Aborted_silent: load_worker run against a session that's already
  gone is a no-op.
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr2.data.entity_core import FxtrID
from fxtr_workstation import WorkstationServer, expose_entity
from fxtr_workstation.client_protocol import (
    LoadErrorEvent,
    LoadResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    SessionEventFrame,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import (
    _deliver_load_error,
    _ensure_entity_record_via_track,
    load_worker,
    provenanced_key_for,
)
from fxtr_workstation.wire_escape import TAG_ENTITY

from ._fixtures import Note
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


# --- Successful load -----------------------------------------------


async def test_load_primitive_entity_delivers_result_event() -> None:
    """Load a Note that has no fields referencing other user entities;
    the resulting payload still contains a ``$fxtr.type`` reference
    (the type entity) which is workstation-tracked at delivery time."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="hello", body="world").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # Issue load_entity in slot 1.
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
                    committed = await client.receive()
                    assert isinstance(committed, MutationCommittedResponse)
                    assert committed.seq == 1

                    # Result event arrives next on the wire.
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, LoadResultEvent)
                    assert event.slot == 1
                    assert event.seq == 2  # state-update was seq=1

                    payload = event.payload
                    assert isinstance(payload, dict)
                    # Note's user fields pass through as primitives.
                    assert payload["title"] == "hello"
                    assert payload["body"] == "world"
                    # $fxtr.type is encoded as an entity tag.
                    type_tag = payload["$fxtr.type"]
                    assert isinstance(type_tag, list)
                    assert type_tag[0] == TAG_ENTITY
                    type_short_id = type_tag[1]

                    # Server-side: slot_counter advanced; the entities
                    # *referenced inside the result* are workstation-
                    # tracked and the session takes a refcount on each.
                    # The loaded entity itself (note.entity_id) is NOT
                    # in the result's refs — it's the outer container,
                    # not a child reference. (Use TrackEntityOp if you
                    # want to keep the loaded entity tracked.)
                    session = ws.state.sessions[UUID(sid)]
                    assert session.slot_counter == 1
                    note_key = provenanced_key_for(note.entity_id)
                    assert note_key not in session.entity_refcounts
                    type_fid = ws.state.short_ids.lookup_active_entity(type_short_id)
                    assert type_fid is not None
                    type_key = provenanced_key_for(type_fid)
                    assert type_key in session.entity_refcounts
                    # event_refs sidecar records the LoadResultEvent's refs.
                    assert event.seq in session.event_refs
                    refs_e, refs_s = session.event_refs[event.seq]
                    assert type_key in refs_e
                    assert refs_s == set()


async def test_load_event_refs_drop_when_event_passes_retention_horizon() -> None:
    """After loading an entity, advance the session's ack horizon past
    the result event; verify the event_refs entry is dropped and
    session refcounts are decremented."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # Load.
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
                    committed = await client.receive()
                    assert isinstance(committed, MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    result_seq = frame.event.seq

                    session = ws.state.sessions[UUID(sid)]
                    assert result_seq in session.event_refs

                    # Retention rule is "drop ``seq <= ack_seq``", so a
                    # single follow-up mutation that acks the result
                    # event's seq is enough to drop it.
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

                    # Result event is gone; event_refs entry is gone;
                    # the children the result referenced (e.g. the
                    # type marker) are no longer pinned by this session.
                    assert result_seq not in session.event_refs
                    assert all(e.seq != result_seq for e in session.event_log)
                    # No leftover refcount on the children the result
                    # carried — they were the only contribution from
                    # this session and we just decref'd them.
                    assert session.entity_refcounts == {}


# --- Op validation -------------------------------------------------


async def test_slot_must_be_strictly_greater_than_slot_counter() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    # First load claims slot 5.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "load_entity", "slot": 5, "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    # swallow the result event
                    assert isinstance(await client.receive(), SessionEventFrame)

                    # Second load tries slot 5 again — must fail.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "load_entity", "slot": 5, "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "strictly greater" in err.message


async def test_slots_must_strictly_increase_within_a_mutation() -> None:
    """Each load/invoke op's slot must be strictly greater than every
    slot claimed before it (in this mutation or in earlier mutations).
    Two ops at the same slot is a special case of the same rule."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    # Two ops at the same slot: second must reject.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "load_entity", "slot": 1, "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "load_entity", "slot": 1, "short_id": ent_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "strictly greater" in err.message

                    # Second op going *backward* (lower slot than the
                    # first) also rejects.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=2,
                            patch=[],
                            ops=[
                                {"kind": "load_entity", "slot": 5, "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "load_entity", "slot": 3, "short_id": ent_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "strictly greater" in err.message


async def test_load_before_close_commits_but_result_lands_on_gone_session() -> None:
    """``CloseOp`` can ride at the end of a mutation that also
    spawns work-launching ops like ``load_entity``. The mutation
    commits and the load worker is spawned, but by the time it
    tries to deliver its result, the session record is gone and
    the deliver helper short-circuits. This is a footgun (waste
    of work) rather than a correctness hole — bundling close with
    cleanup-only ops like Untrack / Unpreserve is the supported
    pattern; load + close is permitted but pointless."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # load + close in same mutation → commits.
                    # Session is gone after the close; the load
                    # worker's result delivery short-circuits on
                    # session-gone (see `_deliver_load_error` /
                    # `deliver_load_result_to_slot`).
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "load_entity", "slot": 1, "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "close"},  # type: ignore[list-item]
                            ],
                        )
                    )
                    resp = await client.receive()
                    assert isinstance(resp, MutationCommittedResponse), resp

                    # Session torn down.
                    assert UUID(sid) not in ws.state.sessions


async def test_unknown_short_id_rejects_at_op_validation() -> None:
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
                        ops=[{"kind": "load_entity", "slot": 1, "short_id": "nope"}],  # type: ignore[list-item]
                    )
                )
                err = await client.receive()
                assert isinstance(err, MutationRejectedResponse)
                assert "unknown or inactive" in err.message


# --- Multiple loads in one mutation --------------------------------


async def test_two_loads_in_one_mutation_both_deliver() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note_a = Note(title="a", body="b").store(rpc.scope)
                note_b = Note(title="c", body="d").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "a", note_a.entity_id, rpc.scope) as a_id,
                    expose_entity(ws_ref, "b", note_b.entity_id, rpc.scope) as b_id,
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
                                {"kind": "load_entity", "slot": 1, "short_id": a_id},  # type: ignore[list-item]
                                {"kind": "load_entity", "slot": 2, "short_id": b_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Two LoadResultEvents arrive, one per slot.
                    slots_seen: set[int] = set()
                    for _ in range(2):
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        assert isinstance(frame.event, LoadResultEvent)
                        slots_seen.add(frame.event.slot)
                    assert slots_seen == {1, 2}

                    session = ws.state.sessions[UUID(sid)]
                    assert session.slot_counter == 2


# --- Error path ----------------------------------------------------


async def test_load_cbor_failure_delivers_load_error_event() -> None:
    """Force the per-record ``load_scope.load_cbor`` to raise (via
    monkey-patching ``FxtrScope.load_cbor``); the worker delivers a
    ``LoadErrorEvent``.

    We mint a real per-record scope so the workstation's invariants
    hold throughout (every :class:`TrackedEntityRecord` owns its own
    scope holding the layered FxtrID), then patch
    ``FxtrScope.load_cbor`` for a single call so the load worker's
    transient ``load_scope`` raises instead of returning CBOR.
    """
    from fxtr2.data.entity_ref_scope import FxtrScope

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # Mint a real record (same-store, sync via track_from_scope).
                scratch = FxtrScope(
                    isolated=True, include=[ws.state.root_scope]
                ).lowlevel_start_lifespanless(
                    parent_leases={ws.state.root_scope: ws.state.root_scope.clone_authorization()}
                )
                try:
                    note_ref = Note(title="t", body="b").store(scratch)
                    fid = note_ref.entity_id
                    with ws.state.mutation_context():
                        record = _ensure_entity_record_via_track(ws.state, fid, scratch)
                    short_id = record.short_id
                finally:
                    scratch.force_close()

                # Patch the per-instance ``load_cbor`` so the worker's
                # transient ``load_scope`` (a child of record.scope)
                # inherits the patched method. We use class-level patch
                # since FxtrScope is created internally.
                original_load_cbor = FxtrScope.load_cbor

                async def _broken_load_cbor(self, node_id):  # type: ignore[no-untyped-def]
                    raise ValueError("simulated load_cbor failure")

                FxtrScope.load_cbor = _broken_load_cbor  # type: ignore[method-assign]
                try:
                    async with create_and_subscribe(client) as (_sid, sub_id):
                        await client.send(
                            MutateSessionRequest(
                                request_id=10,
                                subscription_id=sub_id,
                                ack_seq=0,
                                update_id=1,
                                patch=[],
                                ops=[
                                    {"kind": "load_entity", "slot": 1, "short_id": short_id}  # type: ignore[list-item]
                                ],
                            )
                        )
                        assert isinstance(await client.receive(), MutationCommittedResponse)

                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        event = frame.event
                        assert isinstance(event, LoadErrorEvent)
                        assert event.slot == 1
                        # Error message includes the underlying load_cbor exc text.
                        assert "simulated load_cbor failure" in event.message
                finally:
                    FxtrScope.load_cbor = original_load_cbor  # type: ignore[method-assign]


# --- Aborted_silent: closed session before worker runs -------------


async def test_load_worker_bails_silently_when_session_already_gone() -> None:
    """Direct unit test: ``load_worker`` invoked against a session
    that's already been removed from the registry no-ops cleanly."""
    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import _maybe_release_entity_record

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            scratch = FxtrScope(
                isolated=True, include=[ws.state.root_scope]
            ).lowlevel_start_lifespanless(
                parent_leases={ws.state.root_scope: ws.state.root_scope.clone_authorization()}
            )
            try:
                note_ref = Note(title="t", body="b").store(scratch)
                fid = note_ref.entity_id
                with ws.state.mutation_context():
                    tracked = _ensure_entity_record_via_track(ws.state, fid, scratch)
            finally:
                scratch.force_close()

            # Synthesize a session id that doesn't exist in the registry.
            from uuid import uuid4

            ghost_session_id = uuid4()

            # Should run without raising and without mutating state.
            await load_worker(ws.state, ghost_session_id, 1, fid, set(), set(), False)

            # Tracked record still has no tracking_sessions.
            assert tracked.tracking_sessions == set()

            # Cleanup the synthesized record so server shutdown sees
            # an empty registry.
            with ws.state.mutation_context():
                _maybe_release_entity_record(ws.state, tracked)


async def test_deliver_load_error_no_op_when_session_gone() -> None:
    """``_deliver_load_error`` to a missing session is a silent no-op."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            from uuid import uuid4

            # Should not raise.
            _deliver_load_error(ws.state, uuid4(), 1, "boom")
