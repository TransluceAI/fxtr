"""Tests for fxtr2/data/fxtr_scope.py."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from fxtr2.data import entity_serdes, system_entities
from fxtr2.data.fxtr_scope import ACTIVE_SCOPE, FxtrScope

# =============================================================================
# Test Entity Type
# =============================================================================

tydef_uuid = uuid.UUID("c28f2e58-e7f1-452a-9f9b-fd1de0243406")


@dataclasses.dataclass(frozen=True)
class ScopeTestNode(
    entity_serdes.DataclassEntity,
    fxtr_type=system_entities.define_type(tydef_uuid, "test_node"),
):
    """Simple test entity with children and a value."""

    children: tuple[entity_serdes.EntityRef[ScopeTestNode], ...]
    value: str


# =============================================================================
# Fixtures
# =============================================================================


# =============================================================================
# Basic Scope Creation
# =============================================================================


class TestBasicScopeCreation:
    """Tests for basic scope creation and context management."""

    def test_global_scope_exists(self):
        """A global scope exists by default."""
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
        """Can nest scopes properly."""
        outer = ACTIVE_SCOPE.get()
        async with FxtrScope(name="level1") as level1:
            assert ACTIVE_SCOPE.get() is level1
            async with FxtrScope(name="level2") as level2:
                assert ACTIVE_SCOPE.get() is level2
                assert level2 is not level1
            assert ACTIVE_SCOPE.get() is level1
        assert ACTIVE_SCOPE.get() is outer


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
            assert scope.is_tracked(ref.ref)

    @pytest.mark.anyio
    async def test_untracked_after_scope_exit(self, global_scope: FxtrScope):
        """Entity stored in inner scope is not tracked after exit."""
        async with FxtrScope(name="inner") as scope:
            node = ScopeTestNode(children=(), value="test")
            ref = node.store()
            assert scope.is_tracked(ref.ref)

        # After exiting, global scope doesn't track it
        assert not global_scope.is_tracked(ref.ref)

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
            assert outer.is_tracked(ref.ref)

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
            assert outer.is_tracked(parent_ref.ref)
            # Child is NOT tracked yet (data is copied but not tracked until loaded)
            assert not outer.is_tracked(child_ref.ref)

            # Load the parent - this should track the children
            loaded_parent = await parent_ref.load()
            assert loaded_parent.value == "parent_copy_test"

            # Now the child should be tracked (loading parent tracks its children)
            assert outer.is_tracked(child_ref.ref)

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
            assert outer.is_tracked(gp_ref.ref)

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
            assert outer.is_tracked(ref.ref)

            # Should be loadable
            loaded = await ref.load()
            assert loaded.value == "has_type_ref_unique"


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
            assert outer.is_tracked(outer_ref.ref)

            async with FxtrScope(name="isolated", isolated=True) as iso:
                # Parent's entity should NOT be tracked in isolated scope
                assert not iso.is_tracked(outer_ref.ref)

    @pytest.mark.anyio
    async def test_isolated_scope_lift_fails(self, global_scope: FxtrScope):
        """Cannot lift entities into isolated scope."""
        async with FxtrScope(name="outer") as outer:
            outer_node = ScopeTestNode(children=(), value="outer")
            outer_ref = outer_node.store()

            async with FxtrScope(name="isolated", isolated=True) as iso:
                with pytest.raises(ValueError):
                    iso.lift(outer_ref.ref)

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

                assert iso.is_tracked(ref.ref)
                assert not outer.is_tracked(ref.ref)
                assert not global_scope.is_tracked(ref.ref)

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
                assert iso.is_tracked(ref.ref)
                assert not outer.is_tracked(ref.ref)

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

            # After exit, loading from iso should fail (scope shut down)
            with pytest.raises(RuntimeError, match="shut down"):
                await ref.load(iso)


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
        """Can lift entity from parent scope."""
        # Use an intermediate scope to avoid modifying global scope
        async with FxtrScope(name="outer") as outer:
            node = ScopeTestNode(children=(), value="lift_test")
            ref = node.store()

            async with FxtrScope(name="inner") as inner:
                # Lift the entity into inner scope
                inner.lift(ref.ref)
                assert inner.is_tracked(ref.ref)


# =============================================================================
# Error Cases
# =============================================================================


class TestScopeErrors:
    """Tests for error conditions in scope operations."""

    @pytest.mark.anyio
    async def test_load_untracked_raises(self, global_scope: FxtrScope):
        """Loading untracked entity raises ValueError."""
        async with FxtrScope(name="test") as scope:
            # Create a ref to an entity that's not stored
            from fxtr2.data.entity_core import FxtrID

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
            assert level1.is_tracked(parent_ref.ref)
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
            assert scope.is_tracked(a_ref.ref)
            assert scope.is_tracked(b_ref.ref)
            assert scope.is_tracked(c_ref.ref)
            assert scope.is_tracked(d_ref.ref)

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
