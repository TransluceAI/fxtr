"""Unit tests for the snapshot builders that back ``/client``
subscriptions. Stand up a real ``WorkstationServer`` and an ``/rpc``
peer that exercises every record kind, then call the snapshot
builders directly and verify shape + content.

End-to-end tests for the live-update behavior live in
``test_client_protocol.py`` once the ASGI route is wired up.
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import AckResponse, ResourceSnapshotEvent
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import (
    ResourceSubscription,
    _build_entities_snapshot,
    _build_rpc_connections_snapshot,
    _build_services_snapshot,
)

from ._fixtures import EchoImpl, Note
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def test_snapshot_builders_with_live_records() -> None:
    """Open a server, register one service and copy one entity from
    a single connection, and verify all three snapshot builders
    produce the documented shape."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session:
                ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
                workstation = await ws_ref.resolve(session.scope)
                await workstation.name_connection("alice")

                echo_ref = await session.scope.mount(EchoImpl())
                note = Note(title="hi", body="there").store(session.scope)

                async with (
                    expose_service(ws_ref, "the-echo", echo_ref, session.scope) as svc_short_id,
                    expose_entity(
                        ws_ref, "the-note", note.entity_id, session.scope
                    ) as ent_short_id,
                ):
                    # rpc_connections snapshot
                    conns = _build_rpc_connections_snapshot(ws.state)
                    assert len(conns) == 1
                    (entry,) = conns
                    assert entry["name"] == "alice"
                    assert entry["peer_addr"] is not None
                    assert entry["peer_addr"].startswith("127.0.0.1:")
                    assert isinstance(entry["connected_at"], str)
                    assert entry["service_registration_short_ids"] == [svc_short_id]
                    assert entry["entity_registration_short_ids"] == [ent_short_id]
                    # connection_id round-trips through str
                    assert isinstance(entry["connection_id"], str)
                    assert len(entry["connection_id"]) == 36  # uuid

                    # services snapshot
                    services = _build_services_snapshot(ws.state)
                    assert len(services) == 1
                    (svc,) = services
                    assert svc["short_id"] == svc_short_id
                    assert svc["service_type_id"]  # id_string
                    assert svc["external_names"] == ["the-echo"]
                    assert svc["registering_connection_ids"] == [entry["connection_id"]]

                    # entities snapshot
                    entities = _build_entities_snapshot(ws.state)
                    assert len(entities) == 1
                    (ent,) = entities
                    assert ent["short_id"] == ent_short_id
                    assert ent["fxtr_id"].startswith("d")
                    assert ent["external_names"] == ["the-note"]
                    assert ent["registering_connection_ids"] == [entry["connection_id"]]


async def test_snapshot_builders_empty_state() -> None:
    """No connections, no services, no entities — empty lists."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            assert _build_rpc_connections_snapshot(ws.state) == []
            assert _build_services_snapshot(ws.state) == []
            assert _build_entities_snapshot(ws.state) == []


async def test_add_client_connection_lifecycle() -> None:
    """``add_client_connection`` registers / removes the record and
    yields a usable send/receive stream pair."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            assert ws.state.client_connections == {}
            async with ws.state.add_client_connection("test:1234") as (
                record,
                receive,
            ):
                assert record.connection_id in ws.state.client_connections
                assert record.peer_addr == "test:1234"
                # send_nowait works
                ack = AckResponse(request_id=42)
                assert record.send_nowait(ack) is True
                # And lands on the receive side
                assert await receive.receive() == ack
            # Cleaned up on exit
            assert ws.state.client_connections == {}


