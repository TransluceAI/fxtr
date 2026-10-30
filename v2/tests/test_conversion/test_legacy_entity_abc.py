"""Tests for SelfLoadingLegacyEntity bridge."""

from __future__ import annotations

from typing import Any, Self

import pytest

from fxtr2.conversion.converters.legacy_entity_abc import SelfLoadingLegacyEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

# =============================================================================
# Test Legacy Entity Definitions
# =============================================================================


class MyLegacyEntity(SelfLoadingLegacyEntity):
    """A test entity using the old Entity ABC pattern."""

    def __init__(self, data: str):
        self.data = data

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        return FxtrID.builtin("test_legacy")

    def _to_fxtr_cbor(self, scope: Any) -> FxtrCbor:
        return {
            "$fxtr.type": RawStrongFxtrID(inner_id=self._fxtr_type_id()),
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
            "data": self.data,
        }

    @classmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any, scope: Any) -> Self:
        return cls(data=cbor["data"])

    def _fxtr_immutable(self) -> bool:
        return False

    def __eq__(self, other):
        return isinstance(other, MyLegacyEntity) and self.data == other.data

    def __repr__(self):
        return f"LegacyEntity({self.data!r})"


class MyImmutableLegacyEntity(SelfLoadingLegacyEntity):
    """A test immutable entity using the old Entity ABC pattern."""

    def __init__(self, name: str):
        self.name = name

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        return FxtrID.builtin("test_imm_legacy")

    def _to_fxtr_cbor(self, scope: Any) -> FxtrCbor:
        return {
            "$fxtr.type": RawStrongFxtrID(inner_id=self._fxtr_type_id()),
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
            "name": self.name,
        }

    @classmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any, scope: Any) -> Self:
        return cls(name=cbor["name"])

    def _fxtr_immutable(self) -> bool:
        return True

    def __eq__(self, other):
        return isinstance(other, MyImmutableLegacyEntity) and self.name == other.name

    def __hash__(self):
        return hash(self.name)

    def __repr__(self):
        return f"ImmutableLegacyEntity({self.name!r})"


def track_test_builtins(scope: FxtrScope) -> None:
    """Track builtin type IDs."""
    scope.track_builtin(FxtrID.builtin("test_legacy"))
    scope.track_builtin(FxtrID.builtin("test_imm_legacy"))


# =============================================================================
# Legacy Entity ABC Tests
# =============================================================================


class TestLegacyEntityABC:
    """Tests for the SelfLoadingLegacyEntity bridge that adapts old-style entities."""

    @pytest.mark.anyio
    async def test_legacy_entity_store_and_load(self):
        """Legacy Entity ABC subclasses should be storable and loadable through the registry."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = MyLegacyEntity("hello_legacy")
            ref = scope.store(entity)
            loaded = await ref.load()
            assert isinstance(loaded, MyLegacyEntity)
            assert loaded.data == "hello_legacy"

    @pytest.mark.anyio
    async def test_legacy_entity_via_store_method(self):
        """Legacy entities can use the .store() method."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = MyLegacyEntity("via_method")
            ref = entity.store()
            loaded = await ref.load()
            assert loaded.data == "via_method"

    @pytest.mark.anyio
    async def test_immutable_legacy_entity_cached(self):
        """Immutable legacy entities should be cached."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = MyImmutableLegacyEntity("cached_legacy")
            ref = scope.store(entity)
            first = await ref.load()
            second = await ref.load()
            assert first is second  # Cached
            assert first.name == "cached_legacy"

    @pytest.mark.anyio
    async def test_mutable_legacy_entity_not_cached(self):
        """Mutable legacy entities should NOT be cached."""
        async with FxtrScope(name="test") as scope:
            track_test_builtins(scope)
            entity = MyLegacyEntity("not_cached")
            ref = scope.store(entity)
            first = await ref.load()
            second = await ref.load()
            assert first is not second
            assert first == second
