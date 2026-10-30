"""Stage-3b.1 tests: mutation pipeline (JSON Patch only, no ops).

Covers:
- Patch happy path (replace/add/remove/test/move/copy).
- Stale-ack rejection (foreign mutator) + same-mutator skip-ahead.
- ack_seq monotonic non-decreasing.
- No-op mutation (empty patch + empty ops) bumps the seq.
- Multi-subscriber broadcast with mutator self-suppression.
- Retention horizon: events older than ack_seq are pruned.
- Atomicity on patch failure.
- Subscription-id validation: mutate must reference a session sub
  on the same connection.
- Stage-3b.1 op rejection (non-empty ops list).
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer
from fxtr_workstation.client_protocol import (
    AckResponse,
    CreateSessionRequest,
    ErrorResponse,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    ServerFrame,
    SessionCreatedResponse,
    SessionEventFrame,
    SessionSnapshotEvent,
    StateUpdateEvent,
    SubscribeSessionRequest,
)

from ._session_helpers import create_and_subscribe
from ._ws_client import ClientWS, connect_client

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


# --- Patch happy path -------------------------------------------------


async def test_simple_replace_patch() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/k", "value": "v"}],
                        ops=[],
                    )
                )
                resp = await client.receive()
                assert isinstance(resp, MutationCommittedResponse)
                assert resp.request_id == 10
                assert resp.subscription_id == sub_id
                assert resp.update_id == 1
                assert resp.seq == 1

                # State has been updated server-side.
                from uuid import UUID

                assert ws.state.sessions[UUID(sid)].state == {"k": "v"}


async def test_full_patch_dialect() -> None:
    """Exercise add / replace / remove / test / move / copy."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=1,
                        patch=[
                            {"op": "add", "path": "/x", "value": 1},
                            {"op": "add", "path": "/y", "value": 2},
                            {"op": "replace", "path": "/x", "value": 10},
                            {"op": "test", "path": "/x", "value": 10},
                            {"op": "copy", "from": "/y", "path": "/y2"},
                            {"op": "move", "from": "/y2", "path": "/z"},
                            {"op": "remove", "path": "/y"},
                        ],
                        ops=[],
                    )
                )
                resp = await client.receive()
                assert isinstance(resp, MutationCommittedResponse)
                from uuid import UUID

                assert ws.state.sessions[UUID(sid)].state == {"x": 10, "z": 2}


async def test_no_op_mutation_bumps_seq() -> None:
    """Empty patch + empty ops is valid and bumps the state seq."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                from uuid import UUID

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
                resp = await client.receive()
                assert isinstance(resp, MutationCommittedResponse)
                assert resp.seq == 1
                assert ws.state.sessions[UUID(sid)].last_state_seq == 1
                assert ws.state.sessions[UUID(sid)].state == {}


# --- Stale-ack rejection ---------------------------------------------


async def test_foreign_stale_ack_rejected() -> None:
    """Two clients on the same session: first one's commit invalidates
    the second one's stale ack_seq."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as a,
                connect_client(ws.client_url) as b,
            ):
                # Setup: create session and subscribe both clients.
                await a.send(CreateSessionRequest(request_id=1, metadata={}))
                created = await a.receive()
                assert isinstance(created, SessionCreatedResponse)
                sid = created.session_id

                await a.send(SubscribeSessionRequest(request_id=2, session_id=sid))
                assert (await a.receive()) == AckResponse(request_id=2)
                assert isinstance(await a.receive(), SessionSnapshotEvent)

                await b.send(SubscribeSessionRequest(request_id=3, session_id=sid))
                assert (await b.receive()) == AckResponse(request_id=3)
                assert isinstance(await b.receive(), SessionSnapshotEvent)

                # A mutates first.
                await a.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=2,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/a", "value": 1}],
                        ops=[],
                    )
                )
                ack = await a.receive()
                assert isinstance(ack, MutationCommittedResponse) and ack.seq == 1

                # B sees A's update as a foreign state_update event.
                evt = await b.receive()
                assert isinstance(evt, SessionEventFrame)
                assert isinstance(evt.event, StateUpdateEvent)
                assert evt.event.seq == 1

                # B tries to mutate with the stale ack_seq=0 — rejected.
                await b.send(
                    MutateSessionRequest(
                        request_id=20,
                        subscription_id=3,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/b", "value": 2}],
                        ops=[],
                    )
                )
                err = await b.receive()
                assert isinstance(err, MutationRejectedResponse) and err.request_id == 20
                assert "stale" in err.message.lower()
                assert err.reason == "conflict"


