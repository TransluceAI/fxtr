"""Tests for the stage-12 authorization model: scope-level base + per-context
overrides, decompose/compose/clone, and inert construction.

See ``plans/phase3/stage12_better_scope/new_scope_design.md`` §2–§4.
"""

from __future__ import annotations

import gc

import anyio
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope

pytestmark = pytest.mark.anyio

_BUILTIN = FxtrID.builtin("auth_test_builtin")


# ---------------------------------------------------------------------------
# Base (scope-level) authorization
# ---------------------------------------------------------------------------


class TestBaseAuthorization:
    async def test_open_scope_usable_from_sibling_task(self):
        """An open scope's base authorization is usable from any task (scope-level)."""
        async with FxtrScope(isolated=True) as scope:
            seen: list[bool] = []

            async def sibling():
                # No override, no inherited active scope set — base attribute
                # is globally visible, so the scope is authorized here.
                seen.append(scope.is_authorized())
                scope.track_builtin(_BUILTIN)  # must not raise

            async with anyio.create_task_group() as tg:
                tg.start_soon(sibling)
            assert seen == [True]

    async def test_base_gone_after_aclose_rejects_use(self):
        """Once aclose drops the base, ambient use raises everywhere."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        await scope.aclose()
        assert not scope.is_authorized()
        with pytest.raises(RuntimeError):
            scope.track_builtin(_BUILTIN)

    async def test_is_open_vs_is_authorized_on_drained_scope(self):
        """A drained-but-alive scope: open True, authorized False (no override)."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        held = scope.clone_authorization()  # keeps the token alive past base drop
        try:
            scope.discard_base_authorization()  # drops the base
            assert scope.is_open()  # token still alive (held)
            assert not scope.is_authorized()  # base gone, no override
        finally:
            held.discard()


# ---------------------------------------------------------------------------
# Per-context overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    async def test_authorize_reauthorizes_drained_scope(self):
        """`authorize` makes a drained-but-open scope usable again in-context."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        held = scope.clone_authorization()
        scope.discard_base_authorization()  # base gone
        assert not scope.is_authorized()
        with scope.authorize(held):  # ownership taken
            assert scope.is_authorized()
            scope.track_builtin(_BUILTIN)  # works under the override
        # After the block the override is gone and the token has drained.
        assert not scope.is_open()

    async def test_discarded_override_falls_through_to_live_base(self):
        """Base-floor: while the base is live, a discarded override is a no-op —
        ops keep working via the base (overrides union with the base, §2)."""
        async with FxtrScope(isolated=True) as scope:
            # Base is live here. Authorize with a clone, then discard the override.
            with scope.authorize(scope.clone_authorization()) as auth:
                assert scope.is_authorized()
                auth.discard()  # drops THIS override's lease
                # The base is still live, so resolution falls through to it:
                # still authorized, op still works.
                assert scope.is_authorized()
                scope.track_builtin(_BUILTIN)  # must not raise
            # Outside the authorize block the base still authorizes.
            assert scope.is_authorized()
            scope.track_builtin(_BUILTIN)

    async def test_inherited_override_after_origin_discard_uses_live_base(self):
        """Context-copy hazard, base-floor resolution: a subtask that snapshots
        an override and uses it after the origin discarded the lease **succeeds
        via the live base** (the dead override is never consulted while the base
        is live)."""
        async with FxtrScope(isolated=True) as scope:
            errors: list[str] = []
            ok: list[bool] = []

            async def child_uses_override():
                # The spawning context's override map was copied into this task;
                # the origin discards the lease below, leaving the entry dead —
                # but the live base authorizes regardless.
                await anyio.sleep(0.01)
                try:
                    scope.track_builtin(_BUILTIN)
                    ok.append(scope.is_authorized())
                except RuntimeError as e:
                    errors.append(str(e))

            with scope.authorize(scope.clone_authorization()) as auth:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(child_uses_override)
                    await anyio.sleep(0)  # let the child start + park on sleep
                    auth.discard()  # kill the inherited override lease
            assert not errors, errors
            assert ok == [True]

    async def test_dead_override_raises_only_when_base_also_gone(self):
        """The discarded-override error fires only when the base is *also* gone
        (draining): a held lease keeps the token alive, the base is dropped, and
        a context whose override lease died then resolves to neither."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        keepalive = scope.clone_authorization()  # pin the token past base drop
        try:
            with scope.authorize(scope.clone_authorization()) as auth:
                scope.discard_base_authorization()  # base gone — now draining
                auth.discard()  # and the override lease dies
                assert not scope.is_authorized()
                with pytest.raises(RuntimeError, match="override"):
                    scope.track_builtin(_BUILTIN)
        finally:
            keepalive.discard()


# ---------------------------------------------------------------------------
# Root / global scope
# ---------------------------------------------------------------------------


