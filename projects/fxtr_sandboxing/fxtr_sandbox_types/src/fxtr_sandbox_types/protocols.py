"""Service protocols shared between the host and the sandbox.

Sandbox-side runtimes (`fxtr_sandbox_rpc_adapter`) mount a
:class:`SandboxConnectProto` impl as the connection's main service.
Host-side spawners (`fxtr_sandboxing.spawner`) mount a
:class:`SandboxSpawnerProto` impl that vends those connect-shaped
service refs.

:class:`NetworkHandleAuthorizerProto` (stage 3) is the host-side
capability for opening connections on a single network route. The
host mounts an authorizer impl per registered transport via
``Environment.add_transport`` / ``Environment.add_raw_transport``,
and bridges the resulting ref into sandboxes. The sandbox calls
``get_transport_token()`` to mint a single-use token + a
:class:`TokenWitnessProto` ref bounding its lifetime, then redeems
the token on the runtime-specific side channel for one byte-stream
connection.
"""

from typing import Protocol

from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing
from fxtr2.services.typed_service import service_protocol

from ._namespace import SHARED_SANDBOX_NS
from .deprecated_entities import TaskBundle
from .entities import (
    NixContainerServiceDefn,
    NixPyodideServiceDefn,
    NixServiceDefn,
    NixWasmServiceDefn,
)

SANDBOX_CONNECT_TYPE_ID = system_entities.define_type(SHARED_SANDBOX_NS, "sandbox_connect")
SANDBOX_GUEST_IMPL_TYPE_ID = system_entities.define_type(SHARED_SANDBOX_NS, "sandbox_guest_impl")
SANDBOX_SPAWNER_TYPE_ID = system_entities.define_type(SHARED_SANDBOX_NS, "sandbox_spawner")
NETWORK_HANDLE_AUTHORIZER_TYPE_ID = system_entities.define_type(
    SHARED_SANDBOX_NS, "network_handle_authorizer"
)
TOKEN_WITNESS_TYPE_ID = system_entities.define_type(SHARED_SANDBOX_NS, "token_witness")


@service_protocol
class SandboxConnectProto[T, U](Protocol):
    """The main service exposed by every sandbox process.

    Generic over the connect-side config type ``T`` and the resulting user
    service type ``U``. :meth:`SandboxSpawnerProto.spawn` returns a ref whose
    parameterization is :data:`~fxtr2.services.service_dialect.OverrideThisTypeBeforeUsing`
    stubs — callers **must** ``replace_service_type`` to a concrete
    ``SandboxConnectProto[MyConfig, MyUserProto]`` (or to
    ``SandboxConnectProto[FxtrServiceData, UnknownService]`` for the same
    wire shape as the guest) before ``resolve`` / ``connect``.

    The guest-side ``_GuestMainImpl`` always mounts as the default-untyped
    form; the typed-service framework's args codec passes
    ``FxtrServiceData`` through unchanged, and the guest's impl re-decodes
    against the user entrypoint's declared annotation. So a host caller
    using a specialized parameterization and a guest using the
    untyped form interoperate via the same wire shape.

    Multiple ``connect`` calls on the same ref are valid — each
    invocation gets its own dispatch scope and a fresh entrypoint
    invocation.
    """

    async def connect(self, config: T) -> ServiceRef[U]: ...


@service_protocol
class SandboxGuestImplProto[T, U](Protocol):
    """The internal protocol the guest-side ``_GuestMainImpl`` exposes.

    End-user code never sees this protocol — host-side spawn wraps it
    behind a :class:`SandboxConnectProto` adapter that threads in the
    bundle ref on every ``connect`` call. The wire shape is the same
    as :class:`SandboxConnectProto` but with an extra positional
    ``bundle`` argument that the host adapter supplies.

    The bundle ref + the decoded user config together become the
    payload of a per-call ``SandboxProvenanceContext`` entity, which
    the guest then wraps as a c-node and binds as its dispatch
    scope's provenance context before invoking the user entrypoint.
    See :class:`~fxtr_sandbox_types.entities.SandboxProvenanceContext`
    and ``fxtr_sandbox_rpc_adapter/_provenance.py`` for the build
    pipeline.

    Generic parameters mirror :class:`SandboxConnectProto`: ``T`` is
    the user-config type the entrypoint accepts; ``U`` is the user
    service-proto type the entrypoint returns.
    """

    async def connect_with_bundle_provenance(
        self, config: T, bundle: EntityRef[UnknownEntity]
    ) -> ServiceRef[U]: ...


