"""Tests for FxtrQueryableNamespace user-facing API (Part 3).

Tests the user-facing layer that wraps IndexablePinningBackend:
register_indexer, unregister_indexer, list_indexers, iter_matches, sql.
Uses SQLiteFxtrNamespace as the concrete implementation.
"""

from __future__ import annotations

import dataclasses

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, get_active_scope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathKey,
    TypedPathExistsIndexer,
    TypedPathValueIndexer,
    VarMarker,
)
from fxtr2.data.store_indexing.indexing_types import BindingConstraint, IndexerColumnType, QueryView

# =============================================================================
# Test helpers
# =============================================================================

_SCORE_INDEXER = TypedPathValueIndexer(
    initial_lookup=DerefMarker(),
    strict=False,
    path=(PathKey(key="items"), VarMarker(), PathKey(key="score")),
    var_types=(IndexerColumnType.INT,),
    value_type=IndexerColumnType.FLOAT,
)

_NAME_INDEXER = TypedPathValueIndexer(
    initial_lookup=DerefMarker(),
    strict=False,
    path=(PathKey(key="name"),),
    var_types=(),
    value_type=IndexerColumnType.STR,
)

_EXISTS_INDEXER = TypedPathExistsIndexer(
    initial_lookup=DerefMarker(),
    strict=False,
    path=(PathKey(key="items"), VarMarker()),
    var_types=(IndexerColumnType.INT,),
)


@dataclasses.dataclass(frozen=True)
class _SimpleEntity:
    """A plain dict entity for testing (not using DataclassEntity to keep it minimal)."""

    data: dict

    def store_into(self, scope: FxtrScope) -> FxtrID:
        from fxtr2.data.entity_core import RawFxtrEntityMarkerID, RawWeakFxtrID

        ctx = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))
        cbor = {
            "$fxtr.ctx": ctx,
            "$fxtr.type": RawWeakFxtrID(
                inner_id=scope.track_builtin(FxtrID.builtin("testing.simple_entity"))
            ),
            **self.data,
        }
        return scope.store_cbor(cbor)


def _entity(**fields: object) -> _SimpleEntity:
    return _SimpleEntity(data=fields)


# =============================================================================
# Tests: register_indexer
# =============================================================================


