"""Tests for indexer serialization (1c): PathElement roundtrip and indexer store/load."""

from __future__ import annotations

import uuid

import mashumaro.codecs
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathElement,
    PathIndex,
    PathKey,
    TypedDerefMarker,
    TypedPathExistsIndexer,
    TypedPathValueIndexer,
    VarMarker,
    WellKnownBuiltinIndexer,
)
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.system_entities import define_type

_TEST_NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _make_type(name: str) -> FxtrID:
    """Create a type ID that is tracked in the global scope."""
    return define_type(_TEST_NS, name)


# Pre-create type IDs at module level so they are tracked in the global scope.
_TYPE_A = _make_type("test_type_a")
_TYPE_B = _make_type("test_type_b")
_TYPE_CHILD = _make_type("test_child_type")


# ---------------------------------------------------------------------------
# PathElement roundtrip tests
# ---------------------------------------------------------------------------


class TestPathElementRoundtrip:
    def test_path_key(self):
        elem = PathKey(key="turns")
        d = elem.to_dict()
        assert d == {"_k": "k", "key": "turns"}
        restored = mashumaro.codecs.BasicDecoder(PathElement).decode(d)
        assert restored == elem
        assert isinstance(restored, PathKey)

    def test_path_index(self):
        elem = PathIndex(index=3)
        d = elem.to_dict()
        assert d == {"_k": "i", "index": 3}
        restored = mashumaro.codecs.BasicDecoder(PathElement).decode(d)
        assert restored == elem
        assert isinstance(restored, PathIndex)

    def test_var_marker(self):
        elem = VarMarker()
        d = elem.to_dict()
        assert d == {"_k": "v"}
        restored = mashumaro.codecs.BasicDecoder(PathElement).decode(d)
        assert restored == elem
        assert isinstance(restored, VarMarker)

    def test_deref_marker(self):
        elem = DerefMarker()
        d = elem.to_dict()
        assert d == {"_k": "d"}
        restored = mashumaro.codecs.BasicDecoder(PathElement).decode(d)
        assert restored == elem
        assert isinstance(restored, DerefMarker)

    def test_typed_deref_marker(self):
        elem = TypedDerefMarker(
            supported_types=frozenset({_TYPE_A, _TYPE_B}),
            strict=True,
        )
        d = elem.to_dict()
        assert d["_k"] == "td"
        assert d["strict"] is True
        # supported_types should serialize to a sorted list
        assert isinstance(d["supported_types"], list)
        assert len(d["supported_types"]) == 2

        restored = mashumaro.codecs.BasicDecoder(PathElement).decode(d)
        assert restored == elem
        assert isinstance(restored, TypedDerefMarker)
        assert restored.supported_types == frozenset({_TYPE_A, _TYPE_B})
        assert restored.strict is True

    def test_typed_deref_marker_lax(self):
        elem = TypedDerefMarker(
            supported_types=frozenset({_TYPE_A}),
            strict=False,
        )
        d = elem.to_dict()
        assert d["strict"] is False
        restored = mashumaro.codecs.BasicDecoder(PathElement).decode(d)
        assert restored == elem
        assert isinstance(restored, TypedDerefMarker)
        assert restored.strict is False


# ---------------------------------------------------------------------------
# Indexer store/load tests
# ---------------------------------------------------------------------------


class TestTypedPathValueIndexerSerdes:
    @pytest.mark.anyio
    async def test_store_and_load(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_A}), strict=False),
            strict=False,
            path=(PathKey(key="turns"), VarMarker(), PathKey(key="score")),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.FLOAT,
        )
        ref = indexer.store()
        loaded = await ref.load()
        assert loaded == indexer
        assert isinstance(loaded, TypedPathValueIndexer)
        assert isinstance(loaded.initial_lookup, TypedDerefMarker)
        assert loaded.initial_lookup.supported_types == frozenset({_TYPE_A})
        assert loaded.path == (PathKey(key="turns"), VarMarker(), PathKey(key="score"))
        assert loaded.var_types == (IndexerColumnType.INT,)
        assert loaded.value_type == IndexerColumnType.FLOAT

    @pytest.mark.anyio
    async def test_store_with_deref_marker_initial(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=True,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        ref = indexer.store()
        loaded = await ref.load()
        assert loaded == indexer
        assert isinstance(loaded.initial_lookup, DerefMarker)
        assert loaded.strict is True

    @pytest.mark.anyio
    async def test_complex_path_roundtrip(self, local_fxtr_scope: FxtrScope):
        """Test an indexer with all path element types."""
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(
                PathKey(key="items"),
                VarMarker(),
                PathKey(key="ref"),
                DerefMarker(),
                PathKey(key="sub"),
                PathIndex(index=0),
                PathKey(key="data"),
                TypedDerefMarker(supported_types=frozenset({_TYPE_CHILD}), strict=True),
                PathKey(key="value"),
            ),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.FLOAT,
        )
        ref = indexer.store()
        loaded = await ref.load()
        assert loaded == indexer


class TestTypedPathExistsIndexerSerdes:
    @pytest.mark.anyio
    async def test_store_and_load(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathExistsIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_A}), strict=True),
            strict=False,
            path=(PathKey(key="items"), VarMarker()),
            var_types=(IndexerColumnType.STR,),
        )
        ref = indexer.store()
        loaded = await ref.load()
        assert loaded == indexer
        assert isinstance(loaded, TypedPathExistsIndexer)

    @pytest.mark.anyio
    async def test_empty_path(self, local_fxtr_scope: FxtrScope):
        """Exists indexer with no path — just checks type."""
        indexer = TypedPathExistsIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_A}), strict=True),
            strict=False,
            path=(),
            var_types=(),
        )
        ref = indexer.store()
        loaded = await ref.load()
        assert loaded == indexer
        assert loaded.path == ()


class TestWellKnownBuiltinIndexerUnion:
    @pytest.mark.anyio
    async def test_load_value_indexer_via_union(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        ref = indexer.store()
        union_ref = EntityRef(entity_id=ref.entity_id, target_type=WellKnownBuiltinIndexer)
        loaded = await union_ref.load()
        assert isinstance(loaded, TypedPathValueIndexer)
        assert loaded == indexer

    @pytest.mark.anyio
    async def test_load_exists_indexer_via_union(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathExistsIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
        )
        ref = indexer.store()
        union_ref = EntityRef(entity_id=ref.entity_id, target_type=WellKnownBuiltinIndexer)
        loaded = await union_ref.load()
        assert isinstance(loaded, TypedPathExistsIndexer)
        assert loaded == indexer


class TestCanonicalDeterminism:
    @pytest.mark.anyio
    async def test_frozenset_order_does_not_affect_id(self, local_fxtr_scope: FxtrScope):
        """Two identical indexers with different frozenset insertion order produce the same ID."""
        indexer1 = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(
                supported_types=frozenset({_TYPE_A, _TYPE_B}), strict=False
            ),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        indexer2 = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(
                supported_types=frozenset({_TYPE_B, _TYPE_A}), strict=False
            ),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        ref1 = indexer1.store()
        ref2 = indexer2.store()
        assert ref1.entity_id == ref2.entity_id
