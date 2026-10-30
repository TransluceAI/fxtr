"""Tests for core tracking functionality."""

from __future__ import annotations

import gc
from typing import Any

import pytest
from thingwatcher import (
    SnapshotResult,
    ThingState,
    Watcher,
    register_snapshotter,
    tick,
    track,
    untrack,
)
from thingwatcher.api import StatelessSnapshotter
from thingwatcher.registry import clear_global_registries

# =============================================================================
# Test fixtures and helpers
# =============================================================================


class Counter:
    """Simple test class for tracking."""

    def __init__(self, value: int = 0):
        self.value = value

    def increment(self) -> None:
        self.value += 1


class CounterSnapshotter(StatelessSnapshotter):
    """Snapshotter for Counter objects."""

    def snapshot(self, obj: Counter) -> ThingState:
        return ThingState(
            key=id(obj),
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=[],
        )


class BrokenSnapshotter(StatelessSnapshotter):
    """Snapshotter that always raises an exception."""

    def snapshot(self, obj: Any) -> ThingState:
        raise RuntimeError("Intentional error for testing")


class MarkerSnapshotter(StatelessSnapshotter):
    """Snapshotter that adds a marker to identify which one was used."""

    def __init__(self, marker: str):
        self.marker = marker

    def snapshot(self, obj: Counter) -> ThingState:
        return ThingState(
            key=id(obj),
            thing_type="Counter",
            thing_data={"value": obj.value, "marker": self.marker},
            links=[],
        )


@pytest.fixture(autouse=True)
def clean_registries():
    """Clean global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


# =============================================================================
# Tests
# =============================================================================


class TestBasicTracking:
    """Tests for basic track/tick functionality."""

    @pytest.mark.asyncio
    async def test_basic_tracking_and_tick(self):
        """Test that we can track an object and tick to capture its state."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(42)
            track(counter)
            tick("info", "initial state")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        result = captured[0]
        assert result.tick_number == 1
        assert result.level == "info"
        assert result.desc == "initial state"
        assert len(result.states) == 1

        state = result.states[0]
        assert isinstance(state, ThingState)
        assert state.thing_type == "Counter"
        assert state.thing_data == {"value": 42}

    @pytest.mark.asyncio
    async def test_multiple_ticks_increment_counter(self):
        """Test that tick numbers increment."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(0)
            track(counter)
            tick("info", "tick 1")
            counter.increment()
            tick("info", "tick 2")
            counter.increment()
            tick("info", "tick 3")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 3
        assert captured[0].tick_number == 1
        assert captured[1].tick_number == 2
        assert captured[2].tick_number == 3

        # Values should reflect state at each tick
        assert captured[0].states[0].thing_data == {"value": 0}
        assert captured[1].states[0].thing_data == {"value": 1}
        assert captured[2].states[0].thing_data == {"value": 2}

    @pytest.mark.asyncio
    async def test_track_multiple_objects(self):
        """Test tracking multiple objects."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            c1 = Counter(10)
            c2 = Counter(20)
            c3 = Counter(30)
            track(c1)
            track(c2)
            track(c3)
            tick("info", "all tracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        assert len(captured[0].states) == 3
        values = {s.thing_data["value"] for s in captured[0].states}
        assert values == {10, 20, 30}


class TestLocalSnapshotterOverride:
    """Tests for watcher-local snapshotter overriding global."""

    @pytest.mark.asyncio
    async def test_local_overrides_global(self):
        """Test that watcher-local snapshotter takes precedence over global."""
        captured: list[SnapshotResult] = []

        # Register global snapshotter
        register_snapshotter(Counter, MarkerSnapshotter("global"))

        # Create watcher with local override
        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, MarkerSnapshotter("local"))
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(1)
            track(counter)
            tick("info", "test")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        state = captured[0].states[0]
        assert state.thing_data["marker"] == "local"

    @pytest.mark.asyncio
    async def test_global_used_when_no_local(self):
        """Test that global snapshotter is used when no local override."""
        captured: list[SnapshotResult] = []

        # Register global snapshotter only
        register_snapshotter(Counter, MarkerSnapshotter("global"))

        watcher = Watcher(port=None)
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(1)
            track(counter)
            tick("info", "test")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        state = captured[0].states[0]
        assert state.thing_data["marker"] == "global"