class TestRootScope:
    async def test_root_authorized_in_fresh_task(self):
        """The global scope reads authorized in a freshly-spawned task (base
        attribute is globally visible) — bare store works with no setup."""
        results: list[bool] = []

        async def fresh():
            results.append(GLOBAL_SCOPE.is_authorized())
            # A read-only authorized op (no global-scope mutation): resolving
            # the auth lease + reading the context marker both require auth.
            GLOBAL_SCOPE.check_authorized()
            GLOBAL_SCOPE.get_context_marker_id()

        async with anyio.create_task_group() as tg:
            tg.start_soon(fresh)
        assert results == [True]

    async def test_as_active_scope_does_not_authorize(self):
        """`as_active_scope` is a pure contextvar set — it does not authorize a
        scope whose base is gone."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        held = scope.clone_authorization()
        try:
            scope.discard_base_authorization()  # base gone
            with scope.as_active_scope():
                assert not scope.is_authorized()
                with pytest.raises(RuntimeError):
                    scope.track_builtin(_BUILTIN)
        finally:
            held.discard()


# ---------------------------------------------------------------------------
# Inert construction
# ---------------------------------------------------------------------------


class TestInertConstruction:
    async def test_unentered_scope_methods_raise_not_started(self):
        scope = FxtrScope(isolated=True)
        assert scope._token is None
        assert not scope.is_open()
        assert not scope.is_authorized()
        with pytest.raises(RuntimeError, match="not been started"):
            scope.track_builtin(_BUILTIN)

    async def test_unentered_scope_gc_is_silent(self):
        """A constructed-but-unentered scope can be GC'd with no lease-leak."""
        # The autouse _assert_no_lease_leaks fixture is the real assertion; we
        # just construct and drop without entering.
        FxtrScope(isolated=True)
        gc.collect()


# ---------------------------------------------------------------------------
# decompose / compose / clone
# ---------------------------------------------------------------------------


class TestDecomposeCompose:
    async def test_roundtrip_decompose_then_compose(self):
        """decompose_authorization -> compose_authorization round-trips a lease."""
        async with FxtrScope(isolated=True) as parent:
            async with FxtrScope(isolated=True, include=[parent]) as child:
                lease = child.clone_authorization()
                vias = child.decompose_authorization(lease)
                # Flat {token: lease} map: key via + one per-parent via.
                assert set(vias.keys()) == {child.key_token, parent.scope_token}
                assert vias[child.key_token].is_active()
                assert vias[parent.scope_token].subject is parent.scope_token
                # Recombine into a fresh Lease[child.main].
                recomposed = child.compose_authorization(vias)
                assert recomposed.subject is child.scope_token
                assert recomposed.is_active()
                recomposed.discard()

    async def test_compose_rejects_mismatched_token_set(self):
        async with FxtrScope(isolated=True) as parent:
            async with FxtrScope(isolated=True, include=[parent]) as child:
                vias = child.decompose_authorization(child.clone_authorization())
                # Hold back the parent via so the token set no longer matches.
                parent_via = vias.pop(parent.scope_token)
                with pytest.raises(RuntimeError, match="derived inputs"):
                    child.compose_authorization(vias)
                # The raising path discarded the leases passed to it (the key
                # via); clean up the parent via we held back.
                parent_via.discard()

    async def test_roundtrip_with_extra_derived_token(self):
        """decompose/compose round-trips a scope that has an extra (non-parent)
        derived-input token (the dispatch arg-token shape)."""
        from fxtr2.leases.local_lease import UnconditionalLocalLifetime

        with UnconditionalLocalLifetime("extra").revoke_after_block() as ext:
            scope = FxtrScope(isolated=True, include=[GLOBAL_SCOPE])
            async with scope.open_with_explicit_leases(
                parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()},
                extra_derived_leases={ext.token: ext.root_lease.clone()},
            ):
                vias = scope.decompose_authorization(scope.clone_authorization())
                assert set(vias.keys()) == {
                    scope.key_token,
                    GLOBAL_SCOPE.scope_token,
                    ext.token,
                }
                recomposed = scope.compose_authorization(vias)
                assert recomposed.subject is scope.scope_token
                assert recomposed.is_active()
                recomposed.discard()

    async def test_extra_derived_lease_duplicating_parent_is_tolerated(self):
        """An extra_derived_lease whose token is already a parent token is
        accepted: the redundant lease is discarded (no leak) and the token keeps
        its parent treatment rather than being recorded as an extra."""
        async with FxtrScope(isolated=True) as parent:
            scope = FxtrScope(isolated=True, include=[parent])
            async with scope.open_with_explicit_leases(
                parent_leases={parent: parent.clone_authorization()},
                # Duplicates the parent input; tolerated + discarded by entry.
                extra_derived_leases={parent.scope_token: parent.clone_authorization()},
            ):
                # The duplicate token was NOT recorded as an extra, so decompose
                # yields only the key via + the single parent via.
                vias = scope.decompose_authorization(scope.clone_authorization())
                assert set(vias.keys()) == {scope.key_token, parent.scope_token}
                recomposed = scope.compose_authorization(vias)
                recomposed.discard()

    async def test_clone_authorization_then_authorize_elsewhere(self):
        """clone_authorization in one context -> authorize in another makes the
        scope usable there."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        cloned = scope.clone_authorization()
        scope.discard_base_authorization()  # drop the base; only `cloned` keeps it alive
        seen: list[bool] = []

        async def other_task(lease):
            with scope.authorize(lease):
                seen.append(scope.is_authorized())
                scope.track_builtin(_BUILTIN)

        async with anyio.create_task_group() as tg:
            tg.start_soon(other_task, cloned)
        assert seen == [True]
        assert not scope.is_open()  # `cloned` was consumed by authorize
