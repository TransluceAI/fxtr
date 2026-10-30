"""Tests for streaming_update functionality."""

from __future__ import annotations

import asyncio
import threading

import pytest
from httpx import AsyncClient

from thingwatcher import (
    StatefulOwningSnapshotter,
    StatelessSnapshotter,
    ThingState,
    Watcher,
    streaming_update,
    tick,
    track,
)

from .conftest import find_free_port, graphql_query, wait_for_server

# =============================================================================
# Test objects and snapshotters
# =============================================================================


class StreamingCounter:
    """A counter that can be streamed."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value


class StreamingCounterSnapshotter(StatefulOwningSnapshotter):
    """Owning snapshotter for StreamingCounter."""

    def snapshot_owned(self, obj: StreamingCounter) -> ThingState:
        return ThingState(
            key=("streaming_counter", obj.name),
            thing_type="StreamingCounter",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )

    def stateless_snapshot(self, obj: StreamingCounter) -> ThingState:
        """Return full state for hard recapture (same as snapshot_owned)."""
        return self.snapshot_owned(obj)


class StatelessCounter:
    """A counter with stateless snapshotter."""

    def __init__(self, value: int = 0):
        self.value = value


class StatelessCounterSnapshotter(StatelessSnapshotter):
    """Stateless snapshotter - cannot use streaming_update."""

    def snapshot(self, obj: StatelessCounter) -> ThingState:
        return ThingState(
            key=("stateless_counter",),
            thing_type="StatelessCounter",
            thing_data={"value": obj.value},
            links=[],
        )


class MultiKeyOwner:
    """An object that owns multiple keys."""

    def __init__(self, name: str, keys: list[str]):
        self.name = name
        self.keys = keys


class MultiKeySnapshotter(StatefulOwningSnapshotter):
    """Owning snapshotter that produces multiple keys."""

    def snapshot_owned(
        self, obj: MultiKeyOwner
    ) -> ThingState | list[ThingState]:  # type: ignore[override]
        return [
            ThingState(
                key=("multi", obj.name, key),
                thing_type="MultiKey",
                thing_data={"owner": obj.name, "key": key},
                links=[],
            )
            for key in obj.keys
        ]

    def stateless_snapshot(self, obj: MultiKeyOwner) -> list[ThingState]:
        """Return full state for hard recapture (same as snapshot_owned)."""
        return self.snapshot_owned(obj)  # type: ignore[return-value]


# =============================================================================
# Basic streaming tests
# =============================================================================


class TestBasicStreaming:
    """Tests for basic streaming_update functionality."""

    @pytest.mark.asyncio
    async def test_basic_streaming_update(self):
        """Test that streaming_update creates a stream tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = StreamingCounter("test", 1)
            track(counter)

            # Do a normal tick first
            tick("info", "initial")

            # Now do a streaming update
            counter.value = 2
            streaming_update(counter)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Check we have a stream tick
                result = await graphql_query(
                    client,
                    """
                    query {
                        ticks { index level type }
                    }
                    """,
                )
                ticks = result["data"]["ticks"]
                # Should have tick 1 (normal) and tick 2 (stream)
                assert len(ticks) == 2
                assert ticks[0]["type"] == "api_tick"
                assert ticks[1]["type"] == "stream"

                # Check the data in the stream tick
                latest = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            index
                            type
                            things { data }
                        }
                    }
                    """,
                )
                assert latest["data"]["latestTick"]["type"] == "stream"
                assert latest["data"]["latestTick"]["things"][0]["data"]["value"] == 2

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_multiple_streaming_updates_same_tick(self):
        """Test multiple streaming updates to the same stream tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = StreamingCounter("test", 1)
            track(counter)

            # Normal tick
            tick("info", "initial")

            # Multiple streaming updates
            counter.value = 2
            streaming_update(counter)
            counter.value = 3
            streaming_update(counter)
            counter.value = 4
            streaming_update(counter)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Should still only have 2 ticks (1 normal + 1 stream)
                result = await graphql_query(
                    client,
                    "query { ticks { index type } }",
                )
                ticks = result["data"]["ticks"]
                assert len(ticks) == 2

                # Latest should show value=4
                latest = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                assert latest["data"]["latestTick"]["things"][0]["data"]["value"] == 4

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_normal_tick_after_stream(self):
        """Test that a normal tick after streaming creates a new tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = StreamingCounter("test", 1)
            track(counter)

            # Normal tick
            tick("info", "first")

            # Stream update
            counter.value = 2
            streaming_update(counter)

            # Another normal tick
            counter.value = 3
            tick("info", "second")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    "query { ticks { index type } }",
                )
                ticks = result["data"]["ticks"]
                # Should have 3 ticks: api_tick, stream, api_tick
                assert len(ticks) == 3
                assert ticks[0]["type"] == "api_tick"
                assert ticks[1]["type"] == "stream"
                assert ticks[2]["type"] == "api_tick"

        await watcher.watch_and_serve_until(run)


class TestStreamingValidation:
    """Tests for streaming_update validation."""

    @pytest.mark.asyncio
    async def test_streaming_requires_tracked_object(self):
        """Test that streaming_update requires object to be tracked."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            counter = StreamingCounter("test", 1)
            # NOT tracked

            with pytest.raises(ValueError, match="not tracked"):
                streaming_update(counter)

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_streaming_requires_owning_snapshotter(self):
        """Test that streaming_update requires StatefulOwningSnapshotter."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(StatelessCounter, StatelessCounterSnapshotter())

        async def run():
            counter = StatelessCounter(1)
            track(counter)

            with pytest.raises(ValueError, match="StatefulOwningSnapshotter"):
                streaming_update(counter)

        await watcher.watch_and_serve_until(run)

    def test_streaming_without_watcher_is_noop(self):
        """Test that streaming_update is a no-op when no watcher is active."""
        counter = StreamingCounter("test", 1)
        # Should not raise - just a no-op like tick()
        streaming_update(counter)

    @pytest.mark.asyncio
    async def test_stream_level_reserved(self):
        """Test that 'stream' level is reserved and cannot be used in tick()."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            counter = StreamingCounter("test", 1)
            track(counter)

            with pytest.raises(ValueError, match="reserved for streaming updates"):
                tick("stream", "should fail")

        await watcher.watch_and_serve_until(run)


