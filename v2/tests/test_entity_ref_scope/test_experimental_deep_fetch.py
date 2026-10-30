"""Tests for the experimental _deep_fetch functionality on FxtrScope."""

from __future__ import annotations

import dataclasses
from typing import Any, override

import pytest

from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.abstract_arena import DeepFetchable, ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleNode(DataclassEntity, fxtr_type=FxtrID.builtin("test_df_simple")):
    name: str


@dataclasses.dataclass(frozen=True)
class NodeWithRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_df_with_ref")):
    name: str
    child: ChildRef[SimpleNode]


def _track_builtins(scope: FxtrScope) -> None:
    scope.track_builtin(FxtrID.builtin("test_df_simple"))
    scope.track_builtin(FxtrID.builtin("test_df_with_ref"))


# =============================================================================
# Mock DeepFetchable
# =============================================================================


class MockDeepFetchable(DeepFetchable[Any]):
    """A ``DeepFetchable`` that delegates to a source scope's memory store.

    Registered on a consumer scope by cloning ``source_scope._scope_auth_lease`` as the
    key lease. The narrowed ``DeepFetchable.try_lookup(node_id)`` has no per-call
    lease; this mock delegates to the source scope's store using the source's
    main lease (same as before the backend-lease rollback for ``try_lookup``).
    """

    def __init__(self, source_scope: FxtrScope):
        self._source_scope = source_scope
        self.try_lookup_calls: list[FxtrID] = []

    @override
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena[Any] | None:
        self.try_lookup_calls.append(node_id)
        return await self._source_scope._global_store.try_lookup_with_lease(
            self._source_scope.clone_authorization(), node_id
        )


def _register_mock(consumer: FxtrScope, mock: MockDeepFetchable) -> None:
    """Register ``mock`` on ``consumer`` using a clone of the source scope's lease
    as the bridge (case 1: key and Y-brand match, no converter needed)."""
    from fxtr2.leases.lease_bridges import IndependentLocalBridge

    consumer.register_deep_fetch_source(
        mock,
        IndependentLocalBridge(lease=mock._source_scope.clone_authorization()),
    )


# =============================================================================
# Tests
# =============================================================================


class TestDeepFetchFlags:
    """Tests for the deeply-fetchable flag mechanics."""

    @pytest.mark.anyio
    async def test_root_scope_deeply_fetchable(self):
        """Root scope defaults to deeply fetchable."""
        async with FxtrScope(name="root") as scope:
            assert scope._is_deeply_fetchable()

    @pytest.mark.anyio
    async def test_child_inherits_deeply_fetchable(self):
        """Child of deeply-fetchable parent is deeply fetchable."""
        async with FxtrScope(name="parent") as parent:
            async with FxtrScope(include=[parent], name="child") as child:
                assert child._is_deeply_fetchable()

    @pytest.mark.anyio
    async def test_child_inherits_not_deeply_fetchable(self):
        """Child of not-deeply-fetchable parent is not deeply fetchable."""
        parent = FxtrScope(name="parent")
        parent._mark_not_deeply_fetchable()
        async with parent:
            async with FxtrScope(include=[parent], name="child") as child:
                assert not child._is_deeply_fetchable()

    @pytest.mark.anyio
    async def test_mark_not_deeply_fetchable(self):
        """Explicit marking before entry works."""
        scope = FxtrScope(name="test")
        scope._mark_not_deeply_fetchable()
        async with scope:
            assert not scope._is_deeply_fetchable()

    @pytest.mark.anyio
    async def test_mark_not_deeply_fetchable_after_true_raises(self):
        """Can't mark False after already marked True."""
        async with FxtrScope(name="test") as scope:
            with pytest.raises(RuntimeError, match="already been marked"):
                scope._mark_not_deeply_fetchable()


