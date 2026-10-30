"""Tests for fxtr2/data/entity_serdes.py."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.entity_serdes import (
    Child,
    ChildRef,
    DataclassEntity,
    DeferredLoadPlaceholder,
    Entity,
    EntityRef,
    LinkedEntity,
    build_converter_for,
    deep_load,
    store_entity,
)
from fxtr2.data.fxtr_scope import ACTIVE_SCOPE, FxtrScope

# =============================================================================
# Test Entity Classes
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_simple")):
    """A simple test entity."""

    name: str
    value: int


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
class EntityWithBytes(DataclassEntity, fxtr_type=FxtrID.builtin("test_bytes")):
    """An entity with binary data."""

    name: str
    data: bytes


@dataclasses.dataclass(frozen=True)
class EntityWithDict(DataclassEntity, fxtr_type=FxtrID.builtin("test_dict")):
    """An entity with a dict field."""

    name: str
    metadata: dict[str, Any]


# Helper to track all test entity type builtins
def track_test_entity_types(scope: FxtrScope) -> None:
    """Track all builtin types used by test entities."""
    scope.track_builtin(FxtrID.builtin("test_simple"))
    scope.track_builtin(FxtrID.builtin("test_with_ref"))
    scope.track_builtin(FxtrID.builtin("test_opt_ref"))
    scope.track_builtin(FxtrID.builtin("test_list_refs"))
    scope.track_builtin(FxtrID.builtin("test_bytes"))
    scope.track_builtin(FxtrID.builtin("test_dict"))
    scope.track_builtin(FxtrID.builtin("test_primitive"))
    scope.track_builtin(FxtrID.builtin("test_list"))


@pytest.fixture
async def serdes_scope() -> AsyncIterator[FxtrScope]:
    """Scope with test entity types pre-tracked and set as active."""
    async with FxtrScope(name="serdes_test") as scope:
        track_test_entity_types(scope)
        yield scope


# =============================================================================
# Entity Base Class
# =============================================================================


class TestEntityBaseClass:
    """Tests for the Entity abstract base class."""

    def test_entity_is_abstract(self):
        """Cannot instantiate Entity directly."""
        with pytest.raises(TypeError):
            Entity()  # type: ignore

    def test_entity_subclass_requires_methods(self):
        """Subclass must implement abstract methods."""

        class IncompleteEntity(Entity):
            pass

        with pytest.raises(TypeError):
            IncompleteEntity()  # type: ignore


# =============================================================================
# DataclassEntity Basics
# =============================================================================


class TestDataclassEntityBasics:
    """Tests for DataclassEntity basic functionality."""

    @pytest.mark.anyio
    async def test_dataclass_entity_creation(self, serdes_scope: FxtrScope):
        """Can create DataclassEntity subclass."""
        entity = SimpleEntity(name="test", value=42)
        assert entity.name == "test"
        assert entity.value == 42

    @pytest.mark.anyio
    async def test_dataclass_entity_frozen(self, serdes_scope: FxtrScope):
        """Entity is frozen (immutable)."""
        entity = SimpleEntity(name="test", value=42)
        with pytest.raises(dataclasses.FrozenInstanceError):
            entity.name = "modified"  # type: ignore

    @pytest.mark.anyio
    async def test_dataclass_entity_type_registration(self, serdes_scope: FxtrScope):
        """Type is registered for deserialization."""
        entity = SimpleEntity(name="test", value=42)
        assert entity._fxtr_type_id() == FxtrID.builtin("test_simple")

    @pytest.mark.anyio
    async def test_dataclass_entity_fields(self, serdes_scope: FxtrScope):
        """Fields are preserved in serialization."""
        entity = SimpleEntity(name="test_name", value=123)
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert cbor["name"] == "test_name"
        assert cbor["value"] == 123

    @pytest.mark.anyio
    async def test_dataclass_entity_has_fxtr_type(self, serdes_scope: FxtrScope):
        """Entity has fxtr_type attribute."""
        entity = SimpleEntity(name="test", value=42)
        assert entity.fxtr_type == FxtrID.builtin("test_simple")


# =============================================================================
# Entity Serialization (to_fxtr_cbor)
# =============================================================================


class TestEntitySerialization:
    """Tests for entity serialization to FxtrCbor."""

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_basic(self, serdes_scope: FxtrScope):
        """Simple entity serializes to FxtrCbor."""
        entity = SimpleEntity(name="test", value=42)
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert isinstance(cbor, dict)

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_has_ctx(self, serdes_scope: FxtrScope):
        """Result has $fxtr.ctx field."""
        entity = SimpleEntity(name="test", value=42)
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert "$fxtr.ctx" in cbor
        assert isinstance(cbor["$fxtr.ctx"], RawFxtrEntityMarkerID)

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_has_type(self, serdes_scope: FxtrScope):
        """Result has $fxtr.type field."""
        entity = SimpleEntity(name="test", value=42)
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert "$fxtr.type" in cbor
        assert isinstance(cbor["$fxtr.type"], (RawWeakFxtrID, RawStrongFxtrID))
        assert cbor["$fxtr.type"].inner_id == FxtrID.builtin("test_simple")

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_primitive_fields(self, serdes_scope: FxtrScope):
        """int, str, bool, float fields serialize correctly."""

        @dataclasses.dataclass(frozen=True)
        class PrimitiveEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_primitive")):
            int_field: int
            str_field: str
            bool_field: bool
            float_field: float

        entity = PrimitiveEntity(int_field=42, str_field="hello", bool_field=True, float_field=3.14)
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert cbor["int_field"] == 42
        assert cbor["str_field"] == "hello"
        assert cbor["bool_field"] is True
        assert cbor["float_field"] == 3.14

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_list_fields(self, serdes_scope: FxtrScope):
        """List fields serialize correctly."""

        @dataclasses.dataclass(frozen=True)
        class ListEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_list")):
            items: list[int]

        entity = ListEntity(items=[1, 2, 3])
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert cbor["items"] == [1, 2, 3]

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_dict_fields(self, serdes_scope: FxtrScope):
        """Dict fields serialize correctly."""
        entity = EntityWithDict(name="test", metadata={"key": "value", "number": 42})
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        assert cbor["metadata"] == {"key": "value", "number": 42}

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_bytes_field(self, serdes_scope: FxtrScope):
        """Bytes fields serialize correctly (as base64 by mashumaro)."""
        entity = EntityWithBytes(name="test", data=b"\x00\x01\x02\xff")
        cbor = typing.cast(Any, entity._to_fxtr_cbor())
        # mashumaro serializes bytes as base64 strings by default.
        # Users can customize this (e.g., to use raw CBOR bytes) but that's
        # outside the scope of these tests - we just verify the default behavior.
        import base64

        assert base64.b64decode(cbor["data"]) == b"\x00\x01\x02\xff"


# =============================================================================
# Entity Deserialization (from_fxtr_cbor)
# =============================================================================


class TestEntityDeserialization:
    """Tests for entity deserialization from FxtrCbor."""

    @pytest.mark.anyio
    async def test_from_fxtr_cbor_basic(self, serdes_scope: FxtrScope):
        """Deserialize simple entity."""
        cbor: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b"")),
            "$fxtr.type": RawWeakFxtrID(inner_id=FxtrID.builtin("test_simple")),
            "name": "test",
            "value": 42,
        }
        entity = SimpleEntity._from_fxtr_cbor(cbor, None)
        assert entity.name == "test"
        assert entity.value == 42

    @pytest.mark.anyio
    async def test_from_fxtr_cbor_roundtrip(self, serdes_scope: FxtrScope):
        """to_fxtr_cbor -> from_fxtr_cbor roundtrips."""
        original = SimpleEntity(name="roundtrip", value=100)
        cbor = original._to_fxtr_cbor()
        restored = SimpleEntity._from_fxtr_cbor(cbor, None)
        assert restored.name == original.name
        assert restored.value == original.value


# =============================================================================
# EntityRef
# =============================================================================


class TestEntityRef:
    """Tests for EntityRef class."""

    @pytest.mark.anyio
    async def test_entity_ref_creation(self, serdes_scope: FxtrScope):
        """Create EntityRef from FxtrID."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"test_data")

        # Use build_converter_for to get a proper async converter
        converter = build_converter_for(SimpleEntity)

        ref = EntityRef[SimpleEntity](fxtr_id, converter)
        assert ref is not None

    @pytest.mark.anyio
    async def test_entity_ref_ref_property(self, serdes_scope: FxtrScope):
        """ref attribute returns the FxtrID."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"test_data")

        converter = build_converter_for(SimpleEntity)

        ref = EntityRef[SimpleEntity](fxtr_id, converter)
        assert ref.ref == fxtr_id

    @pytest.mark.anyio
    async def test_entity_ref_load(self, serdes_scope: FxtrScope):
        """await ref.load() returns entity."""
        # Store an entity
        entity = SimpleEntity(name="loadable", value=42)
        ref = entity.store()

        # Load it back
        loaded = await ref.load()
        assert loaded.name == "loadable"
        assert loaded.value == 42

    @pytest.mark.anyio
    async def test_entity_ref_load_requires_tracked_id(self, serdes_scope: FxtrScope):
        """load() raises if ID is not tracked in scope."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"nonexistent_entity")

        converter = build_converter_for(SimpleEntity)

        ref = EntityRef[SimpleEntity](fxtr_id, converter)

        # Try to load an entity that's not tracked - should fail
        with pytest.raises(ValueError, match="not found"):
            await ref.load()

    @pytest.mark.anyio
    async def test_entity_ref_equality(self, serdes_scope: FxtrScope):
        """EntityRefs with same ID are equal."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"same")

        converter = build_converter_for(SimpleEntity)

        ref1 = EntityRef[SimpleEntity](fxtr_id, converter)
        ref2 = EntityRef[SimpleEntity](fxtr_id, converter)
        assert ref1 == ref2

    @pytest.mark.anyio
    async def test_entity_ref_hash(self, serdes_scope: FxtrScope):
        """EntityRefs can be used as dict keys."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"hashable")

        converter = build_converter_for(SimpleEntity)

        ref = EntityRef[SimpleEntity](fxtr_id, converter)
        d = {ref: "value"}
        assert d[ref] == "value"