@service_protocol
class SandboxSpawnerProto(Protocol):
    """Host-side capability for launching new sandboxes.

    ``spawn`` accepts an ``EntityRef`` over the union of spawnable
    reference types — the live git-ref defns (``NixWasmServiceDefn``,
    ``NixContainerServiceDefn``, ``NixPyodideServiceDefn``, and the
    kind-agnostic ``NixServiceDefn``, dispatched by evaluating the
    attr's ``kind``) plus the deprecated in-band WASM
    ``TaskBundle`` — and each member has a load handler so the
    typed-service decoder can build the ref directly. Callers with a
    more specific ``EntityRef[NixWasmServiceDefn]`` can pass it through
    unchanged thanks to ``EntityRef``'s covariant target type.

    Returns a ``SandboxConnectProto`` ref carrying
    :data:`~fxtr2.services.service_dialect.OverrideThisTypeBeforeUsing`
    placeholders — callers **must**
    ``replace_service_type(SandboxConnectProto[MyConfig, MyProto])`` (or the
    untyped-but-concrete ``[FxtrServiceData, UnknownService]``) before
    ``resolve`` / ``connect``.
    """

    async def spawn(
        self,
        bundle: EntityRef[
            NixWasmServiceDefn
            | NixContainerServiceDefn
            | NixPyodideServiceDefn
            | NixServiceDefn
            | TaskBundle
        ],
    ) -> ServiceRef[
        SandboxConnectProto[OverrideThisTypeBeforeUsing, OverrideThisTypeBeforeUsing]
    ]: ...


@service_protocol
class TokenWitnessProto(Protocol):
    """Witness ref bounding a transport token's lifetime.

    Returned alongside a freshly-minted token by
    :meth:`NetworkHandleAuthorizerProto.get_transport_token`. While the
    witness ref is alive on the caller's side, the token can be
    redeemed on the runtime-specific side channel. When the witness
    bridge drops (caller scope drains, or :meth:`revoke_token` is
    called explicitly), an unredeemed token is removed from the host's
    table — subsequent redemption attempts raise. Once a token has
    already been redeemed (the side channel handed out a stream),
    revocation is a no-op since the table entry is gone.

    This is purely a lifetime-bounding affordance; the witness has no
    other authority. Callers don't need to hold it indefinitely —
    typical use is to mint, redeem, and let the witness drop with the
    connect-call's scope.
    """

    async def revoke_token(self) -> None: ...


@service_protocol
class NetworkHandleAuthorizerProto(Protocol):
    """Capability to open connections on one network route.

    Each authorizer ref is bound at construction time to a specific
    transport handler (e.g. an HTTP route + upstream session, or a
    user-supplied byte-stream handler). The host mounts authorizer
    impls via ``Environment.add_transport`` /
    ``Environment.add_raw_transport`` and bridges the resulting ref
    into sandboxes; this lets sandboxes open connections without ever
    seeing the route's secrets (auth headers, upstream URL, etc.).

    ``get_transport_token`` mints a single-use token plus a
    :class:`TokenWitnessProto` ref bounding its lifetime. The sandbox
    redeems the token on the runtime-specific side channel for one
    byte-stream connection. Stolen tokens grant at most one
    connection's worth of access before they're consumed.
    """

    async def get_transport_token(
        self,
    ) -> tuple[str, ServiceRef[TokenWitnessProto]]: ...
