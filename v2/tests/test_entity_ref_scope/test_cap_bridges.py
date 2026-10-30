"""Unit tests for substage 8c-6 cap-bridge primitives.

Covers:

- ``ServiceRef`` value semantics.
- ``mount_lowlevel_cap_as_service`` / ``attach_bridged_lowlevel_cap``
  / ``mount``.
- ``_find_bridge_for_token`` parent-walk + closest-scope-wins.
- ``_resolve_service`` happy path + error paths.
- ``FxtrScope.lift`` / ``is_tracked`` for :class:`ServiceRef`.
- Bridge teardown on scope drain.

Dispatch / packet machinery is not exercised here — that lives in the
``UntypedService`` tests once step 2 lands.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import anyio.abc
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    FxtrScope,
    FxtrServiceImpl,
    ServiceKey,
    ServiceRef,
    register_service_resolver,
)
from fxtr2.leases.lease_bridges import (
    DependentLocalBridge,
    IndependentLocalBridge,
    IndependentPipelinedBridge,
)
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_ir import IRFuture, IRHandle, IRValue, Request, UnwrapForPipelining
from fxtr2.leases.local_lease import STATIC_LIFETIME, UnconditionalLocalLifetime

# ---------------------------------------------------------------------------
# Test stubs
# ---------------------------------------------------------------------------


class _StubFuture(IRFuture[Any, Any]):
    """Minimal IRFuture: stable identity, no actual resolution.

    Used to construct ``UnwrapForPipelining`` values for the
    Independent-bridge clone/discard helper tests; we never wait on
    it, so the wait/dispatch methods can be no-ops.
    """

    def __init__(self, token: LifetimeToken[Any]) -> None:
        self._token = token

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._token

    async def wait(self, lease: Lease[Any]) -> Any:  # noqa: ARG002
        raise NotImplementedError

    def walk(self, attr_path: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError

    def pipeline_dispatch(self, *a: Any, **k: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError


class _StubHandle(IRHandle[Any]):
    """Minimal IRHandle: stores a token, dispatches raise."""

    def __init__(self, token: LifetimeToken[Any]) -> None:
        self._token = token

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._token

    async def request(
        self,
        lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],  # noqa: ARG002
        request: Request,  # noqa: ARG002
    ) -> IRValue:
        raise NotImplementedError

    def dispatch(
        self,
        lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],  # noqa: ARG002
        request: Request,  # noqa: ARG002
    ) -> Any:
        raise NotImplementedError


_ECHO_TYPE_ID = FxtrID.builtin("test_echo_target")
"""Per-module builtin FxtrID used as the ``service_type_id`` for tests.

