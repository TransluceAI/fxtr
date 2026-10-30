"""Tests for snapshot ownership system."""

from __future__ import annotations

import gc

import pytest
from thingwatcher import POP_ALL, SnapshotResult, ThingDelta, ThingState, Watcher, tick
from thingwatcher.api import (
    AnchorCapturer,
    StatefulOwningSnapshotter,
    StatelessSnapshotter,
    _NoPushedChunk,
    _PopAll,
)
from thingwatcher.registry import clear_global_registries

# =============================================================================
# Test fixtures and helpers
# =============================================================================


class TrackedObj:
    """Simple test class for tracking."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value


class StatelessObjSnapshotter(StatelessSnapshotter):
    """Stateless snapshotter for TrackedObj."""

    def __init__(self, key_prefix: str = "obj"):
        self.key_prefix = key_prefix

    def snapshot(self, obj: TrackedObj) -> ThingState:
        return ThingState(
            key=(self.key_prefix, obj.name),
            thing_type="TrackedObj",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


class OwningObjSnapshotter(StatefulOwningSnapshotter):
    """Owning snapshotter for TrackedObj."""

    def __init__(self, key_prefix: str = "obj"):
        self.key_prefix = key_prefix
        self._seen: dict[int, int] = {}

    def snapshot_owned(self, obj: TrackedObj) -> ThingState | ThingDelta:
        obj_id = id(obj)
        if obj_id not in self._seen:
            self._seen[obj_id] = obj.value
            return ThingState(
                key=(self.key_prefix, obj.name),
                thing_type="TrackedObj",
                thing_data={"name": obj.name, "value": obj.value},
                links=[],
            )
        elif self._seen[obj_id] != obj.value:
            self._seen[obj_id] = obj.value
            return ThingDelta(
                key=(self.key_prefix, obj.name),
                pop_chunks=POP_ALL,
                push_chunk={"name": obj.name, "value": obj.value},
            )
        else:
            return ThingDelta(
                key=(self.key_prefix, obj.name),
                # Default: no push, no pop = no data change
            )

    def stateless_snapshot(self, obj: TrackedObj) -> ThingState:
        """Return full state for hard recapture."""
        return ThingState(
            key=(self.key_prefix, obj.name),
            thing_type="TrackedObj",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


class MultiKeyOwningSnapshotter(StatefulOwningSnapshotter):
    """Owning snapshotter that produces multiple keys per object."""

    def snapshot_owned(self, obj: TrackedObj) -> list[ThingState]:
        return [
            ThingState(
                key=("multi", obj.name, "a"),
                thing_type="TrackedObj",
                thing_data={"name": obj.name, "part": "a"},
                links=[],
            ),
            ThingState(
                key=("multi", obj.name, "b"),
                thing_type="TrackedObj",
                thing_data={"name": obj.name, "part": "b"},
                links=[],
            ),
        ]

    def stateless_snapshot(self, obj: TrackedObj) -> list[ThingState]:
        """Return full state for hard recapture (same as snapshot_owned)."""
        return self.snapshot_owned(obj)


class ObjAnchorCapturer(AnchorCapturer):
    """Anchor capturer for TrackedObj."""

    def __init__(self, key_prefix: str = "obj"):
        self.key_prefix = key_prefix

    def handle(self, anchor: TrackedObj) -> ThingState:
        return ThingState(
            key=(self.key_prefix, anchor.name),
            thing_type="TrackedObj",
            thing_data={"name": anchor.name, "value": anchor.value},
            links=[],
        )


@pytest.fixture(autouse=True)
def clean_registries():
    """Clean global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


# =============================================================================
# Tests for stateless overlap (should work)
# =============================================================================


