"""Tests for thingwatcher.fxtr.track_helpers module."""

from __future__ import annotations

import anyio
import pytest

# Skip all tests if thingwatcher is not installed
pytest.importorskip("thingwatcher")

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID
from fxtr2.data.fxtr_scope import FxtrScope
from thingwatcher.api import FxtrSupportDisabledError, NoWatcherConnectedError
from thingwatcher.fxtr import track_id
from thingwatcher.watcher import Watcher


class TestTrackId:
    """Tests for track_id() function."""

    def test_track_id_requires_watcher(self):
        """Test that track_id raises when no watcher is active."""
        fxtr_id = FxtrID.from_id_string(".")

        with pytest.raises(NoWatcherConnectedError, match="track_id requires an active watcher"):
            track_id(fxtr_id)

    @pytest.mark.anyio
    async def test_track_id_requires_fxtr_support(self, tmp_path):
        """Test that track_id raises when fxtr_support=False."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=False)

        async with watcher.watching_and_serving():
            fxtr_id = FxtrID.from_id_string(".")

            with pytest.raises(
                FxtrSupportDisabledError,
                match="track_fxtr_id requires fxtr_support=True",
            ):
                track_id(fxtr_id)

    @pytest.mark.anyio
    async def test_track_id_requires_tracked_entity(self, tmp_path):
        """Test that track_id raises when entity is not tracked in scope."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            # Create an entity in one scope
            async with FxtrScope() as scope1:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope1._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "value": 42,
                    }
                )

            # Try to track it from a different scope where it's not tracked
            async with FxtrScope() as scope2:
                with pytest.raises(ValueError, match="is not tracked in the source scope"):
                    track_id(entity_id, from_scope=scope2)

    @pytest.mark.anyio
    async def test_track_id_returns_correct_key(self, tmp_path):
        """Test that track_id returns the correct thing_key."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            # Create a test entity in a scope
            async with FxtrScope() as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "value": 42,
                    }
                )

                thing_key = track_id(entity_id)

                # Should return ["fxtr_entity", id_string]
                assert thing_key == ["fxtr_entity", entity_id.id_string()]

    @pytest.mark.anyio
    async def test_track_id_deduplicates(self, tmp_path):
        """Test that tracking the same ID twice only queues one copy."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            async with FxtrScope() as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "value": 42,
                    }
                )

                # Track the same ID twice
                key1 = track_id(entity_id)
                key2 = track_id(entity_id)

                # Should return the same key
                assert key1 == key2

                # Should only have one tracked ID
                assert len(watcher._fxtr_tracked_ids) == 1

    @pytest.mark.anyio
    async def test_track_id_uses_active_scope_by_default(self, tmp_path):
        """Test that track_id uses ACTIVE_SCOPE when from_scope is None."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            async with FxtrScope() as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "value": 42,
                    }
                )

                # Call without explicit scope
                thing_key = track_id(entity_id)

                # Should work and return the right key
                assert thing_key == ["fxtr_entity", entity_id.id_string()]

    @pytest.mark.anyio
    async def test_track_id_entity_becomes_available(self, tmp_path):
        """Test that tracked entity becomes available via the immutable store."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            async with FxtrScope() as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "value": 42,
                    }
                )

                thing_key = track_id(entity_id)

                # Give the background worker time to process
                await anyio.sleep(0.1)

                # Entity should be available via the internal immutable store
                store = watcher._immutable_stores.get("_fxtr_internal")
                assert store is not None

                thing_state = await store.get_thing_state(thing_key)
                assert thing_state is not None
                assert thing_state.key == thing_key
                # The entity data should include the "value" field
                assert thing_state.thing_data.get("value") == 42

    @pytest.mark.anyio
    async def test_track_id_explicit_scope(self, tmp_path):
        """Test track_id with an explicit from_scope parameter."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            async with FxtrScope() as outer_scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = outer_scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "value": 123,
                    }
                )

                # Create a different active scope
                async with FxtrScope() as inner_scope:
                    # Track using the outer scope explicitly
                    thing_key = track_id(entity_id, from_scope=outer_scope)

                    assert thing_key == ["fxtr_entity", entity_id.id_string()]

    @pytest.mark.anyio
    async def test_track_id_in_memory_db(self):
        """Test track_id works with in-memory watcher DB."""
        # No db_path means in-memory database
        watcher = Watcher(db_path=None, port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            async with FxtrScope() as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                        "data": "test",
                    }
                )

                thing_key = track_id(entity_id)
                assert thing_key == ["fxtr_entity", entity_id.id_string()]

                # Wait for copy
                await anyio.sleep(0.1)

                # Should be available
                store = watcher._immutable_stores.get("_fxtr_internal")
                thing_state = await store.get_thing_state(thing_key)
                assert thing_state is not None


class TestWatcherTrackFxtrId:
    """Tests for Watcher.track_fxtr_id() method directly."""

    @pytest.mark.anyio
    async def test_watcher_track_fxtr_id_basic(self, tmp_path):
        """Test basic Watcher.track_fxtr_id() usage."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=True)

        async with watcher.watching_and_serving():
            async with FxtrScope() as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                    }
                )

                thing_key = watcher.track_fxtr_id(entity_id)
                assert thing_key == ["fxtr_entity", entity_id.id_string()]

    @pytest.mark.anyio
    async def test_watcher_track_fxtr_id_disabled(self, tmp_path):
        """Test that track_fxtr_id raises when fxtr_support is disabled."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=str(db_path), port=None, fxtr_support=False)

        async with watcher.watching_and_serving():
            fxtr_id = FxtrID.from_id_string(".")

            with pytest.raises(FxtrSupportDisabledError):
                watcher.track_fxtr_id(fxtr_id)