class TestStreamingObjectLifecycle:
    """Tests for objects appearing and disappearing during streaming."""

    @pytest.mark.asyncio
    async def test_thing_disappearing_during_streaming(self):
        """Test that things can disappear during streaming updates."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(MultiKeyOwner, MultiKeySnapshotter())

        async def run():
            await wait_for_server(port)
            owner = MultiKeyOwner("test", ["a", "b", "c"])
            track(owner)

            # Normal tick with all keys
            tick("info", "initial")

            # Remove a key and streaming update
            owner.keys = ["a", "b"]  # c is gone
            streaming_update(owner)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Check only 2 things remain in latest tick
                result = await graphql_query(
                    client,
                    "query { latestTick { things { key } } }",
                )
                assert len(result["data"]["latestTick"]["things"]) == 2

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_thing_appearing_during_streaming(self):
        """Test that new things can appear during streaming updates."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(MultiKeyOwner, MultiKeySnapshotter())

        async def run():
            await wait_for_server(port)
            owner = MultiKeyOwner("test", ["a"])
            track(owner)

            # Normal tick with one key
            tick("info", "initial")

            # Add keys and streaming update
            owner.keys = ["a", "b", "c"]
            streaming_update(owner)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Check 3 things in latest tick
                result = await graphql_query(
                    client,
                    "query { latestTick { things { key } } }",
                )
                assert len(result["data"]["latestTick"]["things"]) == 3

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_streaming_object_not_yet_in_tick(self):
        """Test that streaming_update works on object tracked but not yet ticked."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = StreamingCounter("test", 1)
            track(counter)
            # No tick yet!

            # Streaming update should work
            counter.value = 2
            streaming_update(counter)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                # Should have the thing from the streaming update
                assert result["data"]["latestTick"]["things"][0]["data"]["value"] == 2

        await watcher.watch_and_serve_until(run)


class TestStreamingThreadSafety:
    """Tests for streaming_update thread safety."""

    @pytest.mark.asyncio
    async def test_concurrent_streaming_updates(self):
        """Test concurrent streaming updates from multiple threads."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        counters_created: list[StreamingCounter] = []
        errors: list[Exception] = []

        async def run():
            def worker(name: str, count: int):
                try:
                    counter = StreamingCounter(name, 0)
                    # Use watcher methods directly - context doesn't propagate to threads
                    watcher.track(counter)
                    counters_created.append(counter)

                    for i in range(count):
                        counter.value = i
                        watcher.streaming_update(counter)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(f"counter{i}", 10)) for i in range(5)]

            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0, f"Errors: {errors}"
            assert len(counters_created) == 5

        await watcher.watch_and_serve_until(run)


