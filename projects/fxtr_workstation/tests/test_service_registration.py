"""Service registration flows: re-registration, cross-connection
sharing, release-while-consumer-holds, and connection-drop cleanup.

These exercise the lifecycle invariants documented in
``fxtr_workstation/notes/service_lifecycle.md``.
"""

from __future__ import annotations

import anyio
import pytest
from fxtr_workstation import WorkstationServer
from fxtr_workstation.protocols import ReleaseCallbackProto, WorkstationServiceProto

from ._fixtures import EchoImpl, EchoProto
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _resolve_workstation(session, scope):  # type: ignore[no-untyped-def]
    """Helper: resolve the workstation main service into ``scope``."""
    return await session.remote_service.replace_service_type(WorkstationServiceProto).resolve(scope)


async def _resolve_release_cb(release_cb_ref, scope):  # type: ignore[no-untyped-def]
    """Helper: resolve a chain-bridged release-cb ref into ``scope``."""
    return await release_cb_ref.replace_service_type(ReleaseCallbackProto).resolve(scope)


async def test_reregister_same_handle_returns_same_short_id() -> None:
    """A registers the same EchoImpl twice in sequence (release in
    between, different external names). Both calls return the same
    workstation short ID — the mapping persists across deactivation."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)
                echo_ref = await session_a.scope.mount(EchoImpl())

                short_id_1, rcb_1 = await workstation.register_service("name-1", echo_ref)
                rcb1_obj = await _resolve_release_cb(rcb_1, session_a.scope)
                await rcb1_obj.release()

                short_id_2, rcb_2 = await workstation.register_service("name-2", echo_ref)
                rcb2_obj = await _resolve_release_cb(rcb_2, session_a.scope)
                await rcb2_obj.release()

                assert short_id_1 == short_id_2


async def test_bounceback_returns_same_short_id_in_both_connections() -> None:
    """A registers; B retrieves via short_id; B re-registers the
    retrieved ref. Both registrations resolve to the same workstation
    short ID — the ServiceKey survives the chain-bridge round trip."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                echo_ref = await session_a.scope.mount(EchoImpl())
                short_id_a, rcb_a = await ws_a.register_service("from-a", echo_ref)

                retrieved_ref = await ws_b.get_service(short_id_a)
                short_id_b, rcb_b = await ws_b.register_service("from-b", retrieved_ref)
                assert short_id_a == short_id_b

                # Clean up both registrations explicitly so neither
                # session-drop triggers the cycle.
                rcb_a_obj = await _resolve_release_cb(rcb_a, session_a.scope)
                rcb_b_obj = await _resolve_release_cb(rcb_b, session_b.scope)
                await rcb_a_obj.release()
                await rcb_b_obj.release()


