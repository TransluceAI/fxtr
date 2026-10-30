"""Tests for FxtrScope lifecycle, hierarchy, tracking, and isolation."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    ACTIVE_SCOPE,
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    get_active_scope,
)

# =============================================================================
# Test Entity Type
# =============================================================================

tydef_uuid = uuid.UUID("c28f2e58-e7f1-452a-9f9b-fd1de0243406")


@dataclasses.dataclass(frozen=True)
class ScopeTestNode(
    DataclassEntity,
    fxtr_type=system_entities.define_type(tydef_uuid, "test_node"),
):
    """Simple test entity with children and a value."""

    children: tuple[EntityRef[ScopeTestNode], ...]
    value: str


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def global_scope() -> FxtrScope:
    """Returns the global scope."""
    return ACTIVE_SCOPE.get()


# =============================================================================
# Basic Scope Creation and ACTIVE_SCOPE
# =============================================================================


class TestBasicScopeCreation:
    """Tests for basic scope creation and the ACTIVE_SCOPE context variable."""

    def test_global_scope_exists(self):
        """A global scope exists by default (ACTIVE_SCOPE has a default value)."""
        scope = ACTIVE_SCOPE.get()
        assert scope is not None
        assert isinstance(scope, FxtrScope)

    @pytest.mark.anyio
    async def test_create_named_scope(self):
        """Can create a named scope."""
        async with FxtrScope(name="test_scope") as scope:
            assert scope._name == "test_scope"

    @pytest.mark.anyio
    async def test_scope_becomes_active(self):
        """Entering scope makes it the active scope."""
        outer_scope = ACTIVE_SCOPE.get()
        async with FxtrScope(name="inner") as inner_scope:
            assert ACTIVE_SCOPE.get() is inner_scope
            assert ACTIVE_SCOPE.get() is not outer_scope
        assert ACTIVE_SCOPE.get() is outer_scope

    @pytest.mark.anyio
    async def test_nested_scopes(self):
        """Can nest scopes properly and they update ACTIVE_SCOPE correctly."""
        outer = ACTIVE_SCOPE.get()
        async with FxtrScope(name="level1") as level1:
            assert ACTIVE_SCOPE.get() is level1
            async with FxtrScope(name="level2") as level2:
                assert ACTIVE_SCOPE.get() is level2
                assert level2 is not level1
            assert ACTIVE_SCOPE.get() is level1
        assert ACTIVE_SCOPE.get() is outer

    def test_get_active_returns_global_scope_by_default(self):
        """FxtrScope.get_active() returns the global scope when no scope is entered."""
        assert FxtrScope.get_active() is ACTIVE_SCOPE.get()

    @pytest.mark.anyio
    async def test_get_active_tracks_entered_scope(self):
        """FxtrScope.get_active() reflects the most recently entered scope."""
        outer = FxtrScope.get_active()
        async with FxtrScope(name="inner") as inner:
            assert FxtrScope.get_active() is inner
            assert FxtrScope.get_active() is not outer
        assert FxtrScope.get_active() is outer

    @pytest.mark.anyio
    async def test_get_active_with_nested_scopes(self):
        """FxtrScope.get_active() follows nesting correctly."""
        async with FxtrScope(name="level1") as level1:
            assert FxtrScope.get_active() is level1
            async with FxtrScope(name="level2") as level2:
                assert FxtrScope.get_active() is level2
            assert FxtrScope.get_active() is level1

    @pytest.mark.anyio
    async def test_get_active_matches_get_active_scope(self):
        """FxtrScope.get_active() returns the same value as get_active_scope()."""
        assert FxtrScope.get_active() is get_active_scope()
        async with FxtrScope(name="test") as scope:
            assert FxtrScope.get_active() is get_active_scope()

    @pytest.mark.anyio
    async def test_scope_context_marker(self):
        """Each scope has its own context marker."""
        outer_marker = ACTIVE_SCOPE.get().get_context_marker_id()
        async with FxtrScope(name="inner") as inner_scope:
            inner_marker = inner_scope.get_context_marker_id()
            # Markers are the same (both are "." ground context by default)
            # but each scope tracks it separately
            assert inner_marker.get_tag() == "."
        assert outer_marker.get_tag() == "."


# =============================================================================
# Storing and Tracking in Scopes
# =============================================================================


class TestScopeTracking:
    """Tests for storing and tracking entities in scopes."""

    @pytest.mark.anyio
    async def test_store_in_scope(self):
        """Storing entity in scope makes it tracked."""
        async with FxtrScope(name="test") as scope:
            node = ScopeTestNode(children=(), value="test")
            ref = node.store()
            assert scope.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_untracked_after_scope_exit(self, global_scope: FxtrScope):
        """Entity stored in inner scope is not tracked after exit."""
        async with FxtrScope(name="inner") as scope:
            node = ScopeTestNode(children=(), value="test")
            ref = node.store()
            assert scope.is_tracked(ref.entity_id)

        # After exiting, global scope doesn't track it
        assert not global_scope.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_load_within_scope(self):
        """Can load entity within the scope it was stored in."""
        async with FxtrScope(name="test") as scope:
            node = ScopeTestNode(children=(), value="hello")
            ref = node.store()
            loaded = await ref.load()
            assert loaded.value == "hello"

    @pytest.mark.anyio
    async def test_load_after_exit_fails(self, global_scope: FxtrScope):
        """Loading entity from closed scope fails."""
        async with FxtrScope(name="inner") as scope:
            node = ScopeTestNode(children=(), value="test")
            ref = node.store()

        # After scope exit, loading should fail (entity not found in any parent scope)
        with pytest.raises(ValueError, match="not found in any parent scope"):
            await ref.load()


# =============================================================================
# Scope Hierarchy and Outer Access
# =============================================================================


class TestScopeHierarchy:
    """Tests for scope hierarchy and outer scope access."""

    @pytest.mark.anyio
    async def test_scope_has_outer(self, global_scope: FxtrScope):
        """Inner scope has outer scope reference."""
        async with FxtrScope(name="inner") as inner:
            assert inner.outer is global_scope

    @pytest.mark.anyio
    async def test_copy_to_outer_scope(self, global_scope: FxtrScope):
        """Can copy entity to outer scope."""
        async with FxtrScope(name="outer") as outer:
            async with FxtrScope(name="inner") as inner:
                node = ScopeTestNode(children=(), value="test_copy_outer")
                ref = node.store()

                await ref.copy_to(inner.outer)

            # After inner exits, entity should be in outer scope
            assert outer.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_copy_tracks_entity_and_loads_children(self, global_scope: FxtrScope):
        """Copy_to copies entity data; children become tracked when parent is loaded."""
        async with FxtrScope(name="outer") as outer:
            async with FxtrScope(name="inner") as inner:
                # Create all entities inside inner scope
                child = ScopeTestNode(children=(), value="child_copy_test")
                child_ref = child.store()
                parent = ScopeTestNode(children=(child_ref,), value="parent_copy_test")
                parent_ref = parent.store()

                # Copy just the parent to outer scope
                await parent_ref.copy_to(inner.outer)

            # Parent is tracked in outer scope
            assert outer.is_tracked(parent_ref.entity_id)
            # Child is NOT tracked yet (data is copied but not tracked until loaded)
            assert not outer.is_tracked(child_ref.entity_id)

            # Load the parent - this should track the children
            loaded_parent = await parent_ref.load()
            assert loaded_parent.value == "parent_copy_test"

            # Now the child should be tracked (loading parent tracks its children)
            assert outer.is_tracked(child_ref.entity_id)

            # And we can load the child
            loaded_child = await loaded_parent.children[0].load()
            assert loaded_child.value == "child_copy_test"

    @pytest.mark.anyio
    async def test_partial_copy(self, global_scope: FxtrScope):
        """Only explicitly copied entities go to outer scope."""
        async with FxtrScope(name="outer") as outer:
            async with FxtrScope(name="inner") as inner:
                # Create a tree: gp -> parent -> child
                child = ScopeTestNode(children=(), value="child_partial")
                child_ref = child.store()
                parent = ScopeTestNode(children=(child_ref,), value="parent_partial")
                parent_ref = parent.store()
                gp = ScopeTestNode(children=(parent_ref,), value="grandparent_partial")
                gp_ref = gp.store()

                # Only copy the grandparent
                await gp_ref.copy_to(inner.outer)

            # Grandparent should be tracked in outer scope
            assert outer.is_tracked(gp_ref.entity_id)

    @pytest.mark.anyio
    async def test_copy_handles_builtin_type_refs(self, global_scope: FxtrScope):
        """Copy correctly handles entities with builtin type references ($fxtr.type)."""
        async with FxtrScope(name="outer") as outer:
            async with FxtrScope(name="inner") as inner:
                # Create entity with a type reference (which is a builtin)
                # ScopeTestNode has fxtr_type which creates a builtin reference
                node = ScopeTestNode(children=(), value="has_type_ref_unique")
                ref = node.store()

                # Copy should handle the builtin type reference correctly
                await ref.copy_to(inner.outer)

            # Entity should be tracked in outer scope
            assert outer.is_tracked(ref.entity_id)

            # Should be loadable
            loaded = await ref.load()
            assert loaded.value == "has_type_ref_unique"

    @pytest.mark.anyio
    async def test_nested_scopes_inherit_access(self):
        """Child scopes can access entities from parent scopes."""
        async with FxtrScope(name="parent") as parent:
            # Store entity in parent
            node = ScopeTestNode(children=(), value="in_parent")
            ref = node.store()

            async with FxtrScope(name="child") as child:
                # Child should be able to load parent's entity
                loaded = await ref.load()
                assert loaded.value == "in_parent"


# =============================================================================
# Isolated Scopes
# =============================================================================


class TestIsolatedScopes:
    """Tests for isolated scope behavior."""

    @pytest.mark.anyio
    async def test_isolated_scope_not_active(self, global_scope: FxtrScope):
        """Isolated scope does not become the active scope."""
        async with FxtrScope(name="outer") as outer:
            async with FxtrScope(name="isolated", isolated=True) as iso:
                # Active scope should still be outer, not the isolated one
                assert ACTIVE_SCOPE.get() is outer
                assert ACTIVE_SCOPE.get() is not iso

    @pytest.mark.anyio
    async def test_isolated_scope_independent_tracking(self, global_scope: FxtrScope):
        """Isolated scope has independent tracking from parent."""
        async with FxtrScope(name="outer") as outer:
            outer_node = ScopeTestNode(children=(), value="outer")
            outer_ref = outer_node.store()
            assert outer.is_tracked(outer_ref.entity_id)

            async with FxtrScope(name="isolated", isolated=True) as iso:
                # Parent's entity should NOT be tracked in isolated scope
                assert not iso.is_tracked(outer_ref.entity_id)

    @pytest.mark.anyio
    async def test_isolated_scope_lift_fails(self, global_scope: FxtrScope):
        """Cannot lift entities into isolated scope."""
        async with FxtrScope(name="outer") as outer:
            outer_node = ScopeTestNode(children=(), value="outer")
            outer_ref = outer_node.store()

            async with FxtrScope(name="isolated", isolated=True) as iso:
                with pytest.raises(ValueError):
                    iso.lift(outer_ref.entity_id)

    @pytest.mark.anyio
    async def test_isolated_scope_load_fails_for_parent_entities(self, global_scope: FxtrScope):
        """Cannot load parent's entities in isolated scope without include."""
        async with FxtrScope(name="outer") as outer:
            outer_node = ScopeTestNode(children=(), value="outer")
            outer_ref = outer_node.store()

            # Isolated scope without including outer cannot access outer's entities
            async with FxtrScope(name="isolated", isolated=True, include=[global_scope]) as iso:
                # outer_ref is in outer scope, not global_scope or iso
                with pytest.raises(ValueError, match="not found in any parent scope"):
                    await outer_ref.load(iso)

    @pytest.mark.anyio
    async def test_store_in_isolated_scope(self, global_scope: FxtrScope):
        """Can store entities in isolated scope."""
        async with FxtrScope(name="outer") as outer:
            # Include global_scope to have access to builtins for entity types
            async with FxtrScope(name="isolated", isolated=True, include=[global_scope]) as iso:
                node = ScopeTestNode(children=(), value="isolated_entity")
                ref = node.store(iso)

                assert iso.is_tracked(ref.entity_id)
                assert not outer.is_tracked(ref.entity_id)
                assert not global_scope.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_load_in_isolated_scope(self, global_scope: FxtrScope):
        """Can load entities stored in isolated scope."""
        async with FxtrScope(name="outer") as outer:
            # Include global_scope for builtins
            async with FxtrScope(name="isolated", isolated=True, include=[global_scope]) as iso:
                node = ScopeTestNode(children=(), value="test_value")
                ref = node.store(iso)

                loaded = await ref.load(iso)
                assert loaded.value == "test_value"

    @pytest.mark.anyio
    async def test_isolated_scope_entity_not_in_outer(self, global_scope: FxtrScope):
        """Entity stored in isolated scope is not tracked in outer scope."""
        async with FxtrScope(name="outer") as outer:
            # Include global_scope for builtins
            async with FxtrScope(name="isolated", isolated=True, include=[global_scope]) as iso:
                node = ScopeTestNode(children=(), value="test")
                ref = node.store(iso)

                # Entity is tracked in iso, but NOT in outer
                assert iso.is_tracked(ref.entity_id)
                assert not outer.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_isolated_scope_load_fails_from_active(self, global_scope: FxtrScope):
        """Loading entity from isolated scope via active scope should fail."""
        async with FxtrScope(name="outer") as outer:
            # Include global_scope for builtins
            async with FxtrScope(name="isolated", isolated=True, include=[global_scope]) as iso:
                # Use a unique value to avoid content-addressed ID collision with other tests
                node = ScopeTestNode(children=(), value="isolated_scope_test_unique_value")
                ref = node.store(iso)

                # Active scope is outer (because iso is isolated), not iso
                assert ACTIVE_SCOPE.get() is outer

                # Loading without explicit scope uses active scope (outer),
                # which should fail because outer doesn't have access to iso's entities
                with pytest.raises(ValueError, match="not found in any parent scope"):
                    await ref.load()

    @pytest.mark.anyio
    async def test_load_from_closed_isolated_scope_fails(self, global_scope: FxtrScope):
        """Cannot load from closed isolated scope."""
        async with FxtrScope(name="outer") as outer:
            # Include global_scope for builtins
            async with FxtrScope(name="isolated", isolated=True, include=[global_scope]) as iso:
                node = ScopeTestNode(children=(), value="test")
                ref = node.store(iso)

            # After exit, loading from iso should fail (scope closed)
            with pytest.raises(RuntimeError, match="has already been closed"):
                await ref.load(iso)

    @pytest.mark.anyio
    async def test_basic_isolated_scope_usage(self):
        """Basic test of isolated scope without parent access."""
        outer = get_active_scope()
        async with FxtrScope(name="isolated", isolated=True) as iso:
            # Active scope should not change
            assert get_active_scope() is outer
            # Isolated scope should be alive
            assert iso.is_open()