Tests track this in any scope they then mount/attach/resolve through.
"""


def _track_echo_type_id(scope: FxtrScope) -> FxtrID:
    """Track ``_ECHO_TYPE_ID`` in ``scope`` (idempotent) and return it."""
    return scope.track_builtin(_ECHO_TYPE_ID)


class _EchoTarget:
    """Test-only target type: also doubles as the resolver's return type.

    The echo resolver constructs an ``_EchoTarget`` carrying its
    inputs so tests can assert on which bridge / scope drove the
    resolution.
    """

    def __init__(
        self,
        context: Any,  # ServiceProxyContext
        target_type: Any,
    ) -> None:
        self.scope = context.home_scope
        self.handle = context.proxy_handle
        self.context = context
        self.target_type = target_type
        # Convenience: a callable that mints a lease for the proxy's
        # own service key. Lets the existing tests keep their
        # ``resolved.lease_provider()`` shape.
        self.lease_provider = lambda: context.mint_lease_for_key(context.proxy_key)


async def _echo_resolver(
    context: Any,  # ServiceProxyContext
    target_type: Any,
) -> _EchoTarget:
    return _EchoTarget(context, target_type)


@pytest.fixture(scope="module", autouse=True)
def _register_echo_resolver() -> Any:
    """Register the test resolver once per module run.

    The registry is process-wide so we'd accumulate duplicate registrations
    across modules; ``allow_implicit_overrides=True`` keeps the registration
    idempotent for repeated test runs in the same process.
    """
    register_service_resolver(_EchoTarget, _echo_resolver, allow_implicit_overrides=True)
    yield


# ---------------------------------------------------------------------------
# ServiceRef value semantics
# ---------------------------------------------------------------------------


class TestServiceRefValue:
    def test_equality(self) -> None:
        with UnconditionalLocalLifetime("v").revoke_after_block() as lt:
            h1 = _StubHandle(lt.token)
            h2 = _StubHandle(lt.token)
            assert ServiceRef(
                handle=h1, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID
            ) == ServiceRef(handle=h1, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID)
            # Different handle instances → different refs even on the same
            # token (handles compare by identity).
            assert ServiceRef(
                handle=h1, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID
            ) != ServiceRef(handle=h2, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID)
            # Different target type → different ref.
            assert ServiceRef(
                handle=h1, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID
            ) != ServiceRef(handle=h1, target_type=int, service_type_id=_ECHO_TYPE_ID)
            # Different service_type_id → different ref.
            assert ServiceRef(
                handle=h1, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID
            ) != ServiceRef(
                handle=h1,
                target_type=_EchoTarget,
                service_type_id=FxtrID.builtin("other_id"),
            )

    def test_frozen(self) -> None:
        with UnconditionalLocalLifetime("v").revoke_after_block() as lt:
            ref = ServiceRef(
                handle=_StubHandle(lt.token), target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID
            )
            with pytest.raises(dataclasses.FrozenInstanceError):
                ref.handle = _StubHandle(lt.token)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# mount_lowlevel_cap_as_service
# ---------------------------------------------------------------------------


class TestMountLowlevel:
    @pytest.mark.anyio
    async def test_mount_self_token_creates_dependent_bridge(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            handle = _StubHandle(scope._token)
            scope.track_builtin(_ECHO_TYPE_ID)
            ref = scope.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            assert ref.handle is handle
            assert ref.target_type is _EchoTarget
            entry = scope._cap_bridges[scope._token]
            assert isinstance(entry.bridge, DependentLocalBridge)
            # Identity converter with no input leases.
            assert entry.bridge.leases == {}
            # The handle is recorded against the entry.
            assert ServiceKey(handle, _ECHO_TYPE_ID) in entry.handles

    @pytest.mark.anyio
    async def test_mount_rejects_foreign_token(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
                handle = _StubHandle(lt.token)
                with pytest.raises(ValueError, match="own token"):
                    scope.track_builtin(_ECHO_TYPE_ID)
                    scope.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)

    @pytest.mark.anyio
    async def test_mount_requires_task_group(self) -> None:
        # A lifespanless scope has no task group and cannot host services.
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        try:
            handle = _StubHandle(scope._token)
            scope.track_builtin(_ECHO_TYPE_ID)
            with pytest.raises(RuntimeError, match="no task group"):
                scope.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
        finally:
            scope.force_close()

    @pytest.mark.anyio
    async def test_mount_idempotent(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            handle = _StubHandle(scope._token)
            scope.track_builtin(_ECHO_TYPE_ID)
            ref1 = scope.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            entry1 = scope._cap_bridges[scope._token]
            scope.track_builtin(_ECHO_TYPE_ID)
            ref2 = scope.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            entry2 = scope._cap_bridges[scope._token]
            # Same entry instance — idempotent.
            assert entry1 is entry2
            assert entry1.handles == {ServiceKey(handle, _ECHO_TYPE_ID)}
            # New ServiceRefs each call (frozen dataclass equality), but
            # value-equal.
            assert ref1 == ref2


# ---------------------------------------------------------------------------
# attach_bridged_lowlevel_cap (Independent shape)
# ---------------------------------------------------------------------------


class TestAttachBridgedIndependent:
    @pytest.mark.anyio
    async def test_attach_stores_independent_bridge(self) -> None:
        with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
            async with FxtrScope(isolated=True) as scope:
                handle = _StubHandle(lt.token)
                scope.track_builtin(_ECHO_TYPE_ID)
                ref = scope.attach_bridged_lowlevel_cap(
                    handle,
                    _EchoTarget,
                    _ECHO_TYPE_ID,
                    IndependentLocalBridge(lease=lt.root_lease.clone()),
                )
                entry = scope._cap_bridges[lt.token]
                assert isinstance(entry.bridge, IndependentLocalBridge)
                stored = entry.bridge.lease
                assert isinstance(stored, Lease)
                assert stored.subject is lt.token
                assert ServiceKey(handle, _ECHO_TYPE_ID) in entry.handles
                assert ref.handle is handle

    @pytest.mark.anyio
    async def test_attach_idempotent_discards_duplicate_bridge(self) -> None:
        with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
            async with FxtrScope(isolated=True) as scope:
                handle = _StubHandle(lt.token)
                first = lt.root_lease.clone()
                second = lt.root_lease.clone()
                scope.track_builtin(_ECHO_TYPE_ID)
                scope.attach_bridged_lowlevel_cap(
                    handle, _EchoTarget, _ECHO_TYPE_ID, IndependentLocalBridge(lease=first)
                )
                entry = scope._cap_bridges[lt.token]
                # Second call with another clone-bridge discards it cleanly.
                scope.track_builtin(_ECHO_TYPE_ID)
                scope.attach_bridged_lowlevel_cap(
                    handle, _EchoTarget, _ECHO_TYPE_ID, IndependentLocalBridge(lease=second)
                )
                assert scope._cap_bridges[lt.token] is entry
                # `first` is held by the bridge; `second` was discarded —
                # is_active reflects that.
                assert not second.is_active()

    @pytest.mark.anyio
    async def test_attach_requires_task_group(self) -> None:
        with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
            scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
            try:
                handle = _StubHandle(lt.token)
                scope.track_builtin(_ECHO_TYPE_ID)
                with pytest.raises(RuntimeError, match="no task group"):
                    scope.attach_bridged_lowlevel_cap(
                        handle,
                        _EchoTarget,
                        _ECHO_TYPE_ID,
                        IndependentLocalBridge(lease=lt.root_lease.clone()),
                    )
            finally:
                scope.force_close()

    @pytest.mark.anyio
    async def test_attach_rejects_lease_token_mismatch(self) -> None:
        """IndependentLocalBridge whose lease subject ≠ handle.token raises ValueError."""
        with UnconditionalLocalLifetime("a").revoke_after_block() as lta:
            with UnconditionalLocalLifetime("b").revoke_after_block() as ltb:
                async with FxtrScope(isolated=True) as scope:
                    handle = _StubHandle(lta.token)
                    bad_lease = ltb.root_lease.clone()
                    with pytest.raises(ValueError, match="does not match"):
                        scope.track_builtin(_ECHO_TYPE_ID)
                        scope.attach_bridged_lowlevel_cap(
                            handle,
                            _EchoTarget,
                            _ECHO_TYPE_ID,
                            IndependentLocalBridge(lease=bad_lease),
                        )
                    # The supplied bridge's lease was discarded by the
                    # raise path's cleanup.
                    assert not bad_lease.is_active()


# ---------------------------------------------------------------------------
# attach_bridged_lowlevel_cap (Dependent shape)
# ---------------------------------------------------------------------------


class TestAttachBridgedDependent:
    @pytest.mark.anyio
    async def test_attach_stores_dependent_bridge(self) -> None:
        # We don't run the converter here — just validate storage shape.
        def fake_converter(
            leases: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
        ) -> Lease[Any]:
            raise NotImplementedError

        with UnconditionalLocalLifetime("target").revoke_after_block() as target_lt:
            async with FxtrScope(isolated=True) as scope:
                handle = _StubHandle(target_lt.token)
                static_lease = STATIC_LIFETIME.lease()
                bridge = DependentLocalBridge(
                    leases={static_lease.subject: static_lease},
                    converter=fake_converter,
                )
                scope.track_builtin(_ECHO_TYPE_ID)
                ref = scope.attach_bridged_lowlevel_cap(handle, _EchoTarget, _ECHO_TYPE_ID, bridge)
                entry = scope._cap_bridges[target_lt.token]
                assert isinstance(entry.bridge, DependentLocalBridge)
                assert STATIC_LIFETIME.token in entry.bridge.leases
                assert ref.handle is handle


# ---------------------------------------------------------------------------
# mount(impl) high-level wrapper
# ---------------------------------------------------------------------------


class _CountingImpl(FxtrServiceImpl[_EchoTarget]):
    def __init__(self) -> None:
        self.mount_calls = 0

    async def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[_EchoTarget]:
        self.mount_calls += 1
        handle = _StubHandle(scope._token)
        scope.track_builtin(_ECHO_TYPE_ID)
        return scope.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)


class TestMount:
    @pytest.mark.anyio
    async def test_mount_delegates_to_impl(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            impl = _CountingImpl()
            ref = await scope.mount(impl)
            assert impl.mount_calls == 1
            assert ref.target_type is _EchoTarget
            assert isinstance(scope._cap_bridges[scope._token].bridge, DependentLocalBridge)

    @pytest.mark.anyio
    async def test_mount_requires_task_group(self) -> None:
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        try:
            with pytest.raises(RuntimeError, match="no task group"):
                await scope.mount(_CountingImpl())
        finally:
            scope.force_close()


# ---------------------------------------------------------------------------
# _find_bridge_for_handle / parent walk
# ---------------------------------------------------------------------------


class TestParentWalk:
    @pytest.mark.anyio
    async def test_self_bridge_takes_precedence(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            with UnconditionalLocalLifetime("shared").revoke_after_block() as shared_lt:
                shared_handle = _StubHandle(shared_lt.token)
                parent.track_builtin(_ECHO_TYPE_ID)
                parent.attach_bridged_lowlevel_cap(
                    shared_handle,
                    _EchoTarget,
                    _ECHO_TYPE_ID,
                    IndependentLocalBridge(lease=shared_lt.root_lease.clone()),
                )
                async with FxtrScope(include=[parent], isolated=True) as child:
                    # Install a separate bridge for the same handle in child.
                    child.track_builtin(_ECHO_TYPE_ID)
                    child.attach_bridged_lowlevel_cap(
                        shared_handle,
                        _EchoTarget,
                        _ECHO_TYPE_ID,
                        IndependentLocalBridge(lease=shared_lt.root_lease.clone()),
                    )
                    found = child._find_bridge_for_service_key(
                        ServiceKey(shared_handle, _ECHO_TYPE_ID)
                    )
                    assert found is not None
                    bridge_scope, _ = found
                    assert bridge_scope is child  # closest scope wins

    @pytest.mark.anyio
    async def test_parent_bridge_visible_from_child(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            with UnconditionalLocalLifetime("up").revoke_after_block() as up_lt:
                handle = _StubHandle(up_lt.token)
                parent.track_builtin(_ECHO_TYPE_ID)
                parent.attach_bridged_lowlevel_cap(
                    handle,
                    _EchoTarget,
                    _ECHO_TYPE_ID,
                    IndependentLocalBridge(lease=up_lt.root_lease.clone()),
                )
                async with FxtrScope(include=[parent], isolated=True) as child:
                    found = child._find_bridge_for_service_key(ServiceKey(handle, _ECHO_TYPE_ID))
                    assert found is not None
                    bridge_scope, _ = found
                    assert bridge_scope is parent

    @pytest.mark.anyio
    async def test_unregistered_handle_returns_none(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            with UnconditionalLocalLifetime("missing").revoke_after_block() as lt:
                assert (
                    scope._find_bridge_for_service_key(
                        ServiceKey(_StubHandle(lt.token), _ECHO_TYPE_ID)
                    )
                    is None
                )

    @pytest.mark.anyio
    async def test_unregistered_handle_on_known_token_returns_none(self) -> None:
        """Two distinct handles share the same token; only one is mounted.

        Resolving the unmounted handle through this scope must fail —
        bridge presence on the token is necessary but not sufficient.
        """
        async with FxtrScope(isolated=True) as scope:
            mounted = _StubHandle(scope._token)
            scope.track_builtin(_ECHO_TYPE_ID)
            scope.mount_lowlevel_cap_as_service(mounted, _EchoTarget, _ECHO_TYPE_ID)
            stranger = _StubHandle(scope._token)
            assert scope._find_bridge_for_service_key(ServiceKey(stranger, _ECHO_TYPE_ID)) is None


# ---------------------------------------------------------------------------
# ServiceRef.resolve
# ---------------------------------------------------------------------------


class TestResolve:
    @pytest.mark.anyio
    async def test_resolve_invokes_registered_resolver(self) -> None:
        # Mount on outer, resolve on a child — the self-mount restriction
        # (``resolve_service`` rejects refs whose handle.token equals
        # the home scope's token) means we can't resolve back into the
        # mount scope. See
        # ``plans/phase3/stage8c_user_caps/resolve_into_mount_scope_drain_cycle.md``.
        async with FxtrScope(isolated=True) as outer:
            handle = _StubHandle(outer._token)
            outer.track_builtin(_ECHO_TYPE_ID)
            ref = outer.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[outer], isolated=True) as inner:
                resolved = await ref.resolve(inner)
                assert resolved.scope is inner
                assert resolved.handle is handle
                assert resolved.target_type is _EchoTarget
                # The closure produced by the resolver should mint a lease
                # on outer's token (the bridge was lifted from outer onto
                # inner, so inner's bridge has a clone of outer's main lease).
                lease = resolved.lease_provider()
                assert isinstance(lease, Lease)
                assert lease.subject is outer._token
                lease.discard()

    @pytest.mark.anyio
    async def test_resolve_walks_parent(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            # Caller scope must be mountable (resolve_service requires it).
            async with FxtrScope(include=[parent], isolated=True) as child:
                resolved = await ref.resolve(child)
                assert resolved.scope is child
                # After resolve, the bridge has been lifted onto child as a
                # fresh ``IndependentLocalBridge``; the minted lease still has
                # ``handle.token`` (== parent's main token) as its subject.
                lease = resolved.lease_provider()
                assert isinstance(lease, Lease)
                assert lease.subject is parent._token
                lease.discard()

    @pytest.mark.anyio
    async def test_resolve_uses_active_scope_default(self) -> None:
        # Mount on outer, resolve from inner where inner is the active
        # scope. Self-mount restriction prevents same-scope resolve.
        async with FxtrScope(isolated=False) as outer:
            handle = _StubHandle(outer._token)
            outer.track_builtin(_ECHO_TYPE_ID)
            ref = outer.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(isolated=False) as inner:
                # inner is now the active scope.
                resolved = await ref.resolve()
                assert resolved.scope is inner

    @pytest.mark.anyio
    async def test_resolve_errors_when_handle_unknown(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
                ref = ServiceRef(
                    handle=_StubHandle(lt.token),
                    target_type=_EchoTarget,
                    service_type_id=_ECHO_TYPE_ID,
                )
                with pytest.raises(RuntimeError, match="not registered"):
                    await ref.resolve(scope)

    @pytest.mark.anyio
    async def test_resolve_requires_task_group(self) -> None:
        """``resolve_service`` requires the home scope to have a task group."""
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            # A lifespanless child has no task group, so it can't resolve.
            child = FxtrScope(include=[parent], isolated=True).lowlevel_start_lifespanless(
                parent_leases={parent: parent.clone_authorization()}
            )
            try:
                with pytest.raises(RuntimeError, match="no task group"):
                    await ref.resolve(child)
            finally:
                child.force_close()

    @pytest.mark.anyio
    async def test_resolve_errors_when_handle_on_known_token_but_not_registered(
        self,
    ) -> None:
        """Mounting handle A on a token does not let handle B (same token) resolve."""
        # Mount/resolve in distinct scopes (self-mount restriction).
        async with FxtrScope(isolated=True) as outer:
            mounted = _StubHandle(outer._token)
            outer.track_builtin(_ECHO_TYPE_ID)
            outer.mount_lowlevel_cap_as_service(mounted, _EchoTarget, _ECHO_TYPE_ID)
            stranger_ref = ServiceRef(
                handle=_StubHandle(outer._token),
                target_type=_EchoTarget,
                service_type_id=_ECHO_TYPE_ID,
            )
            async with FxtrScope(include=[outer], isolated=True) as inner:
                with pytest.raises(RuntimeError, match="not registered"):
                    await stranger_ref.resolve(inner)

    @pytest.mark.anyio
    async def test_public_resolve_service_is_equivalent(self) -> None:
        async with FxtrScope(isolated=True) as outer:
            handle = _StubHandle(outer._token)
            outer.track_builtin(_ECHO_TYPE_ID)
            ref = outer.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[outer], isolated=True) as inner:
                via_method = await inner.resolve_service(ref)
                via_ref = await ref.resolve(inner)
                assert via_method.scope is inner
                assert via_ref.scope is inner


# ---------------------------------------------------------------------------
# Lift-on-resolve / lift-on-mint behaviour
# ---------------------------------------------------------------------------


class TestLiftOnResolve:
    """``resolve_service`` and ``mint_lease_for_handle`` lift parent bridges
    into the home scope on first contact, so the home scope no longer
    relies on the parent retaining the handle.
    """

    @pytest.mark.anyio
    async def test_resolve_lifts_parent_bridge_into_child(self) -> None:
        """First resolve through child registers the handle on a local entry.

        Note: child already has an ``is_ancestor_or_arena_owner=True`` entry for
        ``parent._token`` at construction (parent is in ``child._parents``). In
        the uniform-derived model that entry is a **case-3 DependentLocalBridge**
        (converter ``child._derived.subtake(base, parent._token)``, no stored
        lease) — child.main is derived from parent.main. The lift here adds
        ``handle`` to that entry's ``handles`` set rather than installing a
        separate bridge.
        """
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                # Pre-resolve: child already has an ancestor entry for
                # parent._token (case-3 Dependent, no handles yet, no stored
                # lease — the converter mints Lease[parent.main] from child's
                # own auth via subtake).
                pre_entry = child._cap_bridges[parent._token]
                assert pre_entry.is_ancestor_or_arena_owner is True
                assert pre_entry.handles == set()
                assert isinstance(pre_entry.bridge, DependentLocalBridge)
                assert pre_entry.bridge.leases == {}
                assert ServiceKey(handle, _ECHO_TYPE_ID) not in child._known_handles

                await ref.resolve(child)

                # Post-resolve: the same entry now has the handle
                # registered. Lift shares the existing entry rather
                # than installing a duplicate.
                post_entry = child._cap_bridges[parent._token]
                assert post_entry is pre_entry
                assert post_entry.is_ancestor_or_arena_owner is True
                assert ServiceKey(handle, _ECHO_TYPE_ID) in post_entry.handles
                assert ServiceKey(handle, _ECHO_TYPE_ID) in child._known_handles

    @pytest.mark.anyio
    async def test_repeat_resolve_short_circuits_local(self) -> None:
        """Subsequent resolves reuse the local entry — no re-lift."""
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                await ref.resolve(child)
                entry_after_first = child._cap_bridges[parent._token]
                await ref.resolve(child)
                # Same entry instance — second resolve hit the fast path.
                assert child._cap_bridges[parent._token] is entry_after_first

    @pytest.mark.anyio
    async def test_lift_preserves_token_when_local_entry_already_exists(self) -> None:
        """If we already have a local bridge for the token via a different
        registered handle, lifting another handle on the same token shares
        the existing entry rather than installing a duplicate ``IndependentLocalBridge``
        whose vias would pin our own token (see ``§1.5`` of the part 6 plan).
        """
        async with FxtrScope(isolated=True) as parent:
            # Two handles on parent's main token: one mounted on parent, the
            # other only registered (via attach) on the parent. We then
            # ``mount`` a third handle locally on child (sharing the same
            # token, since child was constructed below — but actually we
            # need a different shape; let's mount on child first and
            # register the parent's other handle, then lift).
            mounted_on_parent = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            parent.mount_lowlevel_cap_as_service(mounted_on_parent, _EchoTarget, _ECHO_TYPE_ID)

            async with FxtrScope(include=[parent], isolated=True) as child:
                # Install a child-local Dependent bridge on parent._token via
                # mount on child: doesn't apply (mount needs handle.token to
                # equal child's own token). Instead: use
                # ``attach_bridged_lowlevel_cap`` for a *different* handle
                # on the same token.
                local_handle = _StubHandle(parent._token)
                child.track_builtin(_ECHO_TYPE_ID)
                child.attach_bridged_lowlevel_cap(
                    local_handle,
                    _EchoTarget,
                    _ECHO_TYPE_ID,
                    IndependentLocalBridge(lease=parent.clone_authorization()),
                )
                local_entry = child._cap_bridges[parent._token]
                assert ServiceKey(local_handle, _ECHO_TYPE_ID) in local_entry.handles

                # Now resolve a ref for the parent-mounted handle through
                # child. The lift logic should see the token already local
                # and share the existing entry (not install a duplicate).
                ref = ServiceRef(
                    handle=mounted_on_parent, target_type=_EchoTarget, service_type_id=_ECHO_TYPE_ID
                )
                await ref.resolve(child)
                assert child._cap_bridges[parent._token] is local_entry
                assert ServiceKey(mounted_on_parent, _ECHO_TYPE_ID) in local_entry.handles
                assert ServiceKey(local_handle, _ECHO_TYPE_ID) in local_entry.handles

    @pytest.mark.anyio
    async def test_mint_lease_for_handle_lifts_parent_bridge(self) -> None:
        """``ServiceProxyContext.mint_lease_for_handle`` lifts on first call.

        Symmetric with ``resolve_service``: any handle reachable from the
        home scope gets a co-located bridge after first contact.
        """
        from fxtr2.data.entity_ref_scope import ServiceProxyContext

        async with FxtrScope(isolated=True) as parent:
            handle_a = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref_a = parent.mount_lowlevel_cap_as_service(handle_a, _EchoTarget, _ECHO_TYPE_ID)
            # Second handle only known to the parent.
            handle_b = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            parent.mount_lowlevel_cap_as_service(handle_b, _EchoTarget, _ECHO_TYPE_ID)

            async with FxtrScope(include=[parent], isolated=True) as child:
                # Resolve handle_a via child to install a local bridge; this
                # is the precondition for handle_b's mint to take the
                # "share existing entry" branch.
                await ref_a.resolve(child)
                entry_after_resolve = child._cap_bridges[parent._token]
                assert ServiceKey(handle_a, _ECHO_TYPE_ID) in entry_after_resolve.handles
                assert ServiceKey(handle_b, _ECHO_TYPE_ID) not in entry_after_resolve.handles

                # mint_lease_for_handle on handle_b walks parents (because
                # handle_b is registered there), then shares the local
                # entry rather than lifting a duplicate.
                ctx = ServiceProxyContext(
                    home_scope=child, proxy_handle=handle_a, service_type_id=_ECHO_TYPE_ID
                )
                lease = ctx.mint_lease_for_key(ServiceKey(handle_b, _ECHO_TYPE_ID))
                assert isinstance(lease, Lease)
                assert lease.subject is parent._token
                lease.discard()
                # Post-mint, handle_b shares the local entry.
                assert child._cap_bridges[parent._token] is entry_after_resolve
                assert ServiceKey(handle_b, _ECHO_TYPE_ID) in entry_after_resolve.handles
                assert ServiceKey(handle_b, _ECHO_TYPE_ID) in child._known_handles


# ---------------------------------------------------------------------------
# ServiceRef lift / is_tracked
# ---------------------------------------------------------------------------


class TestServiceRefLiftAndIsTracked:
    @pytest.mark.anyio
    async def test_is_tracked_true_on_mount_scope_false_on_child_until_lift(self) -> None:
        """``is_tracked(ref)`` is local: parent registration does not imply child."""
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                assert parent.is_tracked(ref)
                assert not child.is_tracked(ref)

    @pytest.mark.anyio
    async def test_lift_from_parent_registers_service_key_on_child(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                assert ServiceKey(handle, _ECHO_TYPE_ID) not in child._known_handles
                assert child.lift(ref) is ref
                assert child.is_tracked(ref)
                assert ServiceKey(handle, _ECHO_TYPE_ID) in child._known_handles

    @pytest.mark.anyio
    async def test_lift_works_when_child_not_mountable(self) -> None:
        """Unlike ``resolve_service``, ``lift`` does not require ````."""
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                child.lift(ref)
                assert child.is_tracked(ref)

    @pytest.mark.anyio
    async def test_service_ref_lift_convenience_delegates(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                assert ref.lift(child) is ref
                assert child.is_tracked(ref)

    @pytest.mark.anyio
    async def test_lift_unknown_raises_value_error(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
                ref = ServiceRef(
                    handle=_StubHandle(lt.token),
                    target_type=_EchoTarget,
                    service_type_id=_ECHO_TYPE_ID,
                )
                with pytest.raises(ValueError, match="not registered"):
                    scope.lift(ref)

    @pytest.mark.anyio
    async def test_is_tracked_false_for_unregistered_handle_same_token(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            mounted = _StubHandle(scope._token)
            scope.track_builtin(_ECHO_TYPE_ID)
            ref_ok = scope.mount_lowlevel_cap_as_service(mounted, _EchoTarget, _ECHO_TYPE_ID)
            stranger_ref = ServiceRef(
                handle=_StubHandle(scope._token),
                target_type=_EchoTarget,
                service_type_id=_ECHO_TYPE_ID,
            )
            assert scope.is_tracked(ref_ok)
            assert not scope.is_tracked(stranger_ref)

    @pytest.mark.anyio
    async def test_repeat_lift_idempotent(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                child.lift(ref)
                entry = child._cap_bridges[parent._token]
                child.lift(ref)
                assert child._cap_bridges[parent._token] is entry
                assert child.is_tracked(ref)

    @pytest.mark.anyio
    async def test_lift_does_not_bring_service_type_id_into_child_arena(self) -> None:
        """``service_type_id`` is wire-portable identity, not arena-tracked
        content — lift registers the cap-bridge but does NOT lift the
        type_id into the child's arena. To introspect the loadable form,
        callers dispatch ``$fxtr.type``.
        """
        async with FxtrScope(isolated=True) as parent:
            handle = _StubHandle(parent._token)
            parent.track_builtin(_ECHO_TYPE_ID)
            ref = parent.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            async with FxtrScope(include=[parent], isolated=True) as child:
                assert not child.is_tracked(_ECHO_TYPE_ID)
                child.lift(ref)
                # ServiceKey is registered locally...
                assert child.is_tracked(ref)
                # ...but the type_id is NOT separately tracked.
                assert not child.is_tracked(_ECHO_TYPE_ID)


# ---------------------------------------------------------------------------
# forget_service
# ---------------------------------------------------------------------------


class TestForgetService:
    @pytest.mark.anyio
    async def test_forget_unknown_handle_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            stranger_ref = ServiceRef(
                handle=_StubHandle(scope._token),
                target_type=_EchoTarget,
                service_type_id=_ECHO_TYPE_ID,
            )
            with pytest.raises(ValueError, match="not registered"):
                scope.forget_service(stranger_ref)

    @pytest.mark.anyio
    async def test_forget_drops_bridge_when_last_handle(self) -> None:
        with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
            async with FxtrScope(isolated=True) as scope:
                handle = _StubHandle(lt.token)
                scope.track_builtin(_ECHO_TYPE_ID)
                ref = scope.attach_bridged_lowlevel_cap(
                    handle,
                    _EchoTarget,
                    _ECHO_TYPE_ID,
                    IndependentLocalBridge(lease=lt.root_lease.clone()),
                )
                entry = scope._cap_bridges[lt.token]
                assert isinstance(entry.bridge, IndependentLocalBridge)
                stored = entry.bridge.lease
                assert isinstance(stored, Lease)
                # Forget: handle is the only user of this bridge → bridge dropped.
                scope.forget_service(ref)
                assert lt.token not in scope._cap_bridges
                assert ServiceKey(handle, _ECHO_TYPE_ID) not in scope._known_handles
                assert not stored.is_active()

    @pytest.mark.anyio
    async def test_forget_keeps_bridge_when_other_handles_remain(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            h1 = _StubHandle(scope._token)
            h2 = _StubHandle(scope._token)
            scope.track_builtin(_ECHO_TYPE_ID)
            r1 = scope.mount_lowlevel_cap_as_service(h1, _EchoTarget, _ECHO_TYPE_ID)
            scope.track_builtin(_ECHO_TYPE_ID)
            scope.mount_lowlevel_cap_as_service(h2, _EchoTarget, _ECHO_TYPE_ID)
            entry_before = scope._cap_bridges[scope._token]
            assert entry_before.handles == {
                ServiceKey(h1, _ECHO_TYPE_ID),
                ServiceKey(h2, _ECHO_TYPE_ID),
            }
            scope.forget_service(r1)
            # h2 still uses the bridge → entry stays.
            assert scope._cap_bridges[scope._token] is entry_before
            assert entry_before.handles == {ServiceKey(h2, _ECHO_TYPE_ID)}
            assert ServiceKey(h1, _ECHO_TYPE_ID) not in scope._known_handles
            assert ServiceKey(h2, _ECHO_TYPE_ID) in scope._known_handles

    @pytest.mark.anyio
    async def test_forget_makes_resolve_fail(self) -> None:
        async with FxtrScope(isolated=True) as outer:
            handle = _StubHandle(outer._token)
            outer.track_builtin(_ECHO_TYPE_ID)
            ref = outer.mount_lowlevel_cap_as_service(handle, _EchoTarget, _ECHO_TYPE_ID)
            outer.forget_service(ref)
            async with FxtrScope(include=[outer], isolated=True) as inner:
                with pytest.raises(RuntimeError, match="not registered"):
                    await ref.resolve(inner)


# ---------------------------------------------------------------------------
# Bridge clone() / discard_at_rest() — both Local and Pipelined Independent
# ---------------------------------------------------------------------------


class TestIndependentBridgeStorageHelpers:
    """Direct unit tests for the ``clone()`` / ``discard_at_rest()`` methods
    on the Independent bridge variants.

    The pipelined form arises in the lift path when a parent's
    ``DependentPipelinedBridge`` returns ``UnwrapForPipelining`` (e.g.
    an IRHandle-backed chain converter); ``clone()`` preserves the
    "shared future, cloned keep-alive lease" contract so subsequent
    mints stay valid.
    """

    def test_local_bridge_clone_and_discard(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            bridge = IndependentLocalBridge(lease=lt.root_lease.clone())
            cloned = bridge.clone()
            assert cloned.lease is not bridge.lease
            assert cloned.lease.subject is lt.token
            assert bridge.lease.is_active()
            cloned.discard_at_rest()
            assert not cloned.lease.is_active()
            assert bridge.lease.is_active()
            bridge.discard_at_rest()
            assert not bridge.lease.is_active()

    def test_pipelined_bridge_clone_and_discard(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            future = _StubFuture(lt.token)
            bridge = IndependentPipelinedBridge(
                pipelined=UnwrapForPipelining(future=future, lease=lt.root_lease.clone())
            )

            cloned = bridge.clone()
            # The future is the same object — pipelined consumers share
            # the underlying resolution.
            assert cloned.pipelined.future is future
            # The keep-alive lease was cloned, not aliased.
            assert cloned.pipelined.lease is not bridge.pipelined.lease
            assert cloned.pipelined.lease.subject is lt.token
            assert bridge.pipelined.lease.is_active()

            cloned.discard_at_rest()
            assert not cloned.pipelined.lease.is_active()
            # The original wrapper's lease is still alive — clones are
            # independent.
            assert bridge.pipelined.lease.is_active()
            bridge.discard_at_rest()
            assert not bridge.pipelined.lease.is_active()


# ---------------------------------------------------------------------------
# Drain-time bridge teardown
# ---------------------------------------------------------------------------


class TestBridgeTeardown:
    @pytest.mark.anyio
    async def test_independent_lease_discarded_on_drain(self) -> None:
        # Hold a separate clone of `foreign_lt`'s root lease to keep it alive
        # after the scope drains; the bridge's own clone should be released.
        with UnconditionalLocalLifetime("foreign").revoke_after_block() as lt:
            held = lt.root_lease.clone()
            try:
                async with FxtrScope(isolated=True) as scope:
                    handle = _StubHandle(lt.token)
                    scope.track_builtin(_ECHO_TYPE_ID)
                    scope.attach_bridged_lowlevel_cap(
                        handle,
                        _EchoTarget,
                        _ECHO_TYPE_ID,
                        IndependentLocalBridge(lease=lt.root_lease.clone()),
                    )
                    entry = scope._cap_bridges[lt.token]
                    assert isinstance(entry.bridge, IndependentLocalBridge)
                    bridge_lease = entry.bridge.lease  # capture before drain clears the dict
                    assert isinstance(bridge_lease, Lease)
                # On exit aclose runs → token ends → callback fires.
                assert not bridge_lease.is_active()
            finally:
                held.discard()
            # `lt` itself should still be alive because we held `held` past
            # the scope; only after held.discard does its drain unblock.

    @pytest.mark.anyio
    async def test_dependent_key_lease_discarded_on_drain(self) -> None:
        with UnconditionalLocalLifetime("k").revoke_after_block() as key_lt:
            with UnconditionalLocalLifetime("target").revoke_after_block() as target_lt:

                def fake(
                    leases: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
                ) -> Lease[Any]:
                    raise NotImplementedError

                async with FxtrScope(isolated=True) as scope:
                    handle = _StubHandle(target_lt.token)
                    captured_key = key_lt.root_lease.clone()
                    scope.track_builtin(_ECHO_TYPE_ID)
                    scope.attach_bridged_lowlevel_cap(
                        handle,
                        _EchoTarget,
                        _ECHO_TYPE_ID,
                        DependentLocalBridge(
                            leases={key_lt.token: captured_key},
                            converter=fake,
                        ),
                    )
                    entry = scope._cap_bridges[target_lt.token]
                    assert isinstance(entry.bridge, DependentLocalBridge)
                    assert entry.bridge.leases[key_lt.token] is captured_key
                # Bridge cleared, key lease released.
                assert not captured_key.is_active()


# ---------------------------------------------------------------------------
# anyio_backend fixture for pytest-anyio
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