class TestWeakrefCleanup:
    """Tests for automatic cleanup when objects are garbage collected."""

    @pytest.mark.asyncio
    async def test_gc_removes_object_from_tracking(self):
        """Test that garbage collected objects don't appear in ticks."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            # Track an object
            counter = Counter(99)
            track(counter)
            tick("info", "before gc")

            # Delete the object and force gc
            del counter
            gc.collect()

            tick("info", "after gc")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 2
        # First tick has the object
        assert len(captured[0].states) == 1
        assert captured[0].states[0].thing_data == {"value": 99}
        # Second tick has no objects (gc'd)
        assert len(captured[1].states) == 0


class TestErrorHandling:
    """Tests for error handling in snapshotters."""

    @pytest.mark.asyncio
    async def test_snapshotter_error_produces_broken_thing(self):
        """Test that snapshotter exceptions produce broken thing states."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, BrokenSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(1)
            track(counter)
            tick("info", "test")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        assert len(captured[0].states) == 1

        state = captured[0].states[0]
        assert state.thing_type == "__broken__"
        assert "Intentional error for testing" in state.thing_data["error"]
        # Key should be tuple with __broken__ prefix
        assert state.key[0] == "__broken__"

    @pytest.mark.asyncio
    async def test_track_without_snapshotter_raises(self):
        """Test that tracking without a registered snapshotter raises ValueError."""
        watcher = Watcher(port=None)
        # Don't register any snapshotter

        async def run():
            counter = Counter(1)
            with pytest.raises(ValueError, match="no snapshotter registered"):
                track(counter)

        await watcher.watch_and_serve_until(run)


class TestUntrack:
    """Tests for untrack functionality."""

    @pytest.mark.asyncio
    async def test_untrack_removes_object(self):
        """Test that untracked objects don't appear in subsequent ticks."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")

            untrack(counter)
            tick("info", "untracked")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 2
        assert len(captured[0].states) == 1
        assert len(captured[1].states) == 0


class TestNoWatcherActive:
    """Tests for behavior when no watcher is active."""

    def test_track_noop_without_watcher(self):
        """Test that track is a no-op when no watcher is active."""
        # This should not raise, just do nothing
        counter = Counter(1)
        track(counter)  # Should not raise

    def test_tick_noop_without_watcher(self):
        """Test that tick is a no-op when no watcher is active."""
        # This should not raise, just do nothing
        tick("info", "test")  # Should not raise

    def test_untrack_noop_without_watcher(self):
        """Test that untrack is a no-op when no watcher is active."""
        counter = Counter(1)
        untrack(counter)  # Should not raise


class TestNonWeakrefObjects:
    """Tests for objects that can't be weakref'd."""

    @pytest.mark.asyncio
    async def test_non_weakref_object_raises(self):
        """Test that tracking non-weakref-able objects raises ValueError."""

        class IntSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj: int) -> ThingState:
                return ThingState(key=obj, thing_type="int", thing_data=obj, links=[])

        watcher = Watcher(port=None)
        watcher.register_snapshotter(int, IntSnapshotter())

        async def run():
            # int objects can't be weakref'd
            with pytest.raises(ValueError, match="weakref-referenceable"):
                track(42, typename=int)

        await watcher.watch_and_serve_until(run)


class TestTickDescriptions:
    """Tests for tick level and description handling."""

    @pytest.mark.asyncio
    async def test_tick_with_no_description(self):
        """Test that tick works with desc=None."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(1)
            track(counter)
            tick("debug")  # No description

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 1
        assert captured[0].level == "debug"
        assert captured[0].desc is None

    @pytest.mark.asyncio
    async def test_various_tick_levels(self):
        """Test that various tick levels are captured correctly."""
        captured: list[SnapshotResult] = []

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        async def run():
            counter = Counter(1)
            track(counter)
            tick("debug", "debug message")
            tick("info", "info message")
            tick("warning", "warning message")
            tick("error", "error message")
            tick("custom_level", "custom message")

        await watcher.watch_and_serve_until(run)

        assert len(captured) == 5
        levels = [r.level for r in captured]
        assert levels == ["debug", "info", "warning", "error", "custom_level"]