class TestTransientStates:
    """Tests for transient states during streaming."""

    @pytest.mark.asyncio
    async def test_transient_state_not_visible_but_exists_in_db(self):
        """Test that a thing created and removed in the same stream tick is transient.

        A transient state is one that is created and removed within the same stream tick
        (before any normal tick captures it). It should:
        - NOT be visible at any tick query (since valid_from == valid_until)
        - Still exist in the database for full history preservation
        """
        from sqlalchemy import select

        from thingwatcher.db import thing_states

        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(MultiKeyOwner, MultiKeySnapshotter())

        async def run():
            await wait_for_server(port)
            owner = MultiKeyOwner("test", ["a", "b"])
            track(owner)

            # 1. Normal tick to establish baseline
            tick("info", "initial")

            # 2. Add key "c" and streaming update - "c" now exists at stream tick
            owner.keys = ["a", "b", "c"]
            streaming_update(owner)

            # 3. Remove key "c" and streaming update - "c" is now transient
            #    (created and invalidated at the same stream tick)
            owner.keys = ["a", "b"]
            streaming_update(owner)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # 4. Verify "c" is NOT visible via GraphQL at any tick

                # Check tick 1 (the initial normal tick)
                tick1_result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 1) { things { keyJSON } }
                    }
                    """,
                )
                tick1_keys = [t["keyJSON"] for t in tick1_result["data"]["tick"]["things"]]
                assert ["multi", "test", "c"] not in tick1_keys, "c should not be at tick 1"

                # Check tick 2 (the stream tick) - c should NOT be visible
                tick2_result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 2) { things { keyJSON } }
                    }
                    """,
                )
                tick2_keys = [t["keyJSON"] for t in tick2_result["data"]["tick"]["things"]]
                assert [
                    "multi",
                    "test",
                    "c",
                ] not in tick2_keys, "c should not be at tick 2 (transient)"

                # Check latestTick as well
                latest_result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick { things { keyJSON } }
                    }
                    """,
                )
                latest_keys = [t["keyJSON"] for t in latest_result["data"]["latestTick"]["things"]]
                assert ["multi", "test", "c"] not in latest_keys, "c should not be in latest tick"

            # 5. Verify the transient state EXISTS in the database with valid_from == valid_until
            import cbor2

            c_key_bytes = cbor2.dumps(("multi", "test", "c"))

            async with watcher._db.begin() as conn:
                # Query ALL thing_states for key "c" (not just valid ones)
                query = select(thing_states).where(thing_states.c.thing_key == c_key_bytes)
                result = await conn.execute(query)
                rows = result.fetchall()

                # Should have at least one row (the transient state)
                assert len(rows) >= 1, "Transient state should exist in database"

                # Find the transient state (where valid_from == valid_until)
                transient_rows = [r for r in rows if r.valid_from == r.valid_until]
                assert len(transient_rows) >= 1, (
                    f"Should have at least one transient state with valid_from == valid_until. "
                    f"Got rows: {[(r.valid_from, r.valid_until) for r in rows]}"
                )

        await watcher.watch_and_serve_until(run)


class TestStreamingHistory:
    """Tests for historical data with streaming."""

    @pytest.mark.asyncio
    async def test_historical_tick_preserved_after_streaming(self):
        """Test that historical tick data is preserved after streaming updates."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(StreamingCounter, StreamingCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = StreamingCounter("test", 1)
            track(counter)

            # Normal tick with value=1
            tick("info", "first")

            # Stream updates
            counter.value = 2
            streaming_update(counter)
            counter.value = 3
            streaming_update(counter)

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Historical tick 1 should still show value=1
                result = await graphql_query(
                    client,
                    "query { tick(index: 1) { things { data } } }",
                )
                assert result["data"]["tick"]["things"][0]["data"]["value"] == 1

                # Latest tick should show value=3
                latest = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                assert latest["data"]["latestTick"]["things"][0]["data"]["value"] == 3

        await watcher.watch_and_serve_until(run)
