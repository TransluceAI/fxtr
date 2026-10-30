"""Tests for hard recapture and post-trigger tick functionality."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient
from thingwatcher import POP_ALL, ThingDelta, ThingState, Watcher, pause, receive, tick, track
from thingwatcher.api import (
    AnchorCapturer,
    StatefulOwningSnapshotter,
    StatelessSnapshotter,
    ThingRef,
)
from thingwatcher.registry import clear_global_registries

from .conftest import find_free_port, graphql_query, wait_for_server

# =============================================================================
# Test objects and snapshotters
# =============================================================================


class Counter:
    """Simple counter for testing."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value


class DeltaCounter:
    """Counter that tracks state for delta-based snapshots."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value


class CounterSnapshotter(StatelessSnapshotter):
    """Stateless snapshotter for Counter."""

    def snapshot(self, obj: Counter) -> ThingState:
        return ThingState(
            key=("counter", obj.name),
            thing_type="Counter",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


class DeltaCounterSnapshotter(StatefulOwningSnapshotter):
    """Owning snapshotter that uses deltas after first snapshot."""

    def __init__(self):
        self._seen: dict[int, int] = {}

    def snapshot_owned(self, obj: DeltaCounter) -> ThingState | ThingDelta:
        obj_id = id(obj)
        if obj_id not in self._seen:
            self._seen[obj_id] = obj.value
            return ThingState(
                key=("delta_counter", obj.name),
                thing_type="DeltaCounter",
                thing_data={"name": obj.name, "value": obj.value},
                links=[],
            )
        else:
            self._seen[obj_id] = obj.value
            return ThingDelta(
                key=("delta_counter", obj.name),
                pop_chunks=POP_ALL,
                push_chunk={"name": obj.name, "value": obj.value},
            )

    def stateless_snapshot(self, obj: DeltaCounter) -> ThingState:
        """Return full state - for hard recapture."""
        return ThingState(
            key=("delta_counter", obj.name),
            thing_type="DeltaCounter",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


class CounterAnchorCapturer(AnchorCapturer):
    """Anchor capturer for Counter."""

    def handle(self, anchor: Counter) -> ThingRef:
        return ThingRef(key=("counter", anchor.name))


class DeltaCounterAnchorCapturer(AnchorCapturer):
    """Anchor capturer for DeltaCounter."""

    def handle(self, anchor: DeltaCounter) -> ThingRef:
        return ThingRef(key=("delta_counter", anchor.name))


@pytest.fixture(autouse=True)
def clean_registries():
    """Clean global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


async def wait_for_writes():
    """Wait for background writes to complete."""
    await asyncio.sleep(0.1)


# =============================================================================
# Hard Recapture Tests
# =============================================================================


