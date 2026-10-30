"""Tests for ``FxtrScope.bridge_service_from_scope`` /
``ServiceRef.bridge_from_scope``.

Cross-tree bridging: lets a destination scope register a ServiceRef
that lives in a non-ancestor source scope. The bridge is held until
the destination scope drains — there is no early-cleanup primitive
(see stage10 part1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceKey, ServiceRef
from fxtr2.leases.lease_bridges import IndependentLocalBridge, IndependentPipelinedBridge
from fxtr2.services.untyped_service import UnknownServiceProxy, UntypedServiceImpl, service_method

# ---------------------------------------------------------------------------
# Fixtures + sample impls
# ---------------------------------------------------------------------------


class _EchoService(UntypedServiceImpl):
    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def add(self, a: int, b: int) -> int:
        return a + b


class _SpawnerService(UntypedServiceImpl):
    """Mounts a fresh service inside the call body and returns the ref.

    Used for the pipelined-source variant: spawning produces a chain
    bridge on the caller side that is `Independent`Pipelined (or
    `Dependent`Pipelined depending on the caller's setup).
    """

    @service_method
    async def spawn_echo(self) -> Any:
        scope = FxtrScope.get_active()
        return await scope.mount(_EchoService())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Cross-tree bridge happy path
# ---------------------------------------------------------------------------


class TestCrossTreeBridge:
    @pytest.mark.anyio
    async def test_bridge_then_resolve_and_call(self) -> None:
        """Two sibling scopes (no parent/child relationship). Mount on
        A; bridge into B; resolve + call from B works.

        Drain ordering: B holds a Lease[A.main] via the bridge, so B
        must close before A. The nested ``async with``s enforce that —
        B is the inner scope.
        """
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                await scope_b.bridge_service_from_scope(
                    ref, scope_a, keepalive_via="independent_lease"
                )
                # Now resolvable + callable from B.
                proxy = await ref.resolve(scope_b)
                assert isinstance(proxy, UnknownServiceProxy)
                assert await proxy.echo("ping") == "ping"
                assert await proxy.add(2, 3) == 5


class TestTypeIdPropagation:
    """``service_type_id`` is wire-portable identity, NOT arena-tracked
    content — bridging does not lift the type_id into ``dest``. To get
    the loadable form, callers dispatch ``$fxtr.type`` (which routes
    through the per-call return arena and lands the id in the home
    scope's main arena).
    """

    @pytest.mark.anyio
    async def test_type_id_not_tracked_after_bridge_but_loadable_via_fxtr_type(
        self,
    ) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                # Pre-bridge: type_id not tracked in B.
                assert not scope_b.is_tracked(ref.service_type_id)
                await scope_b.bridge_service_from_scope(
                    ref, scope_a, keepalive_via="independent_lease"
                )
                # Post-bridge: still not tracked (no copy_from_scope).
                assert not scope_b.is_tracked(ref.service_type_id)
                # Resolve + dispatch $fxtr.type → returned id IS now
                # tracked in B (came back through the return-arena path).
                proxy = await ref.resolve(scope_b)
                returned = await proxy.service_type_id()
                assert returned == ref.service_type_id
                assert scope_b.is_tracked(returned)


# ---------------------------------------------------------------------------
# Strict-on-duplicate behaviour
# ---------------------------------------------------------------------------


class TestStrictOnDuplicate:
    @pytest.mark.anyio
    async def test_double_bridge_raises(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                await scope_b.bridge_service_from_scope(
                    ref, scope_a, keepalive_via="independent_lease"
                )
                with pytest.raises(ValueError, match="already registered"):
                    await scope_b.bridge_service_from_scope(
                        ref, scope_a, keepalive_via="independent_lease"
                    )

    @pytest.mark.anyio
    async def test_self_source_rejected(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(_EchoService())
            with pytest.raises(ValueError, match="different from self"):
                await scope.bridge_service_from_scope(ref, scope, keepalive_via="independent_lease")

    @pytest.mark.anyio
    async def test_ref_not_in_source_rejected(self) -> None:
        """Bridging a ref whose handle isn't in source raises."""
        # Mount on a third scope; pass a different scope as source.
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="MOUNT") as mount_scope:
            ref = await mount_scope.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="SRC") as fake_source:
                async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="DST") as dest:
                    with pytest.raises(ValueError, match="not registered"):
                        await dest.bridge_service_from_scope(
                            ref, fake_source, keepalive_via="independent_lease"
                        )


# ---------------------------------------------------------------------------
# Bridge stays registered for self's lifetime
# ---------------------------------------------------------------------------