class TestThreadSafety:
    """Tests for thread safety of tracking operations."""

    @pytest.mark.asyncio
    async def test_concurrent_track_untrack(self):
        """Test that multiple threads can track/untrack objects simultaneously."""
        import threading

        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        errors: list[Exception] = []
        tracked_count = 0
        count_lock = threading.Lock()

        def track_untrack_loop(thread_id: int):
            nonlocal tracked_count
            try:
                for i in range(100):
                    counter = Counter(thread_id * 1000 + i)
                    watcher.track(counter)
                    with count_lock:
                        tracked_count += 1
                    watcher.untrack(counter)
            except Exception as e:
                errors.append(e)

        async def run():
            threads = [threading.Thread(target=track_untrack_loop, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        await watcher.watch_and_serve_until(run)

        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert tracked_count == 500  # 5 threads * 100 iterations

    @pytest.mark.asyncio
    async def test_concurrent_tick(self):
        """Test that multiple threads can call tick simultaneously (serialized)."""
        import threading

        captured: list[SnapshotResult] = []
        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        errors: list[Exception] = []

        def tick_loop(thread_id: int):
            try:
                for i in range(20):
                    watcher.tick("info", f"thread {thread_id} tick {i}")
            except Exception as e:
                errors.append(e)

        async def run():
            # Track one object so ticks have something to snapshot
            counter = Counter(42)
            watcher.track(counter)

            threads = [threading.Thread(target=tick_loop, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        await watcher.watch_and_serve_until(run)

        assert len(errors) == 0, f"Errors occurred: {errors}"
        # Should have 5 threads * 20 ticks = 100 ticks
        assert len(captured) == 100
        # Tick numbers should be unique and sequential
        tick_numbers = [r.tick_number for r in captured]
        assert sorted(tick_numbers) == list(range(1, 101))

    @pytest.mark.asyncio
    async def test_track_during_tick(self):
        """Test that tracking objects during a tick doesn't corrupt state."""
        import threading
        import time

        captured: list[SnapshotResult] = []
        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.set_tick_interceptor(captured.append)

        errors: list[Exception] = []
        objects_tracked = []

        class SlowSnapshotter(StatelessSnapshotter):
            """Snapshotter that takes some time, allowing other threads to act."""

            def snapshot(self, obj: Counter) -> ThingState:
                time.sleep(0.001)  # Small delay
                return ThingState(
                    key=id(obj),
                    thing_type="SlowCounter",
                    thing_data={"value": obj.value},
                    links=[],
                )

        watcher.register_snapshotter("SlowCounter", SlowSnapshotter())

        def track_objects():
            try:
                for i in range(50):
                    counter = Counter(i)
                    objects_tracked.append(counter)  # Keep reference
                    watcher.track(counter, typename="SlowCounter")
                    time.sleep(0.0005)  # Stagger tracking
            except Exception as e:
                errors.append(e)

        def do_ticks():
            try:
                for i in range(10):
                    watcher.tick("info", f"tick {i}")
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        async def run():
            # Start tracking and ticking concurrently
            tracker_thread = threading.Thread(target=track_objects)
            ticker_thread = threading.Thread(target=do_ticks)

            tracker_thread.start()
            ticker_thread.start()

            tracker_thread.join()
            ticker_thread.join()

        await watcher.watch_and_serve_until(run)

        assert len(errors) == 0, f"Errors occurred: {errors}"
        # Should have completed 10 ticks
        assert len(captured) == 10
        # Each tick should have captured some objects (number may vary due to timing)
        for result in captured:
            # All states should be valid (no corruption)
            for state in result.states:
                assert state.thing_type == "SlowCounter"
                assert "value" in state.thing_data
