"""Tests for AsyncThingUpdate functionality."""

from __future__ import annotations

import asyncio

import anyio
import pytest
from httpx import AsyncClient

from thingwatcher import (
    AsyncThingUpdate,
    StatefulOwningSnapshotter,
    ThingState,
    Watcher,
    tick,
    track,
)

from .conftest import find_free_port, graphql_query, wait_for_server

# =============================================================================
# Test objects and snapshotters
# =============================================================================


class AsyncCounter:
    """A counter that uses async updates."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value
        self.async_delay: float = 0.5  # Default async delay

    async def get_async_value(self) -> int:
        """Simulate fetching value from async source."""
        await anyio.sleep(self.async_delay)
        return self.value * 10  # Return multiplied value


class AsyncCounterSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that returns AsyncThingUpdate."""

    def snapshot_owned(self, obj: AsyncCounter) -> AsyncThingUpdate:
        # Capture current state for closure
        current_value = obj.value
        name = obj.name

        immediate = ThingState(
            key=("async_counter", name),
            thing_type="AsyncCounter",
            thing_data={"name": name, "value": current_value, "status": "pending"},
            links=[],
        )

        async def complete_async() -> ThingState:
            async_value = await obj.get_async_value()
            return ThingState(
                key=("async_counter", name),
                thing_type="AsyncCounter",
                thing_data={
                    "name": name,
                    "value": current_value,
                    "async_value": async_value,
                    "status": "completed",
                },
                links=[],
            )

        return AsyncThingUpdate(
            immediate_update=immediate,
            async_updater=complete_async,
        )

    def stateless_snapshot(self, obj: AsyncCounter) -> ThingState:
        return ThingState(
            key=("async_counter", obj.name),
            thing_type="AsyncCounter",
            thing_data={"name": obj.name, "value": obj.value, "status": "unknown"},
            links=[],
        )