# =============================================================================
# Scope Include Parameter
# =============================================================================


class TestScopeIncludes:
    """Tests for scope include parameter."""

    @pytest.mark.anyio
    async def test_include_scope(self, global_scope: FxtrScope):
        """Can include another scope."""
        # Use an intermediate scope to avoid modifying global scope
        async with FxtrScope(name="source") as source:
            # Store something in the source scope
            outer_node = ScopeTestNode(children=(), value="include_test")
            outer_ref = outer_node.store()

            async with FxtrScope(name="inner", include=[source]) as inner:
                # Should be able to load from included scope
                loaded = await outer_ref.load()
                assert loaded.value == "include_test"

    @pytest.mark.anyio
    async def test_include_isolated_with_parent(self, global_scope: FxtrScope):
        """Isolated scope with include can access included scope entities."""
        # Use an intermediate scope to avoid modifying global scope
        async with FxtrScope(name="source") as source:
            outer_node = ScopeTestNode(children=(), value="include_isolated_test")
            outer_ref = outer_node.store()

            async with FxtrScope(name="outer") as outer:
                async with FxtrScope(name="iso", include=[source], isolated=True) as iso:
                    # Should be able to load from the included source scope
                    loaded = await outer_ref.load(iso)
                    assert loaded.value == "include_isolated_test"


