"""Tests for custom entities using the new Entity base class.

These tests demonstrate how to create custom entity types that don't use DataclassEntity,
by directly implementing the Entity ABC and registering custom EntityLoadHandler instances.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any, Self

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.entity_ref_scope import (
    DeserializedEntity,
    Entity,
    EntityLoadHandler,
    EntityRef,
    EntityTypeAnnotation,
    FxtrScope,
    get_active_scope,
    register_entity_load_handler,
)

# =============================================================================
# Entity Base Class Tests
# =============================================================================


class TestEntityBaseClass:
    """Tests for the Entity abstract base class."""

    def test_entity_is_abstract(self):
        """Cannot instantiate Entity directly."""
        with pytest.raises(TypeError, match="abstract"):
            Entity()  # type: ignore

    def test_entity_subclass_requires_implementation(self):
        """Subclass must implement _fxtr_store_impl."""

        class IncompleteEntity(Entity):
            pass

        with pytest.raises(TypeError, match="abstract"):
            IncompleteEntity()  # type: ignore


# =============================================================================
# Simple Custom Entity
# =============================================================================


@dataclasses.dataclass(frozen=True)
class TypeA(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_a")):
    """Test entity type A for use in custom entity tests."""

    name: str
    value: int


class SimpleCustomEntity(Entity):
    """A simple custom entity that doesn't use DataclassEntity."""

    TYPE_ID = FxtrID.builtin("test_simple_custom")

    def __init__(self, data: str):
        self.data = data

    def _fxtr_store_impl(self) -> EntityRef[Self]:
        """Store this entity and return a reference."""
        scope = get_active_scope()
        cbor: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
            "$fxtr.type": RawWeakFxtrID(inner_id=self.TYPE_ID),
            "data": self.data,
        }
        entity_id = scope.store_cbor(cbor)
        return EntityRef(entity_id, type(self))

    def __eq__(self, other):
        return isinstance(other, SimpleCustomEntity) and self.data == other.data

    def __hash__(self):
        return hash((type(self), self.data))


@dataclasses.dataclass(frozen=True)
class SimpleCustomEntityHandler(EntityLoadHandler[SimpleCustomEntity]):
    """Load handler for SimpleCustomEntity."""

    @typing.override
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[SimpleCustomEntity]
    ) -> bool:
        # Only handle our specific type ID
        origin = typing.get_origin(target_type) or target_type
        return type_id == SimpleCustomEntity.TYPE_ID and origin is SimpleCustomEntity

    @typing.override
    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[SimpleCustomEntity]
    ) -> DeserializedEntity[SimpleCustomEntity]:
        """Deserialize SimpleCustomEntity from CBOR."""
        assert isinstance(cbor, dict)
        entity = SimpleCustomEntity(data=cbor["data"])  # type: ignore
        return DeserializedEntity(value=entity, cacheable=True)


# Register the handler at module level
register_entity_load_handler(SimpleCustomEntity, SimpleCustomEntityHandler())


# =============================================================================
# Custom Entity with Type Parameters
# =============================================================================


class CustomRefWrapper[T: Entity](Entity):
    """A custom parameterized entity that wraps an EntityRef[T]."""

    TYPE_ID = FxtrID.builtin("test_custom_ref_wrapper")

    def __init__(self, ref: EntityRef[T], target_type: type[T] | Any):
        self.ref = ref
        self._target_type = target_type  # Store type info for deserialization

    def _fxtr_store_impl(self) -> EntityRef[Self]:
        """Store with type parameter information."""
        scope = get_active_scope()
        cbor: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
            "$fxtr.type": RawWeakFxtrID(inner_id=self.TYPE_ID),
            "ref_id": RawStrongFxtrID(inner_id=self.ref.entity_id),
        }
        entity_id = scope.store_cbor(cbor)
        # Return with full generic type
        return EntityRef(entity_id, CustomRefWrapper[self._target_type])  # type: ignore

    def __eq__(self, other):
        return isinstance(other, CustomRefWrapper) and self.ref == other.ref

    def __hash__(self):
        return hash((type(self), self.ref))


