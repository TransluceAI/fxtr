"""Tests for EntityRef creation, properties, and validation."""

from __future__ import annotations

import dataclasses

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import Entity, EntityRef, FxtrScope

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_simple")):
    """Simple test entity."""

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


def track_test_builtins(scope: FxtrScope) -> None:
    """Track all builtin type IDs used in these tests."""
    scope.track_builtin(FxtrID.builtin("test_simple"))
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_b"))


# =============================================================================
# EntityRef Creation and Properties
# =============================================================================


class TestEntityRefCreation:
    """Tests for creating EntityRef and accessing its properties."""

    @pytest.mark.anyio
    async def test_entity_ref_creation(self):
        """Create EntityRef from FxtrID."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            fxtr_id = FxtrID.from_tag_and_payload("d", b"test_data")
            ref = EntityRef(fxtr_id, SimpleEntity)
            assert ref is not None

    @pytest.mark.anyio
    async def test_entity_ref_ref_property(self):
        """entity_id attribute returns the FxtrID."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            fxtr_id = FxtrID.from_tag_and_payload("d", b"test_data")
            ref = EntityRef(fxtr_id, SimpleEntity)
            assert ref.entity_id == fxtr_id

    @pytest.mark.anyio
    async def test_entity_ref_load(self):
        """await ref.load() returns entity."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = SimpleEntity(name="loadable", value=42)
            ref = entity.store()
            loaded = await ref.load()
            assert loaded.name == "loadable"
            assert loaded.value == 42

    @pytest.mark.anyio
    async def test_entity_ref_load_requires_tracked_id(self):
        """load() raises if ID is not tracked in scope."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            fxtr_id = FxtrID.from_tag_and_payload("d", b"nonexistent_entity")
            ref = EntityRef(fxtr_id, SimpleEntity)
            with pytest.raises(ValueError, match="not found"):
                await ref.load()


# =============================================================================
# EntityRef Equality and Hashing
# =============================================================================


class TestEntityRefEquality:
    """Tests for EntityRef equality and hashing."""

    def test_entityref_equality_same_id_and_type(self):
        """EntityRefs with same ID and type are equal."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"same")
        ref1 = EntityRef(fxtr_id, SimpleEntity)
        ref2 = EntityRef(fxtr_id, SimpleEntity)
        assert ref1 == ref2

    def test_entityref_inequality_by_type(self):
        """EntityRefs with same ID but different target types are not equal."""
        id1 = FxtrID.builtin("test")
        ref_a = EntityRef(id1, TypeA)
        ref_b = EntityRef(id1, TypeB)
        assert ref_a != ref_b

    def test_entityref_hash_consistency(self):
        """EntityRefs with same ID and type have consistent hashes."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"hashable")
        ref1 = EntityRef(fxtr_id, SimpleEntity)
        ref2 = EntityRef(fxtr_id, SimpleEntity)
        assert hash(ref1) == hash(ref2)
        # Should deduplicate in sets
        assert {ref1, ref2} == {ref1}

    def test_entityref_usable_as_dict_key(self):
        """EntityRefs can be used as dict keys."""
        fxtr_id = FxtrID.from_tag_and_payload("d", b"hashable")
        ref = EntityRef(fxtr_id, SimpleEntity)
        d = {ref: "value"}
        assert d[ref] == "value"


# =============================================================================
# EntityRef Type Validation
# =============================================================================


class TestEntityRefTypeValidation:
    """Tests for EntityRef type annotation validation."""

    def test_valid_single_type(self):
        """Single entity type is valid."""
        EntityRef(FxtrID.builtin("anything"), TypeA)

    def test_valid_union_type(self):
        """Union of entity types is valid."""
        EntityRef(FxtrID.builtin("anything"), TypeA | TypeB)

    def test_invalid_non_entity_type_raises(self):
        """Non-entity type should raise LookupError (no handler registered)."""
        with pytest.raises(LookupError):
            EntityRef(FxtrID.builtin("anything"), int)

    def test_entityref_base_class_error(self):
        """EntityRef cannot be created with Entity base class as target type."""
        id1 = FxtrID.builtin("test")
        with pytest.raises(
            TypeError,
            match="EntityRef cannot be created with the Entity base class as the target type",
        ):
            EntityRef(id1, Entity)
