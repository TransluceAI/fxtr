"""Tests for IndexComputer (1f)."""

from __future__ import annotations

import uuid

import pytest

from fxtr2.data.abstract_arena import ReadableArena
from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import ACTIVE_SCOPE, FxtrScope
from fxtr2.data.store_indexing.index_computer import IndexComputer
from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathKey,
    TypedDerefMarker,
    TypedPathExistsIndexer,
    TypedPathValueIndexer,
    VarMarker,
)
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.system_entities import define_type

_TEST_NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeef")
_TYPE_FOO = define_type(_TEST_NS, "ic_test_foo")
_TYPE_BAR = define_type(_TEST_NS, "ic_test_bar")
_TYPE_OTHER = define_type(_TEST_NS, "ic_test_other")

_GROUND_CTX = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))


def _entity(type_id: FxtrID, **fields: object) -> dict:
    """Build a raw CBOR entity dict with $fxtr.ctx and $fxtr.type set."""
    return {
        "$fxtr.ctx": _GROUND_CTX,
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        **fields,
    }


def _store_indexer(
    indexer: TypedPathValueIndexer | TypedPathExistsIndexer,
    scope: FxtrScope,
) -> FxtrID:
    """Store an indexer entity and return its FxtrID."""
    ref = indexer.store(scope=scope)
    return ref.entity_id


def _make_arena(scope: FxtrScope, ids: list[FxtrID]) -> ReadableArena:
    """Create a standalone arena containing the given entity IDs from the scope."""
    return scope._main_arena.copy_to_new_arena(scope.clone_authorization(), ids)


# ---------------------------------------------------------------------------
# get_indexer_types
# ---------------------------------------------------------------------------


class TestGetIndexerTypes:
    @pytest.mark.anyio
    async def test_value_indexer(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="turns"), VarMarker(), PathKey(key="score")),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.FLOAT,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [iid])

        async with IndexComputer() as ic:
            key_types, value_type = await ic.get_indexer_types(
                iid, arena, local_fxtr_scope.clone_authorization()
            )
        assert list(key_types) == [IndexerColumnType.INT]
        assert value_type == IndexerColumnType.FLOAT

    @pytest.mark.anyio
    async def test_exists_indexer(self, local_fxtr_scope: FxtrScope):
        indexer = TypedPathExistsIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker()),
            var_types=(IndexerColumnType.STR,),
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [iid])

        async with IndexComputer() as ic:
            key_types, value_type = await ic.get_indexer_types(
                iid, arena, local_fxtr_scope.clone_authorization()
            )
        assert list(key_types) == [IndexerColumnType.STR]
        assert value_type == IndexerColumnType.BOOL


# ---------------------------------------------------------------------------
# compute_indexer_entries — basic cases
# ---------------------------------------------------------------------------


class TestComputeSingleIndexerSingleEntity:
    @pytest.mark.anyio
    async def test_basic(self, local_fxtr_scope: FxtrScope):
        """One value indexer on one entity with matching data."""
        entity = _entity(_TYPE_FOO, name="hello", score=42)
        eid = local_fxtr_scope.store_cbor(entity)

        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert (iid, eid) in results
        assert results[(iid, eid)] == [((), "hello")]


class TestComputeSingleIndexerMultipleEntities:
    @pytest.mark.anyio
    async def test_basic(self, local_fxtr_scope: FxtrScope):
        """One indexer applied to three entities."""
        e1 = _entity(_TYPE_FOO, name="alice")
        e2 = _entity(_TYPE_FOO, name="bob")
        e3 = _entity(_TYPE_FOO, name="charlie")
        eid1 = local_fxtr_scope.store_cbor(e1)
        eid2 = local_fxtr_scope.store_cbor(e2)
        eid3 = local_fxtr_scope.store_cbor(e3)

        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid1, eid2, eid3, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid1, eid2, eid3],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert len(results) == 3
        assert results[(iid, eid1)] == [((), "alice")]
        assert results[(iid, eid2)] == [((), "bob")]
        assert results[(iid, eid3)] == [((), "charlie")]