async def test_same_mutator_skip_ahead_allowed() -> None:
    """The same (connection, subscription_id) can mutate again with
    a stale ack_seq because they made the latest state themselves."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                # First mutation.
                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/a", "value": 1}],
                        ops=[],
                    )
                )
                assert isinstance(await client.receive(), MutationCommittedResponse)

                # Second mutation, same client. ack_seq=0 is "stale"
                # in the foreign sense but allowed for same-mutator.
                await client.send(
                    MutateSessionRequest(
                        request_id=11,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=2,
                        patch=[{"op": "add", "path": "/b", "value": 2}],
                        ops=[],
                    )
                )
                resp = await client.receive()
                assert isinstance(resp, MutationCommittedResponse)
                assert resp.seq == 2


async def test_ack_seq_below_retention_horizon_rejected() -> None:
    """ack_seq must be monotonic non-decreasing across same-client
    mutations: going below the retention horizon (= last accepted
    ack_seq) is rejected even for the same mutator."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                # First mutation acks up to seq=0 (initial state).
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

                # Second mutation acks up to seq=1.
                await client.send(
                    MutateSessionRequest(
                        request_id=11,
                        subscription_id=sub_id,
                        ack_seq=1,
                        update_id=2,
                        patch=[],
                        ops=[],
                    )
                )
                assert isinstance(await client.receive(), MutationCommittedResponse)

                # Third mutation tries ack_seq=0 — below horizon. Reject.
                await client.send(
                    MutateSessionRequest(
                        request_id=12,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=3,
                        patch=[],
                        ops=[],
                    )
                )
                err = await client.receive()
                assert isinstance(err, MutationRejectedResponse) and err.request_id == 12
                assert "monotonic" in err.message.lower() or "horizon" in err.message.lower()
                assert err.reason == "stale_ack"


# --- Self-suppression / foreign broadcast -----------------------------


async def test_mutator_does_not_receive_state_update_event() -> None:
    """The mutator gets only mutation_committed; no SessionEventFrame
    for their own state_update."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/k", "value": "v"}],
                        ops=[],
                    )
                )
                resp = await client.receive()
                assert isinstance(resp, MutationCommittedResponse)

                # No SessionEventFrame should follow.
                with anyio.move_on_after(0.2) as cs:
                    frame = await client.receive()
                    assert not isinstance(
                        frame, SessionEventFrame
                    ), f"mutator received own state_update event: {frame}"
                assert cs.cancelled_caught


async def test_other_subscription_on_same_connection_sees_foreign_event() -> None:
    """Two subscriptions on the same connection: only the mutator's
    own subscription is suppressed; the other one gets the foreign
    state_update event."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # Create + subscribe twice.
                await client.send(CreateSessionRequest(request_id=1, metadata={}))
                created = await client.receive()
                assert isinstance(created, SessionCreatedResponse)
                sid = created.session_id

                await client.send(SubscribeSessionRequest(request_id=2, session_id=sid))
                assert (await client.receive()) == AckResponse(request_id=2)
                assert isinstance(await client.receive(), SessionSnapshotEvent)

                await client.send(SubscribeSessionRequest(request_id=3, session_id=sid))
                assert (await client.receive()) == AckResponse(request_id=3)
                assert isinstance(await client.receive(), SessionSnapshotEvent)

                # Mutate using subscription 2.
                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=2,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/k", "value": "v"}],
                        ops=[],
                    )
                )
                # Drain frames until we see the mutation_committed.
                seen = await _drain_until(
                    client,
                    lambda s: any(isinstance(f, MutationCommittedResponse) for f in s),
                )
                # Subscription 3 should have received a SessionEventFrame.
                evts = [
                    f for f in seen if isinstance(f, SessionEventFrame) and f.subscription_id == 3
                ]
                assert evts and isinstance(evts[0].event, StateUpdateEvent)
                # Subscription 2 should NOT have received one.
                assert not any(
                    isinstance(f, SessionEventFrame) and f.subscription_id == 2 for f in seen
                )