class TestBridgeLifetime:
    @pytest.mark.anyio
    async def test_bridge_entry_persists_for_self_lifetime(self) -> None:
        """After bridging, the entry stays in dest's _cap_bridges for
        as long as dest is alive — there's no early-release primitive."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                await scope_b.bridge_service_from_scope(
                    ref, scope_a, keepalive_via="independent_lease"
                )
                # Bridge is in B's _cap_bridges and stays there.
                assert ref.handle.token in scope_b._cap_bridges
                # Use the bridge a few times — entry still present.
                proxy = await ref.resolve(scope_b)
                await proxy.echo("hi")
                assert ref.handle.token in scope_b._cap_bridges


# ---------------------------------------------------------------------------
# Pipelined-source variant
# ---------------------------------------------------------------------------


class TestPipelinedSource:
    """When the source scope's bridge for the handle is pipelined (e.g.
    a chain bridge produced by a service dispatch return), bridging to
    a sibling should preserve the pipelined-ness — the destination ends
    up with an ``IndependentPipelinedBridge`` rather than a concrete
    ``IndependentLocalBridge``.

    A concrete-bridge source ends up with ``IndependentLocalBridge`` on
    the destination; a chain (Dependent Pipelined) bridge produces an
    ``IndependentPipelinedBridge`` on the destination because
    ``_mint_lease_via_ancestor_bridge`` runs the dependent converter
    against source's main lease and returns the pipelined result.
    """

    @pytest.mark.anyio
    async def test_local_source_yields_local_bridge(self) -> None:
        """Mount-on-source yields an `IndependentLocalBridge` — when we
        bridge to a sibling, the resulting bridge is also Independent
        Local (the lift reads from source's identity bridge).

        Note: source `mount` installs a `DependentLocalBridge` (case-2
        identity), so the lift would ordinarily produce a concrete
        lease. After my dest-side wrap, that's `IndependentLocalBridge`.
        """
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                await scope_b.bridge_service_from_scope(
                    ref, scope_a, keepalive_via="independent_lease"
                )
                entry = scope_b._cap_bridges[ref.handle.token]
                assert isinstance(entry.bridge, IndependentLocalBridge)

    @pytest.mark.anyio
    async def test_pipelined_source_yields_pipelined_bridge(self) -> None:
        """A spawn_echo return on source produces a `DependentPipelinedBridge`
        chain bridge on the caller. Bridging that ref to a sibling should
        produce an `IndependentPipelinedBridge` — the chain converter
        runs against source's main lease yielding an
        `UnwrapForPipelining`, which we wrap as Independent on dest.
        """
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="HOST") as host:
            spawner_ref = await host.mount(_SpawnerService())
            async with FxtrScope(include=[host], isolated=True, name="A") as scope_a:
                # Calling spawn_echo from A produces a chain bridge in
                # A's _cap_bridges for the spawned ref's token.
                proxy = await spawner_ref.resolve(scope_a)
                spawned_ref = await proxy.spawn_echo()
                assert isinstance(spawned_ref, ServiceRef)
                # Verify A's bridge for the spawned ref's token IS
                # pipelined (sanity check on the test setup).
                from fxtr2.leases.lease_bridges import DependentPipelinedBridge

                a_entry = scope_a._cap_bridges[spawned_ref.handle.token]
                assert isinstance(a_entry.bridge, DependentPipelinedBridge)

                async with FxtrScope(include=[host], isolated=True, name="B") as scope_b:
                    with anyio.fail_after(10):
                        await scope_b.bridge_service_from_scope(
                            spawned_ref, scope_a, keepalive_via="independent_lease"
                        )
                        entry = scope_b._cap_bridges[spawned_ref.handle.token]
                        assert isinstance(entry.bridge, IndependentPipelinedBridge)
                        # Dispatch through the pipelined bridge works.
                        spawned_proxy = await spawned_ref.resolve(scope_b)
                        assert await spawned_proxy.echo("hi") == "hi"


# ---------------------------------------------------------------------------
# Active-scope default for ServiceRef.bridge_from_scope
# ---------------------------------------------------------------------------


class TestActiveScopeDefault:
    @pytest.mark.anyio
    async def test_bridge_from_scope_uses_active_scope(self) -> None:
        """``ref.bridge_from_scope(source, keepalive_via="independent_lease")`` (no into_scope) bridges
        into the currently-active scope."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            # Make B the active scope (non-isolated → active).
            async with FxtrScope(isolated=False, name="B") as scope_b:
                # ``into_scope=None`` → defaults to active = scope_b.
                await ref.bridge_from_scope(scope_a, keepalive_via="independent_lease")
                proxy = await ref.resolve()  # active scope is B
                assert await proxy.echo("hello") == "hello"
                key = ServiceKey(ref.handle, ref.service_type_id)
                assert key in scope_b._known_handles


# ---------------------------------------------------------------------------
# concurrent_child_bridge
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _concurrent_parent_child(
    *, child_extra_parents: tuple[FxtrScope, ...] = ()
) -> AsyncIterator[tuple[FxtrScope, FxtrScope, ServiceRef[Any]]]:
    """Set up dest (parent) + source (child) in **concurrent tasks**, with an
    ``_EchoService`` mounted on source and ``concurrent_child_bridge``'d into dest.

    Yields ``(parent, child, ref)``. On exit, both scopes close *simultaneously*
    (a single signal unparks both tasks at once), so dest's drain cascades source
    closed — the only non-deadlocking arrangement for this mode. ``child_extra_parents``
    adds further (separate-tree) scope-parents to the child.
    """
    close_all = anyio.Event()
    async with anyio.create_task_group() as tg:

        async def parent_task(*, task_status: Any) -> None:
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="P") as parent:
                task_status.started(parent)
                await close_all.wait()

        parent = await tg.start(parent_task)

        async def child_task(*, task_status: Any) -> None:
            async with FxtrScope(
                include=[parent, *child_extra_parents], isolated=True, name="C"
            ) as child:
                ref = await child.mount(_EchoService())
                await parent.bridge_service_from_scope(
                    ref, child, keepalive_via="concurrent_child_bridge"
                )
                task_status.started((child, ref))
                await close_all.wait()

        child, ref = await tg.start(child_task)
        try:
            yield parent, child, ref
        finally:
            # One signal → both tasks leave their `async with` at once → dest
            # drains and cascades source closed. Bounded by the caller's fail_after.
            close_all.set()


