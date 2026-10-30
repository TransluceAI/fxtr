"""Host-side impls for ``NetworkHandleAuthorizerProto`` + ``TokenWitnessProto``.

Each ``Environment.add_transport`` / ``Environment.add_raw_transport`` call
mounts a :class:`NetworkHandleAuthorizerImpl` bound to a specific
:data:`TransportHandler` plus a reference to the env-level
:class:`TransportConnectionServer`. The mount lives on the env's host
scope; bridging the resulting ``ServiceRef`` into a sandbox is what
gives the sandbox capability to open one connection per token.

When the sandbox calls ``get_transport_token()`` the impl mints a
single-use token, registers it with the transport server, and returns
``(token, witness_ref)``:

- ``token`` — an unguessable hex string the sandbox redeems on the
  runtime-specific side channel.
- ``witness_ref`` — a :class:`_TokenWitnessImpl` mounted on the
  *dispatch* (call) scope. The framework chain-bridges the ref back
  to the caller. When the caller's bridge drops, the dispatch scope
  drains, and an end-callback on the scope's lifetime token revokes
  the token from the env's table. Lease-driven and immune to GC
  cycles; fires the moment the scope ends.

Once a token has been redeemed via the side channel (= consumed from
the table) the late end-callback revoke is a no-op. The witness
pattern is the safety net for "minted but never used" tokens.

Caller-side note (subtle but load-bearing): the lifetime of resources
returned by a service call is bounded by the **resolution scope** of
the service ref the caller called against — *not* the active scope at
the call site. As a corollary: the authorizer should be passed around
as a ``ServiceRef[NetworkHandleAuthorizerProto]``, never as an
already-resolved proxy. A caller that wants prompt witness-driven
cleanup must resolve the ref *inside* the same (short-lived) scope
they want the witness's lifetime bounded by:

    async with FxtrScope() as connect_scope:
        authorizer = await authorizer_ref.resolve(connect_scope)
        token, witness = await authorizer.get_transport_token()
        stream = await connect_transport(token)
    # connect_scope drains here → unredeemed token revoked promptly.

Resolving the authorizer once into a long-lived scope and reusing the
proxy across calls would tie all minted witnesses to that long-lived
scope — defeating the lifetime-bounding the witness pattern is meant
to provide. ``get_client``'s backend resolves the ref in a child
scope per ``connect_tcp`` for exactly this reason.
"""

from __future__ import annotations

import secrets
from typing import Any

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.protocols import (
    NETWORK_HANDLE_AUTHORIZER_TYPE_ID,
    TOKEN_WITNESS_TYPE_ID,
    NetworkHandleAuthorizerProto,
    TokenWitnessProto,
)

from .transport_server import TransportConnectionServer, TransportHandler


class _TokenWitnessImpl(TypedServiceImpl[TokenWitnessProto]):
    """Witness ref bounding a single transport token's lifetime.

    Mounted on the dispatch (call) scope of the
    ``get_transport_token()`` invocation; chain-bridged back to the
    caller via the standard typed-service flow. The lifetime-bounded
    revoke is wired via an end-callback on the call scope's lifetime
    token (set up by :class:`NetworkHandleAuthorizerImpl`), not via
    Python GC of this object.

    :meth:`revoke_token` is a manual fast-path that revokes
    immediately. Both paths call into ``server.revoke_token`` which
    is idempotent, so a redundant late-fire is harmless.
    """

    def __init__(self, token: str, server: TransportConnectionServer) -> None:
        self._token = token
        self._server = server

    def _target_type_annotation(self) -> Any:
        return TokenWitnessProto

    def _service_type_id(self) -> FxtrID:
        return TOKEN_WITNESS_TYPE_ID

    @service_method
    async def revoke_token(self) -> None:
        self._server.revoke_token(self._token)


class NetworkHandleAuthorizerImpl(TypedServiceImpl[NetworkHandleAuthorizerProto]):
    """Capability impl bound to one transport handler.

    Held by the env (mounted on the env's host scope) for the env's
    lifetime. Each ``get_transport_token()`` call mints a fresh
    unguessable token, registers ``(token, handler)`` with the
    transport server, and returns the token + a witness ref. The
    witness's lifetime is bounded by the dispatch scope's token via
    ``register_end_callback``.
    """

    def __init__(
        self,
        handler: TransportHandler,
        server: TransportConnectionServer,
    ) -> None:
        self._handler = handler
        self._server = server

    def _target_type_annotation(self) -> Any:
        return NetworkHandleAuthorizerProto

    def _service_type_id(self) -> FxtrID:
        return NETWORK_HANDLE_AUTHORIZER_TYPE_ID

    @service_method
    async def get_transport_token(self) -> tuple[str, ServiceRef[TokenWitnessProto]]:
        token = secrets.token_hex(32)
        self._server.register_token(token, self._handler)

        call_scope = get_active_scope()
        witness_impl = _TokenWitnessImpl(token, self._server)
        witness_ref = await call_scope.mount(witness_impl)

        # Bind the unredeemed-token cleanup to the dispatch scope's
        # lifetime. When the caller drops the witness's chain-bridge,
        # the dispatch scope drains, its lifetime token ends, and
        # this callback fires. ``revoke_token`` is idempotent — if
        # the token has been consumed via the side channel or
        # explicitly revoked by then, this is a no-op.
        server = self._server  # avoid pinning ``self`` in the closure
        call_scope.scope_token.register_end_callback(lambda: server.revoke_token(token))

        return token, witness_ref
