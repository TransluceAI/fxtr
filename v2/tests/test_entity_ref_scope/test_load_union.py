"""Tests for loading union types with mixed entity implementations."""

from __future__ import annotations

import dataclasses
import typing
from typing import Any

import pytest

from fxtr2.conversion.converters.legacy_entity_abc import SelfLoadingLegacyEntity
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope

# =============================================================================
# Test Entity Definitions
# =============================================================================


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


def track_test_builtins(scope: FxtrScope) -> None:
    """Track builtin type IDs."""
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_b"))
    scope.track_builtin(FxtrID.builtin("test_type_c"))


# =============================================================================
# Union Loading with DataclassEntity
# =============================================================================


class TestUnionWithDataclassEntities:
    """Tests for union loading with pure DataclassEntity types."""

    @pytest.mark.anyio
    async def test_union_loading_with_dataclass_entities(self):
        """EntityRef with union target type dispatches to correct variant based on $fxtr.type."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            # Store different types
            a = TypeA(a_field="a")
            b = TypeB(b_field=1)
            c = TypeC(c_field=3.14)

            a_ref = a.store()
            b_ref = b.store()
            c_ref = c.store()

            # Create union-typed refs and load them
            union_ref_a = EntityRef[TypeA | TypeB | TypeC](a_ref.entity_id, TypeA | TypeB | TypeC)
            union_ref_b = EntityRef[TypeA | TypeB | TypeC](b_ref.entity_id, TypeA | TypeB | TypeC)
            union_ref_c = EntityRef[TypeA | TypeB | TypeC](c_ref.entity_id, TypeA | TypeB | TypeC)

            loaded_a = await union_ref_a.load()
            loaded_b = await union_ref_b.load()
            loaded_c = await union_ref_c.load()

            assert isinstance(loaded_a, TypeA)
            assert loaded_a.a_field == "a"

            assert isinstance(loaded_b, TypeB)
            assert loaded_b.b_field == 1

            assert isinstance(loaded_c, TypeC)
            assert loaded_c.c_field == 3.14

    @pytest.mark.anyio
    async def test_store_and_load_via_union_converter(self):
        """Store different entities and load via union-typed refs."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            # Store TypeA
            a = TypeA(a_field="union_a")
            a_ref = a.store()

            # Store TypeB
            b = TypeB(b_field=123)
            b_ref = b.store()

            # Create union-typed refs manually
            union_ref_a = EntityRef[TypeA | TypeB](a_ref.entity_id, TypeA | TypeB)
            union_ref_b = EntityRef[TypeA | TypeB](b_ref.entity_id, TypeA | TypeB)

            # Load and verify types
            loaded_a = await union_ref_a.load()
            loaded_b = await union_ref_b.load()

            assert isinstance(loaded_a, TypeA)
            assert loaded_a.a_field == "union_a"

            assert isinstance(loaded_b, TypeB)
            assert loaded_b.b_field == 123

    @pytest.mark.anyio
    async def test_load_with_union_type(self):
        """Loading with a union type should dispatch to the correct handler."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity_a = TypeA(a_field="union_a")
            ref_a = scope.store(entity_a)
            # Create a union-typed ref
            union_ref = EntityRef(ref_a.entity_id, TypeA | TypeB)
            loaded = await union_ref.load()
            assert isinstance(loaded, TypeA)
            assert loaded.a_field == "union_a"

    @pytest.mark.anyio
    async def test_load_union_type_b(self):
        """Loading TypeB via union."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity_b = TypeB(b_field=456)
            ref_b = scope.store(entity_b)
            union_ref = EntityRef(ref_b.entity_id, TypeA | TypeB)
            loaded = await union_ref.load()
            assert isinstance(loaded, TypeB)
            assert loaded.b_field == 456


# =============================================================================
# Union Loading with Mixed Entity Types
# =============================================================================


class TestUnionWithMixedEntityTypes:
    """Tests for union loading with mix of DataclassEntity and custom entities."""

    @pytest.mark.anyio
    async def test_union_loading_with_custom_non_mashumaro_entity(self):
        """EntityRef with union containing custom (non-DataclassEntity) type dispatches correctly."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)

            # Define a custom entity that doesn't use mashumaro/DataclassEntity
            class CustomSimpleEntity(SelfLoadingLegacyEntity):
                """A custom entity with simple serialization, not using mashumaro."""

                _TYPE_ID = scope.track_builtin(FxtrID.builtin("test_custom_simple"))

                def __init__(self, value: str):
                    self._value = value

                @property
                def value(self) -> str:
                    return self._value

                def __eq__(self, other):
                    return type(other) is type(self) and self._value == other._value

                def __hash__(self):
                    return hash((type(self), self._value))

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

            # Store instances of each type
            type_a = TypeA(a_field="from_a")
            custom = CustomSimpleEntity(value="from_custom")
            type_b = TypeB(b_field=42)

            a_ref = type_a.store()
            custom_ref = scope.store(custom)
            b_ref = type_b.store()

            # Create union-typed refs and load them
            union_type = TypeA | CustomSimpleEntity | TypeB
            union_ref_a = EntityRef[TypeA | CustomSimpleEntity | TypeB](a_ref.entity_id, union_type)
            union_ref_custom = EntityRef[TypeA | CustomSimpleEntity | TypeB](
                custom_ref.entity_id, union_type
            )
            union_ref_b = EntityRef[TypeA | CustomSimpleEntity | TypeB](b_ref.entity_id, union_type)

            loaded_a = await union_ref_a.load()
            loaded_custom = await union_ref_custom.load()
            loaded_b = await union_ref_b.load()

            # Verify DataclassEntity types
            assert isinstance(loaded_a, TypeA)
            assert loaded_a.a_field == "from_a"

            assert isinstance(loaded_b, TypeB)
            assert loaded_b.b_field == 42

            # Verify custom Entity type
            assert isinstance(loaded_custom, CustomSimpleEntity)
            assert loaded_custom.value == "from_custom"

    @pytest.mark.anyio
    async def test_store_load_union_with_custom_entity(self):
        """Store and load different entity types via union-typed EntityRef."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)

            # Same custom entity as above (defined locally for test isolation)
            class CustomSimpleEntity(SelfLoadingLegacyEntity):
                _TYPE_ID = scope.track_builtin(FxtrID.builtin("test_custom_simple_v2"))

                def __init__(self, value: str):
                    self._value = value

                @property
                def value(self) -> str:
                    return self._value

                def __eq__(self, other):
                    return type(other) is type(self) and self._value == other._value

                def __hash__(self):
                    return hash((type(self), self._value))

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

            # Store a DataclassEntity
            type_a = TypeA(a_field="stored_a")
            a_ref = type_a.store()

            # Store a custom Entity
            custom = CustomSimpleEntity(value="stored_custom")
            custom_ref = scope.store(custom)

            # Build union converter and create union-typed refs
            union_ref_a = EntityRef[TypeA | CustomSimpleEntity](
                a_ref.entity_id, TypeA | CustomSimpleEntity
            )
            union_ref_custom = EntityRef[TypeA | CustomSimpleEntity](
                custom_ref.entity_id, TypeA | CustomSimpleEntity
            )

            # Load via union-typed refs
            loaded_a = await union_ref_a.load()
            loaded_custom = await union_ref_custom.load()

            # Verify correct types loaded
            assert isinstance(loaded_a, TypeA)
            assert loaded_a.a_field == "stored_a"

            assert isinstance(loaded_custom, CustomSimpleEntity)
            assert loaded_custom.value == "stored_custom"
