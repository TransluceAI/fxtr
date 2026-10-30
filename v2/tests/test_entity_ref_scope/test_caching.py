"""Tests for entity caching behavior and side effect replay."""

from __future__ import annotations

import dataclasses
import typing
from typing import Any

import pytest

from fxtr2.conversion.converters.legacy_entity_abc import SelfLoadingLegacyEntity
from fxtr2.conversion.converters.mashumaro_entities import Child, ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityNotLoadedError, EntityRef, FxtrScope

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_simple")):
    """Simple immutable test entity."""

    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class TypeA(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_a")):
    """Test entity type A."""

    a_field: str


@dataclasses.dataclass(frozen=True)
class TypeB(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_b")):
    """Test entity type B."""

    b_field: int


@dataclasses.dataclass(frozen=True)
class MutableEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_mutable")):
    """A mutable entity (has mutable list field)."""

    items: list[str]

    def __hash__(self):
        raise TypeError("unhashable")


@dataclasses.dataclass(frozen=True)
class CachingTestEntityC(DataclassEntity, fxtr_type=FxtrID.builtin("test_caching_entity_c")):
    """Leaf entity for caching tests."""

    value: int


@dataclasses.dataclass(frozen=True)
class CachingTestEntityB(DataclassEntity, fxtr_type=FxtrID.builtin("test_caching_entity_b")):
    """Middle entity with ref for caching tests."""

    name: str
    ref_to_c: ChildRef[CachingTestEntityC]


@dataclasses.dataclass(frozen=True)
class CachingTestEntityA(DataclassEntity, fxtr_type=FxtrID.builtin("test_caching_entity_a")):
    """Root entity with child for caching tests."""

    name: str
    child_b: Child[CachingTestEntityB]


@dataclasses.dataclass(frozen=True)
class ParentWithChild(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_child")):
    """Parent with inline child."""

    name: str
    child: Child[SimpleEntity]


def track_test_builtins(scope: FxtrScope) -> None:
    """Track all builtin type IDs used in these tests."""
    scope.track_builtin(FxtrID.builtin("test_simple"))
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_b"))
    scope.track_builtin(FxtrID.builtin("test_mutable"))
    scope.track_builtin(FxtrID.builtin("test_caching_entity_a"))
    scope.track_builtin(FxtrID.builtin("test_caching_entity_b"))
    scope.track_builtin(FxtrID.builtin("test_caching_entity_c"))
    scope.track_builtin(FxtrID.builtin("test_parent_child"))


# =============================================================================
# Basic Caching Behavior
# =============================================================================


class TestBasicCaching:
    """Tests for basic entity caching behavior."""

    @pytest.mark.anyio
    async def test_immutable_entity_is_cached(self):
        """Immutable entities should return the same Python object on repeated loads."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = SimpleEntity(name="cached", value=99)
            ref = scope.store(entity)
            first = await ref.load()
            second = await ref.load()
            assert first is second  # Same object due to caching

    @pytest.mark.anyio
    async def test_mutable_entity_not_cached(self):
        """Mutable entities should not be cached (re-deserialized each time)."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = MutableEntity(items=["a", "b"])
            ref = scope.store(entity)
            first = await ref.load()
            second = await ref.load()
            assert first is not second
            assert first.items == second.items

    @pytest.mark.anyio
    async def test_same_entity_ref_shares_cached_entity(self):
        """Two EntityRefs with same (entity_id, target_type) share cached immutable entity."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            # Store an immutable entity
            entity = SimpleEntity(name="cached", value=42)
            ref1 = entity.store()

            # Load it once
            loaded1 = await ref1.load()

            # Create a second EntityRef to the same ID/type
            ref2 = EntityRef[SimpleEntity](ref1.entity_id, SimpleEntity)

            # Load it again
            loaded2 = await ref2.load()

            # Should be the exact same Python object (same ID)
            assert loaded1 is loaded2, "Cached immutable entities should have same Python ID"

    @pytest.mark.anyio
    async def test_union_and_concrete_type_share_cache(self):
        """EntityRef(x, Foo) and EntityRef(x, Foo|Bar) share cached entity."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)

            # Store a TypeA entity
            entity_a = TypeA(a_field="shared_cache")
            ref_a = entity_a.store()

            # Test concrete type first, then union
            concrete_ref = EntityRef[TypeA](ref_a.entity_id, TypeA)
            loaded_concrete = await concrete_ref.load()

            union_ref = EntityRef[TypeA | TypeB](ref_a.entity_id, TypeA | TypeB)
            loaded_union = await union_ref.load()

            assert loaded_concrete is loaded_union, "Concrete and union types should share cache"

            # Try the opposite order: union first, then concrete
            entity_b = TypeB(b_field=99)
            ref_b = entity_b.store()

            union_ref2 = EntityRef[TypeA | TypeB](ref_b.entity_id, TypeA | TypeB)
            loaded_union2 = await union_ref2.load()

            concrete_ref2 = EntityRef[TypeB](ref_b.entity_id, TypeB)
            loaded_concrete2 = await concrete_ref2.load()

            assert loaded_union2 is loaded_concrete2, "Union and concrete types should share cache"


# =============================================================================
# Cache Interaction with .val Property
# =============================================================================


class TestValProperty:
    """Tests for the .val property that accesses cached values."""

    @pytest.mark.anyio
    async def test_val_property_after_load(self):
        """After loading, .val should return the cached value synchronously."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = SimpleEntity(name="val_test", value=1)
            ref = scope.store(entity)
            await ref.load()
            assert ref.val.name == "val_test"

    @pytest.mark.anyio
    async def test_val_property_before_load_raises(self):
        """Before loading, .val should raise EntityNotLoadedError."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = SimpleEntity(name="no_load", value=1)
            ref = scope.store(entity)
            with pytest.raises(EntityNotLoadedError):
                _ = ref.val

    @pytest.mark.anyio
    async def test_clear_cache(self):
        """clear_cache should invalidate cached values."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = SimpleEntity(name="clear_me", value=1)
            ref = scope.store(entity)
            first = await ref.load()
            scope.clear_cache()
            with pytest.raises(EntityNotLoadedError):
                _ = ref.val
            second = await ref.load()
            assert first == second
            # After clearing cache, new load produces new object
            assert first is not second


# =============================================================================
# Side Effect Replay
# =============================================================================


class TestSideEffectReplay:
    """Tests for side effect replay when loading cached entities."""

    @pytest.mark.anyio
    async def test_cached_load_replays_side_effects(self):
        """When an immutable entity with Child is loaded from cache, side effects are replayed."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            child = SimpleEntity(name="side_effect_child", value=1)
            parent = ParentWithChild(name="parent_se", child=child)
            ref = parent.store()

            # Load once to cache
            first = await ref.load()
            assert first.child.name == "side_effect_child"

            # Load again — should use cache and replay side effects
            second = await ref.load()
            assert second is first  # Same cached object
            assert second.child.name == "side_effect_child"

    @pytest.mark.anyio
    async def test_side_effect_replay_tracks_children(self):
        """Loading a cached entity replays side-effect loads to track child entities."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)

            async with FxtrScope(name="inner") as inner_scope:
                # Create the nested structure: A -> B -> C
                c = CachingTestEntityC(value=100)
                c_ref = c.store()
                b = CachingTestEntityB(name="b", ref_to_c=c_ref)
                a = CachingTestEntityA(name="a", child_b=b)

                # Get the IDs for B and C
                b_id = b.store().entity_id
                c_id = c_ref.entity_id

                # Store A
                a_ref = a.store()

                # Move A out
                await a_ref.copy_to(scope)

            # B and C are not tracked in the outer scope
            assert not scope.is_tracked(b_id), "B should not be tracked in the outer scope"
            assert not scope.is_tracked(c_id), "C should not be tracked in the outer scope"

            # Load A - this should track both B and C (B is auto-loaded as Child, C is tracked when B loads)
            loaded_a = await a_ref.load()
            assert scope.is_tracked(a_ref.entity_id)

            # Both B and C should be tracked
            assert scope.is_tracked(b_id), "B should be tracked after loading A"
            assert scope.is_tracked(c_id), "C should be tracked after loading A"

            # Abandon B and C
            scope.abandon(b_id)
            scope.abandon(c_id)

            # Verify they're no longer tracked
            assert not scope.is_tracked(b_id), "B should not be tracked after abandoning"
            assert not scope.is_tracked(c_id), "C should not be tracked after abandoning"

            # Load A again from cache - should replay side effects and re-track B and C
            loaded_a2 = await a_ref.load()
            assert loaded_a2 is loaded_a, "Should use cached entity"
            assert scope.is_tracked(b_id), "B should be re-tracked after cache replay"
            assert scope.is_tracked(c_id), "C should be re-tracked after cache replay"


# =============================================================================
# Cache Interaction with Different Target Types
# =============================================================================


class TestCacheWithDifferentTargetTypes:
    """Tests for how caching interacts with different target type annotations."""

    @pytest.mark.anyio
    async def test_different_target_types_give_different_results(self):
        """EntityRef(x, Foo1) and EntityRef(x, Foo2) with different type instances give different results."""
        async with FxtrScope(name="test") as scope:

            # Define two different classes that could both deserialize the same CBOR
            # but produce different runtime types
            class StringValue(SelfLoadingLegacyEntity):
                """Entity that stores a value as string."""

                _TYPE_ID = scope.track_builtin(FxtrID.builtin("test_string_value"))

                def __init__(self, value: str):
                    self._value = value

                @property
                def value(self) -> str:
                    return self._value

                def _to_fxtr_cbor(self, scope: Any) -> FxtrCbor:
                    return {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
                        "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                        "value": self._value,
                    }

                @classmethod
                def _from_fxtr_cbor(
                    cls,
                    cbor: FxtrCbor,
                    annotated_type: Any | None,
                    scope: Any,
                ) -> typing.Self:
                    assert isinstance(cbor, dict)
                    return cls(value=typing.cast(Any, cbor)["value"])

                @classmethod
                def _fxtr_type_id(cls) -> FxtrID:
                    return cls._TYPE_ID

                def _fxtr_immutable(self) -> bool:
                    return True

            class IntWrapper(SelfLoadingLegacyEntity):
                """Entity that stores an int internally but serializes as string."""

                _TYPE_ID = scope.track_builtin(FxtrID.builtin("test_string_value"))

                def __init__(self, value: int):
                    self._value = value

                @property
                def value(self) -> int:
                    return self._value

                def _to_fxtr_cbor(self, scope: Any) -> FxtrCbor:
                    return {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
                        "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                        "value": str(self._value),  # Store int as string
                    }

                @classmethod
                def _from_fxtr_cbor(
                    cls,
                    cbor: FxtrCbor,
                    annotated_type: Any | None,
                    scope: Any,
                ) -> typing.Self:
                    assert isinstance(cbor, dict)
                    return cls(value=int(typing.cast(Any, cbor)["value"]))  # Parse string to int

                @classmethod
                def _fxtr_type_id(cls) -> FxtrID:
                    return cls._TYPE_ID

                def _fxtr_immutable(self) -> bool:
                    return True

            # Store with StringValue type
            str_entity = StringValue(value="42")
            ref = scope.store(str_entity)

            # Load as StringValue
            ref_str = EntityRef[StringValue](ref.entity_id, StringValue)
            loaded_str = await ref_str.load()

            # Load as IntWrapper
            ref_int = EntityRef[IntWrapper](ref.entity_id, IntWrapper)
            loaded_int = await ref_int.load()

            # Should be different Python objects
            assert (
                loaded_str is not loaded_int
            ), "Different target types should give different objects"

            # But should have compatible data
            assert isinstance(loaded_str, StringValue)
            assert loaded_str.value == "42"
            assert isinstance(loaded_int, IntWrapper)
            assert loaded_int.value == 42
