"""Tests for :mod:`fxtr_workstation.peer_rpc`.

The helper is the public flavor of the test-local ``_ws_client``
plumbing, so the interesting assertions are the typed surface (no
``replace_service_type`` needed on the workstation main) and the
end-to-end path through a real websocket.
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_service
from fxtr_workstation.peer_rpc import open_workstation_rpc
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import EchoImpl, EchoProto
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def test_open_workstation_rpc_round_trip() -> None:
    """A peer session resolves the (already-typed) workstation main,
    looks up a service registered by another connection by external
    name, and calls it."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as provider,
                open_workstation_rpc(ws.rpc_url, name="peer-rpc-test") as peer,
            ):
                provider_ws_ref = provider.remote_service.replace_service_type(
                    WorkstationServiceProto
                )
                echo_ref = await provider.scope.mount(EchoImpl())
                async with expose_service(provider_ws_ref, "peer-echo", echo_ref, provider.scope):
                    peer_ws = await peer.remote_service.resolve(peer.scope)
                    await peer_ws.name_connection("peer-rpc-test")
                    via_name = await peer_ws.get_service_by_external_name("peer-echo")
                    echo = await via_name.replace_service_type(EchoProto).resolve(peer.scope)
                    assert await echo.echo("hi") == "echo:hi"
