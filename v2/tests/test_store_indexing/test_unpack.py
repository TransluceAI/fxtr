"""Tests for indexer unpack logic (1d)."""

from __future__ import annotations

import uuid

import pytest

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathIndex,
    PathKey,
    TypedDerefMarker,
    TypedPathExistsIndexer,
    TypedPathValueIndexer,
    VarMarker,
)
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.system_entities import define_type

_TEST_NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_TYPE_FOO = define_type(_TEST_NS, "unpack_test_foo")
_TYPE_BAR = define_type(_TEST_NS, "unpack_test_bar")
_TYPE_OTHER = define_type(_TEST_NS, "unpack_test_other")

_GROUND_CTX = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))


def _entity(type_id: FxtrID, **fields: object) -> dict:
    """Build a raw CBOR entity dict with $fxtr.ctx and $fxtr.type set."""
    return {
        "$fxtr.ctx": _GROUND_CTX,
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        **fields,
    }


# ---------------------------------------------------------------------------
# Simple field extraction
# ---------------------------------------------------------------------------


class TestSimpleExtraction:
    @pytest.mark.anyio
    async def test_simple_field(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, name="hello")
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), "hello")]

    @pytest.mark.anyio
    async def test_nested_field(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, outer={"inner": 42})
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="outer"), PathKey(key="inner")),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), 42)]

    @pytest.mark.anyio
    async def test_fixed_index(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, items=["a", "b", "c"])
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), PathIndex(index=1)),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), "b")]


# ---------------------------------------------------------------------------
# Variable iteration
# ---------------------------------------------------------------------------


class TestVariableIteration:
    @pytest.mark.anyio
    async def test_dict_var(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, scores={"a": 1.0, "b": 2.0})
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="scores"), VarMarker()),
            var_types=(IndexerColumnType.STR,),
            value_type=IndexerColumnType.FLOAT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert sorted(result) == [(("a",), 1.0), (("b",), 2.0)]

    @pytest.mark.anyio
    async def test_list_var(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, items=["x", "y"])
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker()),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.STR,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((0,), "x"), ((1,), "y")]

    @pytest.mark.anyio
    async def test_multiple_variables(self, local_fxtr_scope: FxtrScope):
        entity = _entity(
            _TYPE_FOO,
            turns=[
                {"metrics": {"acc": {"score": 0.95}, "f1": {"score": 0.87}}},
                {"metrics": {"acc": {"score": 0.91}}},
            ],
        )
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(
                PathKey(key="turns"),
                VarMarker(),
                PathKey(key="metrics"),
                VarMarker(),
                PathKey(key="score"),
            ),
            var_types=(IndexerColumnType.INT, IndexerColumnType.STR),
            value_type=IndexerColumnType.FLOAT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert sorted(result) == [
            ((0, "acc"), 0.95),
            ((0, "f1"), 0.87),
            ((1, "acc"), 0.91),
        ]

    @pytest.mark.anyio
    async def test_dict_var_skips_fxtr_meta_keys(self, local_fxtr_scope: FxtrScope):
        """VarMarker over dict should skip $fxtr.ctx and $fxtr.type."""
        entity = _entity(_TYPE_FOO, name="test")
        eid = local_fxtr_scope.store_cbor(entity)
        # The entity itself is the dict we're iterating; $fxtr.ctx and $fxtr.type are present
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(VarMarker(),),
            var_types=(IndexerColumnType.STR,),
            value_type=IndexerColumnType.STR,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        # Should only get "name" → "test", not $fxtr.ctx or $fxtr.type
        assert result == [(("name",), "test")]


# ---------------------------------------------------------------------------
# Deref markers (mid-path)
# ---------------------------------------------------------------------------


