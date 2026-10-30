"""Sandbox-side ``SandboxGuestImplProto`` impl + entrypoint contract.

The guest entrypoint is a thin async callable:

    async def run(config) -> ServiceRef[Any]:
        return await get_active_scope().mount(MyImpl(config))

It runs inside ``connect_with_bundle_provenance``'s dispatch scope
(call_scope) — that's already the right place to mount: any args that
were bridged into call_scope (FxtrIDs, EntityRefs, ServiceRefs) are
reachable from the mount, and method calls on the returned ref
dispatch in sub-scopes that inherit through call_scope.

The wire shape is ``connect_with_bundle_provenance(config, bundle)``:
the host-side ``_SpawnConnectWrapperImpl`` (mounted as
``SandboxConnectProto`` for end users) supplies ``bundle`` from the
spawner so user code can't forge it. Before invoking the user
entrypoint, this impl:

1. Walks ``config`` to produce a ``FrozenFxtrCbor`` snapshot with
   every ``ServiceRef`` leaf replaced by a stored
   ``ServiceConfigReplacementEntity`` (capturing
   ``$fxtr.ctx`` + ``$fxtr.type`` of each). Top-level EntityRefs are
   simultaneously checked against a SCRE escape-safety filter.
2. Builds a ``SandboxProvenanceContext`` entity (``bundle`` +
   ``config_snapshot``) and converts it to cbor.
3. Mints a c-node from that cbor via ``scope.create_c_node`` and
   overrides ``call_scope``'s context marker via the private
   ``_apply_context_marker_override`` (TODO: ideally a fresh
   sub-scope, but the chain-bridge story for "mount-in-deeper-scope"
   is unverified in v2 — see stage 2p3 plan).
4. Decodes ``config`` against the entrypoint's first-param
   annotation; invokes the user entrypoint inside the now-provenanced
   ``call_scope``. Resulting ``ServiceRef`` is chain-bridged back to
   the caller, carrying the provenance marker.

Lifetimes ride entirely on the ServiceRef chain — the entrypoint
doesn't get a separate user_scope or a ``serve`` callback. If the
entrypoint needs post-teardown cleanup, the impl can register
``register_end_callback`` on ``call_scope.scope_token`` (same pattern the
network authorizer uses for its witness).
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Awaitable, Callable
from typing import Any

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.service_dialect import decode_from_service_data
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types import SandboxProvenanceContext
from fxtr_sandbox_types.protocols import SANDBOX_GUEST_IMPL_TYPE_ID, SandboxGuestImplProto

from ._provenance import walk_config_for_provenance

# Type of the user-written entrypoint. Takes the decoded config (whatever
# shape the user annotates), returns a ``ServiceRef`` to a freshly-mounted
# impl. The framework chain-bridges the returned ref back to the host
# caller; when the caller releases, the dispatch scope drains.
EntrypointFn = Callable[[Any], Awaitable[ServiceRef[Any]]]


def _resolve_user_config_annotation(entrypoint: EntrypointFn) -> Any:
    """Inspect the user's entrypoint signature for the first-parameter type.

    Falls back to ``Any`` when the user didn't annotate. ``typing.get_type_hints``
    handles `from __future__ import annotations` string-form annotations.
    """
    try:
        hints = typing.get_type_hints(entrypoint)
    except Exception:  # noqa: BLE001 — bad annotations shouldn't crash dispatch
        hints = {}
    sig = inspect.signature(entrypoint)
    params = list(sig.parameters)
    if not params:
        return Any
    config_name = params[0]
    return hints.get(config_name, Any)


class _GuestMainImpl(TypedServiceImpl[SandboxGuestImplProto[FxtrServiceData, UnknownService]]):
    """Sandbox-side main service.

    Mounted on the ``guest_host`` scope by ``fxtr_sandbox_rpc_adapter.main``;
    vended over the connection by ``open_rpc_scope``. May be called
    multiple times during one connection — each call gets its own
    dispatch scope and a fresh entrypoint invocation; the returned
    impl's lifetime is the chain-bridge from that dispatch scope to
    the host caller.

    The impl is parameterized as the default-untyped form
    ``SandboxGuestImplProto[FxtrServiceData, UnknownService]`` — the
    args codec passes the wire payload through unchanged, and we
    re-decode against the user entrypoint's first-param annotation.
    Host callers using a specialized parameterization (e.g.
    ``SandboxConnectProto[MyConfigDataclass, MyServiceProto]``) on
    the user-facing wrapper interoperate because the dialect's
    encoding for ``MyConfigDataclass`` lands at the same wire shape
    that ``decode_from_service_data(value, MyConfigDataclass)``
    consumes here.
    """

    def __init__(self, entrypoint: EntrypointFn) -> None:
        self._entrypoint = entrypoint
        self._user_config_annotation = _resolve_user_config_annotation(entrypoint)

    def _target_type_annotation(self) -> Any:
        return SandboxGuestImplProto[FxtrServiceData, UnknownService]

    def _service_type_id(self) -> FxtrID:
        return SANDBOX_GUEST_IMPL_TYPE_ID

    @service_method
    async def connect_with_bundle_provenance(
        self, config: Any, bundle: EntityRef[UnknownEntity]
    ) -> ServiceRef[Any]:
        call_scope = get_active_scope()

        # 1. Single-pass async walk over config: replace ServiceRefs
        #    with stored SCRE refs; reject any user EntityRef that
        #    resolves to a SCRE before we touch the entrypoint.
        config_snapshot = await walk_config_for_provenance(config)

        # 2. Build the SandboxProvenanceContext entity, get its cbor
        #    via the convert-only helper (no separate store + load
        #    round-trip).
        prov_cbor = SandboxProvenanceContext(
            bundle=bundle,
            config_snapshot=config_snapshot,
        )._fxtr_convert_to_cbor()

        # 3. Mint the c-node. ``create_c_node`` stores the d-node from
        #    the cbor internally and returns the c-tagged id.
        c_node = call_scope.create_c_node(prov_cbor)

        # 4. Override call_scope's context marker. The c-node is already
        #    tracked in call_scope (create_c_node materialized + tracked
        #    it), so we pass call_scope as both target and explicit
        #    source — a self-copy that the framework's track-from-arena
        #    path handles cleanly (no parent-walk involved).
        # TODO: consider exposing this as a public scope method
        #   (e.g. `scope.override_provenance_context(c_node)`) once the
        #   semantics are nailed down. Alternatively, make it possible to
        #   return a service that is mounted in a sub-scope of the call scope.
        call_scope._apply_context_marker_override(
            provenance_context=c_node,
            provenance_context_from_scope=call_scope,
        )

        # 5. Decode + invoke entrypoint inside call_scope (now provenanced).
        decoded = decode_from_service_data(config, self._user_config_annotation)
        return await self._entrypoint(decoded)
