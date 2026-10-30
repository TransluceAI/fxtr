"""Tests for thingwatcher.fxtr.immutable_store module."""

from __future__ import annotations

import pytest

# Skip all tests if thingwatcher is not installed
pytest.importorskip("thingwatcher")

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.fxtr_scope import FxtrScope
from fxtr2.data.memory_arena import MemoryStore
from thingwatcher.fxtr.immutable_store import FxtrImmutableStore


@pytest.fixture
def memory_store() -> MemoryStore:
    """Create a fresh MemoryStore for testing."""
    return MemoryStore()


class TestFxtrImmutableStore:
    """Tests for FxtrImmutableStore."""

    @pytest.mark.anyio
    async def test_get_thing_state_not_found(self, memory_store: MemoryStore):
        """Test that get_thing_state returns None for non-existent entities."""
        store = FxtrImmutableStore(memory_store)

        result = await store.get_thing_state(["fxtr_entity", "dnonexistent"])
        assert result is None

    @pytest.mark.anyio
    async def test_get_thing_state_invalid_key(self, memory_store: MemoryStore):
        """Test that get_thing_state returns None for invalid keys."""
        store = FxtrImmutableStore(memory_store)

        # Wrong format
        assert await store.get_thing_state("not a list") is None
        assert await store.get_thing_state([]) is None
        assert await store.get_thing_state(["wrong_prefix", "dtest"]) is None
        assert await store.get_thing_state(["fxtr_entity", 123]) is None

    @pytest.mark.anyio
    async def test_get_thing_state_found(self):
        """Test that get_thing_state returns ThingState for existing entities."""
        async with FxtrScope(name="test") as scope:
            # Store an entity
            ground_ctx = FxtrID.from_id_string(".")
            entity_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "name": "test entity",
                "value": 42,
            }
            entity_id = scope._main_arena.store(entity_data)

            # Create store from the scope's memory store
            store = FxtrImmutableStore(scope._store)

            # Look up the entity
            result = await store.get_thing_state(["fxtr_entity", entity_id.id_string()])

            assert result is not None
            assert result.key == ["fxtr_entity", entity_id.id_string()]
            assert result.thing_data["name"] == "test entity"
            assert result.thing_data["value"] == 42

    @pytest.mark.anyio
    async def test_get_thing_state_with_type(self):
        """Test that entities with $fxtr.type get correct thing_type."""
        async with FxtrScope(name="test") as scope:
            ground_ctx = FxtrID.from_id_string(".")
            type_id = scope._main_arena.track_builtin(FxtrID.builtin("mytype"))
            entity_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "$fxtr.type": RawWeakFxtrID(type_id),
                "data": "hello",
            }
            entity_id = scope._main_arena.store(entity_data)

            store = FxtrImmutableStore(scope._store)
            result = await store.get_thing_state(["fxtr_entity", entity_id.id_string()])

            assert result is not None
            assert result.thing_type == "fxtr.transluce.org/fxtr_entity/d/!mytype"

    @pytest.mark.anyio
    async def test_get_thing_state_extracts_links(self):
        """Test that links are extracted from entity data."""
        async with FxtrScope(name="test") as scope:
            # First create a referenced entity
            ground_ctx = FxtrID.from_id_string(".")
            ref_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "name": "referenced",
            }
            ref_id = scope._main_arena.store(ref_data)

            # Create an entity that references it
            entity_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "reference": RawWeakFxtrID(ref_id),
            }
            entity_id = scope._main_arena.store(entity_data)

            store = FxtrImmutableStore(scope._store)
            result = await store.get_thing_state(["fxtr_entity", entity_id.id_string()])

            assert result is not None

            # Should have links - one for $fxtr.ctx (marker) and one for reference (weak)
            weak_links = [l for l in result.links if l.link_type == "weak"]
            assert len(weak_links) == 1
            assert weak_links[0].link_key == ["reference"]
            assert weak_links[0].to_thing_key == ["fxtr_entity", ref_id.id_string()]

    @pytest.mark.anyio
    async def test_get_backlinks_empty(self, memory_store: MemoryStore):
        """Test that get_backlinks returns empty list for non-existent entities."""
        store = FxtrImmutableStore(memory_store)

        result = await store.get_backlinks(["fxtr_entity", "dnonexistent"])
        assert result == []

    @pytest.mark.anyio
    async def test_get_backlinks_invalid_key(self, memory_store: MemoryStore):
        """Test that get_backlinks returns empty list for invalid keys."""
        store = FxtrImmutableStore(memory_store)

        assert await store.get_backlinks("not a list") == []
        assert await store.get_backlinks(["wrong_prefix", "dtest"]) == []

    @pytest.mark.anyio
    async def test_get_backlinks_with_references(self):
        """Test that get_backlinks returns BacklinkInfo for referencing entities."""
        async with FxtrScope(name="test") as scope:
            ground_ctx = FxtrID.from_id_string(".")

            # Create an entity that will be referenced
            target_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "name": "target",
            }
            target_id = scope._main_arena.store(target_data)

            # Create entities that reference the target
            referrer1_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "ref": RawWeakFxtrID(target_id),
            }
            referrer1_id = scope._main_arena.store(referrer1_data)

            referrer2_data = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                "child": RawStrongFxtrID(target_id),
            }
            referrer2_id = scope._main_arena.store(referrer2_data)

            store = FxtrImmutableStore(scope._store)
            backlinks = await store.get_backlinks(["fxtr_entity", target_id.id_string()])

            # Should have backlinks from both referrers
            assert len(backlinks) >= 2

            # Check that we have backlinks from both referrers
            from_keys = {tuple(bl.from_thing_key) for bl in backlinks}
            assert ("fxtr_entity", referrer1_id.id_string()) in from_keys
            assert ("fxtr_entity", referrer2_id.id_string()) in from_keys

            # Check link types
            link_types = {bl.link_type for bl in backlinks}
            assert "weak" in link_types
            assert "strong" in link_types

    @pytest.mark.anyio
    async def test_get_thing_state_ground_node(self, memory_store: MemoryStore):
        """Test that ground node (.) returns a minimal ThingState without loading."""
        store = FxtrImmutableStore(memory_store)

        result = await store.get_thing_state(["fxtr_entity", "."])

        assert result is not None
        assert result.key == ["fxtr_entity", "."]
        assert result.thing_type == "fxtr.transluce.org/fxtr_entity_fallback"
        assert result.thing_data["$fxtr.node_type"] == "ground"
        assert result.links == []

    @pytest.mark.anyio
    async def test_get_thing_state_builtin_node(self, memory_store: MemoryStore):
        """Test that builtin nodes (!) return a minimal ThingState without loading."""
        store = FxtrImmutableStore(memory_store)

        result = await store.get_thing_state(["fxtr_entity", "!my_builtin_type"])

        assert result is not None
        assert result.key == ["fxtr_entity", "!my_builtin_type"]
        assert result.thing_type == "fxtr.transluce.org/fxtr_entity_fallback"
        assert result.thing_data["$fxtr.node_type"] == "builtin"
        assert result.thing_data["$fxtr.builtin_name"] == "my_builtin_type"
        assert result.links == []
