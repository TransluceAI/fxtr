"""Tests for store/load operations, deep load, copy operations, and error cases."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator

import pytest

from fxtr2.conversion.converters.mashumaro_entities import Child, ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_simple")):
    """A simple test entity."""

    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class TypeA(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_a")):
    """Test entity type A."""

    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class TypeB(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_b")):
    """Test entity type B."""

    label: str


@dataclasses.dataclass(frozen=True)
class EntityWithRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_with_ref")):
    """An entity with a reference to another entity."""

    name: str
    child: ChildRef[SimpleEntity]


@dataclasses.dataclass(frozen=True)
class EntityWithOptionalRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_opt_ref")):
    """An entity with an optional reference."""

    name: str
    child: ChildRef[SimpleEntity] | None = None


@dataclasses.dataclass(frozen=True)
class EntityWithListOfRefs(DataclassEntity, fxtr_type=FxtrID.builtin("test_list_refs")):
    """An entity with a list of references."""

    name: str
    children: list[ChildRef[SimpleEntity]]


@dataclasses.dataclass(frozen=True)
class ParentWithChild(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_child")):
    """Parent with inline child."""

    name: str
    child: Child[TypeA]


@dataclasses.dataclass(frozen=True)
class ParentWithRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_ref")):
    """Parent with child ref."""

    name: str
    ref: ChildRef[TypeA]


def track_test_builtins(scope: FxtrScope) -> None:
    """Track all builtin type IDs used in these tests."""
    scope.track_builtin(FxtrID.builtin("test_simple"))
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_b"))
    scope.track_builtin(FxtrID.builtin("test_with_ref"))
    scope.track_builtin(FxtrID.builtin("test_opt_ref"))
    scope.track_builtin(FxtrID.builtin("test_list_refs"))
    scope.track_builtin(FxtrID.builtin("test_parent_child"))
    scope.track_builtin(FxtrID.builtin("test_parent_ref"))


@pytest.fixture
async def serdes_scope() -> AsyncIterator[FxtrScope]:
    """Scope with test entity types pre-tracked and set as active."""
    async with FxtrScope(name="serdes_test") as scope:
        track_test_builtins(scope)
        yield scope


# =============================================================================
# Basic Store/Load Operations
# =============================================================================


class TestBasicStoreLoad:
    """Tests for basic store and load operations."""

    @pytest.mark.anyio
    async def test_store_and_load_simple(self):
        """Store and load a simple entity."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="hello", value=42)
            ref = scope.store(entity)
            assert isinstance(ref, EntityRef)
            assert ref.target_type is TypeA

            loaded = await ref.load()
            assert isinstance(loaded, TypeA)
            assert loaded.name == "hello"
            assert loaded.value == 42

    @pytest.mark.anyio
    async def test_store_via_entity_method(self):
        """Store using entity.store() method."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="method_store", value=7)
            ref = entity.store()
            loaded = await ref.load()
            assert loaded.name == "method_store"
            assert loaded.value == 7

    @pytest.mark.anyio
    async def test_store_and_load_different_types(self):
        """Store and load different entity types."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            a = TypeA(name="a", value=1)
            b = TypeB(label="b")
            ref_a = scope.store(a)
            ref_b = scope.store(b)
            assert (await ref_a.load()).name == "a"
            assert (await ref_b.load()).label == "b"

    @pytest.mark.anyio
    async def test_store_returns_entity_ref(self, serdes_scope: FxtrScope):
        """store() returns EntityRef."""
        entity = SimpleEntity(name="store_test", value=99)
        ref = entity.store()
        assert isinstance(ref, EntityRef)

    @pytest.mark.anyio
    async def test_store_uses_default_scope(self, serdes_scope: FxtrScope):
        """store() uses the active scope by default."""
        entity = SimpleEntity(name="test", value=1)
        # There's always a default scope, so this should work
        ref = entity.store()
        assert ref is not None
        assert serdes_scope.is_tracked(ref)

    @pytest.mark.anyio
    async def test_store_idempotent(self, serdes_scope: FxtrScope):
        """Storing same entity twice returns same ref ID."""
        entity = SimpleEntity(name="idempotent", value=1)
        ref1 = entity.store()
        ref2 = entity.store()
        assert ref1.entity_id == ref2.entity_id

    @pytest.mark.anyio
    async def test_store_with_explicit_type(self):
        """Store with explicit type using replace_target_type."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="explicit", value=1)
            ref = scope.store(entity).replace_target_type(TypeA)
            loaded = await ref.load()
            assert loaded.name == "explicit"


# =============================================================================
# Deep Load Operations
# =============================================================================


class TestDeepLoad:
    """Tests for deep_load functionality."""

    @pytest.mark.anyio
    async def test_deep_load_basic(self):
        """deep_load loads entities accessed via .val."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="deep", value=42)
            ref = scope.store(entity)

            # Load to populate cache, then use deep_load
            await ref.load()
            result = await scope.deep_load(lambda: ref.val)
            assert result.name == "deep"
            assert result.value == 42

    @pytest.mark.anyio
    async def test_deep_load_auto_loads_unloaded(self, serdes_scope: FxtrScope):
        """deep_load automatically loads unloaded refs."""
        entity = SimpleEntity(name="auto", value=1)
        ref = entity.store()

        # Create a fresh EntityRef that doesn't have a cached load
        fresh_ref = EntityRef[SimpleEntity](ref.entity_id, SimpleEntity)

        # deep_load should load it automatically
        result = await serdes_scope.deep_load(lambda: fresh_ref.val)
        assert result.name == "auto"

    @pytest.mark.anyio
    async def test_deep_load_nested(self):
        """deep_load should handle multiple rounds of lazy loading."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            child_entity = TypeA(name="child", value=1)
            parent_entity = ParentWithRef(name="parent", ref=child_entity.store())
            parent_ref = parent_entity.store()

            result = await scope.deep_load(lambda: parent_ref.val.ref.val)
            assert isinstance(result, TypeA)
            assert result.name == "child"


# =============================================================================
# Copy Operations
# =============================================================================


class TestCopyOperations:
    """Tests for copying entities between scopes."""

    @pytest.mark.anyio
    async def test_copy_between_scopes(self):
        """Copy entity from one scope to another."""
        async with FxtrScope(name="source", isolated=True) as source:
            track_test_builtins(source)
            with source.as_active_scope():
                entity = TypeA(name="copied", value=42)
                ref = entity.store()

            async with FxtrScope(name="dest", isolated=True) as dest:
                with source.as_active_scope():
                    await dest.copy_from_scope(ref, source)

                with dest.as_active_scope():
                    loaded = await ref.load()
                    assert loaded.name == "copied"

    @pytest.mark.anyio
    async def test_track_from_scope_basic(self):
        """track_from_scope transfers an in-memory entity between scopes."""
        async with FxtrScope(name="source", isolated=True) as source:
            track_test_builtins(source)
            with source.as_active_scope():
                entity = TypeA(name="tracked", value=99)
                ref = entity.store()

            async with FxtrScope(name="dest", isolated=True) as dest:
                track_test_builtins(dest)
                with source.as_active_scope():
                    dest.track_from_scope(ref, source)

                with dest.as_active_scope():
                    loaded = await ref.load()
                    assert loaded.name == "tracked"
                    assert loaded.value == 99

    @pytest.mark.anyio
    async def test_track_from_scope_with_children(self):
        """track_from_scope handles entities with child references."""
        async with FxtrScope(name="source", isolated=True) as source:
            track_test_builtins(source)
            with source.as_active_scope():
                child = TypeA(name="child", value=1)
                child_ref = child.store()
                parent = ParentWithRef(name="parent", ref=child_ref)
                parent_ref = parent.store()

            async with FxtrScope(name="dest", isolated=True) as dest:
                track_test_builtins(dest)
                with source.as_active_scope():
                    dest.track_from_scope(parent_ref, source)

                with dest.as_active_scope():
                    loaded = await parent_ref.load()
                    assert loaded.name == "parent"
                    loaded_child = await loaded.ref.load()
                    assert loaded_child.name == "child"

    @pytest.mark.anyio
    async def test_track_from_scope_survives_source_close(self):
        """After track_from_scope, entity survives source scope closing."""
        async with FxtrScope(name="dest", isolated=True) as dest:
            track_test_builtins(dest)

            async with FxtrScope(name="source", isolated=True) as source:
                track_test_builtins(source)
                with source.as_active_scope():
                    entity = TypeA(name="survive", value=7)
                    ref = entity.store()
                    dest.track_from_scope(ref, source)

            # Source scope is now closed.
            with dest.as_active_scope():
                loaded = await ref.load()
                assert loaded.name == "survive"

    @pytest.mark.anyio
    async def test_track_from_scope_copies_cache(self):
        """track_from_scope copies cache entries from the source scope."""
        async with FxtrScope(name="source", isolated=True) as source:
            track_test_builtins(source)
            with source.as_active_scope():
                entity = TypeA(name="cached", value=42)
                ref = entity.store()
                # Load to populate cache in source scope.
                await ref.load()

            async with FxtrScope(name="dest", isolated=True) as dest:
                track_test_builtins(dest)
                with source.as_active_scope():
                    dest.track_from_scope(ref, source)

                # Cache should be copied — verify by checking the scope's cache directly.
                # Cache is keyed on (data_id, prov_parent); the ref has no
                # provenance parent here, so the second tuple element is None.
                assert (ref.entity_id, None) in dest._ref_loaded_value_cache

    @pytest.mark.anyio
    async def test_track_from_scope_default_active(self):
        """track_from_scope defaults to the active scope as source."""
        async with FxtrScope(name="source", isolated=True) as source:
            track_test_builtins(source)
            with source.as_active_scope():
                entity = TypeA(name="default", value=0)
                ref = entity.store()

                async with FxtrScope(name="dest", isolated=True) as dest:
                    track_test_builtins(dest)
                    dest.track_from_scope(ref)

                    with dest.as_active_scope():
                        loaded = await ref.load()
                        assert loaded.name == "default"


# =============================================================================
# Error Cases
# =============================================================================


class TestErrorCases:
    """Tests for error conditions."""

    @pytest.mark.anyio
    async def test_load_untracked_entity_raises(self, serdes_scope: FxtrScope):
        """Raises error when loading entity that's not tracked."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"nonexistent")
        ref = EntityRef(fxtr_id, SimpleEntity)
        with pytest.raises(ValueError, match="not found"):
            await ref.load()

    @pytest.mark.anyio
    async def test_load_from_closed_scope_raises(self, serdes_scope: FxtrScope):
        """Raises RuntimeError when loading from a closed scope."""
        entity = SimpleEntity(name="test", value=1)

        # Create and close a scope
        async with FxtrScope(name="temp") as temp_scope:
            ref = entity.store()

        # temp_scope is now closed, trying to load should fail
        with pytest.raises(RuntimeError, match="has already been closed"):
            await ref.load(scope=temp_scope)


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegrationWorkflows:
    """Integration tests for full store/load workflows."""

    @pytest.mark.anyio
    async def test_full_store_load_cycle(self, serdes_scope: FxtrScope):
        """Create -> store -> load -> verify."""
        original = SimpleEntity(name="integration", value=12345)
        ref = original.store()
        loaded = await ref.load()

        assert loaded.name == original.name
        assert loaded.value == original.value

    @pytest.mark.anyio
    async def test_nested_entities_store_load(self, serdes_scope: FxtrScope):
        """Entity containing other entities."""
        # Create a chain of entities
        child = SimpleEntity(name="leaf", value=0)
        child_ref = child.store()

        parent = EntityWithRef(name="branch", child=child_ref)
        parent_ref = parent.store()

        # Load and verify
        loaded_parent = await parent_ref.load()
        assert loaded_parent.name == "branch"

        loaded_child = await loaded_parent.child.load()
        assert loaded_child.name == "leaf"

    @pytest.mark.anyio
    async def test_large_entity_graph(self, serdes_scope: FxtrScope):
        """Many interconnected entities."""
        # Create 20 entities
        entities = []
        refs = []
        for i in range(20):
            entity = SimpleEntity(name=f"entity_{i}", value=i)
            ref = entity.store()
            entities.append(entity)
            refs.append(ref)

        # Create container with all refs
        container = EntityWithListOfRefs(name="container", children=refs)
        container_ref = container.store()

        # Load and verify
        loaded = await container_ref.load()
        assert len(loaded.children) == 20

        for i, child_ref in enumerate(loaded.children):
            child = await child_ref.load()
            assert child.name == f"entity_{i}"
            assert child.value == i

    @pytest.mark.anyio
    async def test_entity_equality(self, serdes_scope: FxtrScope):
        """Equal entities produce equal serializations."""
        e1 = SimpleEntity(name="same", value=42)
        e2 = SimpleEntity(name="same", value=42)

        ref1 = e1.store()
        ref2 = e2.store()

        # Same content = same ID
        assert ref1.entity_id == ref2.entity_id
