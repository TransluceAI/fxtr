"""End-to-end tests for the workstation's ``/client`` endpoint:
real WebSocket, real subscribe/snapshot/unsubscribe flow, real
mutations triggered from a separate ``/rpc`` connection.

Unit tests for the wire-format parse/serialize live in
``test_client_protocol_unit.py``.
"""

from __future__ import annotations

import anyio
import httpx_ws
import pytest
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    AckResponse,
    ErrorResponse,
    ResourceChangesEvent,
    ResourceSnapshotEvent,
    ServerFrame,
    SubscribeRequest,
    UnsubscribeRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import EchoImpl, Note
from ._ws_client import ClientWS, connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _drain_until(
    client: ClientWS,
    predicate,
    *,
    timeout: float = 2.0,
) -> list[ServerFrame]:
    """Receive frames until ``predicate(seen)`` is True or timeout."""
    seen: list[ServerFrame] = []
    with anyio.fail_after(timeout):
        while not predicate(seen):
            seen.append(await client.receive())
    return seen


# --- Subscribe/ack/initial-snapshot ---------------------------------


@pytest.mark.parametrize("resource", ["rpc_connections", "services", "entities"])
async def test_subscribe_acks_then_sends_initial_snapshot(resource: str) -> None:
    """For each resource: subscribe → ack → empty initial snapshot."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=42, resource=resource))  # type: ignore[arg-type]
                # First the ack...
                ack = await client.receive()
                assert ack == AckResponse(request_id=42)
                # ...then the initial snapshot.
                snap = await client.receive()
                assert isinstance(snap, ResourceSnapshotEvent)
                assert snap.subscription_id == 42
                assert snap.data == []


# --- Mutate via /rpc → snapshot fires --------------------------------


async def test_rpc_connect_triggers_rpc_connections_delta() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=1, resource="rpc_connections"))
                # ack + initial snapshot (empty)
                assert await client.receive() == AckResponse(request_id=1)
                first = await client.receive()
                assert isinstance(first, ResourceSnapshotEvent) and first.data == []

                async with connect_rpc(ws.rpc_url):
                    # Wait for a ``resource_changes`` frame with one
                    # ``added`` connection.
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent) and len(f.added) == 1 for f in s
                        ),
                    )
                    delta = next(
                        f for f in seen if isinstance(f, ResourceChangesEvent) and len(f.added) == 1
                    )
                    assert delta.added[0]["peer_addr"].startswith("127.0.0.1:")

                # After the rpc disconnects, a ``removed`` delta.
                await _drain_until(
                    client,
                    lambda s: any(
                        isinstance(f, ResourceChangesEvent) and len(f.removed) == 1 for f in s
                    ),
                )


async def test_register_service_triggers_services_delta() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client, connect_rpc(ws.rpc_url) as session:
                await client.send(SubscribeRequest(request_id=2, resource="services"))
                assert await client.receive() == AckResponse(request_id=2)
                initial = await client.receive()
                assert isinstance(initial, ResourceSnapshotEvent) and initial.data == []

                ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await session.scope.mount(EchoImpl())
                async with expose_service(ws_ref, "the-echo", echo_ref, session.scope) as short_id:
                    # ``register_service`` mints the record and binds the
                    # external name in two separate mutation contexts
                    # (separated by ``await bridge_ready.wait()``), so
                    # we look across all delta frames for one that
                    # carries the named record — could be in either
                    # ``added`` (if the server collapsed both flushes
                    # for the slow client) or ``updated``.
                    def _matches(rec: dict[str, object]) -> bool:
                        return rec.get("short_id") == short_id and rec.get("external_names") == [
                            "the-echo"
                        ]

                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and (
                                any(_matches(r) for r in f.added)
                                or any(_matches(r) for r in f.updated)
                            )
                            for f in s
                        ),
                    )
                    delta = next(
                        f
                        for f in seen
                        if isinstance(f, ResourceChangesEvent)
                        and (
                            any(_matches(r) for r in f.added) or any(_matches(r) for r in f.updated)
                        )
                    )
                    entry = next(r for r in (*delta.added, *delta.updated) if _matches(r))
                    assert entry["external_names"] == ["the-echo"]


async def test_copy_entity_triggers_entities_delta() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client, connect_rpc(ws.rpc_url) as session:
                await client.send(SubscribeRequest(request_id=3, resource="entities"))
                assert await client.receive() == AckResponse(request_id=3)
                assert (await client.receive()).data == []  # type: ignore[union-attr]

                ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(session.scope)
                async with expose_entity(
                    ws_ref, "the-note", note.entity_id, session.scope
                ) as short_id:
                    delta = next(
                        f
                        for f in await _drain_until(
                            client,
                            lambda s: any(
                                isinstance(f, ResourceChangesEvent)
                                and any(rec.get("short_id") == short_id for rec in f.added)
                                for f in s
                            ),
                        )
                        if isinstance(f, ResourceChangesEvent)
                        and any(rec.get("short_id") == short_id for rec in f.added)
                    )
                    entry = next(rec for rec in delta.added if rec.get("short_id") == short_id)
                    assert entry["fxtr_id"] == note.entity_id.id_string()


# --- Unsubscribe ----------------------------------------------------


async def test_unsubscribe_stops_further_snapshots() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=10, resource="rpc_connections"))
                assert await client.receive() == AckResponse(request_id=10)
                assert (await client.receive()).subscription_id == 10  # type: ignore[union-attr]

                # Unsubscribe
                await client.send(UnsubscribeRequest(request_id=11, subscription_id=10))
                assert await client.receive() == AckResponse(request_id=11)

                # A new RPC connection should NOT trigger a snapshot for
                # subscription 10 anymore.
                async with connect_rpc(ws.rpc_url):
                    with anyio.move_on_after(0.3) as cs:
                        frame = await client.receive()
                        pytest.fail(f"received unexpected frame after unsubscribe: {frame}")
                    assert cs.cancelled_caught


# --- Error frames ---------------------------------------------------


async def test_invalid_json_returns_error_frame() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send_text("{ this is not json")
                err = await client.receive()
                assert isinstance(err, ErrorResponse)
                # Connection still alive — a follow-up subscribe works.
                await client.send(SubscribeRequest(request_id=1, resource="services"))
                assert await client.receive() == AckResponse(request_id=1)


async def test_unknown_resource_returns_error_frame() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send_text('{"id": 7, "op": "subscribe", "resource": "nope"}')
                err = await client.receive()
                assert isinstance(err, ErrorResponse)
                # Connection still alive.
                await client.send(SubscribeRequest(request_id=1, resource="services"))
                assert await client.receive() == AckResponse(request_id=1)


async def test_duplicate_subscription_id_returns_error_frame() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=5, resource="services"))
                assert await client.receive() == AckResponse(request_id=5)
                assert (await client.receive()).subscription_id == 5  # type: ignore[union-attr]

                # Second subscribe with the same id → error
                await client.send(SubscribeRequest(request_id=5, resource="entities"))
                err = await client.receive()
                assert isinstance(err, ErrorResponse)
                assert err.request_id == 5
                assert "already in use" in err.message


async def test_unknown_subscription_id_unsubscribe_returns_error() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(UnsubscribeRequest(request_id=99, subscription_id=42))
                err = await client.receive()
                assert isinstance(err, ErrorResponse)
                assert err.request_id == 99


# --- Multiple clients independent ----------------------------------


async def test_two_clients_both_see_snapshots_and_are_independent() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_client(ws.client_url) as client_a,
                connect_client(ws.client_url) as client_b,
            ):
                await client_a.send(SubscribeRequest(request_id=1, resource="rpc_connections"))
                await client_b.send(SubscribeRequest(request_id=2, resource="rpc_connections"))

                # Each gets ack + initial snapshot
                assert await client_a.receive() == AckResponse(request_id=1)
                first_a = await client_a.receive()
                assert isinstance(first_a, ResourceSnapshotEvent) and first_a.subscription_id == 1
                assert await client_b.receive() == AckResponse(request_id=2)
                first_b = await client_b.receive()
                assert isinstance(first_b, ResourceSnapshotEvent) and first_b.subscription_id == 2

                # Both see a new RPC connection delta.
                async with connect_rpc(ws.rpc_url):
                    await _drain_until(
                        client_a,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent) and len(f.added) == 1 for f in s
                        ),
                    )
                    await _drain_until(
                        client_b,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent) and len(f.added) == 1 for f in s
                        ),
                    )


async def test_one_client_disconnect_does_not_break_other() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client_b:
                await client_b.send(SubscribeRequest(request_id=1, resource="rpc_connections"))
                assert await client_b.receive() == AckResponse(request_id=1)
                assert (await client_b.receive()).subscription_id == 1  # type: ignore[union-attr]

                # Open + close client_a inside this scope.
                async with connect_client(ws.client_url) as client_a:
                    await client_a.send(SubscribeRequest(request_id=2, resource="rpc_connections"))
                    assert await client_a.receive() == AckResponse(request_id=2)
                    assert (await client_a.receive()).subscription_id == 2  # type: ignore[union-attr]

                # client_b still works
                async with connect_rpc(ws.rpc_url):
                    await _drain_until(
                        client_b,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent) and len(f.added) == 1 for f in s
                        ),
                    )


# --- Backpressure → WS close ----------------------------------------


async def test_server_shutdown_closes_open_client_connections() -> None:
    """Hypercorn would otherwise wait for clients to disconnect on
    their own. The workstation cancels each ``/client`` connection's
    cancel scope at shutdown so the server CM exits promptly even
    when browser pages are still attached.

    Structure: server and client run as sibling tasks. The client
    establishes a subscription, signals the server task to exit its
    CM, then expects the WS to be closed by the server."""
    server_url_holder: list[str] = []
    server_url_ready = anyio.Event()
    proceed_to_shutdown = anyio.Event()

    async def _server() -> None:
        async with WorkstationServer() as ws:
            server_url_holder.append(ws.client_url)
            server_url_ready.set()
            await proceed_to_shutdown.wait()
        # Reaching here without timing out is the test assertion.

    async def _client() -> None:
        await server_url_ready.wait()
        async with connect_client(server_url_holder[0]) as client:
            await client.send(SubscribeRequest(request_id=1, resource="rpc_connections"))
            assert await client.receive() == AckResponse(request_id=1)
            # Subscription established; tell the server to shut down.
            proceed_to_shutdown.set()
            # The server should now close the WS from its end.
            with pytest.raises(httpx_ws.HTTPXWSException):
                while True:
                    await client.receive()

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_server)
            tg.start_soon(_client)


async def test_backpressure_closes_the_websocket() -> None:
    """Subscribe but never read; flood the per-connection buffer with
    ``resource_changes`` frames; verify the server cancels the
    connection (the client observes a WS disconnect).

    Flooding is driven by repeatedly opening a fake RPC connection
    record and immediately removing it within a single
    ``mutation_context`` flush — each iteration emits one
    ``resource_changes`` frame to the rpc_connections subscriber.
    The buffer-collapse rules mean the touched key needs to actually
    change between iterations to fire a frame each time, hence
    distinct UUIDs per iteration.
    """
    import datetime
    from uuid import uuid4

    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import RPCConnectionRecord

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=1, resource="rpc_connections"))
                assert await client.receive() == AckResponse(request_id=1)
                assert (await client.receive()).subscription_id == 1  # type: ignore[union-attr]

                # Buffer size is 64; the client never reads. We flood
                # with 200 fake connections, each emitted as a separate
                # ``resource_changes`` frame.
                async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as conn_scope:
                    for _ in range(200):
                        new_id = uuid4()
                        fake = RPCConnectionRecord(
                            connection_id=new_id,
                            peer_addr="fake:0",
                            connected_at=datetime.datetime.now(datetime.timezone.utc),
                            scope=conn_scope,
                        )
                        # One ``resource_changes`` frame per
                        # mutation_context flush. Insert and remove are
                        # in *separate* contexts so add+remove on the
                        # same key in one buffer don't collapse to a
                        # no-op.
                        with ws.state.mutation_context():
                            ws.state.touch("rpc_connections", str(new_id))
                            ws.state.rpc_connections[new_id] = fake
                        with ws.state.mutation_context():
                            ws.state.touch("rpc_connections", str(new_id))
                            del ws.state.rpc_connections[new_id]

                # Server should now close the WS. The client's receive
                # raises an httpx-ws exception.
                with pytest.raises(httpx_ws.HTTPXWSException):
                    while True:
                        await client.receive()