# =============================================================================
# Lift Operations
# =============================================================================


class TestLiftOperations:
    """Tests for lift operations between scopes."""

    @pytest.mark.anyio
    async def test_lift_from_parent(self, global_scope: FxtrScope):
        """Can lift entity from parent scope into child scope."""
        # Use an intermediate scope to avoid modifying global scope
        async with FxtrScope(name="parent") as parent:
            node = ScopeTestNode(children=(), value="lifted")
            ref = node.store()

            async with FxtrScope(name="child") as child:
                # Lift the entity into child scope
                child.lift(ref.entity_id)
                assert child.is_tracked(ref.entity_id)


# =============================================================================
# Scope Errors
# =============================================================================


class TestScopeErrors:
    """Tests for error conditions in scope operations."""

    @pytest.mark.anyio
    async def test_load_untracked_raises(self, global_scope: FxtrScope):
        """Loading untracked entity raises ValueError."""
        async with FxtrScope(name="test") as scope:
            # Create a ref to an entity that's not stored
            fake_id = FxtrID.builtin("nonexistent")
            with pytest.raises(ValueError, match="not found in any parent scope"):
                await scope.load_cbor(fake_id)


# =============================================================================
# Scope Names
# =============================================================================


class TestScopeNames:
    """Tests for scope naming."""

    @pytest.mark.anyio
    async def test_default_name(self):
        """Scopes can be created without explicit name."""
        async with FxtrScope() as scope:
            # Should have some default name or be None
            pass  # Just verify it doesn't error

    @pytest.mark.anyio
    async def test_custom_name(self):
        """Scopes can have custom names."""
        async with FxtrScope(name="my_custom_name") as scope:
            assert scope._name == "my_custom_name"


