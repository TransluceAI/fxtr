"""Integration tests for namespace indexing in SQLiteNamespaceBackend.

Tests the full register → pin → query → unpin → unregister → GC cycle.
"""

from __future__ import annotations

import uuid

import pytest

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.sqlite_store import sqlite_ops
from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession
from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathKey,
    TypedPathValueIndexer,
    VarMarker,
)
from fxtr2.data.store_indexing.indexing_types import (
    BindingConstraint,
    IndexerColumnType,
    IndexerConstraintByID,
)
from fxtr2.data.system_entities import define_type

# =============================================================================
# Test helpers
# =============================================================================

_GROUND_CTX = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))
_TEST_NS = uuid.UUID("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
_TYPE_A = define_type(_TEST_NS, "backend_test_a")


def _entity(**fields: object) -> dict:
    """Build a raw CBOR entity dict with $fxtr.ctx and $fxtr.type set."""
    return {
        "$fxtr.ctx": _GROUND_CTX,
        "$fxtr.type": RawWeakFxtrID(inner_id=_TYPE_A),
        **fields,
    }


def _store_indexer(indexer: TypedPathValueIndexer, scope: FxtrScope) -> FxtrID:
    ref = indexer.store(scope=scope)
    return ref.entity_id


def _make_arena(scope: FxtrScope, ids: list[FxtrID]):
    return scope._main_arena.copy_to_new_arena(scope.clone_authorization(), ids)


# A simple indexer: extracts ``items[*].score`` as (int key, float value)
_SCORE_INDEXER = TypedPathValueIndexer(
    initial_lookup=DerefMarker(),
    strict=False,
    path=(PathKey(key="items"), VarMarker(), PathKey(key="score")),
    var_types=(IndexerColumnType.INT,),
    value_type=IndexerColumnType.FLOAT,
)

# A simpler indexer: extracts ``name`` as (no keys, str value)
_NAME_INDEXER = TypedPathValueIndexer(
    initial_lookup=DerefMarker(),
    strict=False,
    path=(PathKey(key="name"),),
    var_types=(),
    value_type=IndexerColumnType.STR,
)


# =============================================================================
# Tests
# =============================================================================


class TestRegisterIndexer:
    """Tests for register_indexer."""

    @pytest.mark.anyio
    async def test_register_on_empty_namespace(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Register an indexer on an empty namespace => state INDEXED, no index rows."""
        iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.register_indexer(
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
            )

            # Should now appear in list_indexers
            indexers, arena = await ns.list_indexers()
            assert len(indexers) == 1
            fxtr_id, prefix = indexers[0]
            assert fxtr_id == iid
            assert prefix == ""

    @pytest.mark.anyio
    async def test_register_on_namespace_with_existing_pins(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope, memory_arena
    ):
        """Register indexes all existing matching pins."""
        # Create and pin two entities
        e1 = _entity(items=[{"score": 1.0}, {"score": 2.0}])
        e1_id = local_fxtr_scope.store_cbor(e1)
        e2 = _entity(items=[{"score": 3.0}])
        e2_id = local_fxtr_scope.store_cbor(e2)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.pin_entity_from_arena(
                "e1",
                e1_id,
                _make_arena(local_fxtr_scope, [e1_id]),
                local_fxtr_scope.clone_authorization(),
            )
            await ns.pin_entity_from_arena(
                "e2",
                e2_id,
                _make_arena(local_fxtr_scope, [e2_id]),
                local_fxtr_scope.clone_authorization(),
            )

            # Now register the indexer
            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Verify by querying
            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value >= 0"
            )
            results = []
            async for match in ns.iter_matches(constraint):
                results.append(match)

            # e1 has scores at indices 0,1; e2 has score at index 0
            assert len(results) == 3

    @pytest.mark.anyio
    async def test_register_same_indexer_prefix_twice_raises(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Registering the same indexer+prefix twice raises KeyError."""
        iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )
            with pytest.raises(KeyError):
                await ns.register_indexer(
                    iid,
                    _make_arena(local_fxtr_scope, [iid]),
                    local_fxtr_scope.clone_authorization(),
                )

    @pytest.mark.anyio
    async def test_register_with_key_prefix(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Registering with a prefix only indexes matching pins."""
        e1 = _entity(items=[{"score": 1.0}])
        e1_id = local_fxtr_scope.store_cbor(e1)
        e2 = _entity(items=[{"score": 2.0}])
        e2_id = local_fxtr_scope.store_cbor(e2)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.pin_entity_from_arena(
                "group_a/e1",
                e1_id,
                _make_arena(local_fxtr_scope, [e1_id]),
                local_fxtr_scope.clone_authorization(),
            )
            await ns.pin_entity_from_arena(
                "group_b/e2",
                e2_id,
                _make_arena(local_fxtr_scope, [e2_id]),
                local_fxtr_scope.clone_authorization(),
            )

            # Register only for group_a/
            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
                key_prefix="group_a/",
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value >= 0"
            )
            results = []
            async for match in ns.iter_matches(constraint, key_prefix="group_a/"):
                results.append(match)

            # Only e1 is indexed (under group_a/)
            assert len(results) == 1
            key, _, _, _ = results[0]
            assert key == "group_a/e1"


class TestPinWithIndexing:
    """Tests for pin_entity_from_arena with indexing."""

    @pytest.mark.anyio
    async def test_pin_with_no_indexers(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """Pin with no registered indexers works as before (regression)."""
        e = _entity(name="test")
        eid = local_fxtr_scope.store_cbor(e)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.pin_entity_from_arena(
                "pin1",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )
            retrieved_id, _ = await ns.retrieve_pinned("pin1")
            assert retrieved_id == eid

    @pytest.mark.anyio
    async def test_pin_with_one_indexer(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """Pin with an INDEXED indexer creates index rows."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            # Register indexer first
            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Pin an entity
            e = _entity(items=[{"score": 4.5}, {"score": 2.1}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "pin1",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Query should find both scores
            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value > 0"
            )
            results = []
            async for match in ns.iter_matches(constraint):
                results.append(match)
            assert len(results) == 2

    @pytest.mark.anyio
    async def test_pin_overwrite_replaces_index_data(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Pin with overwrite removes old index data and adds new."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Pin original entity
            e1 = _entity(items=[{"score": 1.0}])
            e1_id = local_fxtr_scope.store_cbor(e1)
            await ns.pin_entity_from_arena(
                "pin1",
                e1_id,
                _make_arena(local_fxtr_scope, [e1_id]),
                local_fxtr_scope.clone_authorization(),
            )

            # Overwrite with entity having different scores
            e2 = _entity(items=[{"score": 9.0}, {"score": 8.0}, {"score": 7.0}])
            e2_id = local_fxtr_scope.store_cbor(e2)
            await ns.pin_entity_from_arena(
                "pin1",
                e2_id,
                _make_arena(local_fxtr_scope, [e2_id]),
                local_fxtr_scope.clone_authorization(),
                overwrite=True,
            )

            # Query should find the NEW entity's 3 scores, not the old one
            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value > 0"
            )
            results = []
            async for match in ns.iter_matches(constraint):
                results.append(match)
            assert len(results) == 3
            # All results should reference e2
            for _, entity_id, _, _ in results:
                assert entity_id == e2_id


class TestUnpinWithIndexing:
    """Tests for unpin_entity with indexing."""

    @pytest.mark.anyio
    async def test_unpin_removes_index_rows(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Unpin removes index rows from all index tables."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(items=[{"score": 5.0}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "pin1",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Verify it's queryable
            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="1=1"
            )
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1

            # Unpin
            await ns.unpin_entity("pin1")

            # Should find no results now
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 0

    @pytest.mark.anyio
    async def test_same_entity_pinned_twice_unpin_one(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Same entity pinned under two names: unpinning one keeps the other queryable."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(items=[{"score": 5.0}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "pin_a",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )
            await ns.pin_entity_from_arena(
                "pin_b",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="1=1"
            )

            # Both pins should appear
            results = [r async for r in ns.iter_matches(constraint)]
            assert sorted(r[0] for r in results) == ["pin_a", "pin_b"]

            # Unpin one — the other should still be queryable
            await ns.unpin_entity("pin_a")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1
            assert results[0][0] == "pin_b"

            # Unpin the other — now nothing
            await ns.unpin_entity("pin_b")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 0


class TestUnregisterIndexer:
    """Tests for unregister_indexer."""

    @pytest.mark.anyio
    async def test_unregister_sets_garbage(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """Unregister sets state to GARBAGE; indexer disappears from list."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )
            assert len((await ns.list_indexers())[0]) == 1

            await ns.unregister_indexer(iid)
            assert len((await ns.list_indexers())[0]) == 0


class TestIterMatches:
    """Tests for iter_matches."""

    @pytest.mark.anyio
    async def test_single_constraint_single_match(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Single constraint, single matching entity."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_NAME_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(name="hello")
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "pin1",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=(), condition="value = 'hello'"
            )
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1
            key, entity_id, bindings, arena = results[0]
            assert key == "pin1"
            assert entity_id == eid
            assert bindings == {}

    @pytest.mark.anyio
    async def test_single_constraint_multiple_matches(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Single constraint, multiple matching entities."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e1 = _entity(items=[{"score": 10.0}])
            e1_id = local_fxtr_scope.store_cbor(e1)
            await ns.pin_entity_from_arena(
                "e1",
                e1_id,
                _make_arena(local_fxtr_scope, [e1_id]),
                local_fxtr_scope.clone_authorization(),
            )

            e2 = _entity(items=[{"score": 20.0}])
            e2_id = local_fxtr_scope.store_cbor(e2)
            await ns.pin_entity_from_arena(
                "e2",
                e2_id,
                _make_arena(local_fxtr_scope, [e2_id]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value > 0"
            )
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 2
            keys = sorted(r[0] for r in results)
            assert keys == ["e1", "e2"]

    @pytest.mark.anyio
    async def test_no_matches(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """No matching entities returns empty."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value > 0"
            )
            results = [r async for r in ns.iter_matches(constraint)]
            assert results == []

    @pytest.mark.anyio
    async def test_value_filtering(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """Condition filters by value."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(items=[{"score": 1.0}, {"score": 5.0}, {"score": 10.0}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "e",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="value > 3"
            )
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 2  # scores 5.0 and 10.0

    @pytest.mark.anyio
    async def test_binding_variable_values(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """Binding variables are returned correctly."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(items=[{"score": 7.7}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "e",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("idx",), condition="1=1"
            )
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1
            _, _, bindings, _ = results[0]
            assert bindings == {"idx": 0}  # list index 0

    @pytest.mark.anyio
    async def test_multiple_constraints_with_shared_variable(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Two constraints joined on a shared variable."""
        # Indexer A: items[i].score (int key, float value)
        # Indexer B: items[i].name  (int key, str value)
        # We want items where score > 5 AND name = 'good'
        name_at_idx = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker(), PathKey(key="name")),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.STR,
        )

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            score_iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                score_iid,
                _make_arena(local_fxtr_scope, [score_iid]),
                local_fxtr_scope.clone_authorization(),
            )

            name_iid = _store_indexer(name_at_idx, local_fxtr_scope)
            await ns.register_indexer(
                name_iid,
                _make_arena(local_fxtr_scope, [name_iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(
                items=[
                    {"score": 3.0, "name": "bad"},
                    {"score": 8.0, "name": "good"},
                    {"score": 9.0, "name": "great"},
                ]
            )
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "e",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Join on shared variable "i"
            c1 = IndexerConstraintByID(
                indexer_id=score_iid, key_variables=("i",), condition="value > 5"
            )
            c2 = IndexerConstraintByID(
                indexer_id=name_iid, key_variables=("i",), condition="value = 'good'"
            )
            results = [r async for r in ns.iter_matches(c1, c2)]
            assert len(results) == 1
            _, _, bindings, _ = results[0]
            assert bindings["i"] == 1  # index 1 has score=8 and name='good'

    @pytest.mark.anyio
    async def test_with_binding_constraint(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """BindingConstraint applies a global condition on joined variables."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(items=[{"score": 1.0}, {"score": 2.0}, {"score": 3.0}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "e",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            # Get scores where index > 0
            constraint = IndexerConstraintByID(
                indexer_id=iid, key_variables=("i",), condition="1=1"
            )
            binding = BindingConstraint(condition="$i > 0")
            results = [
                r
                async for r in ns.iter_matches(
                    constraint,
                    where=binding,
                )
            ]
            assert len(results) == 2  # indices 1 and 2

    @pytest.mark.anyio
    async def test_with_key_prefix_filter(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        """key_prefix on iter_matches filters by pin key."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_NAME_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            for name in ["alpha", "beta"]:
                e = _entity(name=name)
                eid = local_fxtr_scope.store_cbor(e)
                await ns.pin_entity_from_arena(
                    f"group/{name}",
                    eid,
                    _make_arena(local_fxtr_scope, [eid]),
                    local_fxtr_scope.clone_authorization(),
                )

            e_other = _entity(name="gamma")
            e_other_id = local_fxtr_scope.store_cbor(e_other)
            await ns.pin_entity_from_arena(
                "other/gamma",
                e_other_id,
                _make_arena(local_fxtr_scope, [e_other_id]),
                local_fxtr_scope.clone_authorization(),
            )

            constraint = IndexerConstraintByID(indexer_id=iid, key_variables=(), condition="1=1")
            results = [r async for r in ns.iter_matches(constraint, key_prefix="group/")]
            assert len(results) == 2
            keys = sorted(r[0] for r in results)
            assert keys == ["group/alpha", "group/beta"]

    @pytest.mark.anyio
    async def test_query_prefix_not_covered_by_registration(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """iter_matches raises KeyError when no registration covers the query prefix."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_NAME_INDEXER, local_fxtr_scope)
            # Register only for keys under "group/"
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
                key_prefix="group/",
            )

            constraint = IndexerConstraintByID(indexer_id=iid, key_variables=(), condition="1=1")

            # Querying under "group/" works (exact match on registration prefix)
            results = [r async for r in ns.iter_matches(constraint, key_prefix="group/")]
            assert results == []

            # Querying under "group/sub/" works (registration prefix is shorter)
            results = [r async for r in ns.iter_matches(constraint, key_prefix="group/sub/")]
            assert results == []

            # Querying under "other/" fails — no registration covers it
            with pytest.raises(KeyError):
                async for _ in ns.iter_matches(constraint, key_prefix="other/"):
                    pass

            # Querying with no prefix fails — "group/" doesn't cover ""
            with pytest.raises(KeyError):
                async for _ in ns.iter_matches(constraint):
                    pass


class TestListIndexers:
    """Tests for list_indexers."""

    @pytest.mark.anyio
    async def test_list_empty(self, tmp_db_path_str: str):
        """list_indexers on namespace with no indexers returns empty."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            indexers, _arena = await ns.list_indexers()
            assert indexers == []

    @pytest.mark.anyio
    async def test_list_returns_indexed_only(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """list_indexers returns only INDEXED registrations."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            indexers, _arena = await ns.list_indexers()
            assert len(indexers) == 1

            # Unregister => GARBAGE, should disappear
            await ns.unregister_indexer(iid)
            indexers, _arena = await ns.list_indexers()
            assert len(indexers) == 0


class TestGC:
    """Tests for GC interaction with indexing."""

    @pytest.mark.anyio
    async def test_gc_cleans_garbage_registration(
        self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """After unregister + GC, the registration and index data are cleaned up."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            iid = _store_indexer(_SCORE_INDEXER, local_fxtr_scope)
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )

            e = _entity(items=[{"score": 1.0}])
            eid = local_fxtr_scope.store_cbor(e)
            await ns.pin_entity_from_arena(
                "e",
                eid,
                _make_arena(local_fxtr_scope, [eid]),
                local_fxtr_scope.clone_authorization(),
            )

            await ns.unregister_indexer(iid)

            # Run GC until no more work
            for _ in range(20):
                work_done, _ = await session._pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.run_one_gc_step
                )
                if not work_done:
                    break

            # Verify: the indexer should no longer be listed (already verified above),
            # and re-registering should work
            await ns.register_indexer(
                iid,
                _make_arena(local_fxtr_scope, [iid]),
                local_fxtr_scope.clone_authorization(),
            )
            indexers, _arena = await ns.list_indexers()
            assert len(indexers) == 1