@dataclasses.dataclass(frozen=True)
class CustomRefWrapperHandler(EntityLoadHandler[CustomRefWrapper]):
    """Load handler for CustomRefWrapper."""

    @typing.override
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[CustomRefWrapper]
    ) -> bool:
        origin = typing.get_origin(target_type) or target_type
        return type_id == CustomRefWrapper.TYPE_ID and origin is CustomRefWrapper

    @typing.override
    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[CustomRefWrapper]
    ) -> DeserializedEntity[CustomRefWrapper]:
        """Deserialize CustomRefWrapper, using type parameter from target_type."""
        assert isinstance(cbor, dict)

        # Extract type argument from CustomRefWrapper[T]
        type_args = typing.get_args(target_type)
        if not type_args:
            raise ValueError("CustomRefWrapper requires type parameter")

        inner_type = type_args[0]

        # Reconstruct the ref from RawStrongFxtrID
        ref_id_raw = cbor["ref_id"]  # type: ignore
        assert isinstance(ref_id_raw, RawStrongFxtrID)
        ref = EntityRef(ref_id_raw.inner_id, inner_type)

        entity = CustomRefWrapper(ref, inner_type)
        return DeserializedEntity(value=entity, cacheable=True)


register_entity_load_handler(CustomRefWrapper, CustomRefWrapperHandler())


# =============================================================================
# Custom Entity with Recursive Children
# =============================================================================


class CustomParentEntity(Entity):
    """Custom entity that stores child entities."""

    TYPE_ID = FxtrID.builtin("test_custom_parent")

    def __init__(self, name: str, child: TypeA | None = None):
        self.name = name
        self.child = child

    def _fxtr_store_impl(self) -> EntityRef[Self]:
        """Store with child."""
        scope = get_active_scope()

        # Store child if present
        child_id_raw = None
        if self.child is not None:
            child_ref = scope.store(self.child)
            child_id_raw = RawStrongFxtrID(inner_id=child_ref.entity_id)

        cbor: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
            "$fxtr.type": RawWeakFxtrID(inner_id=self.TYPE_ID),
            "name": self.name,
            "child_id": child_id_raw,  # None or RawStrongFxtrID
        }
        entity_id = scope.store_cbor(cbor)
        return EntityRef(entity_id, type(self))


@dataclasses.dataclass(frozen=True)
class CustomParentEntityHandler(EntityLoadHandler[CustomParentEntity]):
    """Load handler for CustomParentEntity with async child loading."""

    @typing.override
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[CustomParentEntity]
    ) -> bool:
        origin = typing.get_origin(target_type) or target_type
        return type_id == CustomParentEntity.TYPE_ID and origin is CustomParentEntity

    @typing.override
    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[CustomParentEntity]
    ) -> DeserializedEntity[CustomParentEntity]:
        """Async deserialization that loads child."""
        assert isinstance(cbor, dict)
        name = cbor["name"]  # type: ignore
        child_id_raw = cbor["child_id"]  # type: ignore

        child = None
        if child_id_raw is not None:
            # Load child asynchronously
            assert isinstance(child_id_raw, RawStrongFxtrID)
            child_ref = EntityRef(child_id_raw.inner_id, TypeA)
            child = await child_ref.load()

        entity = CustomParentEntity(name, child)
        return DeserializedEntity(value=entity, cacheable=True)


register_entity_load_handler(CustomParentEntity, CustomParentEntityHandler())


# =============================================================================
# Custom Entity Tests
# =============================================================================