class TestDeepFetchLookup:
    """Tests for the _deep_fetch lookup behavior."""

    @pytest.mark.anyio
    async def test_deep_fetch_finds_entity_in_main_store(self):
        """Entity tracked in scope's main arena is found via _deep_fetch."""
        async with FxtrScope(name="test") as scope:
            _track_builtins(scope)
            ref = scope.store(SimpleNode(name="hello"))
            arena = await scope._deep_fetch(ref.entity_id)
            assert arena is not None
            arena.close()

    @pytest.mark.anyio
    async def test_deep_fetch_finds_entity_in_deep_fetch_source(self):
        """Entity in a DeepFetchable added to _deep_fetch_sources is found."""
        async with FxtrScope(name="source") as source_scope:
            _track_builtins(source_scope)
            ref = source_scope.store(SimpleNode(name="hello"))
            entity_id = ref.entity_id

            async with FxtrScope(name="consumer", isolated=True) as consumer_scope:
                mock_store = MockDeepFetchable(source_scope)
                _register_mock(consumer_scope, mock_store)

                arena = await consumer_scope._deep_fetch(entity_id)
                assert arena is not None
                arena.close()

                # Verify the store was consulted
                assert entity_id in mock_store.try_lookup_calls

    @pytest.mark.anyio
    async def test_deep_fetch_finds_entity_in_parent(self):
        """Entity in parent scope is found via recursion."""
        async with FxtrScope(name="parent") as parent:
            _track_builtins(parent)
            ref = parent.store(SimpleNode(name="hello"))
            entity_id = ref.entity_id

            async with FxtrScope(include=[parent], name="child") as child:
                arena = await child._deep_fetch(entity_id)
                assert arena is not None
                arena.close()

    @pytest.mark.anyio
    async def test_deep_fetch_returns_none_for_missing(self):
        """Returns None for unknown ID."""
        async with FxtrScope(name="test") as scope:
            # Fabricate a data node ID that was never stored
            fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)
            result = await scope._deep_fetch(fake_id)
            assert result is None

    @pytest.mark.anyio
    async def test_deep_fetch_raises_if_not_deeply_fetchable(self):
        """Raises RuntimeError if scope not deeply fetchable."""
        scope = FxtrScope(name="test")
        scope._mark_not_deeply_fetchable()
        async with scope:
            _track_builtins(scope)
            ref = scope.store(SimpleNode(name="hello"))
            with pytest.raises(RuntimeError, match="not deeply fetchable"):
                await scope._deep_fetch(ref.entity_id)

    @pytest.mark.anyio
    async def test_deep_fetch_skips_store_lookup_for_cached_entity(self):
        """After deep-fetching A (which references B), deep-fetching B should use
        the cached arena without calling try_lookup on the deep-fetch sources again."""
        async with FxtrScope(name="source") as source_scope:
            _track_builtins(source_scope)
            child_ref = source_scope.store(SimpleNode(name="child_B"))
            parent_ref = source_scope.store(NodeWithRef(name="parent_A", child=child_ref))

            async with FxtrScope(name="consumer", isolated=True) as consumer_scope:
                mock_store = MockDeepFetchable(source_scope)
                _register_mock(consumer_scope, mock_store)

                # Deep fetch A — should call try_lookup on the mock store
                arena_a = await consumer_scope._deep_fetch(parent_ref.entity_id)
                assert arena_a is not None

                # Load A to bring its children into the store
                await arena_a.load(consumer_scope.clone_authorization(), parent_ref.entity_id)
                arena_a.close()

                # Record how many lookups happened so far
                lookups_after_a = len(mock_store.try_lookup_calls)

                # Deep fetch B — should hit the cached store, not the mock
                arena_b = await consumer_scope._deep_fetch(child_ref.entity_id)
                assert arena_b is not None
                arena_b.close()

                # Verify try_lookup was NOT called again for B
                assert len(mock_store.try_lookup_calls) == lookups_after_a


@pytest.mark.skip(
    reason="Chunk 2: requires SQLiteFxtrNamespace to be migrated to register_deep_fetch_source"
)
class TestDeepFetchWithSqliteNamespace:
    """Tests that SQLiteFxtrNamespace integrates with deep fetch. Re-enable after
    Chunk 2 migration of SQLite/fxtr_context_api."""