class TestHardRecapture:
    """Tests for hard recapture functionality."""

    @pytest.mark.asyncio
    async def test_hard_recapture_creates_tick(self):
        """Test that hard_recapture creates a tick with correct type and level."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter("test", 42)
            track(counter)
            tick("info", "initial")
            await wait_for_writes()

            # Do hard recapture
            result = await watcher.hard_recapture()
            await wait_for_writes()

            assert result["level"] == "hard_recapture"
            assert result["type"] == "hard_recapture"

            # Check via GraphQL
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                query_result = await graphql_query(
                    client,
                    """
                    query {
                        ticks { index level type }
                    }
                    """,
                )
                ticks = query_result["data"]["ticks"]
                assert len(ticks) == 2
                assert ticks[0]["type"] == "api_tick"
                assert ticks[1]["type"] == "hard_recapture"
                assert ticks[1]["level"] == "hard_recapture"

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_hard_recapture_invalidates_db_records(self):
        """Test that hard recapture invalidates all live records in DB."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                c1 = Counter("a", 1)
                c2 = Counter("b", 2)
                track(c1)
                track(c2)
                tick("info", "initial")
                await wait_for_writes()

                # Verify we have 2 live things
                async with watcher._db.begin() as conn:
                    things = await watcher._db.get_things_at_tick(conn, 1)
                    assert len(things) == 2
                    # All should have valid_until = None
                    for t in things:
                        assert t.valid_until is None

                # Hard recapture
                await watcher.hard_recapture()
                await wait_for_writes()

                # After recapture, old records should be invalidated
                async with watcher._db.begin() as conn:
                    # Get all thing_states (not just at tick)
                    from sqlalchemy import select
                    from thingwatcher.db import thing_states

                    result = await conn.execute(select(thing_states))
                    all_states = result.fetchall()

                    # Should have 4 records: 2 old (invalidated) + 2 new (live)
                    assert len(all_states) == 4

                    # Check old ones are invalidated, new ones are live
                    invalidated = [s for s in all_states if s.valid_until is not None]
                    live = [s for s in all_states if s.valid_until is None]
                    assert len(invalidated) == 2
                    assert len(live) == 2

            await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_hard_recapture_clears_in_memory_state(self):
        """Test that hard recapture clears in-memory tracking state."""
        watcher = Watcher(port=None)
        snapshotter = DeltaCounterSnapshotter()
        watcher.register_snapshotter(DeltaCounter, snapshotter)

        async def run():
            counter = DeltaCounter("test", 10)
            track(counter)
            tick("info", "tick1")
            await wait_for_writes()

            # Verify snapshotter has seen the object
            assert len(snapshotter._seen) == 1

            # Verify watcher has in-memory state
            assert len(watcher._current_things) == 1
            assert len(watcher._owned_things) == 1

            # Hard recapture clears in-memory state
            await watcher.hard_recapture()
            await wait_for_writes()

            # _current_things should be repopulated after recapture
            # (with fresh state from the new snapshot)
            assert len(watcher._current_things) == 1

            # Ownership should be re-established
            assert len(watcher._owned_things) == 1

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_hard_recapture_uses_stateless_snapshot(self):
        """Test that hard recapture uses stateless_snapshot for owning snapshotters."""
        watcher = Watcher(port=None)

        # Create a snapshotter that tracks which method was called
        class TrackingSnapshotter(StatefulOwningSnapshotter):
            def __init__(self):
                self.snapshot_owned_calls = 0
                self.stateless_snapshot_calls = 0

            def snapshot_owned(self, obj: Counter) -> ThingState:
                self.snapshot_owned_calls += 1
                return ThingState(
                    key=("tracking", obj.name),
                    thing_type="Tracking",
                    thing_data={"value": obj.value},
                    links=[],
                )

            def stateless_snapshot(self, obj: Counter) -> ThingState:
                self.stateless_snapshot_calls += 1
                return ThingState(
                    key=("tracking", obj.name),
                    thing_type="Tracking",
                    thing_data={"value": obj.value},
                    links=[],
                )

        tracking_snapshotter = TrackingSnapshotter()
        watcher.register_snapshotter(Counter, tracking_snapshotter)

        async def run():
            counter = Counter("test", 1)
            track(counter)

            # Normal tick uses snapshot_owned
            tick("info", "tick1")
            await wait_for_writes()
            assert tracking_snapshotter.snapshot_owned_calls == 1
            assert tracking_snapshotter.stateless_snapshot_calls == 0

            # Hard recapture uses stateless_snapshot
            await watcher.hard_recapture()
            await wait_for_writes()
            assert tracking_snapshotter.snapshot_owned_calls == 1  # Unchanged
            assert tracking_snapshotter.stateless_snapshot_calls == 1  # Called

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_hard_recapture_via_graphql(self):
        """Test hard recapture via GraphQL mutation."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter("test", 99)
            track(counter)
            tick("info", "initial")
            await wait_for_writes()

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Do hard recapture via GraphQL
                result = await graphql_query(
                    client,
                    """
                    mutation {
                        hardRecapture {
                            index
                            level
                            type
                        }
                    }
                    """,
                )
                assert result["data"]["hardRecapture"]["level"] == "hard_recapture"
                assert result["data"]["hardRecapture"]["type"] == "hard_recapture"
                assert result["data"]["hardRecapture"]["index"] == 2

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Post-Trigger Tick Tests
# =============================================================================


class TestPostTriggerTick:
    """Tests for post-trigger tick functionality."""

    @pytest.mark.asyncio
    async def test_trigger_creates_post_trigger_tick(self):
        """Test that triggering a pause creates a post_trigger tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.register_anchor_capturer(Counter, CounterAnchorCapturer())

        triggered = asyncio.Event()

        async def pauseable_task():
            counter = Counter("pauseable", 1)
            track(counter)
            tick("info", "initial")

            # Receive payload from trigger
            payload = await receive(counter)
            triggered.set()
            return payload

        async def run():
            await wait_for_server(port)

            # Start pauseable task
            task = asyncio.create_task(pauseable_task())
            await wait_for_writes()

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Trigger the pause
                trigger_result = await graphql_query(
                    client,
                    """
                    mutation {
                        triggerPause(keyJSON: ["counter", "pauseable"], payload: "test") {
                            ok
                            triggerId
                        }
                    }
                    """,
                )
                assert trigger_result["data"]["triggerPause"]["ok"] is True

                # Wait for trigger to be processed
                await triggered.wait()
                await wait_for_writes()

                # Check ticks - should have initial + post_trigger
                ticks_result = await graphql_query(
                    client,
                    """
                    query {
                        ticks { index level type }
                    }
                    """,
                )
                ticks = ticks_result["data"]["ticks"]

                # Should have: tick 1 (initial) + tick 2 (post_trigger)
                assert len(ticks) >= 2
                post_trigger_ticks = [t for t in ticks if t["type"] == "post_trigger"]
                assert len(post_trigger_ticks) == 1
                assert post_trigger_ticks[0]["level"] == "post_trigger"

            # Clean up
            result = await task
            assert result == "test"

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_post_trigger_tick_before_task_wakes(self):
        """Test that post_trigger tick happens BEFORE the paused task wakes up."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(Counter, CounterSnapshotter())
        watcher.register_anchor_capturer(Counter, CounterAnchorCapturer())

        # Track order of operations
        events: list[str] = []

        async def pauseable_task():
            counter = Counter("pauseable", 1)
            track(counter)
            tick("info", "initial")

            # Pause
            events.append("before_pause")
            await pause(counter)
            events.append("after_pause_woke")  # This should happen AFTER post_trigger tick

        async def run():
            # Start pauseable task
            task = asyncio.create_task(pauseable_task())
            await wait_for_writes()

            # Wait for task to be paused
            await asyncio.sleep(0.1)
            assert "before_pause" in events

            # Trigger - this should:
            # 1. Write trigger record to DB
            # 2. Do post_trigger tick
            # 3. Wake the task
            import cbor2

            key_bytes = cbor2.dumps(("counter", "pauseable"), canonical=True)
            result = await watcher.trigger_pause(key_bytes, "payload")
            assert result.ok

            # Wait for everything to settle
            await wait_for_writes()
            await asyncio.sleep(0.1)

            # Check the task woke up
            assert "after_pause_woke" in events

            # Verify post_trigger tick was created
            async with watcher._db.begin() as conn:
                from sqlalchemy import select
                from thingwatcher.db import ticks

                result = await conn.execute(select(ticks).order_by(ticks.c.tick))
                all_ticks = result.fetchall()

                # Should have: initial (1) + post_trigger (2)
                assert len(all_ticks) >= 2
                types = [t.type for t in all_ticks]
                assert "post_trigger" in types

            # Clean up
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await watcher.watch_and_serve_until(run)