class TestDerefMarkers:
    @pytest.mark.anyio
    async def test_deref_loads_child(self, local_fxtr_scope: FxtrScope):
        """Deref marker should load a child entity and continue."""
        child_entity = _entity(_TYPE_BAR, value=99)
        child_id = local_fxtr_scope.store_cbor(child_entity)

        entity = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="ref"), DerefMarker(), PathKey(key="value")),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), 99)]

    @pytest.mark.anyio
    async def test_typed_deref_matching_type(self, local_fxtr_scope: FxtrScope):
        child_entity = _entity(_TYPE_BAR, value=42)
        child_id = local_fxtr_scope.store_cbor(child_entity)

        entity = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(
                PathKey(key="ref"),
                TypedDerefMarker(supported_types=frozenset({_TYPE_BAR}), strict=False),
                PathKey(key="value"),
            ),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), 42)]

    @pytest.mark.anyio
    async def test_typed_deref_wrong_type_lax(self, local_fxtr_scope: FxtrScope):
        child_entity = _entity(_TYPE_OTHER, value=42)
        child_id = local_fxtr_scope.store_cbor(child_entity)

        entity = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(
                PathKey(key="ref"),
                TypedDerefMarker(supported_types=frozenset({_TYPE_BAR}), strict=False),
                PathKey(key="value"),
            ),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_typed_deref_wrong_type_strict(self, local_fxtr_scope: FxtrScope):
        child_entity = _entity(_TYPE_OTHER, value=42)
        child_id = local_fxtr_scope.store_cbor(child_entity)

        entity = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(
                PathKey(key="ref"),
                TypedDerefMarker(supported_types=frozenset({_TYPE_BAR}), strict=True),
                PathKey(key="value"),
            ),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        with pytest.raises(ValueError, match="not in supported types"):
            await indexer.unpack_entity(eid, local_fxtr_scope)


# ---------------------------------------------------------------------------
# Initial lookup
# ---------------------------------------------------------------------------


class TestInitialLookup:
    @pytest.mark.anyio
    async def test_deref_initial_indexes_any_type(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, x=5)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), 5)]

    @pytest.mark.anyio
    async def test_typed_initial_lax_matching(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, x=5)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=False),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), 5)]

    @pytest.mark.anyio
    async def test_typed_initial_lax_non_matching(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_BAR, x=5)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=False),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_typed_initial_strict_non_matching(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_BAR, x=5)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=True),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        with pytest.raises(ValueError, match="not in supported types"):
            await indexer.unpack_entity(eid, local_fxtr_scope)

    # NOTE: test_typed_initial_no_type_on_entity_{lax,strict} were removed because
    # unpack_entity now loads via load_frozen_cbor, which requires $fxtr.type to be
    # present. Entities without $fxtr.type cannot be loaded through the scope, so the
    # "no type on entity" scenario is no longer reachable through this API.


# ---------------------------------------------------------------------------
# Schema strictness (indexer.strict)
# ---------------------------------------------------------------------------


class TestSchemaStrictness:
    @pytest.mark.anyio
    async def test_missing_key_lax(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, other="val")
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="nonexistent"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_missing_key_strict(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, other="val")
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=True,
            path=(PathKey(key="nonexistent"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        with pytest.raises(ValueError, match="not found"):
            await indexer.unpack_entity(eid, local_fxtr_scope)

    @pytest.mark.anyio
    async def test_index_out_of_bounds_lax(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, items=[1, 2])
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), PathIndex(index=99)),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_index_out_of_bounds_strict(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, items=[1, 2])
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=True,
            path=(PathKey(key="items"), PathIndex(index=99)),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        with pytest.raises(ValueError, match="out of bounds"):
            await indexer.unpack_entity(eid, local_fxtr_scope)

    @pytest.mark.anyio
    async def test_lax_indexer_strict_typed_deref(self, local_fxtr_scope: FxtrScope):
        """Independent axes: lax schema but strict type deref."""
        entity = _entity(_TYPE_BAR, x=5)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=True),
            strict=False,  # lax about schema
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        # Should raise because typed deref is strict, even though indexer is lax
        with pytest.raises(ValueError, match="not in supported types"):
            await indexer.unpack_entity(eid, local_fxtr_scope)