class TestRegisterIndexer:
    """Tests for FxtrQueryableNamespace.register_indexer."""

    @pytest.mark.anyio
    async def test_register_with_indexer_object(self, tmp_db_path_str: str):
        """register_indexer with an Indexer object stores it and delegates."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            indexers = await ns.list_indexers()
            assert len(indexers) == 1

    @pytest.mark.anyio
    async def test_register_with_fxtr_id(self, tmp_db_path_str: str):
        """register_indexer with a FxtrID delegates directly."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            ref = _SCORE_INDEXER.store()
            await ns.register_indexer(ref.entity_id)

            indexers = await ns.list_indexers()
            assert len(indexers) == 1

    @pytest.mark.anyio
    async def test_register_with_entity_ref(self, tmp_db_path_str: str):
        """register_indexer with an EntityRef extracts the ID and delegates."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            ref = _SCORE_INDEXER.store()
            await ns.register_indexer(ref)

            indexers = await ns.list_indexers()
            assert len(indexers) == 1


# =============================================================================
# Tests: unregister_indexer
# =============================================================================


class TestUnregisterIndexer:
    """Tests for FxtrQueryableNamespace.unregister_indexer."""

    @pytest.mark.anyio
    async def test_unregister_with_indexer_object(self, tmp_db_path_str: str):
        """unregister_indexer with an Indexer object works."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)
            indexers = await ns.list_indexers()
            assert len(indexers) == 1

            await ns.unregister_indexer(_SCORE_INDEXER)
            indexers = await ns.list_indexers()
            assert len(indexers) == 0

    @pytest.mark.anyio
    async def test_unregister_with_fxtr_id(self, tmp_db_path_str: str):
        """unregister_indexer with a FxtrID delegates directly."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            ref = _SCORE_INDEXER.store()
            await ns.register_indexer(ref.entity_id)
            await ns.unregister_indexer(ref.entity_id)

            indexers = await ns.list_indexers()
            assert len(indexers) == 0


# =============================================================================
# Tests: list_indexers
# =============================================================================


class TestListIndexers:
    """Tests for FxtrQueryableNamespace.list_indexers."""

    @pytest.mark.anyio
    async def test_list_empty_namespace(self, tmp_db_path_str: str):
        """list_indexers on empty namespace returns empty list."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            indexers = await ns.list_indexers()
            assert indexers == []

    @pytest.mark.anyio
    async def test_list_returns_entity_refs(self, tmp_db_path_str: str):
        """list_indexers returns EntityRef[WellKnownBuiltinIndexer] that can be loaded."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            indexers = await ns.list_indexers()
            assert len(indexers) == 1

            ref, prefix = indexers[0]
            assert isinstance(ref, EntityRef)
            assert prefix == ""

            # The ref should be loadable
            loaded = await ref.load()
            assert isinstance(loaded, (TypedPathValueIndexer, TypedPathExistsIndexer))
            assert loaded == _SCORE_INDEXER

    @pytest.mark.anyio
    async def test_list_with_key_prefix(self, tmp_db_path_str: str):
        """list_indexers returns the key prefix for each registration."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER, key_prefix="group/")

            indexers = await ns.list_indexers()
            assert len(indexers) == 1
            _, prefix = indexers[0]
            assert prefix == "group/"

    @pytest.mark.anyio
    async def test_list_multiple_indexers(self, tmp_db_path_str: str):
        """list_indexers with multiple registered indexers returns all."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)
            await ns.register_indexer(_NAME_INDEXER)

            indexers = await ns.list_indexers()
            assert len(indexers) == 2

    @pytest.mark.anyio
    async def test_list_with_copy_into_scope(self, tmp_db_path_str: str):
        """list_indexers with copy_into_scope=True copies entities into a separate scope.

        Regression test: copy_from_scope must use the namespace scope as source,
        not the active scope (which may be a child that doesn't have the entity).
        """
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            # Create an isolated target scope (not a child of the namespace scope)
            async with FxtrScope(isolated=True) as target:
                indexers = await ns.list_indexers(into_scope=target, copy_into_scope=True)
                assert len(indexers) == 1

                ref, _ = indexers[0]
                # The entity should be tracked in the target scope
                assert target.is_tracked(ref.entity_id)

    @pytest.mark.anyio
    async def test_list_scope_mismatch_raises(self, tmp_db_path_str: str):
        """list_indexers raises ValueError when target scope is not a child of namespace scope."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            # Create an isolated scope that is NOT a child of the namespace scope
            async with FxtrScope(isolated=True) as isolated:
                # Trying to lift without copying should fail
                with pytest.raises(ValueError, match="list_indexers"):
                    await ns.list_indexers(into_scope=isolated)


# =============================================================================
# Tests: iter_matches
# =============================================================================


class TestIterMatches:
    """Tests for FxtrQueryableNamespace.iter_matches."""

    @pytest.mark.anyio
    async def test_basic_query(self, tmp_db_path_str: str):
        """Basic query returns matching entities."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            e = _entity(name="hello")
            eid = e.store_into(get_active_scope())
            await ns.pin("pin1", eid)

            constraint = _NAME_INDEXER.bind((), "value = 'hello'")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1

            key, entity_id, bindings = results[0]
            assert key == "pin1"
            assert entity_id == eid
            assert bindings == {}

    @pytest.mark.anyio
    async def test_no_matches(self, tmp_db_path_str: str):
        """Query with no matching entities yields nothing."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            constraint = _NAME_INDEXER.bind((), "value = 'nonexistent'")
            results = [r async for r in ns.iter_matches(constraint)]
            assert results == []

    @pytest.mark.anyio
    async def test_with_binding_variables(self, tmp_db_path_str: str):
        """Binding variables are returned in results."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            e = _entity(items=[{"score": 7.7}, {"score": 3.3}])
            eid = e.store_into(get_active_scope())
            await ns.pin("pin1", eid)

            constraint = _SCORE_INDEXER.bind(("idx",), "value > 5")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1
            _, _, bindings = results[0]
            assert bindings == {"idx": 0}

    @pytest.mark.anyio
    async def test_multiple_constraints_join(self, tmp_db_path_str: str):
        """Multiple constraints are joined on shared variables."""
        name_at_idx = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker(), PathKey(key="name")),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.STR,
        )

        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)
            await ns.register_indexer(name_at_idx)

            e = _entity(
                items=[
                    {"score": 3.0, "name": "low"},
                    {"score": 8.0, "name": "high"},
                ]
            )
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            c1 = _SCORE_INDEXER.bind(("i",), "value > 5")
            c2 = name_at_idx.bind(("i",), "value = 'high'")
            results = [r async for r in ns.iter_matches(c1, c2)]
            assert len(results) == 1
            _, _, bindings = results[0]
            assert bindings["i"] == 1

    @pytest.mark.anyio
    async def test_with_binding_constraint(self, tmp_db_path_str: str):
        """BindingConstraint applies a global condition on joined variables."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            e = _entity(items=[{"score": 1.0}, {"score": 2.0}, {"score": 3.0}])
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            constraint = _SCORE_INDEXER.bind(("i",), "1=1")
            binding = BindingConstraint(condition="$i > 0")
            results = [r async for r in ns.iter_matches(constraint, where=binding)]
            assert len(results) == 2  # indices 1 and 2

    @pytest.mark.anyio
    async def test_with_key_prefix(self, tmp_db_path_str: str):
        """key_prefix on iter_matches filters by pin key."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            for name in ["alpha", "beta"]:
                e = _entity(name=name)
                eid = e.store_into(get_active_scope())
                await ns.pin(f"group/{name}", eid)

            e_other = _entity(name="gamma")
            eid_other = e_other.store_into(get_active_scope())
            await ns.pin("other/gamma", eid_other)

            constraint = _NAME_INDEXER.bind((), "1=1")
            results = [r async for r in ns.iter_matches(constraint, key_prefix="group/")]
            assert len(results) == 2
            keys = sorted(r[0] for r in results)
            assert keys == ["group/alpha", "group/beta"]

    @pytest.mark.anyio
    async def test_results_tracked_in_scope(self, tmp_db_path_str: str):
        """Matched entities are tracked in the target scope."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            e = _entity(name="test")
            eid = e.store_into(get_active_scope())
            await ns.pin("pin1", eid)

            constraint = _NAME_INDEXER.bind((), "1=1")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1

            _, entity_id, _ = results[0]
            # Entity should be tracked in the active scope
            assert get_active_scope().is_tracked(entity_id)

    @pytest.mark.anyio
    async def test_results_with_copy_into_scope(self, tmp_db_path_str: str):
        """iter_matches with copy_into_scope copies entities to target scope.

        Regression test: copy_from_scope must use the namespace scope as source.
        """
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            e = _entity(name="test")
            eid = e.store_into(get_active_scope())
            await ns.pin("pin1", eid)

            constraint = _NAME_INDEXER.bind((), "1=1")

            # Create an isolated target scope (not a child of the namespace scope)
            async with FxtrScope(isolated=True) as target:
                results = [
                    r
                    async for r in ns.iter_matches(
                        constraint, into_scope=target, copy_into_scope=True
                    )
                ]
                assert len(results) == 1
                _, entity_id, _ = results[0]
                assert target.is_tracked(entity_id)

    @pytest.mark.anyio
    async def test_scope_mismatch_raises(self, tmp_db_path_str: str):
        """iter_matches raises ValueError when target scope is not a child of namespace scope."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            constraint = _NAME_INDEXER.bind((), "1=1")
            # Create an isolated scope that is NOT a child of the namespace scope
            async with FxtrScope(isolated=True) as isolated:
                with pytest.raises(ValueError, match="iter_matches"):
                    async for _ in ns.iter_matches(constraint, into_scope=isolated):
                        pass


# =============================================================================
# Tests: full cycle integration
# =============================================================================


class TestFullCycle:
    """End-to-end integration tests through the user-facing API."""

    @pytest.mark.anyio
    async def test_register_pin_query_unpin_unregister(self, tmp_db_path_str: str):
        """Full lifecycle: register → pin → query → unpin → query empty → unregister."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            # Register
            await ns.register_indexer(_SCORE_INDEXER)

            # Pin
            e = _entity(items=[{"score": 4.5}, {"score": 2.1}])
            eid = e.store_into(get_active_scope())
            await ns.pin("e1", eid)

            # Query
            constraint = _SCORE_INDEXER.bind(("i",), "value > 3")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1
            key, entity_id, bindings = results[0]
            assert key == "e1"
            assert entity_id == eid
            assert bindings["i"] == 0  # score 4.5 is at index 0

            # Unpin
            await ns.unpin("e1")
            results = [r async for r in ns.iter_matches(constraint)]
            assert results == []

            # Unregister
            await ns.unregister_indexer(_SCORE_INDEXER)
            indexers = await ns.list_indexers()
            assert indexers == []

    @pytest.mark.anyio
    async def test_pin_overwrite_updates_index(self, tmp_db_path_str: str):
        """Overwriting a pin updates index data."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            # Pin original
            e1 = _entity(items=[{"score": 1.0}])
            e1_id = e1.store_into(get_active_scope())
            await ns.pin("pin1", e1_id)

            constraint = _SCORE_INDEXER.bind(("i",), "1=1")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1

            # Overwrite with different data
            e2 = _entity(items=[{"score": 9.0}, {"score": 8.0}])
            e2_id = e2.store_into(get_active_scope())
            await ns.pin("pin1", e2_id, overwrite=True)

            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 2
            for _, entity_id, _ in results:
                assert entity_id == e2_id

    @pytest.mark.anyio
    async def test_multiple_indexers_query(self, tmp_db_path_str: str):
        """Register two indexers, query with join."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)
            await ns.register_indexer(_EXISTS_INDEXER)

            e = _entity(items=[{"score": 5.0}, {"score": 10.0}])
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            c1 = _SCORE_INDEXER.bind(("i",), "value > 7")
            c2 = _EXISTS_INDEXER.bind(("i",), "1=1")
            results = [r async for r in ns.iter_matches(c1, c2)]
            # Only index 1 has score > 7
            assert len(results) == 1
            _, _, bindings = results[0]
            assert bindings["i"] == 1

    @pytest.mark.anyio
    async def test_query_with_convenience_api(self, tmp_db_path_str: str):
        """TypedPathValueIndexer.query() convenience works through iter_matches."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            indexer = TypedPathValueIndexer.parse("!.name: str")
            await ns.register_indexer(indexer)

            e = _entity(name="hello")
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            constraint = indexer.bind((), "value = 'hello'")
            results = [r async for r in ns.iter_matches(constraint)]
            assert len(results) == 1
            assert results[0][0] == "e"


# =============================================================================
# Tests: retrieve copy_into_scope regression
# =============================================================================


class TestRetrieveCopyRegression:
    """Regression test for retrieve with copy_into_scope.

    copy_from_scope must use the namespace scope as source, not the active scope.
    Without the source_scope fix, this fails when the active scope differs from
    the namespace scope (e.g., when copying into an isolated scope).
    """

    @pytest.mark.anyio
    async def test_retrieve_copy_into_isolated_scope(self, tmp_db_path_str: str):
        """retrieve with copy_into_scope=True works with an isolated target scope."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            e = _entity(name="test")
            eid = e.store_into(get_active_scope())
            await ns.pin("pin1", eid)

            # Create an isolated target scope (not a child of namespace scope)
            async with FxtrScope(isolated=True) as target:
                result = await ns.retrieve("pin1", into_scope=target, copy_into_scope=True)
                assert result == eid
                assert target.is_tracked(result)


# =============================================================================
# Tests: sql() method
# =============================================================================


class TestDirectSQL:
    """Tests for FxtrQueryableNamespace.sql()."""

    @pytest.mark.anyio
    async def test_basic_single_view(self, tmp_db_path_str: str):
        """Basic sql() with a single view and single column."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            e = _entity(items=[{"score": 4.5}, {"score": 2.1}, {"score": 7.0}])
            eid = e.store_into(get_active_scope())
            await ns.pin("data/e1", eid)

            rows = await ns.sql(
                "SELECT i, score, pin_name FROM v ORDER BY score",
                views=dict(
                    v=QueryView(
                        score=_SCORE_INDEXER.bind(("i",), "1=1"),
                    ),
                ),
            )
            assert len(rows) == 3
            # Check ordering
            scores = [r["score"] for r in rows]
            assert scores == sorted(scores)
            # Check pin_name
            for r in rows:
                assert r["pin_name"] == "data/e1"

    @pytest.mark.anyio
    async def test_prefix_filtering(self, tmp_db_path_str: str):
        """sql() with prefix limits to matching pins."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            for name in ["alpha", "beta"]:
                e = _entity(name=name)
                eid = e.store_into(get_active_scope())
                await ns.pin(f"group/{name}", eid)

            e_other = _entity(name="gamma")
            eid_other = e_other.store_into(get_active_scope())
            await ns.pin("other/gamma", eid_other)

            rows = await ns.sql(
                "SELECT name, pin_name FROM v ORDER BY name",
                views=dict(
                    v=QueryView(
                        "group/",
                        name=_NAME_INDEXER.bind((), "1=1"),
                    ),
                ),
            )
            assert len(rows) == 2
            names = [r["name"] for r in rows]
            assert names == ["alpha", "beta"]

    @pytest.mark.anyio
    async def test_aggregation(self, tmp_db_path_str: str):
        """sql() with GROUP BY and AVG."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            e1 = _entity(items=[{"score": 4.0}, {"score": 6.0}])
            e2 = _entity(items=[{"score": 10.0}])
            e1_id = e1.store_into(get_active_scope())
            e2_id = e2.store_into(get_active_scope())
            await ns.pin("e1", e1_id)
            await ns.pin("e2", e2_id)

            rows = await ns.sql(
                "SELECT pin_name, AVG(score) AS avg_score FROM v GROUP BY pin_name ORDER BY"
                " pin_name",
                views=dict(
                    v=QueryView(
                        score=_SCORE_INDEXER.bind(("i",), "1=1"),
                    ),
                ),
            )
            assert len(rows) == 2
            assert rows[0]["pin_name"] == "e1"
            assert abs(rows[0]["avg_score"] - 5.0) < 0.001
            assert rows[1]["pin_name"] == "e2"
            assert abs(rows[1]["avg_score"] - 10.0) < 0.001

    @pytest.mark.anyio
    async def test_two_columns_shared_variable(self, tmp_db_path_str: str):
        """sql() with two indexer columns sharing a variable."""
        name_at_idx = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=False,
            path=(PathKey(key="items"), VarMarker(), PathKey(key="name")),
            var_types=(IndexerColumnType.INT,),
            value_type=IndexerColumnType.STR,
        )

        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)
            await ns.register_indexer(name_at_idx)

            e = _entity(
                items=[
                    {"score": 4.5, "name": "low"},
                    {"score": 8.0, "name": "high"},
                ]
            )
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            rows = await ns.sql(
                "SELECT i, score, item_name FROM v ORDER BY i",
                views=dict(
                    v=QueryView(
                        score=_SCORE_INDEXER.bind(("i",), "1=1"),
                        item_name=name_at_idx.bind(("i",), "1=1"),
                    ),
                ),
            )
            assert len(rows) == 2
            assert rows[0]["i"] == 0
            assert rows[0]["score"] == 4.5
            assert rows[0]["item_name"] == "low"
            assert rows[1]["i"] == 1
            assert rows[1]["score"] == 8.0
            assert rows[1]["item_name"] == "high"

    @pytest.mark.anyio
    async def test_two_views_joined(self, tmp_db_path_str: str):
        """sql() with two views from different prefixes joined in user query."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)
            await ns.register_indexer(_SCORE_INDEXER, key_prefix="scores/")

            # Create name entities
            e_alpha = _entity(name="alpha")
            eid_alpha = e_alpha.store_into(get_active_scope())
            await ns.pin("names/alpha", eid_alpha)

            # Create score entity
            e_scores = _entity(items=[{"score": 9.5}])
            eid_scores = e_scores.store_into(get_active_scope())
            await ns.pin("scores/alpha", eid_scores)

            rows = await ns.sql(
                """
                SELECT names.name, scores.score
                FROM names
                JOIN scores ON scores.pin_name = ('scores/' || names.name)
                """,
                views=dict(
                    names=QueryView(
                        "names/",
                        name=_NAME_INDEXER.bind((), "1=1"),
                    ),
                    scores=QueryView(
                        "scores/",
                        score=_SCORE_INDEXER.bind(("i",), "1=1"),
                    ),
                ),
            )
            assert len(rows) == 1
            assert rows[0]["name"] == "alpha"
            assert rows[0]["score"] == 9.5

    @pytest.mark.anyio
    async def test_entity_id_column(self, tmp_db_path_str: str):
        """entity_id column returns the FxtrID bytes."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_NAME_INDEXER)

            e = _entity(name="test")
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            rows = await ns.sql(
                "SELECT entity_id FROM v",
                views=dict(
                    v=QueryView(name=_NAME_INDEXER.bind((), "1=1")),
                ),
            )
            assert len(rows) == 1
            # entity_id should be the raw FxtrID bytes
            raw_bytes = rows[0]["entity_id"]
            assert isinstance(raw_bytes, bytes)
            assert raw_bytes == eid._raw_data_id

    @pytest.mark.anyio
    async def test_unsupported_dialect_raises(self, tmp_db_path_str: str):
        """sql() raises ValueError for unsupported dialect."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            with pytest.raises(ValueError, match="Unsupported dialect"):
                await ns.sql(
                    "SELECT 1",
                    views=dict(v=QueryView(name=_NAME_INDEXER.bind((), "1=1"))),
                    dialect="postgres",
                )

    @pytest.mark.anyio
    async def test_empty_views_raises(self, tmp_db_path_str: str):
        """sql() raises ValueError for empty views dict."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            with pytest.raises(ValueError, match="[Aa]t least one view"):
                await ns.sql("SELECT 1", views={})

    @pytest.mark.anyio
    async def test_unregistered_indexer_raises(self, tmp_db_path_str: str):
        """sql() raises KeyError when indexer is not registered."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            with pytest.raises(KeyError):
                await ns.sql(
                    "SELECT * FROM v",
                    views=dict(
                        v=QueryView(
                            score=_SCORE_INDEXER.bind(("i",), "1=1"),
                        ),
                    ),
                )

    @pytest.mark.anyio
    async def test_per_constraint_condition(self, tmp_db_path_str: str):
        """Conditions in IndexerConstraint are applied inside the CTE."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            e = _entity(items=[{"score": 1.0}, {"score": 5.0}, {"score": 9.0}])
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            rows = await ns.sql(
                "SELECT score FROM v ORDER BY score",
                views=dict(
                    v=QueryView(
                        score=_SCORE_INDEXER.bind(("i",), "value > 3"),
                    ),
                ),
            )
            assert len(rows) == 2
            assert rows[0]["score"] == 5.0
            assert rows[1]["score"] == 9.0

    @pytest.mark.anyio
    async def test_convenience_query_api(self, tmp_db_path_str: str):
        """sql() works with TypedPathValueIndexer.query() convenience."""
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            # Register an indexer for the query expression
            indexer = TypedPathValueIndexer.parse("!.items[*: int].score: float")
            await ns.register_indexer(indexer)

            e = _entity(items=[{"score": 3.0}, {"score": 7.0}])
            eid = e.store_into(get_active_scope())
            await ns.pin("e", eid)

            rows = await ns.sql(
                "SELECT i, score FROM v ORDER BY score",
                views=dict(
                    v=QueryView(
                        score=TypedPathValueIndexer.query(
                            "!.items[$i: int].score: float",
                            "value > 5",
                        ),
                    ),
                ),
            )
            assert len(rows) == 1
            assert rows[0]["score"] == 7.0
            assert rows[0]["i"] == 1