# --- Retention horizon -----------------------------------------------


async def test_retention_horizon_drops_old_events() -> None:
    """After a mutation acks past previous events, the log is pruned
    and a fresh subscription's snapshot reflects the trimmed log."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as a,
                create_and_subscribe(a) as (sid, sub_id),
            ):
                from uuid import UUID

                session = ws.state.sessions[UUID(sid)]

                # Mutate three times, advancing ack_seq each time so
                # the log keeps getting pruned.
                for i, ack in enumerate([0, 1, 2], start=10):
                    await a.send(
                        MutateSessionRequest(
                            request_id=i,
                            subscription_id=sub_id,
                            ack_seq=ack,
                            update_id=i,
                            patch=[],
                            ops=[],
                        )
                    )
                    assert isinstance(await a.receive(), MutationCommittedResponse)

                # last_state_seq=3, last_state_ack_seq=2. State_updates
                # are not stored in event_log (3-post-2 invariant) —
                # they're authoritative through last_state_seq /
                # last_state_ack_seq / state. event_log holds only
                # non-state events, of which there are none here.
                assert session.last_state_seq == 3
                assert session.last_state_ack_seq == 2
                assert session.event_log == []

                # A fresh subscriber sees the latest state via
                # last_state, with no events in either bucket.
                async with connect_client(ws.client_url) as b:
                    await b.send(SubscribeSessionRequest(request_id=99, session_id=sid))
                    assert (await b.receive()) == AckResponse(request_id=99)
                    snap = await b.receive()
                    assert isinstance(snap, SessionSnapshotEvent)
                    assert snap.last_state.seq == 3
                    assert snap.last_state.ack_seq == 2
                    assert snap.preserved_events == []
                    assert snap.subsequent_events == []


# --- Atomicity --------------------------------------------------------


async def test_failing_patch_atomic() -> None:
    """A patch that fails mid-application leaves no observable state
    change, no event, no seq bump."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                from uuid import UUID

                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=sub_id,
                        ack_seq=0,
                        update_id=1,
                        # The first op succeeds in isolation, but the
                        # `test` op fails, so the whole patch fails.
                        patch=[
                            {"op": "add", "path": "/k", "value": "v"},
                            {"op": "test", "path": "/k", "value": "wrong"},
                        ],
                        ops=[],
                    )
                )
                err = await client.receive()
                assert isinstance(err, MutationRejectedResponse) and err.request_id == 10

                session = ws.state.sessions[UUID(sid)]
                assert session.state == {}
                assert session.last_state_seq == 0
                assert session.event_log == []


# --- Subscription validation -----------------------------------------


async def test_mutate_with_unknown_subscription_id_errors() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # No subscription exists at all.
                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=99,
                        ack_seq=0,
                        update_id=1,
                        patch=[],
                        ops=[],
                    )
                )
                err = await client.receive()
                assert (
                    isinstance(err, MutationRejectedResponse)
                    and err.request_id == 10
                    and err.reason == "validation_failed"
                )


