"""Stage-3a session tests: lifecycle (create), single-session
subscribe + snapshot, sessions-as-resource listing + subscription.

The bespoke ``list_sessions`` / ``subscribe_session_list`` ops were
retired in stage 4a; sessions are now a regular resource subscribed
to via ``SubscribeRequest(resource="sessions")``.

Closing a session is exercised in test_sessions_3b2.py once the
``close`` mutation op is in place.
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer
from fxtr_workstation.client_protocol import (
    AckResponse,
    CreateSessionRequest,
    ErrorResponse,
    ResourceChangesEvent,
    ResourceSnapshotEvent,
    ServerFrame,
    SessionCreatedResponse,
    SessionSnapshotEvent,
    SubscribeRequest,
    SubscribeSessionRequest,
)

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


# --- create / list ----------------------------------------------------


async def test_create_session_returns_session_id() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(CreateSessionRequest(request_id=1, metadata={"k": "v"}))
                resp = await client.receive()
                assert isinstance(resp, SessionCreatedResponse)
                assert resp.request_id == 1
                assert resp.session_id  # non-empty
                # State has the session.
                assert len(ws.state.sessions) == 1


async def test_sessions_resource_initial_snapshot_and_metadata() -> None:
    """The ``sessions`` resource yields a snapshot on subscribe and
    its records carry per-session ``session_id`` / ``metadata`` /
    ``created_at`` (lifecycle-only — no state-progression fields)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # Subscribe with no sessions — initial snapshot is empty.
                await client.send(SubscribeRequest(request_id=1, resource="sessions"))
                assert (await client.receive()) == AckResponse(request_id=1)
                snap = await client.receive()
                assert isinstance(snap, ResourceSnapshotEvent)
                assert snap.subscription_id == 1 and snap.data == []

                # Create two sessions; expect two ``resource_changes``
                # frames, each carrying the new record in ``added``.
                await client.send(CreateSessionRequest(request_id=2, metadata={"a": 1}))
                seen_a = await _drain_until(
                    client,
                    lambda s: any(isinstance(f, SessionCreatedResponse) for f in s),
                )
                created_a = next(f for f in seen_a if isinstance(f, SessionCreatedResponse))
                changes_a = [f for f in seen_a if isinstance(f, ResourceChangesEvent)]
                assert changes_a, f"no resource_changes in {seen_a!r}"
                assert changes_a[-1].added[0]["session_id"] == created_a.session_id
                assert changes_a[-1].added[0]["metadata"] == {"a": 1}
                assert changes_a[-1].updated == [] and changes_a[-1].removed == []

                await client.send(CreateSessionRequest(request_id=3, metadata={"b": 2}))
                seen_b = await _drain_until(
                    client,
                    lambda s: any(isinstance(f, SessionCreatedResponse) for f in s),
                )
                created_b = next(f for f in seen_b if isinstance(f, SessionCreatedResponse))
                changes_b = [f for f in seen_b if isinstance(f, ResourceChangesEvent)]
                assert changes_b[-1].added[0]["session_id"] == created_b.session_id
                assert changes_b[-1].added[0]["metadata"] == {"b": 2}

                # State-progression fields excluded from the resource record.
                rec = changes_b[-1].added[0]
                assert "last_state_seq" not in rec
                assert "slot_counter" not in rec


# --- subscribe / unsubscribe to a single session ----------------------


async def test_subscribe_session_returns_initial_snapshot() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(CreateSessionRequest(request_id=1, metadata={"x": 1}))
                created = await client.receive()
                assert isinstance(created, SessionCreatedResponse)

                await client.send(
                    SubscribeSessionRequest(request_id=2, session_id=created.session_id)
                )
                ack = await client.receive()
                assert ack == AckResponse(request_id=2)

                snap = await client.receive()
                assert isinstance(snap, SessionSnapshotEvent)
                assert snap.subscription_id == 2
                assert snap.session_id == created.session_id
                assert snap.last_state.state == {}
                assert snap.last_state.seq == 0
                assert snap.last_state.ack_seq == 0
                assert snap.last_state.slot_counter == 0
                assert snap.preserved_events == []
                assert snap.subsequent_events == []


async def test_subscribe_unknown_session_errors() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(
                    SubscribeSessionRequest(
                        request_id=1, session_id="00000000-0000-0000-0000-000000000000"
                    )
                )
                err = await client.receive()
                assert isinstance(err, ErrorResponse) and err.request_id == 1


# --- session-list subscription ----------------------------------------


async def test_sessions_resource_pushes_added_on_create() -> None:
    """Subscribers to the ``sessions`` resource receive a
    ``resource_changes`` frame with the new record in ``added`` when
    a session is created."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=1, resource="sessions"))
                assert (await client.receive()) == AckResponse(request_id=1)
                first = await client.receive()
                assert isinstance(first, ResourceSnapshotEvent)
                assert first.subscription_id == 1 and first.data == []

                await client.send(CreateSessionRequest(request_id=2, metadata={"k": "v"}))
                # Order: the resource buffer flush (before
                # ``create_session`` returns to the handler) precedes
                # the ``SessionCreatedResponse`` enqueue.
                seen = await _drain_until(
                    client,
                    lambda s: any(isinstance(f, SessionCreatedResponse) for f in s),
                )
                deltas = [f for f in seen if isinstance(f, ResourceChangesEvent)]
                assert deltas, f"no resource_changes in {seen!r}"
                assert len(deltas[-1].added) == 1
                assert deltas[-1].added[0]["metadata"] == {"k": "v"}


async def test_two_subscribers_see_same_snapshot() -> None:
    """Two clients both subscribe to the same session; both receive
    identical initial snapshots."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as creator:
                await creator.send(CreateSessionRequest(request_id=1, metadata={"k": "v"}))
                created = await creator.receive()
                assert isinstance(created, SessionCreatedResponse)
                sid = created.session_id

            async with (
                connect_client(ws.client_url) as a,
                connect_client(ws.client_url) as b,
            ):
                await a.send(SubscribeSessionRequest(request_id=10, session_id=sid))
                await b.send(SubscribeSessionRequest(request_id=20, session_id=sid))

                assert (await a.receive()) == AckResponse(request_id=10)
                snap_a = await a.receive()
                assert (await b.receive()) == AckResponse(request_id=20)
                snap_b = await b.receive()

                assert isinstance(snap_a, SessionSnapshotEvent)
                assert isinstance(snap_b, SessionSnapshotEvent)
                assert snap_a.session_id == snap_b.session_id == sid
                assert snap_a.last_state.state == snap_b.last_state.state == {}


# --- subscription id collisions ---------------------------------------


async def test_session_subscribe_id_collision_errors() -> None:
    """The session-subscription path uses the same connection-wide id
    space as the resource subscription path."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(CreateSessionRequest(request_id=1, metadata={}))
                created = await client.receive()
                assert isinstance(created, SessionCreatedResponse)

                await client.send(
                    SubscribeSessionRequest(request_id=5, session_id=created.session_id)
                )
                assert (await client.receive()) == AckResponse(request_id=5)
                assert isinstance(await client.receive(), SessionSnapshotEvent)

                # Re-using id=5 for any subscription kind should error.
                await client.send(SubscribeRequest(request_id=5, resource="sessions"))
                err = await client.receive()
                assert isinstance(err, ErrorResponse) and err.request_id == 5
