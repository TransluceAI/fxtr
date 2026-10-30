"""Smoke tests for the dynamic-dispatch protocol + entity types.

Mounts a hand-rolled ``WorkstationDispatcherProto`` impl as an
external service, confirms an RPC peer can resolve the cap by external
name, invoke :meth:`resolve`, and receive a typed
:class:`RendererResolution` back carrying an :class:`EntityRef` for a
:class:`RendererEntity`.

This is the wire-shape smoke test — it doesn't exercise the actual
basic_dynamic_dispatch machinery (TSX compile, dir-walking,
type-id-string lookup); those land with chunk 2.
"""

from __future__ import annotations

import anyio
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import ExternalServiceConfig, WorkstationServer
from fxtr_workstation.dynamic_dispatch import (
    WORKSTATION_DISPATCHER_TYPE_ID,
    RendererEntity,
    RendererResolution,
    WorkstationDispatcherProto,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 30.0


class _StubDispatchImpl(TypedServiceImpl[WorkstationDispatcherProto]):
    """Hand-rolled dispatcher impl. Returns a pre-configured
    ``RendererResolution`` regardless of the input type ref — the
    smoke test only cares that the wire round-trip works.
    """

    def __init__(self, resolution: RendererResolution) -> None:
        super().__init__()
        self._resolution = resolution

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return WorkstationDispatcherProto

    def _service_type_id(self) -> FxtrID:
        return WORKSTATION_DISPATCHER_TYPE_ID

    @service_method
    async def resolve(
        self,
        type_ref: EntityRef[UnknownEntity],
        dispatch_key_uuid: str,
    ) -> RendererResolution:
        del type_ref, dispatch_key_uuid  # stub: returns the fixed resolution.
        return self._resolution


async def _resolve_dispatcher_invoke(
    session,  # type: ignore[no-untyped-def]
    scope: FxtrScope,
    external_name: str,
    type_ref: EntityRef[UnknownEntity],
) -> RendererResolution:
    """Resolve the workstation main service, look up the external
    dispatcher cap by external name, and invoke
    ``resolve(type_ref, dispatch_key_uuid)``. The dispatch-key arg
    is part of the protocol but ignored by
    :class:`BasicDynamicDispatchImpl`; tests pass the empty string."""
    ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
    workstation = await ws_ref.resolve(scope)
    dispatcher_service_ref = await workstation.get_service_by_external_name(external_name)
    dispatcher = await dispatcher_service_ref.replace_service_type(
        WorkstationDispatcherProto
    ).resolve(scope)
    return await dispatcher.resolve(type_ref, "")


async def test_resolve_returns_typed_renderer_resolution() -> None:
    """Wire-shape round trip: external dispatcher returns a
    :class:`RendererResolution` carrying an :class:`EntityRef` for a
    :class:`RendererEntity`; the RPC peer receives both fields with
    the right types and can read ``subtype_key`` directly."""
    expected_bytes = b"// fake compiled module\nexport default { default: {} };\n"
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        # `include=[GLOBAL_SCOPE]` because the RendererEntity's
        # `$fxtr.type` references the module-loaded type descriptor,
        # which lives in GLOBAL_SCOPE. An isolated scope without that
        # include can't lift the type id during the store walk.
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="dispatcher_owner",
        ) as user_scope:
            # Renderer entity stored in user_scope, anchoring it for as
            # long as the impl lives. Explicit `scope=user_scope` per
            # scope-discipline (isolated scopes don't auto-activate).
            renderer_ref = RendererEntity(module=expected_bytes).store(user_scope)
            resolution = RendererResolution(subtype_key="default", renderer=renderer_ref)
            impl = _StubDispatchImpl(resolution)
            ref = (await user_scope.mount(impl)).replace_service_type(WorkstationDispatcherProto)
            cfg = ExternalServiceConfig(
                external_name="renderer-dispatcher",
                service=ref,
                scope=user_scope,
            )
            async with WorkstationServer(external_services=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    # session.scope is the client's home scope —
                    # `connect_rpc` builds it non-isolated, so the
                    # module-loaded type descriptor in GLOBAL_SCOPE is
                    # already reachable.
                    # Any entity ref works as the input — the stub
                    # ignores it. Store explicitly into session.scope
                    # per scope discipline.
                    dummy_ref = RendererEntity(module=b"dummy").store(session.scope)
                    result = await _resolve_dispatcher_invoke(
                        session,
                        session.scope,
                        "renderer-dispatcher",
                        dummy_ref.replace_target_type(UnknownEntity),
                    )
                    assert isinstance(result, RendererResolution)
                    assert result.subtype_key == "default"
                    assert isinstance(result.renderer, EntityRef)
                    # result.renderer was returned by the dispatcher
                    # invoke; the workstation's return-arena pipeline
                    # lifts it into session.scope before the result
                    # decodes, so loading from session.scope is the
                    # direct supported path.
                    loaded = await result.renderer.load(session.scope)
                    assert loaded.module == expected_bytes


async def test_renderer_entity_round_trip() -> None:
    """Sanity check that :class:`RendererEntity` round-trips through
    store + load locally — exercises the DataclassEntity codec without
    any wire involvement."""
    expected = b"\x00\x01\x02\x03binary-ish module bytes"
    # include=[GLOBAL_SCOPE] so the module-loaded RendererEntity type
    # descriptor is reachable for the store walk.
    async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="entity_test") as scope:
        # Explicit `scope` per scope-discipline: isolated scopes do
        # not auto-activate.
        ref = RendererEntity(module=expected).store(scope)
        loaded = await ref.load(scope)
        assert isinstance(loaded, RendererEntity)
        assert loaded.module == expected
