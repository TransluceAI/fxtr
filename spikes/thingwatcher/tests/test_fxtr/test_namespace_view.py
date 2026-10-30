"""Tests for thingwatcher.fxtr.namespace_view module."""

from __future__ import annotations

import pytest

# Skip all tests if thingwatcher is not installed
pytest.importorskip("thingwatcher")

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.fxtr_scope import FxtrScope
from thingwatcher.api import ThingRef, ThingState
from thingwatcher.fxtr.namespace_view import fxtr_namespace_view
from thingwatcher.fxtr.snapshotter import FxtrNamespaceAnchorCapturer, FxtrNamespaceSnapshotter
from thingwatcher.watcher import Watcher


class TestFxtrNamespaceSnapshotter:
    """Tests for FxtrNamespaceSnapshotter."""

    @pytest.mark.anyio
    async def test_snapshotter_returns_async_thing_update(self):
        """Test that snapshotter returns an AsyncThingUpdate."""
        async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
            snapshotter = FxtrNamespaceSnapshotter(ns._get_backend(), "test_ns")

            result = snapshotter.stateless_snapshot(None)

            # Should be an AsyncThingUpdate
            assert hasattr(result, "immediate_update")
            assert hasattr(result, "async_updater")

            # Immediate update should be a ThingState marked as stale
            immediate = result.immediate_update
            assert isinstance(immediate, ThingState)
            assert immediate.key == ["fxtr_namespace", "test_ns"]
            assert immediate.thing_data["stale"] is True

    @pytest.mark.anyio
    async def test_snapshotter_async_updater_fetches_pins(self):
        """Test that the async updater fetches actual pins."""
        async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
            # Pin an entity
            async with FxtrScope(include=[ns.scope]) as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_data = {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                    "name": "test",
                }
                entity_id = scope._main_arena.store(entity_data)
                await ns.pin("my_pin", entity_id)

            snapshotter = FxtrNamespaceSnapshotter(ns._get_backend(), "test_ns")
            result = snapshotter.stateless_snapshot(None)

            # Run the async updater
            fresh_state = await result.async_updater()

            assert isinstance(fresh_state, ThingState)
            assert fresh_state.thing_data["stale"] is False
            assert fresh_state.thing_data["pin_count"] == 1

            # Should have a link to the pinned entity
            assert len(fresh_state.links) == 1
            assert fresh_state.links[0].link_type == "pin"
            assert fresh_state.links[0].link_key == ["pins", "my_pin"]

    @pytest.mark.anyio
    async def test_snapshotter_caches_pins(self):
        """Test that snapshotter caches pins between calls."""
        async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
            # Pin an entity
            async with FxtrScope(include=[ns.scope]) as scope:
                ground_ctx = FxtrID.from_id_string(".")
                entity_id = scope._main_arena.store(
                    {
                        "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                    }
                )
                await ns.pin("my_pin", entity_id)

            snapshotter = FxtrNamespaceSnapshotter(ns._get_backend(), "test_ns")

            # First call - immediate should be empty (no cache)
            result1 = snapshotter.stateless_snapshot(None)
            assert result1.immediate_update.thing_data["pin_count"] == 0

            # Run async updater to populate cache
            await result1.async_updater()

            # Second call - immediate should have cached data
            result2 = snapshotter.stateless_snapshot(None)
            assert result2.immediate_update.thing_data["pin_count"] == 1


class TestFxtrNamespaceAnchorCapturer:
    """Tests for FxtrNamespaceAnchorCapturer."""

    def test_anchor_capturer_returns_thing_ref(self):
        """Test that anchor capturer returns a ThingRef."""
        capturer = FxtrNamespaceAnchorCapturer("my_namespace")

        result = capturer.handle(None)

        assert isinstance(result, ThingRef)
        assert result.key == ["fxtr_namespace", "my_namespace"]


class TestFxtrNamespaceView:
    """Tests for fxtr_namespace_view context manager."""

    @pytest.mark.anyio
    async def test_namespace_view_basic(self, tmp_path):
        """Test basic namespace view usage."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=db_path, port=0)

        async with watcher.watching_and_serving():
            async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
                async with fxtr_namespace_view(ns, "my_ns", watcher):
                    # Namespace should be registered as a root
                    # (We can't easily verify this without querying GraphQL,
                    # but if no exception is raised, it worked)
                    watcher.tick("info", "Test tick")

    @pytest.mark.anyio
    async def test_namespace_view_with_pins(self, tmp_path):
        """Test namespace view shows pinned entities."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=db_path, port=0)

        async with watcher.watching_and_serving():
            async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
                # Pin an entity before entering namespace view
                async with FxtrScope(include=[ns.scope]) as scope:
                    ground_ctx = FxtrID.from_id_string(".")
                    entity_id = scope._main_arena.store(
                        {
                            "$fxtr.ctx": RawFxtrEntityMarkerID(ground_ctx),
                            "value": 42,
                        }
                    )
                    await ns.pin("data", entity_id)

                async with fxtr_namespace_view(ns, "my_ns", watcher):
                    watcher.tick("info", "After pinning")
                    # Entity should be accessible via the immutable store

    @pytest.mark.anyio
    async def test_namespace_view_cleanup_on_exit(self, tmp_path):
        """Test that namespace view cleans up on exit."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=db_path, port=0)

        async with watcher.watching_and_serving():
            async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
                async with fxtr_namespace_view(ns, "my_ns", watcher):
                    pass
                # After exiting, resources should be cleaned up
                # (If there's an error in cleanup, test would fail)

    @pytest.mark.anyio
    async def test_namespace_view_requires_watcher(self):
        """Test that namespace view raises if no watcher is provided or active."""
        async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
            with pytest.raises(ValueError, match="No watcher provided"):
                async with fxtr_namespace_view(ns, "my_ns"):
                    pass

    @pytest.mark.anyio
    async def test_namespace_view_accepts_backend_directly(self, tmp_path):
        """Test that namespace view accepts a PersistentPinningBackend directly."""
        db_path = tmp_path / "test.db"
        watcher = Watcher(db_path=db_path, port=0)

        async with watcher.watching_and_serving():
            async with SQLiteFxtrNamespace("sqlite+aiosqlite:///:memory:", "test") as ns:
                backend = ns._get_backend()
                # Pass the backend directly instead of the namespace
                async with fxtr_namespace_view(backend, "my_ns", watcher):
                    watcher.tick("info", "Test tick")