# =============================================================================
# Entity.store()
# =============================================================================


class TestEntityStore:
    """Tests for Entity.store() method."""

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
        assert ref1.ref == ref2.ref


# =============================================================================
# ACTIVE_SCOPE Context Variable
# =============================================================================


class TestActiveScope:
    """Tests for ACTIVE_SCOPE context variable."""

    def test_active_scope_has_default(self):
        """There is always a default scope."""
        scope = ACTIVE_SCOPE.get()
        assert scope is not None
        assert isinstance(scope, FxtrScope)

    @pytest.mark.anyio
    async def test_scope_nesting(self):
        """Scopes can be nested and the active scope is updated."""
        outer_scope = ACTIVE_SCOPE.get()
        async with FxtrScope(name="inner") as inner_scope:
            assert ACTIVE_SCOPE.get() is inner_scope
            assert ACTIVE_SCOPE.get() is not outer_scope
        # After exiting, we're back to the outer scope
        assert ACTIVE_SCOPE.get() is outer_scope

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
# Type Discrimination
# =============================================================================


class TestTypeDiscrimination:
    """Tests for type discrimination during deserialization."""

    @pytest.mark.anyio
    async def test_multiple_entity_types(self, serdes_scope: FxtrScope):
        """Multiple DataclassEntity subclasses can coexist."""
        simple = SimpleEntity(name="simple", value=1)
        with_bytes = EntityWithBytes(name="bytes", data=b"data")

        ref1 = simple.store()
        ref2 = with_bytes.store()

        loaded1 = await ref1.load()
        loaded2 = await ref2.load()

        assert isinstance(loaded1, SimpleEntity)
        assert isinstance(loaded2, EntityWithBytes)

    @pytest.mark.anyio
    async def test_type_discrimination_on_load(self, serdes_scope: FxtrScope):
        """Correct type instantiated on load."""
        entity = SimpleEntity(name="discrimination", value=42)
        ref = entity.store()
        loaded = await ref.load()
        assert type(loaded) is SimpleEntity

    @pytest.mark.anyio
    async def test_type_id_uniqueness(self, serdes_scope: FxtrScope):
        """Each type has unique ID."""
        assert SimpleEntity._fxtr_type_id() != EntityWithBytes._fxtr_type_id()
        assert SimpleEntity._fxtr_type_id() != EntityWithDict._fxtr_type_id()


# =============================================================================
# Complex Field Types
# =============================================================================


class TestComplexFieldTypes:
    """Tests for entities with complex field types."""

    @pytest.mark.anyio
    async def test_entity_with_entity_ref_field(self, serdes_scope: FxtrScope):
        """Field of type EntityRef[T]."""
        child = SimpleEntity(name="child", value=1)
        child_ref = child.store()

        parent = EntityWithRef(name="parent", child=child_ref)
        parent_ref = parent.store()

        loaded = await parent_ref.load()
        assert loaded.name == "parent"

        # Load child
        loaded_child = await loaded.child.load()
        assert loaded_child.name == "child"

    @pytest.mark.anyio
    async def test_entity_with_list_of_refs(self, serdes_scope: FxtrScope):
        """Field of type list[EntityRef[T]]."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        parent = EntityWithListOfRefs(name="parent", children=[ref1, ref2])
        parent_ref = parent.store()

        loaded = await parent_ref.load()
        assert len(loaded.children) == 2

        # Load children
        c1 = await loaded.children[0].load()
        c2 = await loaded.children[1].load()
        assert c1.name == "child1"
        assert c2.name == "child2"

    @pytest.mark.anyio
    async def test_entity_with_optional_ref_none(self, serdes_scope: FxtrScope):
        """Field of type Optional[EntityRef[T]] with None value."""
        entity = EntityWithOptionalRef(name="no_child", child=None)
        ref = entity.store()

        loaded = await ref.load()
        assert loaded.child is None

    @pytest.mark.anyio
    async def test_entity_with_optional_ref_some(self, serdes_scope: FxtrScope):
        """Field of type Optional[EntityRef[T]] with Some value."""
        child = SimpleEntity(name="child", value=42)
        child_ref = child.store()

        entity = EntityWithOptionalRef(name="has_child", child=child_ref)
        ref = entity.store()

        loaded = await ref.load()
        assert loaded.child is not None
        child_loaded = await loaded.child.load()
        assert child_loaded.name == "child"

    @pytest.mark.anyio
    async def test_entity_with_bytes_field(self, serdes_scope: FxtrScope):
        """Field of type bytes."""
        entity = EntityWithBytes(name="binary", data=b"\x00\x01\x02\xff")
        ref = entity.store()

        loaded = await ref.load()
        assert loaded.data == b"\x00\x01\x02\xff"


# =============================================================================
# Error Cases
# =============================================================================


class TestErrorCases:
    """Tests for error conditions."""

    @pytest.mark.anyio
    async def test_load_untracked_entity_raises(self, serdes_scope: FxtrScope):
        """Raises error when loading entity that's not tracked."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"nonexistent")

        converter = build_converter_for(SimpleEntity)

        ref = EntityRef[SimpleEntity](fxtr_id, converter)
        with pytest.raises(ValueError, match="not found"):
            await ref.load()

    @pytest.mark.anyio
    async def test_load_from_closed_scope_raises(self, serdes_scope: FxtrScope):
        """Raises RuntimeError when loading from a closed scope."""
        entity = SimpleEntity(name="test", value=1)

        # Create and close a scope
        async with FxtrScope(name="temp") as temp_scope:
            ref = entity.store()
            # Store is in temp_scope

        # temp_scope is now closed, trying to load should fail
        with pytest.raises(RuntimeError, match="shut down"):
            await ref.load(scope=temp_scope)


# =============================================================================
# deep_load
# =============================================================================


