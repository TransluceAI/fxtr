"""Tests for the ``expose_service`` async-context-manager helper.

The recommended way to expose a service to the workstation. Tests
here verify both the happy-path lifecycle and the cycle-avoidance
behavior (the helper's whole reason for existing — see the
``register_service`` docstring for the underlying cycle).
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_service
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import EchoImpl, EchoProto
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def test_expose_service_round_trip() -> None:
    """Inside the ``async with``: B can look up the service by name
    and call it. After the ``async with`` exits, the registration is
    gone — fresh lookups fail."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a_ref = session_a.remote_service.replace_service_type(WorkstationServiceProto)
                ws_b = await session_b.remote_service.replace_service_type(
                    WorkstationServiceProto
                ).resolve(session_b.scope)

                echo_ref = await session_a.scope.mount(EchoImpl())

                async with expose_service(
                    ws_a_ref, "the-echo", echo_ref, session_a.scope
                ) as short_id:
                    assert short_id.startswith("s")

                    via_id = await ws_b.get_service(short_id)
                    echo_b = await via_id.replace_service_type(EchoProto).resolve(session_b.scope)
                    assert await echo_b.echo("hi") == "echo:hi"

                    via_name = await ws_b.get_service_by_external_name("the-echo")
                    echo_b2 = await via_name.replace_service_type(EchoProto).resolve(
                        session_b.scope
                    )
                    assert await echo_b2.echo("hi2") == "echo:hi2"

                # After exit: both lookups fail.
                import pytest

                with pytest.raises(Exception):
                    await ws_b.get_service(short_id)
                with pytest.raises(Exception):
                    await ws_b.get_service_by_external_name("the-echo")


async def test_expose_service_releases_even_when_holder_owns_source() -> None:
    """The whole point of the helper: registering a service mounted in
    the *same* scope where ``expose_service`` resolves the workstation
    ref would normally trigger the auto-release-on-drop cycle. The
    helper sidesteps the cycle by explicitly releasing on exit. This
    test fails (deadlock) if the helper ever stops doing that."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                ws_a_ref = session_a.remote_service.replace_service_type(WorkstationServiceProto)
                # Mount echo_ref on session_a.scope and resolve the
                # workstation ref there too — exactly the layout that
                # deadlocks under the auto-release path.
                echo_ref = await session_a.scope.mount(EchoImpl())
                async with expose_service(ws_a_ref, "boom", echo_ref, session_a.scope) as short_id:
                    assert short_id.startswith("s")
                # Exiting here without deadlock is the assertion.


async def test_expose_service_default_scope_is_active() -> None:
    """When ``scope`` is omitted, the helper captures the active scope
    at entry."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                ws_a_ref = session_a.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await session_a.scope.mount(EchoImpl())

                # session_a.scope is set as active inside ``connect_rpc``
                # (open_rpc_scope's user_scope, isolated=False by
                # default).
                async with expose_service(ws_a_ref, "implicit-scope", echo_ref) as short_id:
                    assert short_id.startswith("s")
                    # Sanity: lookup_active_service finds it.
                    assert ws.state.short_ids.lookup_active_service(short_id) is not None
                # And it's deactivated cleanly on exit.
                assert ws.state.short_ids.lookup_active_service(short_id) is None
