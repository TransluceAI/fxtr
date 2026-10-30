"""Stage-4b tests: session-side debug subscription + pin-set fields
on :class:`StateUpdateEvent`.

Covers:

- ``StateUpdateEvent.tracked_entities`` / ``tracked_services``
  reflect the session's explicit-pin set after a mutation
  (track / untrack / close).
- ``subscribe_session_debug`` returns an initial snapshot and
  pushes fresh snapshots on refcount + slot_counter changes.
- The debug subscription is torn down server-side on session
  close.
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    AckResponse,
    ErrorResponse,
    LoadResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    ServerFrame,
    SessionDebugSnapshotEvent,
    SessionEventFrame,
    SessionSnapshotEvent,
    StateUpdateEvent,
    SubscribeSessionDebugRequest,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import EchoImpl, Note, NoteWithChild
from ._session_helpers import create_and_subscribe
from ._ws_client import ClientWS, connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _drain_until(
    client: ClientWS,
    predicate,
    *,
    timeout: float = 2.0,
) -> list[ServerFrame]:
    seen: list[ServerFrame] = []
    with anyio.fail_after(timeout):
        while not predicate(seen):
            seen.append(await client.receive())
    return seen


# --- StateUpdateEvent pin-set fields --------------------------------


async def test_state_update_event_carries_tracked_pin_short_ids() -> None:
    """``track_entity`` / ``track_service`` ops produce a
    ``state_update`` whose ``tracked_entities`` / ``tracked_services``
    list the post-mutation pin short_ids."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as mutator,
                connect_client(ws.client_url) as observer,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    expose_service(ws_ref, "the-echo", echo_ref, rpc.scope) as svc_short_id,
                    create_and_subscribe(mutator) as (sid, sub_id),
                ):
                    # Foreign observer subscribed to the same session.
                    await observer.send(SubscribeSessionRequest(request_id=20, session_id=sid))
                    assert (await observer.receive()) == AckResponse(request_id=20)
                    snap = await observer.receive()
                    assert isinstance(snap, SessionSnapshotEvent)
                    # Initial snapshot: empty pin sets.
                    assert snap.last_state.tracked_entities == []
                    assert snap.last_state.tracked_services == []

                    # Pin both.
                    await mutator.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "track_entity", "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "track_service", "short_id": svc_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await mutator.receive(), MutationCommittedResponse)
                    # Foreign observer sees the state_update with pin sets populated.
                    frame = await observer.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, StateUpdateEvent)
                    assert frame.event.tracked_entities == [ent_short_id]
                    assert frame.event.tracked_services == [svc_short_id]

                    # Untrack the service.
                    await mutator.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[
                                {"kind": "untrack_service", "short_id": svc_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await mutator.receive(), MutationCommittedResponse)
                    frame = await observer.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, StateUpdateEvent)
                    assert frame.event.tracked_entities == [ent_short_id]
                    assert frame.event.tracked_services == []

                    # Close — terminal state_update has both lists empty
                    # (drain ran before event construction).
                    await mutator.send(
                        MutateSessionRequest(
                            request_id=12,
                            subscription_id=sub_id,
                            ack_seq=2,
                            update_id=3,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await mutator.receive(), MutationCommittedResponse)
                    frame = await observer.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, StateUpdateEvent)
                    assert frame.event.closed is True
                    assert frame.event.tracked_entities == []
                    assert frame.event.tracked_services == []


# --- SubscribeSessionDebug ------------------------------------------