class SyncCounter:
    """A counter that does NOT use async updates."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value


class SyncCounterSnapshotter(StatefulOwningSnapshotter):
    """Regular snapshotter without async."""

    def snapshot_owned(self, obj: SyncCounter) -> ThingState:
        return ThingState(
            key=("sync_counter", obj.name),
            thing_type="SyncCounter",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )

    def stateless_snapshot(self, obj: SyncCounter) -> ThingState:
        return self.snapshot_owned(obj)


# =============================================================================
# Basic async update tests
# =============================================================================


class TestBasicAsyncUpdate:
    """Tests for basic async update flow."""

    @pytest.mark.asyncio
    async def test_async_update_immediate_then_completed(self):
        """Test that async update shows immediate state, then completed state."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(AsyncCounter, AsyncCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 5)
            counter.async_delay = 0.3  # Short delay for testing
            track(counter)

            # Do a tick - should show immediate "pending" state
            tick("info", "initial")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Check immediate state shows "pending"
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "pending"
                assert data["value"] == 5
                assert "async_value" not in data

                # Wait for async completion
                await asyncio.sleep(0.5)

                # Now should show "completed" state in a streaming tick
                result = await graphql_query(
                    client,
                    "query { latestTick { type things { data } } }",
                )
                assert result["data"]["latestTick"]["type"] == "stream"
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "completed"
                assert data["value"] == 5
                assert data["async_value"] == 50  # 5 * 10

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_async_update_creates_streaming_tick(self):
        """Test that async completion creates a streaming tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(AsyncCounter, AsyncCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 1)
            counter.async_delay = 0.2
            track(counter)

            tick("info", "initial")

            await asyncio.sleep(0.5)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Should have 2 ticks: api_tick + stream
                result = await graphql_query(
                    client,
                    "query { ticks { index type } }",
                )
                ticks = result["data"]["ticks"]
                assert len(ticks) == 2
                assert ticks[0]["type"] == "api_tick"
                assert ticks[1]["type"] == "stream"

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Cancellation tests
# =============================================================================


class TestAsyncCancellation:
    """Tests for async update cancellation."""

    @pytest.mark.asyncio
    async def test_cancellation_on_new_tick(self):
        """Test that new tick cancels pending async update."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(AsyncCounter, AsyncCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 1)
            counter.async_delay = 1.0  # Long delay
            track(counter)

            # First tick starts async
            tick("info", "first")

            await asyncio.sleep(0.1)

            # Second tick before async completes - should cancel first async
            counter.value = 2
            tick("info", "second")

            # Wait past first async delay
            await asyncio.sleep(1.2)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Latest should show second tick's completed async
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "completed"
                assert data["value"] == 2  # Second tick's value
                assert data["async_value"] == 20  # 2 * 10

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_cancellation_on_streaming_update(self):
        """Test that streaming_update cancels pending async update."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(AsyncCounter, AsyncCounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 1)
            counter.async_delay = 1.0  # Long delay
            track(counter)

            # First tick starts async
            tick("info", "initial")

            await asyncio.sleep(0.1)

            # Streaming update before async completes
            counter.value = 3
            watcher.streaming_update(counter)

            # Wait past first async delay
            await asyncio.sleep(1.2)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Latest should show streaming update's completed async
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "completed"
                assert data["value"] == 3  # Streaming update's value
                assert data["async_value"] == 30  # 3 * 10

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_cancellation_on_hard_recapture(self):
        """Test that hard_recapture cancels all pending async updates."""
        port = find_free_port()
        watcher = Watcher(port=port)

        # Track whether first async completed (it shouldn't)
        first_async_completed = {"completed": False}

        class HardRecaptureTestSnapshotter(StatefulOwningSnapshotter):
            """Snapshotter that tracks async completion."""

            def snapshot_owned(self, obj: AsyncCounter) -> AsyncThingUpdate:
                current_value = obj.value

                immediate = ThingState(
                    key=("async_counter", obj.name),
                    thing_type="AsyncCounter",
                    thing_data={"value": current_value, "status": "pending"},
                    links=[],
                )

                async def complete_async() -> ThingState:
                    await anyio.sleep(1.0)
                    first_async_completed["completed"] = True
                    return ThingState(
                        key=("async_counter", obj.name),
                        thing_type="AsyncCounter",
                        thing_data={"value": current_value, "status": "completed"},
                        links=[],
                    )

                return AsyncThingUpdate(
                    immediate_update=immediate,
                    async_updater=complete_async,
                )

            def stateless_snapshot(self, obj: AsyncCounter) -> ThingState:
                # stateless_snapshot returns plain ThingState, no async
                return ThingState(
                    key=("async_counter", obj.name),
                    thing_type="AsyncCounter",
                    thing_data={"value": obj.value, "status": "recaptured"},
                    links=[],
                )

        watcher.register_snapshotter(AsyncCounter, HardRecaptureTestSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 1)
            track(counter)

            # First tick starts async
            tick("info", "initial")

            await asyncio.sleep(0.1)

            # Hard recapture before async completes - should cancel first async
            counter.value = 4
            await watcher.hard_recapture()

            # Wait past first async delay
            await asyncio.sleep(1.2)

            # First async should have been cancelled
            assert first_async_completed["completed"] is False

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Latest should show hard_recapture's stateless result
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "recaptured"
                assert data["value"] == 4  # Hard recapture's value

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_cancellation_on_untrack(self):
        """Test that untrack cancels pending async update."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(AsyncCounter, AsyncCounterSnapshotter())

        completed_flag = {"completed": False}

        class TrackingAsyncCounterSnapshotter(StatefulOwningSnapshotter):
            """Snapshotter that tracks if async completes."""

            def snapshot_owned(self, obj: AsyncCounter) -> AsyncThingUpdate:
                immediate = ThingState(
                    key=("async_counter", obj.name),
                    thing_type="AsyncCounter",
                    thing_data={"status": "pending"},
                    links=[],
                )

                async def complete_async() -> ThingState:
                    await anyio.sleep(1.0)
                    completed_flag["completed"] = True
                    return ThingState(
                        key=("async_counter", obj.name),
                        thing_type="AsyncCounter",
                        thing_data={"status": "completed"},
                        links=[],
                    )

                return AsyncThingUpdate(
                    immediate_update=immediate,
                    async_updater=complete_async,
                )

            def stateless_snapshot(self, obj: AsyncCounter) -> ThingState:
                return ThingState(
                    key=("async_counter", obj.name),
                    thing_type="AsyncCounter",
                    thing_data={"status": "unknown"},
                    links=[],
                )

        watcher.register_snapshotter(AsyncCounter, TrackingAsyncCounterSnapshotter())

        async def run():
            counter = AsyncCounter("test", 1)
            track(counter)
            tick("info", "initial")

            await asyncio.sleep(0.1)

            # Untrack before async completes
            watcher.untrack(counter)

            # Wait past async delay
            await asyncio.sleep(1.5)

            # Async should have been cancelled
            assert completed_flag["completed"] is False

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Error handling tests
# =============================================================================