class TestDeepLoad:
    """Tests for deep_load function."""

    @pytest.mark.anyio
    async def test_deep_load_basic(self, serdes_scope: FxtrScope):
        """deep_load loads entities accessed via .val."""
        entity = SimpleEntity(name="deep", value=42)
        ref = entity.store()

        # First load to populate cache
        await ref.load()

        # Now deep_load should work
        result = await deep_load(lambda: ref.val)
        assert result.name == "deep"

    @pytest.mark.anyio
    async def test_deep_load_loads_unloaded(self, serdes_scope: FxtrScope):
        """deep_load automatically loads unloaded refs."""
        entity = SimpleEntity(name="auto", value=1)
        ref = entity.store()

        # Clear cache
        ref.clear_cache()

        # deep_load should load it
        result = await deep_load(lambda: ref.val)
        assert result.name == "auto"


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for full workflows."""

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
        assert ref1.ref == ref2.ref

    @pytest.mark.anyio
    async def test_immutable_entity_caching(self, serdes_scope: FxtrScope):
        """Immutable entities are cached after load."""
        entity = SimpleEntity(name="cached", value=1)
        ref = entity.store()

        # First load
        loaded1 = await ref.load()
        # Second load should use cache (same object for immutable entities)
        loaded2 = await ref.load()

        # Should be same instance due to caching
        assert loaded1 is loaded2


# =============================================================================
# Type Parameter Preservation Through Roundtrips
# =============================================================================

# Additional imports for these tests
import typing

from fxtr2.data.entity_serdes import build_converter_for, preserve_orig_class, store_entity

# --- Test entity types for type parameter preservation ---


@dataclasses.dataclass(frozen=True)
class TypeA(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_a")):
    """First type in a union."""

    a_field: str


@dataclasses.dataclass(frozen=True)
class TypeB(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_b")):
    """Second type in a union."""

    b_field: int


@dataclasses.dataclass(frozen=True)
class TypeC(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_c")):
    """Third type in a union."""

    c_field: float


@preserve_orig_class
@dataclasses.dataclass(frozen=True)
class WrapperWithRef[T: Entity](DataclassEntity, fxtr_type=FxtrID.builtin("test_wrapper_ref")):
    """A wrapper containing an EntityRef whose type is parameterized."""

    name: str
    ref: ChildRef[T]


@dataclasses.dataclass(frozen=True)
class EntityWithUnionRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_union_ref")):
    """An entity containing an EntityRef to a union of types."""

    name: str
    child: ChildRef[TypeA | TypeB]


# Helper to track additional test entity types
def track_type_param_test_types(scope: FxtrScope) -> None:
    """Track builtin types used by type parameter preservation tests."""
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_b"))
    scope.track_builtin(FxtrID.builtin("test_type_c"))
    scope.track_builtin(FxtrID.builtin("test_wrapper_ref"))
    scope.track_builtin(FxtrID.builtin("test_union_ref"))
    scope.track_builtin(FxtrID.builtin("test_custom_entity"))


@pytest.fixture
async def type_param_scope() -> AsyncIterator[FxtrScope]:
    """Scope with type parameter test entity types pre-tracked."""
    async with FxtrScope(name="type_param_test") as scope:
        track_test_entity_types(scope)
        track_type_param_test_types(scope)
        yield scope


class TestTypeParameterPreservation:
    """Tests for preserving type parameters through store/load roundtrips."""

    @pytest.mark.anyio
    async def test_entity_ref_to_concrete_type_loads_correctly(self, type_param_scope: FxtrScope):
        """EntityRef[ConcreteType] loads and deserializes as the concrete type."""
        child = TypeA(a_field="child_value")
        child_ref = child.store()

        # Verify the ref knows how to load as TypeA
        loaded = await child_ref.load()
        assert isinstance(loaded, TypeA)
        assert loaded.a_field == "child_value"

    @pytest.mark.anyio
    async def test_entity_ref_union_loads_correct_type_a(self, type_param_scope: FxtrScope):
        """EntityRef[TypeA | TypeB] loads TypeA correctly based on discriminator."""
        # Store a TypeA
        child_a = TypeA(a_field="i_am_a")
        child_ref = child_a.store()

        # Create entity with union-typed ref
        parent = EntityWithUnionRef(name="parent_a", child=child_ref)
        parent_ref = parent.store()

        # Load parent and then child
        loaded_parent = await parent_ref.load()
        loaded_child = await loaded_parent.child.load()

        # Should load as TypeA specifically
        assert isinstance(loaded_child, TypeA), f"Expected TypeA, got {type(loaded_child)}"
        assert loaded_child.a_field == "i_am_a"

    @pytest.mark.anyio
    async def test_entity_ref_union_loads_correct_type_b(self, type_param_scope: FxtrScope):
        """EntityRef[TypeA | TypeB] loads TypeB correctly based on discriminator."""
        # Store a TypeB
        child_b = TypeB(b_field=42)
        child_ref = child_b.store()

        # Create entity with union-typed ref
        parent = EntityWithUnionRef(name="parent_b", child=child_ref)
        parent_ref = parent.store()

        # Load parent and then child
        loaded_parent = await parent_ref.load()
        loaded_child = await loaded_parent.child.load()

        # Should load as TypeB specifically
        assert isinstance(loaded_child, TypeB), f"Expected TypeB, got {type(loaded_child)}"
        assert loaded_child.b_field == 42

    @pytest.mark.anyio
    @pytest.mark.parametrize("use_explicit_type", [True, False], ids=["explicit", "orig_class"])
    async def test_entity_ref_type_var_propagates_from_outer_type_a(
        self, type_param_scope: FxtrScope, use_explicit_type: bool
    ):
        """EntityRef[T] where T is outer entity's type var uses the bound type.

        WrapperWithRef[TypeA] has field ref: EntityRef[T], when T=TypeA the ref
        should load as TypeA.
        """
        inner = TypeA(a_field="nested")
        inner_ref = inner.store()

        if use_explicit_type:
            # Create wrapper and store with explicit type
            wrapper = WrapperWithRef(name="wrapper", ref=inner_ref)
            wrapper_ref = store_entity(wrapper, as_type=WrapperWithRef[TypeA])
        else:
            # Create wrapper via generic alias (should set __orig_class__)
            wrapper = WrapperWithRef[TypeA](name="wrapper", ref=inner_ref)
            if not hasattr(wrapper, "__orig_class__"):
                pytest.skip("__orig_class__ was not set by generic alias")
            wrapper_ref = wrapper.store()

        # Load wrapper
        loaded_wrapper = await wrapper_ref.load()
        assert loaded_wrapper.name == "wrapper"

        # Load the inner ref - should be TypeA
        loaded_inner = await loaded_wrapper.ref.load()
        assert isinstance(loaded_inner, TypeA), f"Expected TypeA, got {type(loaded_inner)}"
        assert loaded_inner.a_field == "nested"

    @pytest.mark.anyio
    @pytest.mark.parametrize("use_explicit_type", [True, False], ids=["explicit", "orig_class"])
    async def test_entity_ref_type_var_propagates_from_outer_type_b(
        self, type_param_scope: FxtrScope, use_explicit_type: bool
    ):
        """Same WrapperWithRef but bound to TypeB instead."""
        inner = TypeB(b_field=99)
        inner_ref = inner.store()

        if use_explicit_type:
            # Create wrapper and store with explicit type
            wrapper = WrapperWithRef(name="wrapper_b", ref=inner_ref)
            wrapper_ref = store_entity(wrapper, as_type=WrapperWithRef[TypeB])
        else:
            # Create wrapper via generic alias (should set __orig_class__)
            wrapper = WrapperWithRef[TypeB](name="wrapper_b", ref=inner_ref)
            if not hasattr(wrapper, "__orig_class__"):
                pytest.skip("__orig_class__ was not set by generic alias")
            wrapper_ref = wrapper.store()

        # Load wrapper
        loaded_wrapper = await wrapper_ref.load()

        # Load the inner ref - should be TypeB
        loaded_inner = await loaded_wrapper.ref.load()
        assert isinstance(loaded_inner, TypeB), f"Expected TypeB, got {type(loaded_inner)}"
        assert loaded_inner.b_field == 99


class TestCustomParameterizedEntity:
    """Tests for custom Entity classes (not DataclassEntity) with type parameters."""

    @pytest.mark.anyio
    async def test_custom_entity_uses_type_parameter_in_from_fxtr_cbor(
        self, type_param_scope: FxtrScope
    ):
        """Custom Entity with _from_fxtr_cbor that uses its type parameter.

        Tests that a custom (non-DataclassEntity) parameterized entity can use the
        annotated_type parameter to properly deserialize its fields. Uses EntityRef[T]
        as the wrapped type to test the plumbing.
        """

        # Define a custom parameterized entity (not using DataclassEntity)
        class CustomRefWrapper[T: Entity](Entity):
            """A custom entity that wraps an EntityRef[T] and uses the type parameter."""

            _TYPE_ID = FxtrID.builtin("test_custom_entity")

            def __init__(self, ref: EntityRef[T]):
                self.ref = ref

            def __eq__(self, other):
                return type(other) is type(self) and self.ref == other.ref

            def __hash__(self):
                return hash((type(self), self.ref))

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                # Serialize the EntityRef as a strong-linked ID directly
                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "ref": RawStrongFxtrID(inner_id=self.ref.ref),
                }

            @classmethod
            def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> typing.Self:
                # ref_cbor is a RawStrongFxtrID
                ref_cbor = typing.cast(Any, cbor)["ref"]
                assert isinstance(ref_cbor, RawStrongFxtrID)
                fxtr_id = ref_cbor.inner_id

                # Use annotated_type to determine what converter to use for the ref
                if annotated_type is not None:
                    type_args = typing.get_args(annotated_type)
                    if type_args:
                        inner_type = type_args[0]  # T from CustomRefWrapper[T]
                        # Build converter for T and create EntityRef[T]
                        converter = build_converter_for(inner_type)
                        ref = EntityRef(fxtr_id, converter)
                        return cls(ref)

                # Fallback: can't build a proper converter without type info
                raise ValueError("CustomRefWrapper requires annotated_type to deserialize")

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        # Create an inner entity and store it
        inner = TypeA(a_field="custom_inner")
        inner_ref = inner.store()

        # Create wrapper with the ref
        wrapper = CustomRefWrapper[TypeA](inner_ref)

        # Store with explicit type
        ref = store_entity(wrapper, as_type=CustomRefWrapper[TypeA])

        # Load it back
        loaded = await ref.load()

        # The wrapper should be deserialized correctly
        assert isinstance(loaded, CustomRefWrapper)
        assert isinstance(loaded.ref, EntityRef)

        # Load the inner ref - should be TypeA
        loaded_inner = await loaded.ref.load()
        assert isinstance(loaded_inner, TypeA), f"Expected TypeA, got {type(loaded_inner)}"
        assert loaded_inner.a_field == "custom_inner"


class TestUnionOfMixedEntityTypes:
    """Tests for loading unions that contain different kinds of Entity implementations."""

    @pytest.mark.anyio
    async def test_union_converter_with_dataclass_entities(self, type_param_scope: FxtrScope):
        """build_converter_for handles union of DataclassEntity types."""
        # Build a converter for a union type
        converter = build_converter_for(TypeA | TypeB | TypeC)

        # Create and serialize different types
        a = TypeA(a_field="a")
        b = TypeB(b_field=1)
        c = TypeC(c_field=3.14)

        a_cbor = a._to_fxtr_cbor()
        b_cbor = b._to_fxtr_cbor()
        c_cbor = c._to_fxtr_cbor()

        # Converter should produce the right types based on discriminator
        # Converters are now async and take scope as second argument
        loaded_a = await converter(a_cbor, type_param_scope)
        loaded_b = await converter(b_cbor, type_param_scope)
        loaded_c = await converter(c_cbor, type_param_scope)

        assert isinstance(loaded_a, TypeA)
        assert loaded_a.a_field == "a"

        assert isinstance(loaded_b, TypeB)
        assert loaded_b.b_field == 1

        assert isinstance(loaded_c, TypeC)
        assert loaded_c.c_field == 3.14

    @pytest.mark.anyio
    async def test_store_and_load_via_union_converter(self, type_param_scope: FxtrScope):
        """Store different entities and load via union-typed refs."""
        # Store TypeA
        a = TypeA(a_field="union_a")
        a_ref = a.store()

        # Store TypeB
        b = TypeB(b_field=123)
        b_ref = b.store()

        # Create refs with union type converter
        union_converter = build_converter_for(TypeA | TypeB)

        # Create union-typed refs manually
        union_ref_a = EntityRef[TypeA | TypeB](a_ref.ref, union_converter)
        union_ref_b = EntityRef[TypeA | TypeB](b_ref.ref, union_converter)

        # Load and verify types
        loaded_a = await union_ref_a.load()
        loaded_b = await union_ref_b.load()

        assert isinstance(loaded_a, TypeA)
        assert loaded_a.a_field == "union_a"

        assert isinstance(loaded_b, TypeB)
        assert loaded_b.b_field == 123

    @pytest.mark.anyio
    async def test_union_with_custom_non_mashumaro_entity(self, type_param_scope: FxtrScope):
        """build_converter_for handles union containing a custom (non-DataclassEntity) type."""

        # Define a custom entity that doesn't use mashumaro/DataclassEntity
        class CustomSimpleEntity(Entity):
            """A custom entity with simple serialization, not using mashumaro."""

            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_custom_simple"))

            def __init__(self, value: str):
                self._value = value

            @property
            def value(self) -> str:
                return self._value

            def __eq__(self, other):
                return type(other) is type(self) and self._value == other._value

            def __hash__(self):
                return hash((type(self), self._value))

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "value": self._value,
                }

            @classmethod
            def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> typing.Self:
                assert isinstance(cbor, dict)
                return cls(value=typing.cast(Any, cbor)["value"])

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        # Build a converter for a union that includes both DataclassEntity and custom Entity
        converter = build_converter_for(TypeA | CustomSimpleEntity | TypeB)

        # Create instances of each type
        type_a = TypeA(a_field="from_a")
        custom = CustomSimpleEntity(value="from_custom")
        type_b = TypeB(b_field=42)

        # Serialize to CBOR
        a_cbor = type_a._to_fxtr_cbor()
        custom_cbor = custom._to_fxtr_cbor()
        b_cbor = type_b._to_fxtr_cbor()

        # Converter should dispatch correctly based on $fxtr.type discriminator
        # Converters are now async and take scope as second argument
        loaded_a = await converter(a_cbor, type_param_scope)
        loaded_custom = await converter(custom_cbor, type_param_scope)
        loaded_b = await converter(b_cbor, type_param_scope)

        # Verify DataclassEntity types
        assert isinstance(loaded_a, TypeA)
        assert loaded_a.a_field == "from_a"

        assert isinstance(loaded_b, TypeB)
        assert loaded_b.b_field == 42

        # Verify custom Entity type
        assert isinstance(loaded_custom, CustomSimpleEntity)
        assert loaded_custom.value == "from_custom"

    @pytest.mark.anyio
    async def test_store_load_union_with_custom_entity(self, type_param_scope: FxtrScope):
        """Store and load different entity types via union-typed EntityRef."""

        # Same custom entity as above (defined locally for test isolation)
        class CustomSimpleEntity(Entity):
            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_custom_simple_v2"))

            def __init__(self, value: str):
                self._value = value

            @property
            def value(self) -> str:
                return self._value

            def __eq__(self, other):
                return type(other) is type(self) and self._value == other._value

            def __hash__(self):
                return hash((type(self), self._value))

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "value": self._value,
                }

            @classmethod
            def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> typing.Self:
                assert isinstance(cbor, dict)
                return cls(value=typing.cast(Any, cbor)["value"])

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        # Store a DataclassEntity
        type_a = TypeA(a_field="stored_a")
        a_ref = type_a.store()

        # Store a custom Entity
        custom = CustomSimpleEntity(value="stored_custom")
        custom_ref = store_entity(custom)

        # Build union converter and create union-typed refs
        union_converter = build_converter_for(TypeA | CustomSimpleEntity)
        union_ref_a = EntityRef[TypeA | CustomSimpleEntity](a_ref.ref, union_converter)
        union_ref_custom = EntityRef[TypeA | CustomSimpleEntity](custom_ref.ref, union_converter)

        # Load via union-typed refs
        loaded_a = await union_ref_a.load()
        loaded_custom = await union_ref_custom.load()

        # Verify correct types loaded
        assert isinstance(loaded_a, TypeA)
        assert loaded_a.a_field == "stored_a"

        assert isinstance(loaded_custom, CustomSimpleEntity)
        assert loaded_custom.value == "stored_custom"


# =============================================================================
# Custom Entity with Recursive Children (Part 1 Core Changes)
# =============================================================================


class TestCustomEntityWithRecursiveChildren:
    """Tests for custom Entity implementations that store/load child entities."""

    @pytest.mark.anyio
    async def test_custom_entity_stores_child_in_to_fxtr_cbor(self, type_param_scope: FxtrScope):
        """Custom Entity can store child entities in _to_fxtr_cbor using ACTIVE_SCOPE."""

        class ParentWithChild(Entity):
            """A custom entity that stores a child entity during serialization."""

            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_parent_with_child"))

            def __init__(self, name: str, child: TypeA):
                self.name = name
                self.child = child

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                # Store child into active scope (which is guaranteed to be set by caller)
                child_ref = store_entity(self.child)
                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "name": self.name,
                    "child": RawStrongFxtrID(inner_id=child_ref.ref),
                }

            @classmethod
            def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> typing.Self:
                # Simple sync deserialization - doesn't load child
                assert isinstance(cbor, dict)
                child_id = typing.cast(Any, cbor)["child"].inner_id
                # Just create a placeholder - this test focuses on serialization
                return cls(name=cbor["name"], child=TypeA(a_field="placeholder"))

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        # Create parent with child
        child = TypeA(a_field="child_value")
        parent = ParentWithChild(name="parent", child=child)

        # Store parent - this should also store child
        ref = store_entity(parent)

        # Verify parent is stored
        assert type_param_scope.is_tracked(ref)

        # Load the CBOR and check that child was stored as a ref
        cbor = await type_param_scope.load_cbor(ref.ref)
        assert isinstance(cbor, dict)
        assert "child" in cbor
        child_raw = cbor["child"]
        assert isinstance(child_raw, RawStrongFxtrID)

        # Verify the child entity is also tracked
        assert type_param_scope.is_tracked(child_raw.inner_id)

    @pytest.mark.anyio
    async def test_custom_entity_async_from_fxtr_cbor_loads_children(
        self, type_param_scope: FxtrScope
    ):
        """Custom Entity with async _from_fxtr_cbor can load child entities."""

        class ParentWithAsyncLoad(Entity):
            """A custom entity that loads a child entity during deserialization."""

            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_parent_async_load"))

            def __init__(self, name: str, child: TypeA):
                self.name = name
                self.child = child

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                child_ref = store_entity(self.child)
                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "name": self.name,
                    "child": RawStrongFxtrID(inner_id=child_ref.ref),
                }

            @classmethod
            async def _from_fxtr_cbor(
                cls, cbor: FxtrCbor, annotated_type: Any | None
            ) -> typing.Self:
                """Async deserialization that loads child entity."""
                assert isinstance(cbor, dict)
                child_id = typing.cast(Any, cbor)["child"].inner_id

                # Build ref and load child from active scope
                child_ref = EntityRef[TypeA](child_id, build_converter_for(TypeA))
                child = await child_ref.load()

                return cls(name=cbor["name"], child=child)

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        # Create and store parent with child
        child = TypeA(a_field="async_child_value")
        parent = ParentWithAsyncLoad(name="async_parent", child=child)
        ref = store_entity(parent)

        # Load parent - this should trigger async _from_fxtr_cbor and load child
        loaded = await ref.load()

        assert loaded.name == "async_parent"
        assert isinstance(loaded.child, TypeA)
        assert loaded.child.a_field == "async_child_value"

    @pytest.mark.anyio
    async def test_custom_entity_nested_async_loads(self, type_param_scope: FxtrScope):
        """Custom Entity can have nested async loads (A contains B contains C)."""

        class LeafEntity(Entity):
            """Leaf entity with no children."""

            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_leaf"))

            def __init__(self, value: int):
                self.value = value

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "value": self.value,
                }

            @classmethod
            def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> typing.Self:
                return cls(value=typing.cast(Any, cbor)["value"])

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        class MiddleEntity(Entity):
            """Middle entity that contains a LeafEntity."""

            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_middle"))

            def __init__(self, name: str, leaf: LeafEntity):
                self.name = name
                self.leaf = leaf

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                leaf_ref = store_entity(self.leaf)
                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "name": self.name,
                    "leaf": RawStrongFxtrID(inner_id=leaf_ref.ref),
                }

            @classmethod
            async def _from_fxtr_cbor(
                cls, cbor: FxtrCbor, annotated_type: Any | None
            ) -> typing.Self:
                leaf_id = typing.cast(Any, cbor)["leaf"].inner_id
                leaf_ref = EntityRef[LeafEntity](leaf_id, build_converter_for(LeafEntity))
                leaf = await leaf_ref.load()
                return cls(name=cbor["name"], leaf=leaf)

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        class RootEntity(Entity):
            """Root entity that contains a MiddleEntity."""

            _TYPE_ID = type_param_scope.track_builtin(FxtrID.builtin("test_root"))

            def __init__(self, name: str, middle: MiddleEntity):
                self.name = name
                self.middle = middle

            def _to_fxtr_cbor(self) -> FxtrCbor:
                from fxtr2.data.fxtr_scope import get_active_context_marker

                middle_ref = store_entity(self.middle)
                return {
                    "$fxtr.ctx": get_active_context_marker(),
                    "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
                    "name": self.name,
                    "middle": RawStrongFxtrID(inner_id=middle_ref.ref),
                }

            @classmethod
            async def _from_fxtr_cbor(
                cls, cbor: FxtrCbor, annotated_type: Any | None
            ) -> typing.Self:
                middle_id = typing.cast(Any, cbor)["middle"].inner_id
                middle_ref = EntityRef[MiddleEntity](middle_id, build_converter_for(MiddleEntity))
                middle = await middle_ref.load()
                return cls(name=cbor["name"], middle=middle)

            @classmethod
            def _fxtr_type_id(cls) -> FxtrID:
                return cls._TYPE_ID

            def _fxtr_immutable(self) -> bool:
                return True

        # Create nested structure: Root -> Middle -> Leaf
        leaf = LeafEntity(value=42)
        middle = MiddleEntity(name="middle", leaf=leaf)
        root = RootEntity(name="root", middle=middle)

        # Store root (which stores middle and leaf)
        ref = store_entity(root)

        # Load root (which loads middle and leaf via async _from_fxtr_cbor)
        loaded = await ref.load()

        assert loaded.name == "root"
        assert loaded.middle.name == "middle"
        assert loaded.middle.leaf.value == 42


# =============================================================================
# Inline Entity Types: Child[T] and LinkedEntity[T] (Part 2)
# =============================================================================


# Test entity types for inline entity tests
@dataclasses.dataclass(frozen=True)
class LeafEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_leaf_entity")):
    """A simple leaf entity for inline tests."""

    value: int


@dataclasses.dataclass(frozen=True)
class ParentWithChild(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_with_child")):
    """A parent entity with a Child[T] field."""

    name: str
    child: Child[LeafEntity]


@dataclasses.dataclass(frozen=True)
class ParentWithLinkedEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_with_linked")):
    """A parent entity with a LinkedEntity[T] field."""

    name: str
    linked: LinkedEntity[LeafEntity]


@dataclasses.dataclass(frozen=True)
class ParentWithMixedRefs(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_with_mixed")):
    """A parent entity with both inline and lazy refs."""

    name: str
    child: Child[LeafEntity]  # Auto-loaded
    lazy: ChildRef[LeafEntity]  # Must call .load()


@dataclasses.dataclass(frozen=True)
class DeepNestingParent(DataclassEntity, fxtr_type=FxtrID.builtin("test_deep_nesting")):
    """A parent entity that contains another parent with a child."""

    name: str
    nested: Child[ParentWithChild]


def track_inline_entity_types(scope: FxtrScope) -> None:
    """Track builtin types used by inline entity tests."""
    scope.track_builtin(FxtrID.builtin("test_leaf_entity"))
    scope.track_builtin(FxtrID.builtin("test_parent_with_child"))
    scope.track_builtin(FxtrID.builtin("test_parent_with_linked"))
    scope.track_builtin(FxtrID.builtin("test_parent_with_mixed"))
    scope.track_builtin(FxtrID.builtin("test_deep_nesting"))


@pytest.fixture
async def inline_entity_scope() -> AsyncIterator[FxtrScope]:
    """Scope with inline entity test types pre-tracked."""
    async with FxtrScope(name="inline_entity_test") as scope:
        track_test_entity_types(scope)
        track_type_param_test_types(scope)
        track_inline_entity_types(scope)
        yield scope


class TestInlineEntityChild:
    """Tests for Child[T] type alias."""

    @pytest.mark.anyio
    async def test_child_basic_store_load(self, inline_entity_scope: FxtrScope):
        """Basic store and load of entity with Child[T] field."""
        leaf = LeafEntity(value=42)
        parent = ParentWithChild(name="parent", child=leaf)

        # Store parent (child should be stored automatically)
        ref = parent.store()

        # Load parent (child should be loaded automatically)
        loaded = await ref.load()

        assert loaded.name == "parent"
        assert isinstance(loaded.child, LeafEntity)
        assert loaded.child.value == 42

    @pytest.mark.anyio
    async def test_child_creates_strong_link(self, inline_entity_scope: FxtrScope):
        """Child[T] should create a strong link (RawStrongFxtrID) in CBOR."""
        leaf = LeafEntity(value=1)
        parent = ParentWithChild(name="test", child=leaf)

        ref = parent.store()
        cbor = await inline_entity_scope.load_cbor(ref.ref)

        # Check that child is stored as a strong link
        assert isinstance(cbor, dict)
        assert "child" in cbor
        child_value = cbor["child"]
        # After serialization through mashumaro, it should be RawStrongFxtrID
        assert isinstance(child_value, RawStrongFxtrID)


class TestInlineEntityLinked:
    """Tests for LinkedEntity[T] type alias."""

    @pytest.mark.anyio
    async def test_linked_entity_basic_store_load(self, inline_entity_scope: FxtrScope):
        """Basic store and load of entity with LinkedEntity[T] field."""
        leaf = LeafEntity(value=99)
        parent = ParentWithLinkedEntity(name="linked_parent", linked=leaf)

        ref = parent.store()
        loaded = await ref.load()

        assert loaded.name == "linked_parent"
        assert isinstance(loaded.linked, LeafEntity)
        assert loaded.linked.value == 99

    @pytest.mark.anyio
    async def test_linked_entity_creates_weak_link(self, inline_entity_scope: FxtrScope):
        """LinkedEntity[T] should create a weak link (RawWeakFxtrID) in CBOR."""
        leaf = LeafEntity(value=1)
        parent = ParentWithLinkedEntity(name="test", linked=leaf)

        ref = parent.store()
        cbor = await inline_entity_scope.load_cbor(ref.ref)

        # Check that linked is stored as a weak link
        assert isinstance(cbor, dict)
        assert "linked" in cbor
        linked_value = cbor["linked"]
        assert isinstance(linked_value, RawWeakFxtrID)


class TestInlineEntityMixed:
    """Tests for entities with both inline and lazy refs."""

    @pytest.mark.anyio
    async def test_mixed_inline_and_lazy(self, inline_entity_scope: FxtrScope):
        """Entity with both Child[T] (inline) and ChildRef[T] (lazy) fields."""
        child_leaf = LeafEntity(value=1)
        lazy_leaf = LeafEntity(value=2)

        parent = ParentWithMixedRefs(
            name="mixed",
            child=child_leaf,
            lazy=lazy_leaf.store(),
        )

        ref = parent.store()
        loaded = await ref.load()

        # Child should be auto-loaded
        assert loaded.name == "mixed"
        assert isinstance(loaded.child, LeafEntity)
        assert loaded.child.value == 1

        # Lazy should be an EntityRef that needs explicit loading
        assert isinstance(loaded.lazy, EntityRef)
        lazy_loaded = await loaded.lazy.load()
        assert isinstance(lazy_loaded, LeafEntity)
        assert lazy_loaded.value == 2


class TestInlineEntityNested:
    """Tests for nested inline entities."""

    @pytest.mark.anyio
    async def test_nested_inline_entities(self, inline_entity_scope: FxtrScope):
        """Entity A contains Child[B] where B contains Child[C]."""
        leaf = LeafEntity(value=42)
        inner = ParentWithChild(name="inner", child=leaf)
        outer = DeepNestingParent(name="outer", nested=inner)

        ref = outer.store()
        loaded = await ref.load()

        # All levels should be loaded
        assert loaded.name == "outer"
        assert isinstance(loaded.nested, ParentWithChild)
        assert loaded.nested.name == "inner"
        assert isinstance(loaded.nested.child, LeafEntity)
        assert loaded.nested.child.value == 42


class TestInlineEntityPlaceholder:
    """Tests for DeferredLoadPlaceholder handling."""

    @pytest.mark.anyio
    async def test_placeholder_type_exists(self, inline_entity_scope: FxtrScope):
        """DeferredLoadPlaceholder should be importable and have expected structure."""
        placeholder = DeferredLoadPlaceholder(index=5)
        assert placeholder.index == 5

    @pytest.mark.anyio
    async def test_post_init_with_placeholder(self, inline_entity_scope: FxtrScope):
        """Entity with __post_init__ that handles DeferredLoadPlaceholder."""

        @dataclasses.dataclass(frozen=True)
        class ValidatedParent(DataclassEntity, fxtr_type=FxtrID.builtin("test_validated_parent")):
            """A parent that validates child in __post_init__."""

            name: str
            child: Child[LeafEntity]

            def __post_init__(self):
                super().__post_init__()  # Must call parent's __post_init__
                # Skip validation during collecting phase (placeholder values)
                if isinstance(self.child, DeferredLoadPlaceholder):
                    return
                # Actual validation when child is loaded
                if self.child.value < 0:
                    raise ValueError("Child value must be non-negative")

        inline_entity_scope.track_builtin(FxtrID.builtin("test_validated_parent"))

        # Create and store valid parent
        leaf = LeafEntity(value=42)
        parent = ValidatedParent(name="validated", child=leaf)
        ref = parent.store()

        # Load should work (and post_init validation passes)
        loaded = await ref.load()
        assert loaded.name == "validated"
        assert loaded.child.value == 42


class TestInlineEntityErrorHandling:
    """Tests for error handling during inline entity loading."""

    @pytest.mark.anyio
    async def test_successful_inline_load(self, inline_entity_scope: FxtrScope):
        """Verify that inline entity loading works correctly (baseline for error tests)."""
        # This test verifies the happy path works
        leaf = LeafEntity(value=42)
        parent = ParentWithChild(name="success", child=leaf)
        ref = parent.store()

        # Load should succeed
        loaded = await ref.load()
        assert loaded.name == "success"
        assert loaded.child.value == 42

    @pytest.mark.anyio
    async def test_multiple_children_parallel_load(self, inline_entity_scope: FxtrScope):
        """Multiple inline children are loaded in parallel."""

        @dataclasses.dataclass(frozen=True)
        class MultiChildParent(
            DataclassEntity, fxtr_type=FxtrID.builtin("test_multi_child_parent")
        ):
            """A parent with multiple inline children."""

            name: str
            first: Child[LeafEntity]
            second: Child[LeafEntity]
            third: Child[LeafEntity]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_multi_child_parent"))

        # Create parent with multiple children
        parent = MultiChildParent(
            name="multi",
            first=LeafEntity(value=1),
            second=LeafEntity(value=2),
            third=LeafEntity(value=3),
        )
        ref = parent.store()

        # Load and verify all children are loaded
        loaded = await ref.load()
        assert loaded.name == "multi"
        assert loaded.first.value == 1
        assert loaded.second.value == 2
        assert loaded.third.value == 3


class TestInlineEntityGeneric:
    """Tests for generic entities with inline children."""

    @pytest.mark.anyio
    async def test_generic_entity_with_child(self, inline_entity_scope: FxtrScope):
        """Generic Wrapper[T] where the wrapped value is a Child[T]."""

        @preserve_orig_class
        @dataclasses.dataclass(frozen=True)
        class GenericWrapper[T: Entity](
            DataclassEntity, fxtr_type=FxtrID.builtin("test_generic_wrapper_child")
        ):
            """A generic wrapper with an inline child."""

            name: str
            child: Child[T]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_generic_wrapper_child"))

        # Create wrapper with TypeA child
        inner = TypeA(a_field="generic_child")
        wrapper = GenericWrapper[TypeA](name="wrapper", child=inner)
        ref = store_entity(wrapper, as_type=GenericWrapper[TypeA])

        # Load and verify
        loaded = await ref.load()
        assert loaded.name == "wrapper"
        assert isinstance(loaded.child, TypeA)
        assert loaded.child.a_field == "generic_child"

    @pytest.mark.anyio
    async def test_generic_entity_with_multiple_children(self, inline_entity_scope: FxtrScope):
        """Generic Wrapper[T] with multiple inline children of type T."""

        @preserve_orig_class
        @dataclasses.dataclass(frozen=True)
        class MultiChildWrapper[T: Entity](
            DataclassEntity, fxtr_type=FxtrID.builtin("test_multi_child_wrapper")
        ):
            """A wrapper with multiple inline children."""

            name: str
            first: Child[T]
            second: Child[T]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_multi_child_wrapper"))

        # Create wrapper with two TypeB children
        child1 = TypeB(b_field=1)
        child2 = TypeB(b_field=2)
        wrapper = MultiChildWrapper[TypeB](name="multi", first=child1, second=child2)
        ref = store_entity(wrapper, as_type=MultiChildWrapper[TypeB])

        # Load and verify both children are loaded
        loaded = await ref.load()
        assert loaded.name == "multi"
        assert isinstance(loaded.first, TypeB)
        assert loaded.first.b_field == 1
        assert isinstance(loaded.second, TypeB)
        assert loaded.second.b_field == 2

    @pytest.mark.anyio
    async def test_generic_entity_with_list_of_children(self, inline_entity_scope: FxtrScope):
        """Generic Wrapper with list[Child[T]] field."""

        @preserve_orig_class
        @dataclasses.dataclass(frozen=True)
        class ListChildWrapper[T: Entity](
            DataclassEntity, fxtr_type=FxtrID.builtin("test_list_child_wrapper")
        ):
            """A wrapper with a list of inline children."""

            name: str
            children: list[Child[T]]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_list_child_wrapper"))

        # Create wrapper with list of TypeA children
        children = [TypeA(a_field=f"child_{i}") for i in range(3)]
        wrapper = ListChildWrapper[TypeA](name="list_wrapper", children=children)
        ref = store_entity(wrapper, as_type=ListChildWrapper[TypeA])

        # Load and verify all children are loaded
        loaded = await ref.load()
        assert loaded.name == "list_wrapper"
        assert len(loaded.children) == 3
        for i, child in enumerate(loaded.children):
            assert isinstance(child, TypeA)
            assert child.a_field == f"child_{i}"


# =============================================================================
# Collection Types: set, frozenset, dict, frozendict
# =============================================================================

from frozendict import frozendict


@dataclasses.dataclass(frozen=True)
class EntityWithSet(DataclassEntity, fxtr_type=FxtrID.builtin("test_set")):
    """An entity with a set field."""

    name: str
    items: set[str]


@dataclasses.dataclass(frozen=True)
class EntityWithFrozenset(DataclassEntity, fxtr_type=FxtrID.builtin("test_frozenset")):
    """An entity with a frozenset field."""

    name: str
    items: frozenset[str]


@dataclasses.dataclass(frozen=True)
class EntityWithFrozendict(DataclassEntity, fxtr_type=FxtrID.builtin("test_frozendict")):
    """An entity with a frozendict field."""

    name: str
    mapping: frozendict[str, int]


@dataclasses.dataclass(frozen=True)
class EntityWithRefSet(DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_set")):
    """An entity with a set of EntityRefs."""

    name: str
    refs: set[EntityRef[SimpleEntity]]


@dataclasses.dataclass(frozen=True)
class EntityWithRefFrozenset(DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_frozenset")):
    """An entity with a frozenset of EntityRefs."""

    name: str
    refs: frozenset[EntityRef[SimpleEntity]]


@dataclasses.dataclass(frozen=True)
class EntityWithFxtrIDSet(DataclassEntity, fxtr_type=FxtrID.builtin("test_fxtrid_set")):
    """An entity with a set of FxtrIDs."""

    name: str
    ids: set[FxtrID]


@dataclasses.dataclass(frozen=True)
class EntityWithFxtrIDFrozenset(DataclassEntity, fxtr_type=FxtrID.builtin("test_fxtrid_frozenset")):
    """An entity with a frozenset of FxtrIDs."""

    name: str
    ids: frozenset[FxtrID]


@dataclasses.dataclass(frozen=True)
class EntityWithRefKeyDict(DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_key_dict")):
    """An entity with EntityRef keys in a dict."""

    name: str
    mapping: dict[EntityRef[SimpleEntity], str]


@dataclasses.dataclass(frozen=True)
class EntityWithRefKeyFrozendict(
    DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_key_frozendict")
):
    """An entity with EntityRef keys in a frozendict."""

    name: str
    mapping: frozendict[EntityRef[SimpleEntity], str]


@dataclasses.dataclass(frozen=True)
class EntityWithFxtrIDKeyDict(DataclassEntity, fxtr_type=FxtrID.builtin("test_fxtrid_key_dict")):
    """An entity with FxtrID keys in a dict."""

    name: str
    mapping: dict[FxtrID, str]


@dataclasses.dataclass(frozen=True)
class EntityWithNestedFrozen(DataclassEntity, fxtr_type=FxtrID.builtin("test_nested_frozen")):
    """An entity with nested frozen collections."""

    name: str
    mapping: frozendict[str, frozenset[int]]


def track_collection_test_types(scope: FxtrScope) -> None:
    """Track builtin types used by collection tests."""
    scope.track_builtin(FxtrID.builtin("test_set"))
    scope.track_builtin(FxtrID.builtin("test_frozenset"))
    scope.track_builtin(FxtrID.builtin("test_frozendict"))
    scope.track_builtin(FxtrID.builtin("test_ref_set"))
    scope.track_builtin(FxtrID.builtin("test_ref_frozenset"))
    scope.track_builtin(FxtrID.builtin("test_fxtrid_set"))
    scope.track_builtin(FxtrID.builtin("test_fxtrid_frozenset"))
    scope.track_builtin(FxtrID.builtin("test_ref_key_dict"))
    scope.track_builtin(FxtrID.builtin("test_ref_key_frozendict"))
    scope.track_builtin(FxtrID.builtin("test_fxtrid_key_dict"))
    scope.track_builtin(FxtrID.builtin("test_nested_frozen"))


@pytest.fixture
async def collection_scope() -> AsyncIterator[FxtrScope]:
    """Scope with collection test entity types pre-tracked."""
    async with FxtrScope(name="collection_test") as scope:
        track_test_entity_types(scope)
        track_collection_test_types(scope)
        yield scope


class TestBasicCollectionTypes:
    """Tests for basic collection type serialization (set, frozenset, dict, frozendict)."""

    @pytest.mark.anyio
    async def test_set_field_roundtrip(self, collection_scope: FxtrScope):
        """set[str] field stores and loads correctly."""
        entity = EntityWithSet(name="test", items={"a", "b", "c"})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.items == {"a", "b", "c"}
        assert isinstance(loaded.items, set)

    @pytest.mark.anyio
    async def test_frozenset_field_roundtrip(self, collection_scope: FxtrScope):
        """frozenset[str] field stores and loads correctly."""
        entity = EntityWithFrozenset(name="test", items=frozenset({"x", "y", "z"}))
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.items == frozenset({"x", "y", "z"})
        assert isinstance(loaded.items, frozenset)

    @pytest.mark.anyio
    async def test_frozendict_field_roundtrip(self, collection_scope: FxtrScope):
        """frozendict[str, int] field stores and loads correctly."""
        entity = EntityWithFrozendict(name="test", mapping=frozendict({"a": 1, "b": 2}))
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.mapping == frozendict({"a": 1, "b": 2})
        assert isinstance(loaded.mapping, frozendict)

    @pytest.mark.anyio
    async def test_dict_field_roundtrip(self, collection_scope: FxtrScope):
        """dict[str, Any] field stores and loads correctly."""
        entity = EntityWithDict(name="test", metadata={"key": "value", "num": 42})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.metadata == {"key": "value", "num": 42}
        assert isinstance(loaded.metadata, dict)

    @pytest.mark.anyio
    async def test_empty_set_roundtrip(self, collection_scope: FxtrScope):
        """Empty set preserves type."""
        entity = EntityWithSet(name="empty", items=set())
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.items == set()
        assert isinstance(loaded.items, set)

    @pytest.mark.anyio
    async def test_empty_frozenset_roundtrip(self, collection_scope: FxtrScope):
        """Empty frozenset preserves type."""
        entity = EntityWithFrozenset(name="empty", items=frozenset())
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.items == frozenset()
        assert isinstance(loaded.items, frozenset)

    @pytest.mark.anyio
    async def test_empty_frozendict_roundtrip(self, collection_scope: FxtrScope):
        """Empty frozendict preserves type."""
        entity = EntityWithFrozendict(name="empty", mapping=frozendict())
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.mapping == frozendict()
        assert isinstance(loaded.mapping, frozendict)

    @pytest.mark.anyio
    async def test_nested_frozen_collections_roundtrip(self, collection_scope: FxtrScope):
        """Nested frozen collections (frozendict[str, frozenset[int]]) roundtrip."""
        entity = EntityWithNestedFrozen(
            name="nested",
            mapping=frozendict({"nums": frozenset({1, 2, 3}), "more": frozenset({4, 5})}),
        )
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "nested"
        assert loaded.mapping == frozendict(
            {"nums": frozenset({1, 2, 3}), "more": frozenset({4, 5})}
        )
        assert isinstance(loaded.mapping, frozendict)
        assert isinstance(loaded.mapping["nums"], frozenset)


class TestEntityRefInCollections:
    """Tests for EntityRef values in sets and frozensets."""

    @pytest.mark.anyio
    async def test_entityref_in_set_roundtrip(self, collection_scope: FxtrScope):
        """set[EntityRef[T]] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefSet(name="test", refs={ref1, ref2})
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.refs) == 2
        assert isinstance(loaded.refs, set)

        # Verify we can load the referenced entities
        loaded_children = [await r.load() for r in loaded.refs]
        names = {c.name for c in loaded_children}
        assert names == {"child1", "child2"}

    @pytest.mark.anyio
    async def test_entityref_in_frozenset_roundtrip(self, collection_scope: FxtrScope):
        """frozenset[EntityRef[T]] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefFrozenset(name="test", refs=frozenset({ref1, ref2}))
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.refs) == 2
        assert isinstance(loaded.refs, frozenset)

        # Verify we can load the referenced entities
        loaded_children = [await r.load() for r in loaded.refs]
        names = {c.name for c in loaded_children}
        assert names == {"child1", "child2"}

    @pytest.mark.anyio
    async def test_fxtrid_in_set_roundtrip(self, collection_scope: FxtrScope):
        """set[FxtrID] roundtrips correctly."""
        id1 = FxtrID.builtin("test_id_1")
        id2 = FxtrID.builtin("test_id_2")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)

        entity = EntityWithFxtrIDSet(name="test", ids={id1, id2})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.ids == {id1, id2}
        assert isinstance(loaded.ids, set)

    @pytest.mark.anyio
    async def test_fxtrid_in_frozenset_roundtrip(self, collection_scope: FxtrScope):
        """frozenset[FxtrID] roundtrips correctly."""
        id1 = FxtrID.builtin("test_id_1")
        id2 = FxtrID.builtin("test_id_2")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)

        entity = EntityWithFxtrIDFrozenset(name="test", ids=frozenset({id1, id2}))
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.ids == frozenset({id1, id2})
        assert isinstance(loaded.ids, frozenset)


class TestEntityRefAsDictKeys:
    """Tests for EntityRef and FxtrID as dict/frozendict keys."""

    @pytest.mark.anyio
    async def test_entityref_as_dict_key_roundtrip(self, collection_scope: FxtrScope):
        """dict[EntityRef[T], str] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefKeyDict(name="test", mapping={ref1: "first", ref2: "second"})
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.mapping) == 2
        assert isinstance(loaded.mapping, dict)

        # Verify we can look up by the refs
        assert loaded.mapping[ref1] == "first"
        assert loaded.mapping[ref2] == "second"

    @pytest.mark.anyio
    async def test_entityref_as_frozendict_key_roundtrip(self, collection_scope: FxtrScope):
        """frozendict[EntityRef[T], str] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefKeyFrozendict(
            name="test", mapping=frozendict({ref1: "first", ref2: "second"})
        )
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.mapping) == 2
        assert isinstance(loaded.mapping, frozendict)

        # Verify we can look up by the refs
        assert loaded.mapping[ref1] == "first"
        assert loaded.mapping[ref2] == "second"

    @pytest.mark.anyio
    async def test_fxtrid_as_dict_key_roundtrip(self, collection_scope: FxtrScope):
        """dict[FxtrID, str] roundtrips correctly."""
        id1 = FxtrID.builtin("key_id_1")
        id2 = FxtrID.builtin("key_id_2")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)

        entity = EntityWithFxtrIDKeyDict(name="test", mapping={id1: "first", id2: "second"})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert len(loaded.mapping) == 2
        assert isinstance(loaded.mapping, dict)
        assert loaded.mapping[id1] == "first"
        assert loaded.mapping[id2] == "second"


class TestCanonicalOrdering:
    """Tests for canonical byte determinism regardless of insertion order."""

    @pytest.mark.anyio
    async def test_set_canonical_ordering(self, collection_scope: FxtrScope):
        """Different insertion orders for set produce identical CBOR."""
        # Create entities with sets in different insertion orders
        e1 = EntityWithSet(name="test", items={"z", "a", "m"})
        e2 = EntityWithSet(name="test", items={"a", "m", "z"})
        e3 = EntityWithSet(name="test", items={"m", "z", "a"})

        # All should produce identical CBOR (after serialization)
        cbor1 = e1._to_fxtr_cbor()
        cbor2 = e2._to_fxtr_cbor()
        cbor3 = e3._to_fxtr_cbor()

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_frozenset_canonical_ordering(self, collection_scope: FxtrScope):
        """Different insertion orders for frozenset produce identical CBOR."""
        e1 = EntityWithFrozenset(name="test", items=frozenset({"z", "a", "m"}))
        e2 = EntityWithFrozenset(name="test", items=frozenset({"a", "m", "z"}))
        e3 = EntityWithFrozenset(name="test", items=frozenset({"m", "z", "a"}))

        cbor1 = e1._to_fxtr_cbor()
        cbor2 = e2._to_fxtr_cbor()
        cbor3 = e3._to_fxtr_cbor()

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_entityref_set_canonical_ordering(self, collection_scope: FxtrScope):
        """EntityRef sets are canonically ordered by FxtrID bytes."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        child3 = SimpleEntity(name="child3", value=3)
        ref1 = child1.store()
        ref2 = child2.store()
        ref3 = child3.store()

        # Create entities with refs in different orders
        e1 = EntityWithRefSet(name="test", refs={ref1, ref2, ref3})
        e2 = EntityWithRefSet(name="test", refs={ref3, ref1, ref2})
        e3 = EntityWithRefSet(name="test", refs={ref2, ref3, ref1})

        cbor1 = e1._to_fxtr_cbor()
        cbor2 = e2._to_fxtr_cbor()
        cbor3 = e3._to_fxtr_cbor()

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_fxtrid_set_canonical_ordering(self, collection_scope: FxtrScope):
        """FxtrID sets are canonically ordered."""
        id1 = FxtrID.builtin("id_z")
        id2 = FxtrID.builtin("id_a")
        id3 = FxtrID.builtin("id_m")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)
        collection_scope.track_builtin(id3)

        e1 = EntityWithFxtrIDSet(name="test", ids={id1, id2, id3})
        e2 = EntityWithFxtrIDSet(name="test", ids={id2, id3, id1})
        e3 = EntityWithFxtrIDSet(name="test", ids={id3, id1, id2})

        cbor1 = e1._to_fxtr_cbor()
        cbor2 = e2._to_fxtr_cbor()
        cbor3 = e3._to_fxtr_cbor()

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_set_produces_same_entity_id(self, collection_scope: FxtrScope):
        """Sets with same elements but different insertion order produce same entity ID."""
        e1 = EntityWithSet(name="test", items={"z", "a", "m"})
        e2 = EntityWithSet(name="test", items={"a", "m", "z"})

        ref1 = e1.store()
        ref2 = e2.store()

        # Same content should produce same ID (content-addressed)
        assert ref1.ref == ref2.ref