async def test_subscribe_session_debug_initial_snapshot_is_empty() -> None:
    """Subscribing immediately after session creation yields a
    snapshot with empty refcount dicts and slot_counter=0."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, _sub_id):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    snap = await client.receive()
                    assert isinstance(snap, SessionDebugSnapshotEvent)
                    assert snap.subscription_id == 100
                    assert snap.session_id == sid
                    assert snap.entity_refcounts == {}
                    assert snap.service_refcounts == {}
                    assert snap.slot_counter == 0
                    # Cleanly close before exit.
                    await client.send(
                        MutateSessionRequest(
                            request_id=200,
                            subscription_id=_sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    # The close-mutation tears down both the session
                    # subscription and the debug subscription. Drain
                    # whatever frames arrive (mutation_committed and
                    # possibly nothing else).
                    with anyio.move_on_after(0.2):
                        while True:
                            await client.receive()


async def test_subscribe_session_debug_unknown_session_errors() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(
                    SubscribeSessionDebugRequest(
                        request_id=1, session_id="00000000-0000-0000-0000-000000000000"
                    )
                )
                err = await client.receive()
                assert isinstance(err, ErrorResponse) and err.request_id == 1


async def test_session_debug_snapshot_pushes_on_explicit_pin() -> None:
    """An explicit ``track_entity`` op bumps the session's entity
    refcount; the debug subscriber receives a fresh snapshot
    showing the new count."""
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
                    # Subscribe to the debug stream (initial empty snapshot).
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    snap = await client.receive()
                    assert isinstance(snap, SessionDebugSnapshotEvent)
                    assert snap.entity_refcounts == {}

                    # Pin the entity.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "track_entity", "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    # Debug subscriber should see a fresh snapshot
                    # with the new refcount before the
                    # mutation_committed response.
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent)
                            and f.entity_refcounts.get(ent_short_id) == 1
                            for f in s
                        ),
                    )
                    assert seen
                    # Close cleanly.
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
                    with anyio.move_on_after(0.5):
                        while True:
                            await client.receive()


async def test_session_debug_snapshot_pushes_on_load_slot_bump() -> None:
    """A ``load_entity`` op bumps the session's slot_counter and —
    when the loaded entity has a child FxtrID — surfaces the child
    in ``entity_refcounts`` via the delivery worker's phase-1
    attach. The debug subscriber sees snapshots reflecting both.

    Uses :class:`NoteWithChild` so phase-1 finds a real child
    FxtrID to incref against the session.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                child = Note(title="child", body="b").store(rpc.scope)
                parent = NoteWithChild(title="parent", child=child).store(rpc.scope)
                async with (
                    expose_entity(
                        ws_ref, "the-parent", parent.entity_id, rpc.scope
                    ) as parent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    initial = await client.receive()
                    assert isinstance(initial, SessionDebugSnapshotEvent)
                    assert initial.slot_counter == 0
                    assert initial.entity_refcounts == {}

                    # Issue a load against the parent.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "load_entity", "slot": 1, "short_id": parent_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )

                    # Wait for both: a debug snapshot with
                    # slot_counter=1 (apply-phase bump) and one with
                    # *both* the parent (in-flight arg ref) and the
                    # child (phase-1 attach surfacing the CBOR's
                    # child ref). The parent's incref lands at apply
                    # time, the child's only after the worker has
                    # awaited ``load_cbor`` and run phase 1 — so
                    # waiting for two entries in ``entity_refcounts``
                    # confirms phase 1 actually ran.
                    def _both_seen(frames: list[ServerFrame]) -> bool:
                        slot_seen = any(
                            isinstance(f, SessionDebugSnapshotEvent) and f.slot_counter == 1
                            for f in frames
                        )
                        ref_seen = any(
                            isinstance(f, SessionDebugSnapshotEvent)
                            and len(f.entity_refcounts) >= 2
                            for f in frames
                        )
                        return slot_seen and ref_seen

                    seen = await _drain_until(client, _both_seen)
                    ref_frame = next(
                        f
                        for f in seen
                        if isinstance(f, SessionDebugSnapshotEvent) and len(f.entity_refcounts) >= 2
                    )
                    child_short_id = ws.state.short_ids.short_id_for_entity(child.entity_id)
                    assert parent_short_id in ref_frame.entity_refcounts
                    assert child_short_id in ref_frame.entity_refcounts

                    # Drain the LoadResultEvent so the queue is
                    # empty before close.
                    await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionEventFrame)
                            and isinstance(f.event, LoadResultEvent)
                            for f in s
                        ),
                    )

                    # Close cleanly.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=2,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    with anyio.move_on_after(0.5):
                        while True:
                            await client.receive()


async def test_session_debug_subscription_torn_down_on_close() -> None:
    """A debug subscription is implicitly torn down server-side when
    the session closes — subsequent server pushes don't hit it
    (and a client unsubscribe by sub-id would error since it's
    gone)."""
    from fxtr_workstation.client_protocol import UnsubscribeRequest

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    assert isinstance(await client.receive(), SessionDebugSnapshotEvent)

                    # Close via mutation.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    # Drain the mutation_committed.
                    with anyio.move_on_after(0.5):
                        while True:
                            await client.receive()

                    # The debug subscription is no longer on the
                    # connection; an unsubscribe with that id errors.
                    await client.send(UnsubscribeRequest(request_id=99, subscription_id=100))
                    err = await client.receive()
                    assert isinstance(err, ErrorResponse)