# ---------------------------------------------------------------------------
# Value type checking/coercion
# ---------------------------------------------------------------------------


class TestValueTypeChecking:
    @pytest.mark.anyio
    async def test_int_to_float_coercion(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, score=42)  # int, not float
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="score"),),
            var_types=(),
            value_type=IndexerColumnType.FLOAT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), 42.0)]
        assert isinstance(result[0][1], float)

    @pytest.mark.anyio
    async def test_fxtr_id_extraction(self, local_fxtr_scope: FxtrScope):
        # Store a dummy entity so the ref ID is valid in the scope.
        ref_entity = _entity(_TYPE_BAR, dummy=True)
        ref_id = local_fxtr_scope.store_cbor(ref_entity)
        entity = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=ref_id))
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="ref"),),
            var_types=(),
            value_type=IndexerColumnType.FXTR_ID,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), ref_id)]

    @pytest.mark.anyio
    async def test_strong_ref_fxtr_id_extraction(self, local_fxtr_scope: FxtrScope):
        # Store a dummy entity so the ref ID is valid in the scope.
        ref_entity = _entity(_TYPE_BAR, dummy=True)
        ref_id = local_fxtr_scope.store_cbor(ref_entity)
        entity = _entity(_TYPE_FOO, ref=RawStrongFxtrID(inner_id=ref_id))
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="ref"),),
            var_types=(),
            value_type=IndexerColumnType.FXTR_ID,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), ref_id)]

    @pytest.mark.anyio
    async def test_value_type_mismatch_lax(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, x="not_an_int")
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_value_type_mismatch_strict(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, x="not_an_int")
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=True,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        with pytest.raises(ValueError, match="Value type mismatch"):
            await indexer.unpack_entity(eid, local_fxtr_scope)

    @pytest.mark.anyio
    async def test_bool_is_not_int(self, local_fxtr_scope: FxtrScope):
        """bool should not match INT type (Python: isinstance(True, int) is True)."""
        entity = _entity(_TYPE_FOO, x=True)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_bool_matches_bool(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, x=True)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="x"),),
            var_types=(),
            value_type=IndexerColumnType.BOOL,
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), True)]


# ---------------------------------------------------------------------------
# Exists indexer
# ---------------------------------------------------------------------------


class TestExistsIndexer:
    @pytest.mark.anyio
    async def test_exists_returns_true(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, items={"a": 1, "b": 2})
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathExistsIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker()),
            var_types=(IndexerColumnType.STR,),
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert sorted(result) == [(("a",), True), (("b",), True)]

    @pytest.mark.anyio
    async def test_exists_empty_path(self, local_fxtr_scope: FxtrScope):
        """Exists with empty path — just checks initial type."""
        entity = _entity(_TYPE_FOO, x=1)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathExistsIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=False),
            strict=False,
            path=(),
            var_types=(),
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), True)]

    @pytest.mark.anyio
    async def test_exists_type_mismatch_returns_empty(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_BAR, x=1)
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathExistsIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=False),
            strict=False,
            path=(),
            var_types=(),
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []

    @pytest.mark.anyio
    async def test_exists_with_nested_path(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, a={"b": {"c": "whatever"}})
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathExistsIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="a"), PathKey(key="b"), PathKey(key="c")),
            var_types=(),
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == [((), True)]

    @pytest.mark.anyio
    async def test_exists_missing_path_returns_empty(self, local_fxtr_scope: FxtrScope):
        entity = _entity(_TYPE_FOO, a={"b": 1})
        eid = local_fxtr_scope.store_cbor(entity)
        indexer = TypedPathExistsIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="a"), PathKey(key="missing")),
            var_types=(),
        )
        result = await indexer.unpack_entity(eid, local_fxtr_scope)
        assert result == []