class TestStatelessOverlap:
    """Tests that stateless snapshotters can overlap with identical states."""

    @pytest.mark.asyncio
    async def test_identical_stateless_states_ok(self):
        """Two stateless snapshotters producing identical states should work."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        # Both use same key prefix, producing same key
        watcher.register_snapshotter("type_a", StatelessObjSnapshotter("shared"))
        watcher.register_snapshotter("type_b", StatelessObjSnapshotter("shared"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj_a = TrackedObj("test", 42)
            obj_b = TrackedObj("test", 42)  # Same name and value = identical state
            watcher.track(obj_a, "type_a")
            watcher.track(obj_b, "type_b")
            tick("info", "both tracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        # Should have one thing (deduplicated), not broken
        things = [s for s in captured[0].states if s.thing_type != "__broken__"]
        assert len(things) == 1
        assert things[0].thing_data == {"name": "test", "value": 42}

    @pytest.mark.asyncio
    async def test_different_stateless_states_conflict(self):
        """Two stateless snapshotters producing different states should conflict."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter("type_a", StatelessObjSnapshotter("shared"))
        watcher.register_snapshotter("type_b", StatelessObjSnapshotter("shared"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj_a = TrackedObj("test", 42)
            obj_b = TrackedObj("test", 99)  # Same name but different value = conflict
            watcher.track(obj_a, "type_a")
            watcher.track(obj_b, "type_b")
            tick("info", "both tracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        # Should have one broken thing due to conflict
        broken = [s for s in captured[0].states if s.thing_type == "__broken__"]
        assert len(broken) == 1
        assert "different data" in broken[0].thing_data["error"]


# =============================================================================
# Tests for ownership conflicts
# =============================================================================


class TestOwnershipConflicts:
    """Tests that owning snapshotters detect conflicts properly."""

    @pytest.mark.asyncio
    async def test_owning_vs_owning_conflict(self):
        """Two owning snapshotters claiming same key should conflict."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        # Both use same key prefix, producing same key
        watcher.register_snapshotter("type_a", OwningObjSnapshotter("shared"))
        watcher.register_snapshotter("type_b", OwningObjSnapshotter("shared"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj_a = TrackedObj("test", 42)
            obj_b = TrackedObj("test", 42)  # Same key
            watcher.track(obj_a, "type_a")
            watcher.track(obj_b, "type_b")
            tick("info", "both tracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        # Should have one broken thing due to ownership conflict
        broken = [s for s in captured[0].states if s.thing_type == "__broken__"]
        assert len(broken) == 1
        assert "ownership conflict" in broken[0].thing_data["error"]

    @pytest.mark.asyncio
    async def test_owning_vs_stateless_conflict(self):
        """Owning snapshotter conflicting with stateless should produce broken thing."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter("owning", OwningObjSnapshotter("shared"))
        watcher.register_snapshotter("stateless", StatelessObjSnapshotter("shared"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj_a = TrackedObj("test", 42)
            obj_b = TrackedObj("test", 42)  # Same key
            watcher.track(obj_a, "owning")
            watcher.track(obj_b, "stateless")
            tick("info", "both tracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        broken = [s for s in captured[0].states if s.thing_type == "__broken__"]
        assert len(broken) == 1
        assert "ownership conflict" in broken[0].thing_data["error"]

    @pytest.mark.asyncio
    async def test_owning_vs_anchor_capturer_conflict(self):
        """Owning snapshotter conflicting with anchor capturer should produce broken thing."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, OwningObjSnapshotter("shared"))
        watcher.register_anchor_capturer(TrackedObj, ObjAnchorCapturer("shared"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj = TrackedObj("test", 42)
            watcher.track(obj)
            watcher.add_root("myroot", obj)  # Anchor capturer will emit same key
            tick("info", "tracked and rooted")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        broken = [s for s in captured[0].states if s.thing_type == "__broken__"]
        assert len(broken) == 1
        assert "ownership conflict" in broken[0].thing_data["error"]

    @pytest.mark.asyncio
    async def test_owning_different_keys_ok(self):
        """Two owning snapshotters with different keys should work fine."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter("type_a", OwningObjSnapshotter("prefix_a"))
        watcher.register_snapshotter("type_b", OwningObjSnapshotter("prefix_b"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj_a = TrackedObj("test", 42)
            obj_b = TrackedObj("test", 42)
            watcher.track(obj_a, "type_a")
            watcher.track(obj_b, "type_b")
            tick("info", "both tracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        # Should have two things, no broken
        things = [s for s in captured[0].states if s.thing_type != "__broken__"]
        broken = [s for s in captured[0].states if s.thing_type == "__broken__"]
        assert len(things) == 2
        assert len(broken) == 0


# =============================================================================
# Tests for ownership map tracking
# =============================================================================


class TestOwnershipMaps:
    """Tests that ownership maps are correctly maintained."""

    @pytest.mark.asyncio
    async def test_ownership_maps_populated(self):
        """Ownership maps should be populated after tick."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, OwningObjSnapshotter("owned"))

        async def run():
            obj = TrackedObj("test", 42)
            watcher.track(obj)
            tick("info", "tracked")

            # Check ownership maps
            assert len(watcher._owned_things) == 1
            assert len(watcher._thing_owners) == 1

            obj_id = id(obj)
            assert obj_id in watcher._owned_things
            owned_keys = watcher._owned_things[obj_id]
            assert len(owned_keys) == 1

            # The key should map back to the object
            for key_bytes in owned_keys:
                assert watcher._thing_owners[key_bytes] == obj_id

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_stateless_keys_tracked(self):
        """Stateless keys should be tracked in _stateless_keys."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, StatelessObjSnapshotter("stateless"))

        async def run():
            obj = TrackedObj("test", 42)
            watcher.track(obj)
            tick("info", "tracked")

            # Check stateless keys
            assert len(watcher._stateless_keys) == 1
            # Ownership maps should be empty for stateless
            assert len(watcher._owned_things) == 0
            assert len(watcher._thing_owners) == 0

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_ownership_maps_cleaned_on_untrack(self):
        """Ownership maps should be cleaned up when object is untracked."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, OwningObjSnapshotter("owned"))

        async def run():
            obj = TrackedObj("test", 42)
            obj_id = id(obj)
            watcher.track(obj)
            tick("info", "tracked")

            assert obj_id in watcher._owned_things
            assert len(watcher._thing_owners) == 1

            watcher.untrack(obj)

            # Maps should be cleaned up
            assert obj_id not in watcher._owned_things
            assert len(watcher._thing_owners) == 0

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_ownership_maps_cleaned_on_gc(self):
        """Ownership maps should be cleaned up when object is garbage collected."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, OwningObjSnapshotter("owned"))

        obj_id_holder = [None]

        async def run():
            obj = TrackedObj("test", 42)
            obj_id_holder[0] = id(obj)
            watcher.track(obj)
            tick("info", "tracked")

            assert obj_id_holder[0] in watcher._owned_things
            assert len(watcher._thing_owners) == 1

            # Delete reference and force GC
            del obj
            gc.collect()

            # Maps should be cleaned up
            assert obj_id_holder[0] not in watcher._owned_things
            assert len(watcher._thing_owners) == 0

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_multi_key_ownership(self):
        """Snapshotter returning multiple keys should own all of them."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, MultiKeyOwningSnapshotter())

        async def run():
            obj = TrackedObj("test", 42)
            obj_id = id(obj)
            watcher.track(obj)
            tick("info", "tracked")

            # Should own both keys
            assert obj_id in watcher._owned_things
            owned_keys = watcher._owned_things[obj_id]
            assert len(owned_keys) == 2

            # Both keys should map back to same object
            for key_bytes in owned_keys:
                assert watcher._thing_owners[key_bytes] == obj_id

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Tests for delta behavior with owning snapshotters
# =============================================================================


class TestOwningDeltaBehavior:
    """Tests that owning snapshotters work correctly with deltas."""

    @pytest.mark.asyncio
    async def test_owning_snapshotter_delta_works(self):
        """Owning snapshotter should be able to emit deltas."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(TrackedObj, OwningObjSnapshotter("owned"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            obj = TrackedObj("test", 42)
            watcher.track(obj)
            tick("info", "first")

            obj.value = 100
            tick("info", "changed")

            tick("info", "unchanged")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 3

        # First tick: ThingState
        assert len(captured[0].states) == 1
        assert isinstance(captured[0].states[0], ThingState)

        # Second tick: ThingDelta with replace (POP_ALL + push)
        assert len(captured[1].states) == 1
        assert isinstance(captured[1].states[0], ThingDelta)
        assert isinstance(captured[1].states[0].pop_chunks, _PopAll)
        assert not isinstance(captured[1].states[0].push_chunk, _NoPushedChunk)

        # Third tick: ThingDelta with no change (no push, no pop)
        assert len(captured[2].states) == 1
        assert isinstance(captured[2].states[0], ThingDelta)
        assert isinstance(captured[2].states[0].push_chunk, _NoPushedChunk)
        assert captured[2].states[0].pop_chunks == 0