# =============================================================================
# Complex Workflows
# =============================================================================


class TestComplexWorkflows:
    """Integration tests for complex scope workflows."""

    @pytest.mark.anyio
    async def test_nested_scopes_with_copy(self, global_scope: FxtrScope):
        """Create entities entirely in nested scope and copy to parent."""
        async with FxtrScope(name="level1") as level1:
            async with FxtrScope(name="level2") as level2:
                # Create all entities inside level2
                child1 = ScopeTestNode(children=(), value="child1")
                child2 = ScopeTestNode(children=(), value="child2")
                child3 = ScopeTestNode(children=(), value="child3")

                ref1 = child1.store()
                ref2 = child2.store()
                ref3 = child3.store()

                parent = ScopeTestNode(children=(ref1, ref2, ref3), value="parent")
                parent_ref = parent.store()

                # Copy parent (and all deps including builtins) to level1
                await parent_ref.copy_to(level1)

            # After level2 exits, parent should be in level1
            assert level1.is_tracked(parent_ref.entity_id)
            loaded = await parent_ref.load()
            assert loaded.value == "parent"
            assert len(loaded.children) == 3

    @pytest.mark.anyio
    async def test_load_and_traverse_children(self, global_scope: FxtrScope):
        """Store hierarchy and traverse children."""
        async with FxtrScope(name="test") as scope:
            leaf1 = ScopeTestNode(children=(), value="leaf1")
            leaf2 = ScopeTestNode(children=(), value="leaf2")

            ref1 = leaf1.store()
            ref2 = leaf2.store()

            parent = ScopeTestNode(children=(ref1, ref2), value="parent")
            parent_ref = parent.store()

            # Load parent and traverse
            loaded_parent = await parent_ref.load()
            assert loaded_parent.value == "parent"

            # Load children
            child1 = await loaded_parent.children[0].load()
            child2 = await loaded_parent.children[1].load()

            assert child1.value == "leaf1"
            assert child2.value == "leaf2"

    @pytest.mark.anyio
    async def test_diamond_dependency(self, global_scope: FxtrScope):
        """Test diamond dependency pattern: A -> B, C -> D where B and C both reference D."""
        async with FxtrScope(name="test") as scope:
            # D is shared by B and C
            d = ScopeTestNode(children=(), value="D")
            d_ref = d.store()

            b = ScopeTestNode(children=(d_ref,), value="B")
            b_ref = b.store()

            c = ScopeTestNode(children=(d_ref,), value="C")
            c_ref = c.store()

            a = ScopeTestNode(children=(b_ref, c_ref), value="A")
            a_ref = a.store()

            # Verify all tracked
            assert scope.is_tracked(a_ref.entity_id)
            assert scope.is_tracked(b_ref.entity_id)
            assert scope.is_tracked(c_ref.entity_id)
            assert scope.is_tracked(d_ref.entity_id)

            # Load and verify structure
            loaded_a = await a_ref.load()
            assert loaded_a.value == "A"

            loaded_b = await loaded_a.children[0].load()
            loaded_c = await loaded_a.children[1].load()

            # Both B and C should have D as child
            assert len(loaded_b.children) == 1
            assert len(loaded_c.children) == 1

            # D refs should be the same
            d_from_b = await loaded_b.children[0].load()
            d_from_c = await loaded_c.children[0].load()

            assert d_from_b.value == "D"
            assert d_from_c.value == "D"


