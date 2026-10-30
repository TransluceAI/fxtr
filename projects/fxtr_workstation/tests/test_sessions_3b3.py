"""Stage-3b.3 tests: tracker ops (track/untrack entity/service).

Covers:
- track_entity / untrack_entity happy path.
- track_service / untrack_service happy path.
- Cross-source keep-alive: a session-tracked entity outlives its
  RPC-registered source connection dropping.
- Already-tracked / not-tracked / unknown short-id rejections.
- Mixed track+untrack in one mutation (atomicity, working-set logic).
- Close releases all session-source registrations.
- Atomicity: failing op rejects the whole mutation, no registrations
  added.
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import provenanced_key_for

from ._fixtures import EchoImpl, Note
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


# --- track_entity / untrack_entity ----------------------------------


async def test_track_entity_adds_session_source_registration() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                # RPC peer copies an entity in.
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
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
                                {"kind": "track_entity", "short_id": ent_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    resp = await client.receive()
                    assert isinstance(resp, MutationCommittedResponse)

                    # Session bookkeeping reflects the tracker.
                    session = ws.state.sessions[UUID(sid)]
                    note_key = provenanced_key_for(note.entity_id)
                    assert note_key in session.tracked_entities
                    # The entity record now lists this session in its
                    # tracking_sessions.
                    rec = ws.state.entity_records[note_key]
                    assert session.session_id in rec.tracking_sessions


async def test_session_keeps_entity_alive_after_rpc_drops() -> None:
    """Track an RPC-registered entity from a session, drop the RPC
    connection — the entity stays alive (the session's registration
    still pins it)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    # Bring up an RPC, copy in an entity, track from the
                    # session, then drop the RPC.
                    ent_id = None
                    async with connect_rpc(ws.rpc_url) as rpc:
                        ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                        note = Note(title="t", body="b").store(rpc.scope)
                        async with expose_entity(
                            ws_ref, "the-note", note.entity_id, rpc.scope
                        ) as ent_short_id:
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
                            ent_id = note.entity_id

                    # RPC connection has dropped; entity must still be
                    # tracked because the session sources a registration.
                    assert ent_id is not None
                    ent_key = provenanced_key_for(ent_id)
                    assert ent_key in ws.state.entity_records
                    session = ws.state.sessions[UUID(sid)]
                    assert ent_key in session.tracked_entities


async def test_untrack_entity_releases_session_registration() -> None:
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
                    assert isinstance(await client.receive(), MutationCommittedResponse)

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
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    session = ws.state.sessions[UUID(sid)]
                    note_key = provenanced_key_for(note.entity_id)
                    assert note_key not in session.tracked_entities
                    # Entity still alive because RPC connection still tracks it.
                    assert note_key in ws.state.entity_records


# --- track_service / untrack_service --------------------------------


async def test_track_service_adds_session_source_registration() -> None:
    """Session-source service registration is added on track_service.

    Note: a session that tracks a service keeps the registering RPC
    connection alive (graceful drain blocks until the session
    releases, mirroring structured-concurrency semantics — see
    ``stage3_sessions.md``). The test closes the session before
    exiting the RPC scope so the connection can drain cleanly.
    """
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
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "track_service", "short_id": svc_short_id}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    session = ws.state.sessions[UUID(sid)]
                    assert len(session.tracked_services) == 1

                    # Close the session so the RPC connection can
                    # drain cleanly when the outer ``connect_rpc``
                    # context exits. Without this, the bridge from
                    # ``service/s0`` pins the register_service
                    # dispatch scope (a child of conn_scope) and the
                    # graceful drain blocks on the session — by
                    # design.
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


# --- Errors ---------------------------------------------------------


async def test_track_entity_unknown_short_id_rejects() -> None:
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
                        ops=[{"kind": "track_entity", "short_id": "nope"}],  # type: ignore[list-item]
                    )
                )
                err = await client.receive()
                assert isinstance(err, MutationRejectedResponse) and err.request_id == 10


async def test_track_already_tracked_rejects() -> None:
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
                    # Track once.
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

                    # Track again — rejects.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "track_entity", "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "already tracks" in err.message.lower()


async def test_untrack_not_tracked_rejects() -> None:
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
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "untrack_entity", "short_id": ent_short_id}],  # type: ignore[list-item]
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)
                    assert "does not track" in err.message.lower()


# --- Atomicity ------------------------------------------------------


async def test_failing_op_rolls_back_prior_ops() -> None:
    """If the second op fails, the first's add isn't materialized."""
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
                    # First op valid (track), second op invalid (unknown id).
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "track_entity", "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "track_entity", "short_id": "nope"},  # type: ignore[list-item]
                            ],
                        )
                    )
                    err = await client.receive()
                    assert isinstance(err, MutationRejectedResponse)

                    # No tracker added.
                    session = ws.state.sessions[UUID(sid)]
                    assert provenanced_key_for(note.entity_id) not in session.tracked_entities
                    # State seq didn't bump.
                    assert session.last_state_seq == 0


# --- Mixed track + untrack -----------------------------------------


async def test_track_then_untrack_same_mutation_is_no_op() -> None:
    """Working-set logic: track A then untrack A in one mutation
    leaves A not tracked."""
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
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "track_entity", "short_id": ent_short_id},  # type: ignore[list-item]
                                {"kind": "untrack_entity", "short_id": ent_short_id},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    session = ws.state.sessions[UUID(sid)]
                    assert provenanced_key_for(note.entity_id) not in session.tracked_entities


# --- Close releases all session trackers ---------------------------


async def test_close_releases_session_trackers() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    expose_service(ws_ref, "the-echo", echo_ref, rpc.scope) as svc_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    # Track both.
                    await client.send(
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
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # The entity record has 1 registration (RPC) plus
                    # one tracking session.
                    rec = ws.state.entity_records[provenanced_key_for(note.entity_id)]
                    assert len(rec.registrations) == 1
                    assert len(rec.tracking_sessions) == 1

                    # Close.
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

                    # Session is gone.
                    assert UUID(sid) not in ws.state.sessions
                    # The entity record still has its RPC registration,
                    # but tracking_sessions is empty.
                    rec = ws.state.entity_records[provenanced_key_for(note.entity_id)]
                    assert len(rec.registrations) == 1
                    assert rec.tracking_sessions == set()