class TestAsyncErrorHandling:
    """Tests for error handling in async updates."""

    @pytest.mark.asyncio
    async def test_async_updater_error_creates_broken_thing(self):
        """Test that errors in async_updater create broken things."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class ErrorAsyncSnapshotter(StatefulOwningSnapshotter):
            def snapshot_owned(self, obj: AsyncCounter) -> AsyncThingUpdate:
                immediate = ThingState(
                    key=("error_counter", obj.name),
                    thing_type="ErrorCounter",
                    thing_data={"status": "pending"},
                    links=[],
                )

                async def fail_async() -> ThingState:
                    await anyio.sleep(0.2)
                    raise ValueError("Async error!")

                return AsyncThingUpdate(
                    immediate_update=immediate,
                    async_updater=fail_async,
                )

            def stateless_snapshot(self, obj: AsyncCounter) -> ThingState:
                return ThingState(
                    key=("error_counter", obj.name),
                    thing_type="ErrorCounter",
                    thing_data={"status": "unknown"},
                    links=[],
                )

        watcher.register_snapshotter(AsyncCounter, ErrorAsyncSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 1)
            track(counter)

            tick("info", "initial")

            # Wait for async error
            await asyncio.sleep(0.5)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Should have a broken thing
                result = await graphql_query(
                    client,
                    "query { latestTick { things { thingType data } } }",
                )
                thing = result["data"]["latestTick"]["things"][0]
                assert thing["thingType"] == "__broken__"
                assert "Async error" in thing["data"]["error"]

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_async_updater_cannot_return_async_thing_update(self):
        """Test that async_updater returning AsyncThingUpdate creates broken thing."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class NestedAsyncSnapshotter(StatefulOwningSnapshotter):
            def snapshot_owned(self, obj: AsyncCounter) -> AsyncThingUpdate:
                immediate = ThingState(
                    key=("nested_counter", obj.name),
                    thing_type="NestedCounter",
                    thing_data={"status": "pending"},
                    links=[],
                )

                async def return_nested_async():
                    await anyio.sleep(0.1)
                    # This should cause an error!
                    return AsyncThingUpdate(  # type: ignore
                        immediate_update=ThingState(
                            key=("nested_counter", obj.name),
                            thing_type="NestedCounter",
                            thing_data={"status": "nested"},
                            links=[],
                        ),
                        async_updater=lambda: None,  # type: ignore
                    )

                return AsyncThingUpdate(
                    immediate_update=immediate,
                    async_updater=return_nested_async,
                )

            def stateless_snapshot(self, obj: AsyncCounter) -> ThingState:
                return ThingState(
                    key=("nested_counter", obj.name),
                    thing_type="NestedCounter",
                    thing_data={"status": "unknown"},
                    links=[],
                )

        watcher.register_snapshotter(AsyncCounter, NestedAsyncSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 1)
            track(counter)

            tick("info", "initial")

            await asyncio.sleep(0.3)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Should have a broken thing
                result = await graphql_query(
                    client,
                    "query { latestTick { things { thingType data } } }",
                )
                thing = result["data"]["latestTick"]["things"][0]
                assert thing["thingType"] == "__broken__"
                assert "AsyncThingUpdate" in thing["data"]["error"]

        await watcher.watch_and_serve_until(run)