class TestComputeMultipleIndexersSingleEntity:
    @pytest.mark.anyio
    async def test_basic(self, local_fxtr_scope: FxtrScope):
        """Two different indexers on one entity."""
        entity = _entity(_TYPE_FOO, name="hello", score=42)
        eid = local_fxtr_scope.store_cbor(entity)

        name_indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        score_indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="score"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        iid1 = _store_indexer(name_indexer, local_fxtr_scope)
        iid2 = _store_indexer(score_indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid, iid1, iid2])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid],
                [iid1, iid2],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert len(results) == 2
        assert results[(iid1, eid)] == [((), "hello")]
        assert results[(iid2, eid)] == [((), 42)]


class TestComputeMultipleIndexersMultipleEntities:
    @pytest.mark.anyio
    async def test_cartesian_product(self, local_fxtr_scope: FxtrScope):
        """Two indexers on two entities — all four combinations present."""
        e1 = _entity(_TYPE_FOO, name="alice", score=10)
        e2 = _entity(_TYPE_FOO, name="bob", score=20)
        eid1 = local_fxtr_scope.store_cbor(e1)
        eid2 = local_fxtr_scope.store_cbor(e2)

        name_indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        score_indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="score"),),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        iid1 = _store_indexer(name_indexer, local_fxtr_scope)
        iid2 = _store_indexer(score_indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid1, eid2, iid1, iid2])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid1, eid2],
                [iid1, iid2],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert len(results) == 4
        assert results[(iid1, eid1)] == [((), "alice")]
        assert results[(iid1, eid2)] == [((), "bob")]
        assert results[(iid2, eid1)] == [((), 10)]
        assert results[(iid2, eid2)] == [((), 20)]


# ---------------------------------------------------------------------------
# Lax / strict behavior
# ---------------------------------------------------------------------------


class TestLaxIndexerSkipsIncompatible:
    @pytest.mark.anyio
    async def test_type_filter_skip(self, local_fxtr_scope: FxtrScope):
        """Lax indexer with TypedDerefMarker skips entities with wrong type."""
        entity = _entity(_TYPE_OTHER, name="hello")
        eid = local_fxtr_scope.store_cbor(entity)

        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=False),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert (iid, eid) in results
        assert results[(iid, eid)] == []


class TestStrictIndexerRaises:
    @pytest.mark.anyio
    async def test_type_filter_strict(self, local_fxtr_scope: FxtrScope):
        """Strict indexer on incompatible entity raises ValueError."""
        entity = _entity(_TYPE_OTHER, name="hello")
        eid = local_fxtr_scope.store_cbor(entity)

        indexer = TypedPathValueIndexer(
            initial_lookup=TypedDerefMarker(supported_types=frozenset({_TYPE_FOO}), strict=True),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid, iid])

        async with IndexComputer() as ic:
            with pytest.raises(ValueError, match="not in supported types"):
                await ic.compute_indexer_entries(
                    [eid],
                    [iid],
                    arena,
                    local_fxtr_scope.clone_authorization(),
                    arena,
                    local_fxtr_scope.clone_authorization(),
                )


# ---------------------------------------------------------------------------
# Deref tests
# ---------------------------------------------------------------------------