# =============================================================================
# Scope Inclusion Tests
# =============================================================================


class TestScopeInclusion:
    """Tests for included_in_scope and included_in_active_scope methods."""

    @pytest.mark.anyio
    async def test_scope_included_in_itself(self, global_scope: FxtrScope):
        """A scope is always included in itself."""
        async with FxtrScope(name="test") as scope:
            assert scope.included_in_scope(scope)

    @pytest.mark.anyio
    async def test_parent_included_in_child(self, global_scope: FxtrScope):
        """A parent scope is included in its child scope."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="child") as child:
                assert parent.included_in_scope(child)

    @pytest.mark.anyio
    async def test_child_not_included_in_parent(self, global_scope: FxtrScope):
        """A child scope is NOT included in its parent scope."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="child") as child:
                assert not child.included_in_scope(parent)

    @pytest.mark.anyio
    async def test_grandparent_included_in_grandchild(self, global_scope: FxtrScope):
        """An ancestor scope is included in a descendant scope."""
        async with FxtrScope(name="grandparent") as grandparent:
            async with FxtrScope(name="parent") as parent:
                async with FxtrScope(name="child") as child:
                    assert grandparent.included_in_scope(child)
                    assert parent.included_in_scope(child)

    @pytest.mark.anyio
    async def test_sibling_scopes_not_included(self, global_scope: FxtrScope):
        """Sibling scopes are not included in each other."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="sibling1") as sibling1:
                pass  # Exit sibling1
            async with FxtrScope(name="sibling2") as sibling2:
                # sibling1 is not included in sibling2
                assert not sibling1.included_in_scope(sibling2)

    @pytest.mark.anyio
    async def test_isolated_scope_not_included_in_outer(self, global_scope: FxtrScope):
        """An isolated scope is not included in the outer scope's children."""
        async with FxtrScope(name="outer") as outer:
            async with FxtrScope(name="isolated", isolated=True) as isolated:
                # outer is not included in isolated (isolated has no automatic parent)
                assert not outer.included_in_scope(isolated)

    @pytest.mark.anyio
    async def test_included_scope_is_included_in_isolated(self, global_scope: FxtrScope):
        """An explicitly included scope is included in an isolated scope."""
        async with FxtrScope(name="to_include") as to_include:
            async with FxtrScope(name="isolated", isolated=True, include=[to_include]) as isolated:
                assert to_include.included_in_scope(isolated)

    @pytest.mark.anyio
    async def test_included_in_active_scope_for_parent(self, global_scope: FxtrScope):
        """Parent scope is included in active scope (its child)."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="child") as child:
                # child is now active, parent should be included in active scope
                assert parent.included_in_active_scope()

    @pytest.mark.anyio
    async def test_included_in_active_scope_for_self(self, global_scope: FxtrScope):
        """Active scope is included in itself."""
        async with FxtrScope(name="test") as scope:
            # scope is active, it should be included in active scope (itself)
            assert scope.included_in_active_scope()

    @pytest.mark.anyio
    async def test_not_included_in_active_scope_for_sibling(self, global_scope: FxtrScope):
        """A sibling scope is not included in the active scope."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="sibling1") as sibling1:
                pass  # Exit sibling1
            async with FxtrScope(name="sibling2") as sibling2:
                # sibling1 is not included in active scope (sibling2)
                assert not sibling1.included_in_active_scope()