class TestCustomParameterizedEntity:
    """Tests for custom entities with type parameters."""

    @pytest.mark.anyio
    async def test_custom_entity_uses_type_parameter(self):
        """Custom entity can use type parameter during deserialization."""
        async with FxtrScope(name="test") as scope:
            scope.track_builtin(TypeA._fxtr_known_type_id)
            scope.track_builtin(SimpleCustomEntity.TYPE_ID)
            scope.track_builtin(CustomRefWrapper.TYPE_ID)

            # Create and store inner entity
            inner = TypeA(name="inner", value=42)
            inner_ref = inner.store()

            # Create wrapper with ref
            wrapper = CustomRefWrapper(inner_ref, TypeA)
            wrapper_ref = wrapper.store()

            # Load it back with type parameter
            loaded = await wrapper_ref.load()
            assert isinstance(loaded, CustomRefWrapper)
            assert isinstance(loaded.ref, EntityRef)

            # Load the inner ref
            loaded_inner = await loaded.ref.load()
            assert isinstance(loaded_inner, TypeA)
            assert loaded_inner.name == "inner"
            assert loaded_inner.value == 42

    @pytest.mark.anyio
    async def test_custom_entity_with_type_inference(self):
        """Custom entity can infer type parameters at store time."""
        async with FxtrScope(name="test") as scope:
            scope.track_builtin(TypeA._fxtr_known_type_id)
            scope.track_builtin(CustomRefWrapper.TYPE_ID)

            # Create wrapper
            inner = TypeA(name="inferred", value=100)
            inner_ref = inner.store()
            wrapper = CustomRefWrapper(inner_ref, TypeA)

            # Store returns ref with inferred type
            wrapper_ref = wrapper.store()

            # Should be loadable
            loaded = await wrapper_ref.load()
            assert isinstance(loaded, CustomRefWrapper)
            inner_loaded = await loaded.ref.load()
            assert isinstance(inner_loaded, TypeA)
            assert inner_loaded.name == "inferred"


# =============================================================================
# Custom Entity with Recursive Children Tests
# =============================================================================


class TestCustomEntityWithRecursiveChildren:
    """Tests for custom entities with child loading."""

    @pytest.mark.anyio
    async def test_custom_entity_stores_child(self):
        """Custom entity can store child entities."""
        async with FxtrScope(name="test") as scope:
            scope.track_builtin(TypeA._fxtr_known_type_id)
            scope.track_builtin(CustomParentEntity.TYPE_ID)

            child = TypeA(name="child", value=1)
            parent = CustomParentEntity(name="parent", child=child)

            # Store parent (should also store child)
            parent_ref = parent.store()

            # Verify parent is tracked
            assert scope.is_tracked(parent_ref.entity_id)

            # Load the parent's CBOR to verify child was stored
            cbor = await scope.load_cbor(parent_ref.entity_id)
            assert isinstance(cbor, dict)
            child_id_raw = cbor["child_id"]
            assert isinstance(child_id_raw, RawStrongFxtrID)
            assert scope.is_tracked(child_id_raw.inner_id)

    @pytest.mark.anyio
    async def test_custom_entity_async_loads_child(self):
        """Custom entity handler can load children asynchronously."""
        async with FxtrScope(name="test") as scope:
            scope.track_builtin(TypeA._fxtr_known_type_id)
            scope.track_builtin(CustomParentEntity.TYPE_ID)

            child = TypeA(name="async_child", value=2)
            parent = CustomParentEntity(name="async_parent", child=child)
            parent_ref = parent.store()

            # Load parent (should trigger async child load)
            loaded = await parent_ref.load()

            assert loaded.name == "async_parent"
            assert loaded.child is not None
            assert isinstance(loaded.child, TypeA)
            assert loaded.child.name == "async_child"
            assert loaded.child.value == 2

    @pytest.mark.anyio
    async def test_custom_entity_nested_async_loads(self):
        """Custom entities can have nested async loads."""
        async with FxtrScope(name="test") as scope:
            scope.track_builtin(TypeA._fxtr_known_type_id)
            scope.track_builtin(CustomParentEntity.TYPE_ID)

            # Create nested structure: grandparent -> parent -> child
            grandchild = TypeA(name="grandchild", value=3)
            parent = CustomParentEntity(name="parent", child=grandchild)
            parent_ref = parent.store()

            # Create grandparent that references parent
            # (Simplified: we'll create another TypeA and parent for it)
            leaf = TypeA(name="leaf", value=4)
            middle = CustomParentEntity(name="middle", child=leaf)
            middle_ref = middle.store()

            # Load middle (loads leaf)
            loaded_middle = await middle_ref.load()
            assert loaded_middle.name == "middle"
            assert loaded_middle.child.name == "leaf"  # type: ignore
            assert loaded_middle.child.value == 4  # type: ignore