class TestConcurrentChildBridge:
    @pytest.mark.anyio
    async def test_closes_together_and_callable(self) -> None:
        """dest is a direct scope-parent of source (concurrent tasks); the service
        mounted on source is callable from dest while both are alive, and the two
        close together with no deadlock. Bounded so a regression (e.g. a standing
        ``Lease[source.main]`` on dest, or wrong block nesting) hangs loudly."""
        from fxtr2.leases.lease_bridges import DependentLocalBridge

        with anyio.fail_after(10):
            async with _concurrent_parent_child() as (parent, _child, ref):
                proxy = await ref.resolve(parent)
                assert await proxy.echo("ping") == "ping"
                # No standing Lease[source.main] on dest — the bridge is Dependent.
                entry = parent._cap_bridges[ref.handle.token]
                assert isinstance(entry.bridge, DependentLocalBridge)

    @pytest.mark.anyio
    async def test_with_separate_mount_parent(self) -> None:
        """The RPC service-return shape: source derives from dest AND a *separate*
        mount scope (a sibling tree). The separate parent's via is recovered from
        source's authorization (it need not be independently authorized) and the
        safety check passes because it isn't a descendant of dest."""
        with anyio.fail_after(10):
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="MOUNT") as mount:
                async with _concurrent_parent_child(child_extra_parents=(mount,)) as (
                    parent,
                    _child,
                    ref,
                ):
                    proxy = await ref.resolve(parent)
                    assert await proxy.echo("v") == "v"

    @pytest.mark.anyio
    async def test_rejects_when_dest_not_direct_parent(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                with pytest.raises(ValueError, match="direct parent"):
                    await scope_b.bridge_service_from_scope(
                        ref, scope_a, keepalive_via="concurrent_child_bridge"
                    )

    @pytest.mark.anyio
    async def test_rejects_when_ref_not_mounted_on_source(self) -> None:
        """ref mounted on a third scope (token != source.main) → rejected even
        though dest is a direct parent of source."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="MOUNT") as mount:
            ref = await mount.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="P") as parent:
                async with FxtrScope(include=[parent, mount], isolated=True, name="C") as child:
                    # Reachable on child (bridged in) but not *mounted* on it.
                    await child.bridge_service_from_scope(
                        ref, mount, keepalive_via="independent_lease"
                    )
                    with pytest.raises(ValueError, match="mounted on source_scope itself"):
                        await parent.bridge_service_from_scope(
                            ref, child, keepalive_via="concurrent_child_bridge"
                        )

    @pytest.mark.anyio
    async def test_rejects_when_source_has_extra_derived_inputs(self) -> None:
        from fxtr2.leases.local_lease import UnconditionalLocalLifetime

        with UnconditionalLocalLifetime("extra").revoke_after_block() as ext:
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="P") as parent:
                child = FxtrScope(include=[parent], isolated=True, name="C")
                async with child.open_with_explicit_leases(
                    parent_leases={parent: parent.clone_authorization()},
                    extra_derived_leases={ext.token: ext.root_lease.clone()},
                ):
                    ref = await child.mount(_EchoService())
                    with pytest.raises(ValueError, match="no extra"):
                        await parent.bridge_service_from_scope(
                            ref, child, keepalive_via="concurrent_child_bridge"
                        )

    @pytest.mark.anyio
    async def test_rejects_when_other_parent_is_descendant_of_dest(self) -> None:
        """A non-dest parent of source that is itself a descendant of dest would
        reintroduce the drain cycle → rejected."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="P") as parent:
            async with FxtrScope(include=[parent], isolated=True, name="MID") as mid:
                async with FxtrScope(include=[parent, mid], isolated=True, name="C") as child:
                    ref = await child.mount(_EchoService())
                    with pytest.raises(ValueError, match="descendant of dest"):
                        await parent.bridge_service_from_scope(
                            ref, child, keepalive_via="concurrent_child_bridge"
                        )

    @pytest.mark.anyio
    async def test_keepalive_via_is_required(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="A") as scope_a:
            ref = await scope_a.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="B") as scope_b:
                with pytest.raises(TypeError):
                    await scope_b.bridge_service_from_scope(ref, scope_a)  # type: ignore[call-arg]
