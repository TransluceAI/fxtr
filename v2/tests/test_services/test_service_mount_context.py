"""Tests for the framework ``$fxtr.ctx`` mount-scope context-marker introspection (Stage 9 / Stage 4).

Coverage:

- Untyped path via ``getattr(unknown_proxy, "$fxtr.ctx")()``.
- Typed path via ``@proxy_alias_method("$fxtr.ctx")`` on a Protocol method.
- Reserved-prefix rejection on the wire (other ``$fxtr.*`` names).
- Bridged dispatch: marker reflects mount scope, not bridge dest.
- Empty-payload enforcement.
- The fetched marker's c-node prov parent is tracked in the home
  scope and supports sync ``unwrap_provenance`` afterward.
"""

from __future__ import annotations

from typing import Any, Protocol

import pytest

from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, get_active_scope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.service_cap_common import (
    FXTR_CTX_METHOD_NAME,
    FXTR_FRAMEWORK_METHOD_PREFIX,
    invoke_service_method,
)
from fxtr2.services.typed_service import TypedServiceImpl, proxy_alias_method, service_protocol
from fxtr2.services.untyped_service import UnknownServiceProxy, UntypedServiceImpl, service_method

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


GROUND = FxtrID.from_tag_and_payload(".", b"")
_MOUNT_CTX_TYPE_ID = FxtrID.builtin("mount-ctx-test-svc")


def _make_c_node(scope: FxtrScope, label: str) -> FxtrID:
    """Build a ``c``-node in ``scope`` that unwraps to ``"."``."""
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "label": label,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), context_data)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


class _NoOpUntyped(UntypedServiceImpl):
    """An UntypedServiceImpl with no user methods — only the framework path is invokable."""


class _EchoUntyped(UntypedServiceImpl):
    """An UntypedServiceImpl that also has a user method, to confirm coexistence."""

    @service_method
    async def echo(self, value: Any) -> Any:
        return value


# ---------------------------------------------------------------------------
# Untyped path: getattr(proxy, "$fxtr.ctx")()
# ---------------------------------------------------------------------------


class TestUntypedPath:
    """Untyped callers fetch the marker via the existing ``__getattr__`` route."""

    async def test_provenanced_mount(self) -> None:
        """A scope built with ``provenance_context=c1`` mounts an impl;
        the proxy's ``$fxtr.ctx`` returns the layered marker.
        """
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "mount-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as mount_scope:
                ref = await mount_scope.mount(_NoOpUntyped())
                proxy = await ref.resolve(mount_scope)
                assert isinstance(proxy, UnknownServiceProxy)
                marker = await getattr(proxy, FXTR_CTX_METHOD_NAME)()
                assert isinstance(marker, FxtrID)
                assert marker.get_tag() == "."
                assert marker._provenance_parent == c_node

    async def test_unprovenanced_mount(self) -> None:
        """A vanilla scope returns the bare ``"."`` marker (no prov parent)."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as mount_scope:
            ref = await mount_scope.mount(_NoOpUntyped())
            proxy = await ref.resolve(mount_scope)
            marker = await getattr(proxy, FXTR_CTX_METHOD_NAME)()
            assert isinstance(marker, FxtrID)
            assert marker.get_tag() == "."
            assert marker._provenance_parent is None

    async def test_user_method_coexists(self) -> None:
        """The framework path doesn't shadow a normal user ``@service_method``."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "coexist-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as mount_scope:
                ref = await mount_scope.mount(_EchoUntyped())
                proxy = await ref.resolve(mount_scope)
                assert await proxy.echo("hi") == "hi"
                marker = await getattr(proxy, FXTR_CTX_METHOD_NAME)()
                assert marker._provenance_parent == c_node


# ---------------------------------------------------------------------------
# Typed path: @proxy_alias_method("$fxtr.ctx") on a Protocol method
# ---------------------------------------------------------------------------


@service_protocol
class _MountCtxProto(Protocol):
    @proxy_alias_method(FXTR_CTX_METHOD_NAME)
    async def service_mount_context(self) -> FxtrID: ...

    async def echo(self, value: str) -> str: ...


class _MountCtxImpl(TypedServiceImpl[_MountCtxProto]):
    """Note: no body for ``service_mount_context`` — the framework
    handles the ``$fxtr.ctx`` wire path before user dispatch."""

    def _target_type_annotation(self) -> Any:
        return _MountCtxProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_MOUNT_CTX_TYPE_ID)

    @service_method
    async def echo(self, value: str) -> str:
        return value