class TestComputeWithDeref:
    @pytest.mark.anyio
    async def test_follows_ref(self, local_fxtr_scope: FxtrScope):
        """Indexer with DerefMarker follows FxtrID reference to child entity."""
        child = _entity(_TYPE_BAR, value=99)
        child_id = local_fxtr_scope.store_cbor(child)

        parent = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        parent_id = local_fxtr_scope.store_cbor(parent)

        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="ref"), DerefMarker(), PathKey(key="value")),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        # Include child_id in the arena so the IndexComputer can load it
        arena = _make_arena(local_fxtr_scope, [parent_id, child_id, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [parent_id],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert results[(iid, parent_id)] == [((), 99)]


class TestComputeWithTypedDeref:
    @pytest.mark.anyio
    async def test_typed_deref_matching(self, local_fxtr_scope: FxtrScope):
        """TypedDerefMarker in path filters by child entity type."""
        child = _entity(_TYPE_BAR, value=42)
        child_id = local_fxtr_scope.store_cbor(child)

        parent = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        parent_id = local_fxtr_scope.store_cbor(parent)

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
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [parent_id, child_id, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [parent_id],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert results[(iid, parent_id)] == [((), 42)]

    @pytest.mark.anyio
    async def test_typed_deref_wrong_type_skips(self, local_fxtr_scope: FxtrScope):
        """TypedDerefMarker with wrong child type produces empty results."""
        child = _entity(_TYPE_OTHER, value=42)
        child_id = local_fxtr_scope.store_cbor(child)

        parent = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        parent_id = local_fxtr_scope.store_cbor(parent)

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
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [parent_id, child_id, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [parent_id],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert results[(iid, parent_id)] == []


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    @pytest.mark.anyio
    async def test_no_leaks_to_test_scope(self, local_fxtr_scope: FxtrScope):
        """Entities loaded during indexing should not leak into the test scope."""
        child = _entity(_TYPE_BAR, value=99)
        child_id = local_fxtr_scope.store_cbor(child)

        parent = _entity(_TYPE_FOO, ref=RawWeakFxtrID(inner_id=child_id))
        parent_id = local_fxtr_scope.store_cbor(parent)

        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="ref"), DerefMarker(), PathKey(key="value")),
            var_types=(),
            value_type=IndexerColumnType.INT,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [parent_id, child_id, iid])

        # Record tracked entities before
        tracked_before = local_fxtr_scope._main_arena._liveset().copy()

        async with IndexComputer() as ic:
            await ic.compute_indexer_entries(
                [parent_id],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )

        # The test scope's tracked set should not have grown
        tracked_after = local_fxtr_scope._main_arena._liveset().copy()
        assert tracked_after == tracked_before

        # Also verify the active scope is still the test scope
        assert ACTIVE_SCOPE.get() is local_fxtr_scope


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    @pytest.mark.anyio
    async def test_empty_entities(self, local_fxtr_scope: FxtrScope):
        """Empty entities list returns empty dict."""
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        assert results == {}

    @pytest.mark.anyio
    async def test_empty_indexers(self, local_fxtr_scope: FxtrScope):
        """Empty indexers list returns empty dict."""
        entity = _entity(_TYPE_FOO, name="hello")
        eid = local_fxtr_scope.store_cbor(entity)
        arena = _make_arena(local_fxtr_scope, [eid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid], [], arena, local_fxtr_scope.clone_authorization()
            )
        assert results == {}


# ---------------------------------------------------------------------------
# Exists indexer
# ---------------------------------------------------------------------------


class TestExistsIndexerCompute:
    @pytest.mark.anyio
    async def test_exists_produces_true(self, local_fxtr_scope: FxtrScope):
        """TypedPathExistsIndexer produces True as the value for matching paths."""
        entity = _entity(
            _TYPE_FOO,
            items={"a": 1, "b": 2, "c": 3},
        )
        eid = local_fxtr_scope.store_cbor(entity)

        indexer = TypedPathExistsIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker()),
            var_types=(IndexerColumnType.STR,),
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        entries = results[(iid, eid)]
        # Should have three entries, one per dict key, all with value True
        assert len(entries) == 3
        keys = {bindings[0] for bindings, val in entries}
        assert keys == {"a", "b", "c"}
        assert all(val is True for _, val in entries)


# ---------------------------------------------------------------------------
# Variable extraction
# ---------------------------------------------------------------------------


class TestVariableExtraction:
    @pytest.mark.anyio
    async def test_list_variable(self, local_fxtr_scope: FxtrScope):
        """Indexer with VarMarker over a list produces correct bindings."""
        entity = _entity(
            _TYPE_FOO,
            turns=[
                {"score": 0.9},
                {"score": 0.8},
                {"score": 0.7},
            ],
        )
        eid = local_fxtr_scope.store_cbor(entity)

        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="turns"), VarMarker(), PathKey(key="score")),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.FLOAT,
        )
        iid = _store_indexer(indexer, local_fxtr_scope)
        arena = _make_arena(local_fxtr_scope, [eid, iid])

        async with IndexComputer() as ic:
            results = await ic.compute_indexer_entries(
                [eid],
                [iid],
                arena,
                local_fxtr_scope.clone_authorization(),
                arena,
                local_fxtr_scope.clone_authorization(),
            )
        entries = results[(iid, eid)]
        assert len(entries) == 3
        assert ((0,), 0.9) in entries
        assert ((1,), 0.8) in entries
        assert ((2,), 0.7) in entries