# =============================================================================
# Scope Lifecycle Operations
# =============================================================================


class TestScopeLifecycleOperations:
    """Tests for scope lifecycle operations like abandon."""

    @pytest.mark.anyio
    async def test_scope_abandon(self):
        """Entities can be abandoned from a scope."""
        async with FxtrScope(name="test") as scope:
            node = ScopeTestNode(children=(), value="abandoned")
            ref = node.store()
            assert scope.is_tracked(ref.entity_id)
            scope.abandon(ref)
            assert not scope.is_tracked(ref.entity_id)


# =============================================================================
# Basic Lifetime Ownership
# =============================================================================


class TestBasicLifetime:
    """Tests for the basic "each scope owns its token" model."""

    @pytest.mark.anyio
    async def test_default_owns_token(self):
        """By default, scopes create their own token."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="child") as child:
                assert child._token is not parent._token

    @pytest.mark.anyio
    async def test_owned_token_invalidated_on_exit(self):
        """When an owning scope exits, its token is invalidated."""
        token = None
        async with FxtrScope(name="test") as scope:
            token = scope._token
            assert not token.ended()
        assert token.ended()

    @pytest.mark.anyio
    async def test_isolated_scope_owns_token(self):
        """Isolated scopes always own their token."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(name="iso", isolated=True, include=[parent]) as iso:
                assert iso._token is not parent._token


# =============================================================================
# Scope Drain / aclose / force_close
# =============================================================================


