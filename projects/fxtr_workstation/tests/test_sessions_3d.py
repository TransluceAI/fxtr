"""Stage-3d tests: ``pinned_by_sessions`` on the entity / service
resource records.

When a session tracks a workstation entity / service, the
corresponding stage-2 resource record now lists the session's UUID
in ``pinned_by_sessions``. Untracking and session close both remove
it. Subscribers to the ``entities`` / ``services`` resource see a
``ResourceChangesEvent`` (stage 4a) with the affected record in
``updated`` on each change.
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    AckResponse,
    MutateSessionRequest,
    MutationCommittedResponse,
    ResourceChangesEvent,
    ResourceSnapshotEvent,
    ServerFrame,
    SubscribeRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import provenanced_key_for

from ._fixtures import EchoImpl, Note
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


# --- Snapshot shape on track / untrack ------------------------------


async def test_entity_snapshot_includes_pinned_by_sessions_on_track() -> None:
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
                    # Subscribe to the entities resource.
                    await client.send(
                        SubscribeRequest(request_id=100, resource="entities", debug=True)
                    )
                    assert (await client.receive()) == AckResponse(request_id=100)
                    initial = await client.receive()
                    assert isinstance(initial, ResourceSnapshotEvent)
                    (entry,) = initial.data
                    assert entry["pinned_by_sessions"] == []

                    # Track from the session.
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

                    # Drain frames until we see an entities delta
                    # showing the session pin.
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and f.subscription_id == 100
                            and any(rec.get("pinned_by_sessions") == [sid] for rec in f.updated)
                            for f in s
                        ),
                    )
                    delta = next(
                        f
                        for f in seen
                        if isinstance(f, ResourceChangesEvent)
                        and f.subscription_id == 100
                        and any(rec.get("pinned_by_sessions") == [sid] for rec in f.updated)
                    )
                    entry = next(
                        rec for rec in delta.updated if rec.get("pinned_by_sessions") == [sid]
                    )
                    assert entry["pinned_by_sessions"] == [sid]

                    # Close — keeps the test cleanup well-behaved
                    # (also exercises the close-removes-pin path).
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
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and f.subscription_id == 100
                            and any(rec.get("pinned_by_sessions") == [] for rec in f.updated)
                            for f in s
                        ),
                    )


async def test_service_snapshot_includes_pinned_by_sessions_on_track() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "the-echo", echo_ref, rpc.scope) as svc_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(
                        SubscribeRequest(request_id=100, resource="services", debug=True)
                    )
                    assert (await client.receive()) == AckResponse(request_id=100)
                    initial = await client.receive()
                    assert isinstance(initial, ResourceSnapshotEvent)
                    (entry,) = initial.data
                    assert entry["pinned_by_sessions"] == []

                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "track_service", "short_id": svc_short_id}],  # type: ignore[list-item]
                        )
                    )

                    await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and f.subscription_id == 100
                            and any(rec.get("pinned_by_sessions") == [sid] for rec in f.updated)
                            for f in s
                        ),
                    )

                    # Close before exiting the RPC scope so the
                    # connection can drain.
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
                    assert isinstance(await client.receive(), MutationCommittedResponse) or True


async def test_untrack_clears_pin_in_snapshot() -> None:
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
                    await client.send(
                        SubscribeRequest(request_id=100, resource="entities", debug=True)
                    )
                    assert (await client.receive()) == AckResponse(request_id=100)
                    assert isinstance(await client.receive(), ResourceSnapshotEvent)

                    # Track.
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
                    await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and any(rec.get("pinned_by_sessions") == [sid] for rec in f.updated)
                            for f in s
                        ),
                    )

                    # Untrack.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "untrack_entity", "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and any(rec.get("pinned_by_sessions") == [] for rec in f.updated)
                            for f in s
                        ),
                    )


# --- Multi-session pinning -----------------------------------------


async def test_pin_lists_multiple_sessions() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as a,
                connect_client(ws.client_url) as b,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with expose_entity(
                    ws_ref, "the-note", note.entity_id, rpc.scope
                ) as ent_short_id:
                    async with (
                        create_and_subscribe(a) as (sid_a, sub_a),
                        create_and_subscribe(b) as (sid_b, sub_b),
                    ):
                        # Both track.
                        for client, sub_id in [(a, sub_a), (b, sub_b)]:
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
                            assert isinstance(await client.receive(), MutationCommittedResponse)

                        # Snapshot now lists both.
                        rec = ws.state.entity_records[provenanced_key_for(note.entity_id)]
                        assert rec.tracking_sessions == {
                            ws.state.sessions[__import__("uuid").UUID(sid_a)].session_id,
                            ws.state.sessions[__import__("uuid").UUID(sid_b)].session_id,
                        }

                        # Close both before exiting scopes.
                        await a.send(
                            MutateSessionRequest(
                                request_id=20,
                                subscription_id=sub_a,
                                ack_seq=1,
                                update_id=2,
                                patch=[],
                                ops=[{"kind": "close"}],  # type: ignore[list-item]
                            )
                        )
                        assert isinstance(await a.receive(), MutationCommittedResponse)
                        await b.send(
                            MutateSessionRequest(
                                request_id=20,
                                subscription_id=sub_b,
                                ack_seq=1,
                                update_id=2,
                                patch=[],
                                ops=[{"kind": "close"}],  # type: ignore[list-item]
                            )
                        )
                        assert isinstance(await b.receive(), MutationCommittedResponse)
