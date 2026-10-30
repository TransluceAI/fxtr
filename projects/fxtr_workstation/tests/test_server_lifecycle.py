"""Walking-skeleton lifecycle tests for :class:`WorkstationServer`.

These verify only that hypercorn starts, binds an ephemeral port, accepts a
TCP connection, and shuts down cleanly. The websocket protocol on ``/rpc``
is currently a no-op (accept + close); subsequent stages will exercise it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import anyio
import pytest
from fxtr_workstation import WorkstationServer
from fxtr_workstation.protocols import WorkstationServiceProto

from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def test_server_starts_and_stops() -> None:
    async with WorkstationServer() as ws:
        assert ws.bound_port > 0
        assert ws.bound_url.startswith("ws://127.0.0.1:")
        assert ws.rpc_url.endswith("/rpc")


async def test_server_accepts_tcp_connection() -> None:
    async with WorkstationServer() as ws:
        # The server is bound; a plain TCP connect should succeed.
        url = urlsplit(ws.bound_url.replace("ws://", "http://"))
        assert url.hostname is not None and url.port is not None
        stream = await anyio.connect_tcp(url.hostname, url.port)
        await stream.aclose()


async def test_bound_port_unavailable_outside_context() -> None:
    ws = WorkstationServer()
    with pytest.raises(RuntimeError):
        _ = ws.bound_port


async def test_rpc_bootstrap_round_trip() -> None:
    """Smoke: client opens an open_rpc_scope against /rpc, exchanges
    bootstrap packets, and disconnects cleanly."""
    async with WorkstationServer() as ws:
        async with connect_rpc(ws.rpc_url) as session:
            # The workstation's main is currently a NoOpService; we can't
            # call methods on it, but we can confirm the bootstrap landed.
            assert session.remote_service is not None
            assert session.scope is not None


async def test_state_tracks_rpc_connection() -> None:
    async with WorkstationServer() as ws:
        assert len(ws.state.rpc_connections) == 0
        async with connect_rpc(ws.rpc_url):
            # Bootstrap is complete by the time connect_rpc yields, so
            # the server has already entered add_rpc_connection.
            assert len(ws.state.rpc_connections) == 1
            (record,) = list(ws.state.rpc_connections.values())
            assert record.peer_addr is not None
            assert record.peer_addr.startswith("127.0.0.1:")
            assert record.name is None
            connection_id = record.connection_id
        # The server-side rpc_endpoint task may still be unwinding;
        # poll briefly for the record to be removed.
        with anyio.fail_after(2.0):
            while connection_id in ws.state.rpc_connections:
                await anyio.sleep(0.01)


async def test_state_tracks_two_concurrent_connections() -> None:
    async with WorkstationServer() as ws:
        async with connect_rpc(ws.rpc_url), connect_rpc(ws.rpc_url):
            assert len(ws.state.rpc_connections) == 2
            ids = {r.connection_id for r in ws.state.rpc_connections.values()}
            assert len(ids) == 2  # distinct UUIDs


async def test_state_unavailable_outside_context() -> None:
    ws = WorkstationServer()
    with pytest.raises(RuntimeError):
        _ = ws.state


async def test_name_connection_updates_record() -> None:
    """Smallest round-trip method: client calls ``name_connection``;
    the server-side record reflects the new name."""

    async with WorkstationServer() as ws:
        async with connect_rpc(ws.rpc_url) as session:
            ref = session.remote_service.replace_service_type(WorkstationServiceProto)
            service = await ref.resolve()
            await service.name_connection("alice")
            (record,) = list(ws.state.rpc_connections.values())
            assert record.name == "alice"

            # Last-writer-wins: a second call overwrites.
            await service.name_connection("bob")
            assert record.name == "bob"


# ---------- register_service / get_service round-trip ----------

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workstation.protocols import ReleaseCallbackProto

from ._fixtures import EchoImpl, EchoProto


async def test_register_and_get_service_across_connections_explicit_release() -> None:
    """A registers an EchoImpl; B retrieves and calls it; A explicitly
    releases before disconnect.

    The auto-release-on-drop path (chain-bridged release callback's
    dispatch scope drains when the holder drops it) deadlocks if the
    holder is the same scope that owns the registered ref: the
    ``service_scope``'s bridge pins the dispatch scope, and the
    end-callback that would set ``shutdown_event`` only fires once the
    dispatch scope drains. Explicit ``release()`` short-circuits that
    cycle by setting ``shutdown_event`` directly. See the inner-scope
    variant below for the structurally clean version."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation_a = await session_a.remote_service.replace_service_type(
                    WorkstationServiceProto
                ).resolve(session_a.scope)

                echo_ref = await session_a.scope.mount(EchoImpl())
                short_id, release_cb_ref = await workstation_a.register_service("echo", echo_ref)
                assert short_id.startswith("s")

                # B retrieves and invokes.
                async with connect_rpc(ws.rpc_url) as session_b:
                    workstation_b = await session_b.remote_service.replace_service_type(
                        WorkstationServiceProto
                    ).resolve(session_b.scope)
                    retrieved_ref = await workstation_b.get_service(short_id)
                    echo = await retrieved_ref.replace_service_type(EchoProto).resolve(
                        session_b.scope
                    )
                    assert await echo.echo("hello") == "echo:hello"

                # Explicit release before tearing down session_a so the
                # service_record's worker can drain its scope before
                # session_a's scope (which owns echo_ref) starts draining.
                rcb = await release_cb_ref.replace_service_type(ReleaseCallbackProto).resolve(
                    session_a.scope
                )
                await rcb.release()


async def test_register_and_get_service_across_connections_inner_scope() -> None:
    """Structurally clean variant: resolve the workstation ref into a
    *child* of the scope that owns the registered ref.

    When the inner scope exits, the chain-bridged ``release_cb_ref``
    drops, the workstation's dispatch scope drains, the end-callback
    fires, and the registration is released — all while the outer scope
    that owns ``echo_ref`` is still alive to receive the lease-end
    message for ``echo_ref``. No explicit release call needed."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                # Outer scope owns echo_ref; it must outlive the
                # registration.
                echo_ref = await session_a.scope.mount(EchoImpl())

                async with FxtrScope(
                    isolated=True,
                    include=[session_a.scope],
                    name="register-inner",
                ) as inner_scope:
                    workstation_a = await session_a.remote_service.replace_service_type(
                        WorkstationServiceProto
                    ).resolve(inner_scope)
                    short_id, release_cb_ref = await workstation_a.register_service(
                        "echo", echo_ref
                    )
                    assert short_id.startswith("s")
                    assert release_cb_ref is not None

                    # B retrieves and invokes.
                    async with connect_rpc(ws.rpc_url) as session_b:
                        workstation_b = await session_b.remote_service.replace_service_type(
                            WorkstationServiceProto
                        ).resolve(session_b.scope)
                        retrieved_ref = await workstation_b.get_service(short_id)
                        echo = await retrieved_ref.replace_service_type(EchoProto).resolve(
                            session_b.scope
                        )
                        assert await echo.echo("hello") == "echo:hello"
                # inner_scope drains: release_cb_ref drops, dispatch
                # scope drains, registration released, service_scope
                # drains, echo_ref's lease released back to session_a
                # — all while session_a is still alive to process the
                # lease-end message.


async def test_get_unknown_service_short_id_raises() -> None:
    async with WorkstationServer() as ws:
        async with connect_rpc(ws.rpc_url) as session:
            workstation = await session.remote_service.replace_service_type(
                WorkstationServiceProto
            ).resolve(session.scope)
            with pytest.raises(Exception):
                await workstation.get_service("s99999")