class TestScopeDrain:
    """Tests for aclose, force_close, drain-phase guard rails."""

    @pytest.mark.anyio
    async def test_aclose_closes_scope(self):
        """``await scope.aclose()`` closes a lifespanless scope cleanly."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        assert scope.is_open()
        token = scope._token
        await scope.aclose()
        assert not scope.is_open()
        assert scope._closed
        assert token is not None and token.ended()

    @pytest.mark.anyio
    async def test_aclose_is_idempotent(self):
        """Double ``aclose`` is a no-op."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        await scope.aclose()
        await scope.aclose()  # should not raise
        assert scope._closed

    @pytest.mark.anyio
    async def test_force_close_revokes_synchronously(self):
        """``force_close`` ends the token immediately."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        token = scope._token
        scope.force_close()
        assert token is not None and token.ended()
        assert scope._closed
        assert not scope.is_open()

    @pytest.mark.anyio
    async def test_force_close_is_idempotent(self):
        """Double ``force_close`` is a no-op."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        scope.force_close()
        scope.force_close()  # should not raise

    @pytest.mark.anyio
    async def test_force_close_after_aclose_is_noop(self):
        """Mixing aclose + force_close is fine (idempotent)."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        await scope.aclose()
        scope.force_close()  # should not raise

    @pytest.mark.anyio
    async def test_force_close_on_never_opened_raises(self):
        """force_close on a constructed-but-unentered scope raises (inert ctor)."""
        scope = FxtrScope(isolated=True)
        assert scope._token is None
        assert not scope.is_open()
        with pytest.raises(RuntimeError, match="never opened"):
            scope.force_close()

    @pytest.mark.anyio
    async def test_aclose_on_never_opened_raises(self):
        """aclose on a constructed-but-unentered scope raises (inert ctor)."""
        scope = FxtrScope(isolated=True)
        assert scope._token is None
        with pytest.raises(RuntimeError, match="never opened"):
            await scope.aclose()

    @pytest.mark.anyio
    async def test_async_cm_drains_on_normal_exit(self):
        """Normal CM exit discards leases and drains the token."""
        async with FxtrScope(isolated=True) as scope:
            token = scope._token
        assert token.ended()
        assert scope._closed

    @pytest.mark.anyio
    async def test_async_cm_revokes_on_exception(self):
        """An exception inside the CM triggers force_close (synchronous revoke)."""
        captured_token = None
        with pytest.raises(ValueError, match="boom"):
            async with FxtrScope(isolated=True) as scope:
                captured_token = scope._token
                raise ValueError("boom")
        assert captured_token is not None
        assert captured_token.ended()
        assert scope._closed

    @pytest.mark.anyio
    async def test_cannot_reenter_after_close(self):
        """Re-entering a closed scope raises."""
        scope = FxtrScope(isolated=True)
        async with scope:
            pass
        with pytest.raises(RuntimeError, match="already been closed"):
            async with scope:
                pass

    @pytest.mark.anyio
    async def test_mutation_raises_after_close(self):
        """User-facing mutation ops raise once the scope is closed."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        await scope.aclose()
        with pytest.raises(RuntimeError, match="has already been closed"):
            scope.track_builtin(FxtrID.from_tag_and_payload("!", b"x"))

    @pytest.mark.anyio
    async def test_check_authorized_states(self):
        """check_authorized distinguishes not-started / closed."""
        # Not yet started (inert construction).
        scope = FxtrScope(isolated=True)
        assert not scope.is_open()
        with pytest.raises(RuntimeError, match="has not been started"):
            scope.check_authorized()

        # After force_close (closed).
        scope2 = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        scope2.force_close()
        assert not scope2.is_open()
        with pytest.raises(RuntimeError, match="has already been closed"):
            scope2.check_authorized()


# =============================================================================
# Scope entry: task group (every CM scope) vs lifespanless
# =============================================================================


class TestScopeEntry:
    """Every CM-entered scope opens a task group; lifespanless scopes don't."""

    @pytest.mark.anyio
    async def test_cm_scope_opens_task_group(self):
        """An ``async with`` scope opens an anyio task group (within-lifespan)."""
        async with FxtrScope(isolated=True) as scope:
            assert scope._task_group is not None

    @pytest.mark.anyio
    async def test_lifespanless_scope_has_no_task_group(self):
        """A ``lowlevel_start_lifespanless`` scope has no task group."""
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        try:
            assert scope._task_group is None
        finally:
            scope.force_close()

    @pytest.mark.anyio
    async def test_never_entered_is_not_open(self):
        """Inert construction: no token, not open, no close needed (GC-safe)."""
        scope = FxtrScope(isolated=True)
        assert scope._token is None
        assert not scope.is_open()

    @pytest.mark.anyio
    async def test_non_isolated_requires_cm(self):
        """Non-isolated scopes are inert until CM entry (no autostart)."""
        scope = FxtrScope(name="needs_cm")  # non-isolated
        assert not scope.is_open()
        assert scope._token is None

    @pytest.mark.anyio
    async def test_task_group_usable_in_body(self):
        """Spawned tasks run to completion before the CM exits."""
        results = []

        async def _work(tag):
            results.append(tag)

        async with FxtrScope(isolated=True) as scope:
            assert scope._task_group is not None
            scope._task_group.start_soon(_work, "a")
            scope._task_group.start_soon(_work, "b")
        assert set(results) == {"a", "b"}

    # TODO(stage12 chunk 2/3): lifespan coverage lands here once
    # ``attach_lifespan_to_scope`` (chunk 2) and the service mount lifespan
    # (chunk 3) exist — setup-authorized / teardown-after-drain, no-deadlock
    # drain, concurrent teardown, mount/resolve require a task group, etc.