async def test_send_nowait_overflow_cancels_connection() -> None:
    """Filling the send buffer triggers cancel-and-mark-overflowed."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None, send_buffer_size=2) as (
                record,
                _receive,
            ):
                assert record.send_nowait(AckResponse(request_id=1)) is True
                assert record.send_nowait(AckResponse(request_id=2)) is True
                # Buffer is now full; next send fails.
                assert record.send_nowait(AckResponse(request_id=3)) is False
                assert record.overflowed is True
                # And cancels the connection's scope.
                assert record._cancel_scope.cancel_called
                # Subsequent sends short-circuit.
                assert record.send_nowait(AckResponse(request_id=4)) is False


async def test_inline_resource_changes_fire_at_each_mutation_site() -> None:
    """Subscribe to all three resources; exercise every mutation
    site (rpc-connect, name_connection, register_service, copy_entity,
    release of each, rpc-disconnect); drain the queue and verify each
    mutation triggered a matching ``ResourceChangesEvent`` delta on
    the right subscription.

    Stage 4a refactor: previously this verified ``ResourceSnapshotEvent``
    fan-out via ``notify_subscribers``; now the same mutations
    produce ``ResourceChangesEvent`` deltas via the resource-change
    buffer's flush.
    """
    from fxtr_workstation.client_protocol import ResourceChangesEvent

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (
                client_record,
                receive,
            ):
                ws.state._register_subscription(
                    client_record, 1, ResourceSubscription(resource="rpc_connections")
                )
                ws.state._register_subscription(
                    client_record, 2, ResourceSubscription(resource="services")
                )
                ws.state._register_subscription(
                    client_record, 3, ResourceSubscription(resource="entities")
                )

                async def drain_until(predicate, timeout: float = 1.0) -> None:
                    with anyio.fail_after(timeout):
                        while not predicate(seen):
                            seen.append(await receive.receive())

                seen: list = []

                async with connect_rpc(ws.rpc_url) as session_a:
                    # rpc connection added → rpc_connections delta with
                    # an ``added`` entry.
                    await drain_until(
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and f.subscription_id == 1
                            and len(f.added) == 1
                            for f in s
                        )
                    )

                    ws_ref = session_a.remote_service.replace_service_type(WorkstationServiceProto)
                    workstation = await ws_ref.resolve(session_a.scope)

                    await workstation.name_connection("alice")
                    # name_connection → rpc_connections delta with the
                    # renamed record in ``updated``.
                    await drain_until(
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and f.subscription_id == 1
                            and any(rec.get("name") == "alice" for rec in f.updated)
                            for f in s
                        )
                    )

                    echo_ref = await session_a.scope.mount(EchoImpl())
                    note = Note(title="t", body="b").store(session_a.scope)

                    async with expose_service(
                        ws_ref, "the-echo", echo_ref, session_a.scope
                    ) as svc_short_id:
                        # register_service → services delta with the new
                        # record in ``added``.
                        await drain_until(
                            lambda s: any(
                                isinstance(f, ResourceChangesEvent)
                                and f.subscription_id == 2
                                and any(rec.get("short_id") == svc_short_id for rec in f.added)
                                for f in s
                            )
                        )

                        async with expose_entity(
                            ws_ref, "the-note", note.entity_id, session_a.scope
                        ) as ent_short_id:
                            # copy_entity → entities delta with the new
                            # record in ``added``.
                            await drain_until(
                                lambda s: any(
                                    isinstance(f, ResourceChangesEvent)
                                    and f.subscription_id == 3
                                    and any(rec.get("short_id") == ent_short_id for rec in f.added)
                                    for f in s
                                )
                            )
                        # expose_entity exit → entities delta with the
                        # short_id in ``removed``.
                        await drain_until(
                            lambda s: any(
                                isinstance(f, ResourceChangesEvent)
                                and f.subscription_id == 3
                                and ent_short_id in f.removed
                                for f in s
                            )
                        )
                    # expose_service exit → services delta with the
                    # short_id in ``removed``.
                    await drain_until(
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and f.subscription_id == 2
                            and svc_short_id in f.removed
                            for f in s
                        )
                    )
                # session_a closed → rpc_connections delta with the
                # connection_id in ``removed``.
                await drain_until(
                    lambda s: any(
                        isinstance(f, ResourceChangesEvent)
                        and f.subscription_id == 1
                        and len(f.removed) == 1
                        for f in s
                    )
                )