async def test_release_keeps_consumer_ref_alive_but_disables_lookup() -> None:
    """A registers; B retrieves and resolves an EchoProto; A releases.
    B's already-resolved ref keeps working; further ``get_service``
    calls for the same short_id fail."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                echo_ref = await session_a.scope.mount(EchoImpl())
                short_id, rcb_a = await ws_a.register_service("the-echo", echo_ref)
                retrieved = await ws_b.get_service(short_id)
                echo_b = await retrieved.replace_service_type(EchoProto).resolve(session_b.scope)
                assert await echo_b.echo("before") == "echo:before"

                # A releases its registration. Since B has a live
                # consumer ref, the underlying service_scope can't
                # drain yet, but the workstation's tracking is gone.
                rcb_a_obj = await _resolve_release_cb(rcb_a, session_a.scope)
                await rcb_a_obj.release()

                # B's already-resolved ref still works.
                assert await echo_b.echo("after") == "echo:after"

                # But fresh lookups fail.
                with pytest.raises(Exception):
                    await ws_b.get_service(short_id)


async def test_consumer_takes_over_via_reregistration() -> None:
    """A registers; B retrieves; A releases; B re-registers the
    retrieved ref under a new external name. Same short ID persists,
    and a third party C can resolve it via B's external name. (A's
    handle stays alive — kept reachable through B's bridge — until A
    explicitly closes the connection.)"""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                echo_ref = await session_a.scope.mount(EchoImpl())
                short_id_a, rcb_a = await ws_a.register_service("a-name", echo_ref)

                retrieved_b_ref = await ws_b.get_service(short_id_a)

                # A releases.
                rcb_a_obj = await _resolve_release_cb(rcb_a, session_a.scope)
                await rcb_a_obj.release()

                # B re-registers the same handle under a new name.
                short_id_b, rcb_b = await ws_b.register_service("b-name", retrieved_b_ref)
                assert short_id_b == short_id_a

                # C connects and looks up by B's external name.
                async with connect_rpc(ws.rpc_url) as session_c:
                    ws_c = await _resolve_workstation(session_c, session_c.scope)
                    via_name = await ws_c.get_service_by_external_name("b-name")
                    echo_c = await via_name.replace_service_type(EchoProto).resolve(session_c.scope)
                    assert await echo_c.echo("hi-c") == "echo:hi-c"

                    # And via short ID — should still resolve since
                    # B has it active again.
                    via_id = await ws_c.get_service(short_id_a)
                    echo_c2 = await via_id.replace_service_type(EchoProto).resolve(session_c.scope)
                    assert await echo_c2.echo("hi-c2") == "echo:hi-c2"

                # Clean up B's registration explicitly so its
                # session-drop doesn't trigger the cycle.
                rcb_b_obj = await _resolve_release_cb(rcb_b, session_b.scope)
                await rcb_b_obj.release()


async def test_duplicate_external_name_is_rejected() -> None:
    """Two concurrent registrations of the same external name (across
    different connections, with different underlying services) — the
    second one is rejected. Releasing the first frees the name."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                echo_a = await session_a.scope.mount(EchoImpl())
                echo_b = await session_b.scope.mount(EchoImpl())

                _short_id_a, rcb_a = await ws_a.register_service("shared-name", echo_a)

                # B trying to claim the same name fails.
                with pytest.raises(Exception):
                    await ws_b.register_service("shared-name", echo_b)

                # A releases — the name is now free.
                rcb_a_obj = await _resolve_release_cb(rcb_a, session_a.scope)
                await rcb_a_obj.release()

                # B can now claim it.
                _short_id_b, rcb_b = await ws_b.register_service("shared-name", echo_b)
                rcb_b_obj = await _resolve_release_cb(rcb_b, session_b.scope)
                await rcb_b_obj.release()


async def test_duplicate_name_rejected_even_for_same_handle() -> None:
    """Re-registering the *same* handle under a name that's already
    taken (by a still-active registration of the same handle) is also
    rejected — the rule is purely about name uniqueness."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)
                echo_ref = await session_a.scope.mount(EchoImpl())

                short_id_1, rcb_1 = await workstation.register_service("the-echo", echo_ref)

                with pytest.raises(Exception):
                    await workstation.register_service("the-echo", echo_ref)

                # A second registration under a *different* name is fine.
                short_id_2, rcb_2 = await workstation.register_service("the-echo-too", echo_ref)
                # Same handle ⇒ same short ID.
                assert short_id_2 == short_id_1

                rcb1_obj = await _resolve_release_cb(rcb_1, session_a.scope)
                rcb2_obj = await _resolve_release_cb(rcb_2, session_a.scope)
                await rcb1_obj.release()
                await rcb2_obj.release()


async def test_connection_close_clears_short_id_mapping() -> None:
    """A registers, releases (so the close path doesn't hit the
    auto-release cycle — see ``service_lifecycle.md``), then drops
    its RPC connection. After the registration release the short ID
    is *deactivated but remembered*; once the connection actually
    closes (handle token ends), the mapping is forgotten entirely."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            short_id: str | None = None
            service_key = None

            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)
                echo_ref = await session_a.scope.mount(EchoImpl())
                short_id, rcb_ref = await workstation.register_service("the-echo", echo_ref)
                assert isinstance(short_id, str)
                (service_key,) = list(ws.state.service_records.keys())

                # Release the registration explicitly so session_a's
                # close doesn't trigger the auto-release-on-drop cycle.
                rcb = await _resolve_release_cb(rcb_ref, session_a.scope)
                await rcb.release()

                # Mid-connection state: the registration is gone (so
                # the short ID is deactivated) but the handle is still
                # alive (the mount on session_a.scope hasn't dropped),
                # so the mapping is still remembered.
                assert ws.state.short_ids.lookup_active_service(short_id) is None
                assert service_key in ws.state.short_ids._service_short_ids

            # Connection has dropped — A's mount of EchoImpl is gone,
            # the workstation's import handle's lifetime token has
            # ended, and ``ShortIDAllocator.forget_service`` has fired.
            assert isinstance(short_id, str) and service_key is not None
            with anyio.fail_after(2.0):
                while service_key in ws.state.short_ids._service_short_ids:
                    await anyio.sleep(0.01)
            assert ws.state.short_ids.lookup_active_service(short_id) is None
