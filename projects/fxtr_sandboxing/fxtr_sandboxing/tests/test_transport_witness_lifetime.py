"""Witness lifetime tests for ``NetworkHandleAuthorizerProto``.

Exercises the full mint → bridge → release flow over an in-process
memory-stream RPC pair. The host side mounts a
``NetworkHandleAuthorizerImpl`` as the connection's main service; the
client side resolves it, calls ``get_transport_token()``, and observes
how the token's table entry behaves under various scope-lifecycle
conditions:

- Mint, drop the caller's scope → unredeemed token is revoked
  (lease-driven via ``_token.register_end_callback`` on the dispatch
  scope, not via GC).
- Mint, redeem (consume from the table), drop the caller's scope →
  no spurious revoke (consumed token is already gone, revoke is
  idempotent).
- Mint, call ``revoke_token()`` explicitly → table entry gone.

Patterned after ``v2/examples/service_spawner/service_spawner_example.py``.
"""

from __future__ import annotations

import math
from typing import Any

import anyio
import anyio.abc
import pytest
from anyio.streams.stapled import StapledObjectStream
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto
from fxtr_sandboxing.network_authorizer import NetworkHandleAuthorizerImpl
from fxtr_sandboxing.transport_server import TransportConnectionServer


def _captured_handler() -> tuple[
    Any,
    list[anyio.abc.ByteStream],
]:
    """Return ``(handler, sink)``; ``handler`` records each received stream.

    Used to register a handler in the transport server so ``register_token``
    has something to bind. The handler isn't actually invoked in these
    tests — we only test the registry-side behavior.
    """
    sink: list[anyio.abc.ByteStream] = []

    def handler(stream: anyio.abc.ByteStream) -> None:
        sink.append(stream)

    return handler, sink


def _make_transport_pair() -> tuple[
    anyio.abc.ObjectStream[RPCMessage],
    anyio.abc.ObjectStream[RPCMessage],
]:
    """Stapled bidirectional in-memory RPCMessage transport pair."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    host_end = StapledObjectStream(a_to_b_send, b_to_a_recv)
    client_end = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return host_end, client_end


async def _serve_authorizer(
    transport: anyio.abc.ObjectStream[RPCMessage],
    handler: Any,
    server: TransportConnectionServer,
) -> None:
    """Run an authorizer impl as the main service on ``transport``.

    Returns when the peer closes the connection.
    """
    async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="auth_host") as host:
        with host.as_active_scope():
            impl = NetworkHandleAuthorizerImpl(handler, server)
            main_ref = await host.mount(impl)
            async with open_rpc_scope(transport, main_ref, name="auth_rpc"):
                pass


@pytest.mark.anyio
async def test_witness_drop_revokes_unredeemed_token() -> None:
    """Mint a token, drop the caller scope holding the witness; the
    token should be revoked from the env table."""
    server = TransportConnectionServer()
    handler, _sink = _captured_handler()
    host_end, client_end = _make_transport_pair()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_serve_authorizer, host_end, handler, server)

        async with FxtrScope(
            isolated=True, include=[GLOBAL_SCOPE], name="client_root"
        ) as client_root:
            with client_root.as_active_scope():
                async with open_rpc_scope(
                    client_end,
                    remote_service_type=NetworkHandleAuthorizerProto,
                    name="client_rpc",
                ) as session:

                    # Mint the token in a child scope so we can drop the
                    # witness independently.
                    async with FxtrScope(name="mint_scope") as mint_scope:
                        authorizer = await session.remote_service.resolve(mint_scope)
                        token, _witness_ref = await authorizer.get_transport_token()
                        # While the witness is alive on this side, the
                        # token entry exists on the host.
                        assert token in server._table  # type: ignore[attr-defined]

                    # mint_scope has drained → witness's chain-bridge to
                    # the host has dropped → DiscardMessage flows over
                    # the in-process RPC connection → host's dispatch
                    # scope drains → its lifetime token's end callback
                    # fires → token is revoked from the env table.
                    #
                    # The full chain takes a few hundred ms in practice
                    # (driven by the in-process RPC drain timing). Yield
                    # the loop in a tight poll loop with a generous
                    # timeout. No GC needed — the cleanup is
                    # lease-driven.
                    with anyio.fail_after(2.0):
                        while token in server._table:  # type: ignore[attr-defined]
                            await anyio.sleep(0.01)
                    assert token not in server._table  # type: ignore[attr-defined]

        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_witness_drop_after_redemption_is_noop() -> None:
    """Mint a token, simulate a side-channel redemption (consume from
    the table), then drop the witness. The drop should not affect
    anything (the token is already gone)."""
    server = TransportConnectionServer()
    handler, _sink = _captured_handler()
    host_end, client_end = _make_transport_pair()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_serve_authorizer, host_end, handler, server)

        async with FxtrScope(
            isolated=True, include=[GLOBAL_SCOPE], name="client_root"
        ) as client_root:
            with client_root.as_active_scope():
                async with open_rpc_scope(
                    client_end,
                    remote_service_type=NetworkHandleAuthorizerProto,
                    name="client_rpc",
                ) as session:
                    authorizer = await session.remote_service.resolve(session.scope)

                    async with FxtrScope(name="mint_scope") as mint_scope:
                        with mint_scope.as_active_scope():
                            token, witness_ref = await authorizer.get_transport_token()
                            # Simulate the side-channel consuming the token.
                            consumed = server.consume_token(token)
                            assert consumed is handler
                            assert token not in server._table  # type: ignore[attr-defined]
                            del witness_ref
                    # mint_scope drained; the end callback eventually
                    # fires but ``revoke_token`` is idempotent — no
                    # spurious behavior. Nothing else should appear in
                    # the table.
                    for _ in range(5):
                        await anyio.sleep(0.01)
                    assert len(server._table) == 0  # type: ignore[attr-defined]

        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_explicit_revoke_token() -> None:
    """Calling ``revoke_token()`` on the witness ref removes the token."""
    server = TransportConnectionServer()
    handler, _sink = _captured_handler()
    host_end, client_end = _make_transport_pair()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_serve_authorizer, host_end, handler, server)

        async with FxtrScope(
            isolated=True, include=[GLOBAL_SCOPE], name="client_root"
        ) as client_root:
            with client_root.as_active_scope():
                async with open_rpc_scope(
                    client_end,
                    remote_service_type=NetworkHandleAuthorizerProto,
                    name="client_rpc",
                ) as session:
                    authorizer = await session.remote_service.resolve(session.scope)
                    token, witness_ref = await authorizer.get_transport_token()
                    assert token in server._table  # type: ignore[attr-defined]

                    witness = await witness_ref.resolve(session.scope)
                    await witness.revoke_token()
                    assert token not in server._table  # type: ignore[attr-defined]

        tg.cancel_scope.cancel()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