async def test_mutate_with_resource_subscription_id_errors() -> None:
    """The subscription_id must be a session subscription, not a
    resource-list subscription."""
    from fxtr_workstation.client_protocol import SubscribeRequest

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # Create a session so apply_mutation has something to
                # potentially target — but the subscription is a
                # resource sub, not a session sub.
                await client.send(CreateSessionRequest(request_id=1, metadata={}))
                assert isinstance(await client.receive(), SessionCreatedResponse)

                await client.send(SubscribeRequest(request_id=2, resource="services"))
                assert (await client.receive()) == AckResponse(request_id=2)
                assert (await client.receive()).subscription_id == 2  # type: ignore[union-attr]

                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=2,
                        ack_seq=0,
                        update_id=1,
                        patch=[],
                        ops=[],
                    )
                )
                err = await client.receive()
                assert (
                    isinstance(err, MutationRejectedResponse)
                    and err.request_id == 10
                    and err.reason == "validation_failed"
                )


# --- Unknown op kind rejected at the wire layer ---------------------


async def test_unknown_op_kind_rejected() -> None:
    """The ops union is tagged on ``kind``; an unknown kind is
    rejected at msgspec decode time, surfaced as a parse error."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (_sid, sub_id),
            ):
                # Bypass the typed encoder — send raw JSON with a bogus kind.
                await client.send_text(
                    '{"op": "mutate_session", "request_id": 10, '
                    f'"subscription_id": {sub_id}, "ack_seq": 0, '
                    '"update_id": 1, "patch": [], '
                    '"ops": [{"kind": "nope"}]}'
                )
                err = await client.receive()
                assert isinstance(err, ErrorResponse)


# --- Event log shape (3-post-2) -------------------------------------


async def test_event_log_excludes_state_updates() -> None:
    """``event_log`` is a non-state-only list (3-post-2 invariant).
    State_updates are authoritative through ``last_state_*`` fields
    and broadcast in-the-moment, never persisted in the log.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client,
                create_and_subscribe(client) as (sid, sub_id),
            ):
                from uuid import UUID

                # Several mutations advance last_state_seq but leave
                # event_log empty (no non-state events were emitted).
                for i, ack in enumerate([0, 1, 2], start=10):
                    await client.send(
                        MutateSessionRequest(
                            request_id=i,
                            subscription_id=sub_id,
                            ack_seq=ack,
                            update_id=i,
                            patch=[{"op": "add", "path": f"/k{i}", "value": i}],
                            ops=[],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                session = ws.state.sessions[UUID(sid)]
                assert session.last_state_seq == 3
                # Invariant: no StateUpdateEvent ever ends up in
                # event_log, regardless of ack_seq.
                assert all(not isinstance(e, StateUpdateEvent) for e in session.event_log)
                # No non-state ops were issued, so the log is empty.
                assert session.event_log == []


async def test_resubscribe_reflects_latest_commit_in_last_state() -> None:
    """After several commits, a fresh subscription's snapshot has
    ``last_state`` carrying the post-state of the most recent commit
    (with the up-to-date ``slot_counter`` field). ``preserved_events``
    and ``subsequent_events`` are both empty when the mutator stays
    fully caught up.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as a,
                create_and_subscribe(a) as (sid, sub_id_a),
            ):
                # Three mutations, each one acks the previous.
                for i, ack in enumerate([0, 1, 2], start=10):
                    await a.send(
                        MutateSessionRequest(
                            request_id=i,
                            subscription_id=sub_id_a,
                            ack_seq=ack,
                            update_id=i,
                            patch=[{"op": "add", "path": f"/k{i}", "value": i}],
                            ops=[],
                        )
                    )
                    assert isinstance(await a.receive(), MutationCommittedResponse)

                async with connect_client(ws.client_url) as b:
                    await b.send(SubscribeSessionRequest(request_id=99, session_id=sid))
                    assert (await b.receive()) == AckResponse(request_id=99)
                    snap = await b.receive()
                    assert isinstance(snap, SessionSnapshotEvent)
                    assert snap.last_state.seq == 3
                    assert snap.last_state.ack_seq == 2
                    assert snap.last_state.state == {"k10": 10, "k11": 11, "k12": 12}
                    assert snap.last_state.slot_counter == 0
                    assert snap.preserved_events == []
                    assert snap.subsequent_events == []