# =============================================================================
# Uniform derived token + open_with_explicit_leases
# =============================================================================


class TestUniformDerivedToken:
    """Every scope's token is a DerivedLocalLifetime output over key + parents."""

    @pytest.mark.anyio
    async def test_token_is_derived_output(self):
        """The scope's token is its DerivedLocalLifetime's output; key ULL retained."""
        async with FxtrScope(isolated=True) as scope:
            assert scope._derived is not None
            assert scope._token is scope._derived.token
            assert scope._key_ull is not None  # revoke handle retained
            assert scope.isolated

    @pytest.mark.anyio
    async def test_parent_revoke_cascades_to_child(self):
        """force_close on a parent breaks the child's base authorization lease."""
        from fxtr2.leases.lease_types import LeaseBrokenError

        parent = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        async with FxtrScope(isolated=True, include=[parent]) as child:
            assert child._scope_auth_lease is not None
            child._scope_auth_lease.ensure_active()
            parent.force_close()
            with pytest.raises(LeaseBrokenError):
                child._scope_auth_lease.ensure_active()
            # Fast-close the child since its lifetime cascaded shut.
            child.force_close()

    @pytest.mark.anyio
    async def test_open_with_explicit_leases_from_supplied_lease(self):
        """A child can be opened from an explicitly-supplied parent lease.

        Mirrors the dispatch / cross-task pattern: the caller holds a lease for
        the parent (here cloned from the parent's authorization) and feeds it
        to ``open_with_explicit_leases`` rather than relying on in-context
        authorization.
        """
        parent = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        try:
            parent_lease = parent.clone_authorization()
            child = FxtrScope(isolated=True, include=[parent])
            async with child.open_with_explicit_leases(
                parent_leases={parent: parent_lease}
            ) as opened:
                assert opened is child
                assert child.is_open()
                assert child.is_authorized()
                # The supplied lease is consumed at entry; it does not leave the
                # parent authorized for the child's body via that lease object.
            assert not child.is_open()
        finally:
            await parent.aclose()

    @pytest.mark.anyio
    async def test_store_and_load_in_child(self):
        """Storing and loading entities works in a normal (derived) scope."""
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE]) as scope:
            with scope.as_active_scope():
                node = ScopeTestNode(children=(), value="derived")
                ref = node.store()
                assert scope.is_tracked(ref.entity_id)
                loaded = await ref.load(scope=scope)
                assert loaded.value == "derived"


# =============================================================================
# Deep-fetch cleanup on token end
# =============================================================================


class TestDeepFetchCleanup:
    """The scope's deep-fetch source key-leases are discarded on token end."""

    @pytest.mark.anyio
    async def test_deep_fetch_cleanup_on_aclose(self):
        """After aclose, deep-fetch source key-leases have been discarded."""
        from fxtr2.data import abstract_arena as aa
        from fxtr2.leases.local_lease import UnconditionalLocalLifetime

        class _StubSource:
            """Minimal DeepFetchable implementation for the test."""

            def __init__(self, token):
                self.token = token

            async def try_lookup(self, node_id):
                return None

        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        ull = UnconditionalLocalLifetime("stub_source")
        try:
            async with FxtrScope(isolated=True) as scope:
                src = _StubSource(ull.token)
                scope.register_deep_fetch_source(
                    src,
                    IndependentLocalBridge(lease=ull.root_lease.clone()),
                )
                assert len(scope._deep_fetch_sources) == 1
            # After scope exits, the scope's clone of the bridge's at-rest
            # lease was discarded. ULL still alive via its own root lease.
            assert not ull.token.ended()
        finally:
            ull.revoke("test teardown")

    @pytest.mark.anyio
    async def test_deep_fetch_cleanup_on_force_close(self):
        """force_close also runs the deep-fetch cleanup (via token-end callback)."""
        from fxtr2.data import abstract_arena as aa
        from fxtr2.leases.local_lease import UnconditionalLocalLifetime

        class _StubSource:
            def __init__(self, token):
                self.token = token

            async def try_lookup(self, node_id):
                return None

        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        ull = UnconditionalLocalLifetime("stub_source_fc")
        try:
            scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
            src = _StubSource(ull.token)
            scope.register_deep_fetch_source(
                src,
                IndependentLocalBridge(lease=ull.root_lease.clone()),
            )
            scope.force_close()
            # Scope's clone of the bridge's at-rest lease has been discarded
            # via the token-end callback. ULL still alive via its own root.
            assert not ull.token.ended()
        finally:
            ull.revoke("test teardown")