class TestTypedPath:
    """Typed callers opt in via ``@proxy_alias_method(FXTR_CTX_METHOD_NAME)``."""

    async def test_typed_alias_roundtrip(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "typed-mount-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as mount_scope:
                ref = await mount_scope.mount(_MountCtxImpl())
                proxy = await ref.resolve(mount_scope)
                # User method still works.
                assert await proxy.echo("hi") == "hi"
                # Aliased method ships ``$fxtr.ctx`` over the wire.
                marker = await proxy.service_mount_context()
                assert isinstance(marker, FxtrID)
                assert marker.get_tag() == "."
                assert marker._provenance_parent == c_node

    async def test_typed_unprovenanced(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as mount_scope:
            ref = await mount_scope.mount(_MountCtxImpl())
            proxy = await ref.resolve(mount_scope)
            marker = await proxy.service_mount_context()
            assert marker._provenance_parent is None


# ---------------------------------------------------------------------------
# Reserved-prefix rejection on the wire
# ---------------------------------------------------------------------------


class TestReservedPrefix:
    async def test_unknown_framework_path_raises(self) -> None:
        """A ``$fxtr.<other>`` call lands in the framework handler and bounces with UnknownMethod."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as mount_scope:
            ref = await mount_scope.mount(_NoOpUntyped())
            proxy = await ref.resolve(mount_scope)
            with pytest.raises(IRException) as excinfo:
                await getattr(proxy, FXTR_FRAMEWORK_METHOD_PREFIX + "evil")()
            assert excinfo.value.category == "UnknownMethod"

    async def test_typed_unknown_framework_path_raises(self) -> None:
        """Same on the typed dispatch path."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as mount_scope:
            ref = await mount_scope.mount(_MountCtxImpl())
            proxy = await ref.resolve(mount_scope)
            # Reach for the underlying context to invoke a raw method name.
            ctx = proxy._ctx  # type: ignore[attr-defined]
            with pytest.raises(IRException) as excinfo:
                await invoke_service_method(ctx, FXTR_FRAMEWORK_METHOD_PREFIX + "nope")
            assert excinfo.value.category == "UnknownMethod"


# ---------------------------------------------------------------------------
# Bridged dispatch: the marker reflects the mount scope, not the bridge dest
# ---------------------------------------------------------------------------


class TestBridgedDispatch:
    async def test_bridged_marker_is_mount_scope_marker(self) -> None:
        """Mount on A; bridge into sibling B; marker still reflects A."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "bridged-mount-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
                name="A",
            ) as scope_a:
                ref = await scope_a.mount(_NoOpUntyped())
                async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                    await scope_b.bridge_service_from_scope(
                        ref, scope_a, keepalive_via="independent_lease"
                    )
                    proxy = await ref.resolve(scope_b)
                    marker = await getattr(proxy, FXTR_CTX_METHOD_NAME)()
                    assert isinstance(marker, FxtrID)
                    assert marker.get_tag() == "."
                    # Crucial: the marker carries A's c-node, not B's
                    # (B has none — it's un-provenanced).
                    assert marker._provenance_parent == c_node


# ---------------------------------------------------------------------------
# Empty-payload enforcement
# ---------------------------------------------------------------------------


class TestEmptyPayloadEnforcement:
    async def test_extra_args_rejected(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as mount_scope:
            ref = await mount_scope.mount(_NoOpUntyped())
            proxy = await ref.resolve(mount_scope)
            with pytest.raises(IRException) as excinfo:
                await getattr(proxy, FXTR_CTX_METHOD_NAME)("unexpected_positional")
            assert excinfo.value.category == "MalformedPacket"

    async def test_extra_kwargs_rejected(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as mount_scope:
            ref = await mount_scope.mount(_NoOpUntyped())
            proxy = await ref.resolve(mount_scope)
            with pytest.raises(IRException) as excinfo:
                await getattr(proxy, FXTR_CTX_METHOD_NAME)(extra="kwarg")
            assert excinfo.value.category == "MalformedPacket"


# ---------------------------------------------------------------------------
# Tracking on the home scope
# ---------------------------------------------------------------------------


class TestTrackingOnHomeScope:
    """After one fetch, the c-node prov parent is tracked locally and
    supports sync ``unwrap_provenance``."""

    async def test_prov_parent_tracked_and_unwrappable_synchronously(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "tracking-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
                name="A",
            ) as scope_a:
                ref = await scope_a.mount(_NoOpUntyped())
                async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as home:
                    await home.bridge_service_from_scope(
                        ref, scope_a, keepalive_via="independent_lease"
                    )
                    proxy = await ref.resolve(home)
                    marker = await getattr(proxy, FXTR_CTX_METHOD_NAME)()
                    assert isinstance(marker, FxtrID)
                    prov_parent = marker._provenance_parent
                    assert prov_parent is not None
                    # Tracked locally on the home scope.
                    assert home.is_tracked(prov_parent)
                    # Sync unwrap works (the precomp closure flowed in
                    # the return arena, populating home's caches).
                    unwrapped = home._main_arena.unwrap_provenance(
                        home.clone_authorization(), prov_parent
                    )
                    assert unwrapped == GROUND
