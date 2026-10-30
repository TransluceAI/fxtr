"""Tests for FxtrScope freezing."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ACTIVE_SCOPE, EntityRef, FxtrScope

# =============================================================================
# Test Entity Type
# =============================================================================

tydef_uuid = uuid.UUID("d39f3e68-f8f2-463b-a09c-fe2df1354517")


@dataclasses.dataclass(frozen=True)
class FreezeTestNode(
    DataclassEntity,
    fxtr_type=system_entities.define_type(tydef_uuid, "freeze_test_node"),
):
    value: str


# =============================================================================
# Helpers
# =============================================================================


_GLOBAL_SCOPE = ACTIVE_SCOPE.get()
"""Captured at module load time, before any test overrides the active scope."""


async def store_into_isolated_scope(
    scope: FxtrScope, *entities: FreezeTestNode
) -> list[EntityRef[FreezeTestNode]]:
    """Store entities into an isolated scope by using a temporary child scope that also inherits
    from the global scope (needed for type builtins).

    Returns the list of EntityRefs, which are tracked in `scope`.
    """
    refs: list[EntityRef[FreezeTestNode]] = []
    async with FxtrScope(
        isolated=True, include=[scope, _GLOBAL_SCOPE], name="store_helper"
    ) as inner:
        for entity in entities:
            ref = inner.store(entity)
            await scope.copy_from_scope(ref, inner)
            refs.append(ref)
    return refs


async def store_and_load_into_isolated_scope(
    scope: FxtrScope, *entities: FreezeTestNode
) -> list[tuple[EntityRef[FreezeTestNode], FreezeTestNode]]:
    """Store and load entities into an isolated scope. Returns (ref, loaded_value) pairs.

    The entities are loaded in the scope so that their values are cached there.
    """
    results: list[tuple[EntityRef[FreezeTestNode], FreezeTestNode]] = []
    async with FxtrScope(
        isolated=True, include=[scope, _GLOBAL_SCOPE], name="store_load_helper"
    ) as inner:
        for entity in entities:
            ref = inner.store(entity)
            await scope.copy_from_scope(ref, inner)
            loaded = await scope.load(ref)
            results.append((ref, loaded))
    return results


# =============================================================================
# Freezing Mechanics
# =============================================================================


class TestFreezingMechanics:
    """Tests for the freeze_scope method and frozen property."""

    @pytest.mark.anyio
    async def test_freeze_isolated_scope_no_parents(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            assert not scope.frozen
            scope.freeze_scope()
            assert scope.frozen

    @pytest.mark.anyio
    async def test_freeze_isolated_scope_with_frozen_parent(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            parent.freeze_scope()
            async with FxtrScope(isolated=True, include=[parent]) as child:
                child.freeze_scope()
                assert child.frozen

    @pytest.mark.anyio
    async def test_frozen_property_before_and_after(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            assert scope.frozen is False
            scope.freeze_scope()
            assert scope.frozen is True

    @pytest.mark.anyio
    async def test_freeze_is_idempotent(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            scope.freeze_scope()  # should not raise
            assert scope.frozen

    @pytest.mark.anyio
    async def test_freeze_root_scope_raises(self) -> None:
        root = ACTIVE_SCOPE.get()
        with pytest.raises(RuntimeError, match="Root scopes cannot be frozen"):
            root.freeze_scope()

    @pytest.mark.anyio
    async def test_freeze_non_isolated_scope_raises(self) -> None:
        """A non-isolated scope inherits from the global root, which can't be frozen."""
        async with FxtrScope() as scope:
            with pytest.raises(RuntimeError, match="is not frozen"):
                scope.freeze_scope()

    @pytest.mark.anyio
    async def test_freeze_with_unfrozen_parent_raises(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            async with FxtrScope(isolated=True, include=[parent]) as child:
                with pytest.raises(RuntimeError, match="is not frozen"):
                    child.freeze_scope()

    def test_freeze_unentered_scope_raises(self) -> None:
        # Construction is inert: a scope must be entered before it is started;
        # `freeze_scope` on an unentered scope raises. No teardown needed — an
        # inert scope holds nothing and is GC-safe.
        scope = FxtrScope(isolated=True)
        with pytest.raises(RuntimeError, match="has not been started"):
            scope.freeze_scope()


# =============================================================================
# Disallowed Operations on Frozen Scope
# =============================================================================

FROZEN_ERROR_SNIPPET = "Scope has been frozen"
CHILD_SCOPE_GUIDANCE = "create a new FxtrScope that inherits from this one"


class TestDisallowedOperations:
    """Tests that mutating operations raise RuntimeError on a frozen scope."""

    @pytest.mark.anyio
    async def test_store_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`store`") as exc_info:
                scope.store(FreezeTestNode(value="x"))
            assert FROZEN_ERROR_SNIPPET in str(exc_info.value)
            assert CHILD_SCOPE_GUIDANCE in str(exc_info.value)

    @pytest.mark.anyio
    async def test_store_cbor_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`store_cbor`"):
                scope.store_cbor({})

    @pytest.mark.anyio
    async def test_load_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            [ref] = await store_into_isolated_scope(scope, FreezeTestNode(value="x"))
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`load`"):
                await scope.load(ref)

    @pytest.mark.anyio
    async def test_load_cbor_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            [ref] = await store_into_isolated_scope(scope, FreezeTestNode(value="x"))
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`load_cbor`"):
                await scope.load_cbor(ref.entity_id)

    @pytest.mark.anyio
    async def test_load_frozen_cbor_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            [ref] = await store_into_isolated_scope(scope, FreezeTestNode(value="x"))
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`load_frozen_cbor`"):
                await scope.load_frozen_cbor(ref.entity_id)

    @pytest.mark.anyio
    async def test_deep_load_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`deep_load`"):
                await scope.deep_load(lambda: None)

    @pytest.mark.anyio
    async def test_lift_raises(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            [ref] = await store_into_isolated_scope(parent, FreezeTestNode(value="x"))
            parent.freeze_scope()
            async with FxtrScope(isolated=True, include=[parent]) as child:
                # child is not frozen, so lift works on child
                child.lift(ref.entity_id)
                # now freeze child and try to lift again
                child.freeze_scope()
                with pytest.raises(RuntimeError, match="`lift`"):
                    child.lift(ref.entity_id)

    @pytest.mark.anyio
    async def test_copy_from_scope_raises(self) -> None:
        async with FxtrScope(isolated=True) as source:
            [ref] = await store_into_isolated_scope(source, FreezeTestNode(value="x"))
            async with FxtrScope(isolated=True) as target:
                target.freeze_scope()
                with pytest.raises(RuntimeError, match="`copy_from_scope`"):
                    await target.copy_from_scope(ref.entity_id, source)

    @pytest.mark.anyio
    async def test_abandon_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            [ref] = await store_into_isolated_scope(scope, FreezeTestNode(value="x"))
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`abandon`"):
                scope.abandon(ref.entity_id)

    @pytest.mark.anyio
    async def test_track_builtin_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`track_builtin`"):
                scope.track_builtin(FxtrID.builtin("test"))

    @pytest.mark.anyio
    async def test_clear_cache_raises(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            with pytest.raises(RuntimeError, match="`clear_cache`"):
                scope.clear_cache()

    @pytest.mark.anyio
    async def test_error_messages_contain_method_name_and_guidance(self) -> None:
        """Verify all disallowed methods produce the expected error format."""
        method_names = [
            "store",
            "store_cbor",
            "load",
            "deep_load",
            "lift",
            "copy_from_scope",
            "track_from_scope",
            "abandon",
            "track_builtin",
            "clear_cache",
            "load_cbor",
            "load_frozen_cbor",
        ]
        async_methods = {"load", "load_cbor", "load_frozen_cbor", "deep_load", "copy_from_scope"}
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            for method_name in method_names:
                method = getattr(scope, method_name)
                try:
                    # Call with dummy args; we just need it to hit the frozen check
                    if method_name in async_methods:
                        await method(None)  # type: ignore[arg-type]
                    else:
                        method(None)  # type: ignore[arg-type]
                    pytest.fail(f"{method_name} did not raise")
                except RuntimeError as e:
                    msg = str(e)
                    assert (
                        f"`{method_name}`" in msg
                    ), f"{method_name}: missing method name in error: {msg}"
                    assert (
                        CHILD_SCOPE_GUIDANCE in msg
                    ), f"{method_name}: missing guidance in error: {msg}"


# =============================================================================
# Allowed Operations on Frozen Scope
# =============================================================================


class TestAllowedOperations:
    """Tests that read-only operations work on a frozen scope."""

    @pytest.mark.anyio
    async def test_get_cached_value_works(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            [(ref, loaded)] = await store_and_load_into_isolated_scope(
                scope, FreezeTestNode(value="hello")
            )
            assert loaded.value == "hello"
            scope.freeze_scope()
            # get_cached_value should still work
            cached = scope.get_cached_value(ref)
            assert cached.value == "hello"

    @pytest.mark.anyio
    async def test_is_tracked_works(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            [ref] = await store_into_isolated_scope(scope, FreezeTestNode(value="x"))
            scope.freeze_scope()
            assert scope.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_as_active_scope_works(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            with scope.as_active_scope():
                assert FxtrScope.get_active() is scope

    @pytest.mark.anyio
    async def test_check_alive_works(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            scope.check_authorized()  # should not raise

    @pytest.mark.anyio
    async def test_is_alive_works(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            assert scope.is_open()

    @pytest.mark.anyio
    async def test_included_in_scope_works(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            parent.freeze_scope()
            async with FxtrScope(isolated=True, include=[parent]) as child:
                assert parent.included_in_scope(child)

    @pytest.mark.anyio
    async def test_get_context_marker_id_works(self) -> None:
        async with FxtrScope(isolated=True) as scope:
            scope.freeze_scope()
            marker = scope.get_context_marker_id()
            assert isinstance(marker, FxtrID)


# =============================================================================
# Inheritance Patterns
# =============================================================================


class TestInheritancePatterns:
    """Tests for creating child scopes from frozen scopes."""

    @pytest.mark.anyio
    async def test_child_of_frozen_scope_is_mutable(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            parent.freeze_scope()
            async with FxtrScope(isolated=True, include=[parent]) as child:
                assert not child.frozen

    @pytest.mark.anyio
    async def test_child_can_store_and_load(self) -> None:
        """A mutable child of a frozen scope can store and load normally."""
        async with FxtrScope(isolated=True) as parent:
            parent.freeze_scope()
            async with FxtrScope(
                isolated=True, include=[parent, _GLOBAL_SCOPE], name="child"
            ) as child:
                ref = child.store(FreezeTestNode(value="from child"))
                loaded = await child.load(ref)
                assert loaded.value == "from child"

    @pytest.mark.anyio
    async def test_child_can_load_from_frozen_parent(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            [ref] = await store_into_isolated_scope(parent, FreezeTestNode(value="from parent"))
            parent.freeze_scope()
            async with FxtrScope(
                isolated=True, include=[parent, _GLOBAL_SCOPE], name="child"
            ) as child:
                loaded = await child.load(ref)
                assert loaded.value == "from parent"

    @pytest.mark.anyio
    async def test_freeze_chain(self) -> None:
        """Freeze parent, then freeze child that includes it."""
        async with FxtrScope(isolated=True) as grandparent:
            grandparent.freeze_scope()
            async with FxtrScope(isolated=True, include=[grandparent]) as parent:
                parent.freeze_scope()
                async with FxtrScope(isolated=True, include=[parent]) as child:
                    child.freeze_scope()
                    assert grandparent.frozen
                    assert parent.frozen
                    assert child.frozen

    @pytest.mark.anyio
    async def test_cannot_freeze_child_before_parent(self) -> None:
        async with FxtrScope(isolated=True) as parent:
            async with FxtrScope(isolated=True, include=[parent]) as child:
                with pytest.raises(RuntimeError, match="is not frozen"):
                    child.freeze_scope()


# =============================================================================
# Integration Test
# =============================================================================


class TestFrozenScopeIntegration:
    """End-to-end test exercising the frozen scope workflow."""

    @pytest.mark.anyio
    async def test_full_workflow(self) -> None:
        """
        Full workflow:
        1. Create an isolated scope and set it as active
        2. Store some entities in it (via a helper child scope that also inherits global)
        3. Freeze it
        4. Create child scope A (includes frozen scope + global), store more entities, freeze A
        5. Create child scope B (includes frozen scope + global), store different entities, freeze B
        6. Verify get_cached_value works on the frozen scopes
        7. Exit all scopes
        """
        async with FxtrScope(isolated=True, name="base") as base:
            with base.as_active_scope():
                # Store entities into the base scope via a helper child
                [(ref_a, _), (ref_b, _)] = await store_and_load_into_isolated_scope(
                    base,
                    FreezeTestNode(value="alpha"),
                    FreezeTestNode(value="beta"),
                )

                # Verify cached values before freezing
                assert base.get_cached_value(ref_a).value == "alpha"
                assert base.get_cached_value(ref_b).value == "beta"

                # Freeze the base scope
                base.freeze_scope()

                # Verify we can still read cached values
                assert base.get_cached_value(ref_a).value == "alpha"
                assert base.get_cached_value(ref_b).value == "beta"

                # Verify we can't mutate the base scope
                with pytest.raises(RuntimeError, match="Scope has been frozen"):
                    base.store(FreezeTestNode(value="should fail"))

                # Create child scope A
                async with FxtrScope(
                    isolated=True, include=[base, _GLOBAL_SCOPE], name="child_a"
                ) as child_a:
                    # Child A can load from base
                    loaded_from_base = await child_a.load(ref_a)
                    assert loaded_from_base.value == "alpha"

                    # Child A can store its own entities
                    ref_c = child_a.store(FreezeTestNode(value="gamma"))
                    loaded_c = await child_a.load(ref_c)
                    assert loaded_c.value == "gamma"

                    # child_a can't be frozen because _GLOBAL_SCOPE is an unfrozen parent:
                    with pytest.raises(RuntimeError, match="is not frozen"):
                        child_a.freeze_scope()

                # To freeze a child, it must only include frozen parents.
                # So we store into a helper scope, copy out, then freeze.
                async with FxtrScope(isolated=True, include=[base], name="child_b") as child_b:
                    # Use a helper to store into child_b
                    async with FxtrScope(
                        isolated=True, include=[child_b, _GLOBAL_SCOPE], name="child_b_helper"
                    ) as helper:
                        ref_d = helper.store(FreezeTestNode(value="delta"))
                        await child_b.copy_from_scope(ref_d, helper)
                        loaded_d = await child_b.load(ref_d)
                        assert loaded_d.value == "delta"

                    # Now child_b's only parent is base (which is frozen), so we can freeze
                    child_b.freeze_scope()
                    assert child_b.frozen
                    assert child_b.get_cached_value(ref_d).value == "delta"
                    # Values from base are cached in base, not child_b (since child_b
                    # never loaded ref_a). We can still read them from base directly.
                    assert base.get_cached_value(ref_a).value == "alpha"

                # After children exit, base is still frozen and readable
                assert base.frozen
                assert base.get_cached_value(ref_a).value == "alpha"
