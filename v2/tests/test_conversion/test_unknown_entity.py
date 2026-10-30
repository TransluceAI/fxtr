"""Tests for UnknownEntity and UnknownMutableEntity fallback entities."""

from __future__ import annotations

import dataclasses

import pytest
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.conversion.converters.unknown_entity import UnknownEntity, UnknownMutableEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class TypeA(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_a")):
    """Test entity type A."""

    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class TypeWithList(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_with_list")):
    """Test entity with a list field (to verify freeze/thaw of nested collections)."""

    items: list[str]
    metadata: dict[str, int]


def track_test_builtins(scope: FxtrScope) -> None:
    """Track builtin type IDs."""
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_with_list"))


# =============================================================================
# UnknownEntity Tests
# =============================================================================


class TestUnknownEntity:
    """Tests for UnknownEntity (frozen, cacheable)."""

    @pytest.mark.anyio
    async def test_unknown_entity_roundtrip(self):
        """UnknownEntity should preserve raw CBOR through store/load."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="as_unknown", value=5)
            ref = scope.store(entity)
            # Load as UnknownEntity to get frozen CBOR
            unknown_ref = EntityRef(ref.entity_id, UnknownEntity)
            loaded = await unknown_ref.load()
            assert isinstance(loaded, UnknownEntity)
            assert isinstance(loaded.raw_cbor, frozendict)
            assert loaded.raw_cbor["name"] == "as_unknown"
            assert loaded.raw_cbor["value"] == 5

    @pytest.mark.anyio
    async def test_unknown_entity_is_frozen_and_hashable(self):
        """UnknownEntity should be immutable and hashable."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="hashable", value=42)
            ref = scope.store(entity)
            unknown_ref = EntityRef(ref.entity_id, UnknownEntity)
            loaded = await unknown_ref.load()
            # Should be hashable
            h = hash(loaded)
            assert isinstance(h, int)
            # Should be usable in a set
            s = {loaded}
            assert loaded in s

    @pytest.mark.anyio
    async def test_unknown_entity_is_cached(self):
        """UnknownEntity should be cached: loading same ref twice returns same object."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="cached", value=99)
            ref = scope.store(entity)
            unknown_ref = EntityRef(ref.entity_id, UnknownEntity)
            first = await unknown_ref.load()
            second = await unknown_ref.load()
            assert first is second, "UnknownEntity should be cached (same object identity)"

    @pytest.mark.anyio
    async def test_unknown_entity_freezes_nested_collections(self):
        """UnknownEntity should freeze nested lists to tuples and dicts to frozendicts."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeWithList(items=["a", "b", "c"], metadata={"x": 1, "y": 2})
            ref = scope.store(entity)
            unknown_ref = EntityRef(ref.entity_id, UnknownEntity)
            loaded = await unknown_ref.load()
            # lists become tuples
            assert isinstance(loaded.raw_cbor["items"], tuple)
            assert loaded.raw_cbor["items"] == ("a", "b", "c")
            # dicts become frozendicts
            assert isinstance(loaded.raw_cbor["metadata"], frozendict)
            assert loaded.raw_cbor["metadata"] == frozendict({"x": 1, "y": 2})

    @pytest.mark.anyio
    async def test_unknown_entity_store_roundtrip(self):
        """Storing an UnknownEntity and loading it back should preserve data."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="store_roundtrip", value=7)
            ref = scope.store(entity)
            # Load as UnknownEntity
            unknown_ref = EntityRef(ref.entity_id, UnknownEntity)
            loaded_unknown = await unknown_ref.load()
            # Store the UnknownEntity back
            re_stored_ref = scope.store(loaded_unknown)
            # Load again as TypeA to verify data integrity
            reloaded = await EntityRef(re_stored_ref.entity_id, TypeA).load()
            assert reloaded.name == "store_roundtrip"
            assert reloaded.value == 7


# =============================================================================
# UnknownMutableEntity Tests
# =============================================================================


class TestUnknownMutableEntity:
    """Tests for UnknownMutableEntity (mutable, not cached)."""

    @pytest.mark.anyio
    async def test_unknown_mutable_entity_roundtrip(self):
        """UnknownMutableEntity should preserve raw mutable CBOR through store/load."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="mutable", value=10)
            ref = scope.store(entity)
            mutable_ref = EntityRef(ref.entity_id, UnknownMutableEntity)
            loaded = await mutable_ref.load()
            assert isinstance(loaded, UnknownMutableEntity)
            assert isinstance(loaded.raw_cbor, dict)
            assert loaded.raw_cbor["name"] == "mutable"
            assert loaded.raw_cbor["value"] == 10

    @pytest.mark.anyio
    async def test_unknown_mutable_entity_not_cached(self):
        """UnknownMutableEntity should not be cached: loading twice returns different objects."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="not_cached", value=20)
            ref = scope.store(entity)
            mutable_ref = EntityRef(ref.entity_id, UnknownMutableEntity)
            first = await mutable_ref.load()
            second = await mutable_ref.load()
            assert first is not second, "UnknownMutableEntity should not be cached"
            assert first.raw_cbor == second.raw_cbor

    @pytest.mark.anyio
    async def test_unknown_mutable_entity_has_mutable_collections(self):
        """UnknownMutableEntity should have mutable lists and dicts."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeWithList(items=["a", "b"], metadata={"x": 1})
            ref = scope.store(entity)
            mutable_ref = EntityRef(ref.entity_id, UnknownMutableEntity)
            loaded = await mutable_ref.load()
            assert isinstance(loaded.raw_cbor["items"], list)
            assert isinstance(loaded.raw_cbor["metadata"], dict)


# =============================================================================
# load_cbor and load_frozen_cbor Tests
# =============================================================================


class TestLoadCborMethods:
    """Tests for scope.load_cbor and scope.load_frozen_cbor."""

    @pytest.mark.anyio
    async def test_load_cbor_returns_mutable(self):
        """load_cbor should return mutable FxtrCbor (dict/list)."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeWithList(items=["a", "b"], metadata={"x": 1})
            ref = scope.store(entity)
            cbor = await scope.load_cbor(ref.entity_id)
            assert isinstance(cbor, dict)
            assert isinstance(cbor["items"], list)
            assert cbor["items"] == ["a", "b"]

    @pytest.mark.anyio
    async def test_load_frozen_cbor_returns_frozen(self):
        """load_frozen_cbor should return FrozenFxtrCbor (frozendict/tuple)."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeWithList(items=["a", "b"], metadata={"x": 1})
            ref = scope.store(entity)
            frozen = await scope.load_frozen_cbor(ref.entity_id)
            assert isinstance(frozen, frozendict)
            assert isinstance(frozen["items"], tuple)
            assert frozen["items"] == ("a", "b")

    @pytest.mark.anyio
    async def test_load_frozen_cbor_is_cached(self):
        """load_frozen_cbor should be cached: loading same ID twice returns same frozen object."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = TypeA(name="frozen_cached", value=55)
            ref = scope.store(entity)
            first = await scope.load_frozen_cbor(ref.entity_id)
            second = await scope.load_frozen_cbor(ref.entity_id)
            assert first is second, "load_frozen_cbor should return cached frozen CBOR"