# =============================================================================
# AsyncThingUpdate from stateless_snapshot tests
# =============================================================================


class TestAsyncFromStatelessSnapshot:
    """Tests for AsyncThingUpdate from stateless_snapshot."""

    @pytest.mark.asyncio
    async def test_async_from_stateless_snapshot(self):
        """Test that stateless_snapshot can return AsyncThingUpdate."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class StatelessAsyncSnapshotter(StatefulOwningSnapshotter):
            """Snapshotter where stateless_snapshot also uses async."""

            def snapshot_owned(self, obj: AsyncCounter) -> ThingState:
                return ThingState(
                    key=("stateless_async", obj.name),
                    thing_type="StatelessAsync",
                    thing_data={"value": obj.value, "source": "owned"},
                    links=[],
                )

            def stateless_snapshot(self, obj: AsyncCounter) -> AsyncThingUpdate:
                current_value = obj.value

                immediate = ThingState(
                    key=("stateless_async", obj.name),
                    thing_type="StatelessAsync",
                    thing_data={"value": current_value, "source": "stateless", "status": "pending"},
                    links=[],
                )

                async def complete_async() -> ThingState:
                    await anyio.sleep(0.3)
                    return ThingState(
                        key=("stateless_async", obj.name),
                        thing_type="StatelessAsync",
                        thing_data={
                            "value": current_value,
                            "source": "stateless",
                            "status": "completed",
                        },
                        links=[],
                    )

                return AsyncThingUpdate(
                    immediate_update=immediate,
                    async_updater=complete_async,
                )

        watcher.register_snapshotter(AsyncCounter, StatelessAsyncSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = AsyncCounter("test", 5)
            track(counter)

            # Normal tick first
            tick("info", "initial")

            # Hard recapture uses stateless_snapshot which returns AsyncThingUpdate
            await watcher.hard_recapture()

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Immediate state should show "pending"
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "pending"
                assert data["source"] == "stateless"

                # Wait for async to complete
                await asyncio.sleep(0.5)

                # Should now show "completed"
                result = await graphql_query(
                    client,
                    "query { latestTick { things { data } } }",
                )
                data = result["data"]["latestTick"]["things"][0]["data"]
                assert data["status"] == "completed"
                assert data["source"] == "stateless"

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Mixed async/sync tests
# =============================================================================


class TestMixedAsyncSync:
    """Tests for mixing async and sync snapshotters."""

    @pytest.mark.asyncio
    async def test_async_and_sync_objects_together(self):
        """Test that async and sync objects work together."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(AsyncCounter, AsyncCounterSnapshotter())
        watcher.register_snapshotter(SyncCounter, SyncCounterSnapshotter())

        async def run():
            await wait_for_server(port)

            async_counter = AsyncCounter("async", 5)
            async_counter.async_delay = 0.3
            track(async_counter)

            sync_counter = SyncCounter("sync", 10)
            track(sync_counter)

            tick("info", "initial")

            # Immediately, both should be in the tick
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    "query { latestTick { things { thingType data } } }",
                )
                things = {t["thingType"]: t["data"] for t in result["data"]["latestTick"]["things"]}

                assert "AsyncCounter" in things
                assert things["AsyncCounter"]["status"] == "pending"

                assert "SyncCounter" in things
                assert things["SyncCounter"]["value"] == 10

                # Wait for async to complete
                await asyncio.sleep(0.5)

                result = await graphql_query(
                    client,
                    "query { latestTick { things { thingType data } } }",
                )
                things = {t["thingType"]: t["data"] for t in result["data"]["latestTick"]["things"]}

                assert things["AsyncCounter"]["status"] == "completed"
                assert things["AsyncCounter"]["async_value"] == 50

        await watcher.watch_and_serve_until(run)
