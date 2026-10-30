"""Tests for shortId functionality."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from thingwatcher import ThingState, Watcher, tick, track
from thingwatcher.api import AnchorCapturer, StatelessSnapshotter, register_anchor_capturer
from thingwatcher.registry import clear_global_registries

from .conftest import find_free_port, graphql_query, wait_for_server


class TrackedObject:
    """Simple object for tracking."""

    def __init__(self, name: str, value: int = 0):
        self.name = name
        self.value = value


class TrackedObjectSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: TrackedObject) -> ThingState:
        return ThingState(
            key=("tracked", obj.name),
            thing_type="TrackedObject",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


class PauseAnchor:
    """Simple anchor for pause tests."""

    def __init__(self, name: str):
        self.name = name


class PauseAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: PauseAnchor) -> ThingState:
        return ThingState(
            key=("anchor", anchor.name),
            thing_type="PauseAnchor",
            thing_data={"name": anchor.name},
            links=[],
        )


@pytest.fixture(autouse=True)
def cleanup_registries():
    """Clear global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


class TestShortIdBasics:
    """Basic shortId tests."""

    @pytest.mark.asyncio
    async def test_things_have_short_id(self):
        """Things at tick have shortId field."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(TrackedObject, TrackedObjectSnapshotter())

        async def run():
            await wait_for_server(port)
            obj = TrackedObject("test1", 42)
            track(obj)
            tick("info", "test tick")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client, "{ tick(index: 1) { things { shortId key thingType } } }"
                )
                things = result["data"]["tick"]["things"]
                assert len(things) == 1
                assert things[0]["shortId"] >= 1
                assert things[0]["thingType"] == "TrackedObject"

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_short_id_stable_across_ticks(self):
        """Same thing has same shortId across multiple ticks."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(TrackedObject, TrackedObjectSnapshotter())

        async def run():
            await wait_for_server(port)
            obj = TrackedObject("test1", 0)
            track(obj)
            tick("info", "tick 1")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result1 = await graphql_query(
                    client, "{ tick(index: 1) { things { shortId key } } }"
                )
                short_id_1 = result1["data"]["tick"]["things"][0]["shortId"]

            # Change the object and tick again
            obj.value = 100
            tick("info", "tick 2")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result2 = await graphql_query(
                    client, "{ tick(index: 2) { things { shortId key } } }"
                )
                short_id_2 = result2["data"]["tick"]["things"][0]["shortId"]

            # shortId should be the same
            assert short_id_1 == short_id_2

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_different_things_get_different_short_ids(self):
        """Different things get different shortIds."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(TrackedObject, TrackedObjectSnapshotter())

        async def run():
            await wait_for_server(port)
            obj1 = TrackedObject("alice", 1)
            obj2 = TrackedObject("bob", 2)
            track(obj1)
            track(obj2)
            tick("info", "test tick")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client, "{ tick(index: 1) { things { shortId thingType } } }"
                )
                things = result["data"]["tick"]["things"]
                assert len(things) == 2
                short_ids = {t["shortId"] for t in things}
                assert len(short_ids) == 2  # All unique

        await watcher.watch_and_serve_until(run)


class TestThingDataByShortId:
    """Tests for querying thing by shortId."""

    @pytest.mark.asyncio
    async def test_thing_data_by_short_id(self):
        """Can query thing using shortId."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(TrackedObject, TrackedObjectSnapshotter())

        async def run():
            await wait_for_server(port)
            obj = TrackedObject("test1", 42)
            track(obj)
            tick("info", "test tick")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # First get the shortId
                result = await graphql_query(client, "{ tick(index: 1) { things { shortId } } }")
                short_id = result["data"]["tick"]["things"][0]["shortId"]

                # Query by shortId
                result = await graphql_query(
                    client,
                    f"{{ thing(shortId: {short_id}, atTick: 1) {{ shortId thingType dataChunks {{ data }} }} }}",
                )
                thing_data = result["data"]["thing"]
                assert thing_data is not None
                assert thing_data["shortId"] == short_id
                assert thing_data["thingType"] == "TrackedObject"
                assert thing_data["dataChunks"][0]["data"]["value"] == 42

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_thing_data_unknown_short_id(self):
        """Unknown shortId returns null."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)
            tick("info", "test tick")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client, "{ thing(shortId: 99999, atTick: 1) { shortId } }"
                )
                assert result["data"]["thing"] is None

        await watcher.watch_and_serve_until(run)


class TestThingHistoryByShortId:
    """Tests for querying thingHistory by shortId."""

    @pytest.mark.asyncio
    async def test_thing_history_by_short_id(self):
        """Can query thingHistory using shortId."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(TrackedObject, TrackedObjectSnapshotter())

        async def run():
            await wait_for_server(port)
            obj = TrackedObject("test1", 0)
            track(obj)
            tick("info", "tick 1")

            obj.value = 50
            tick("info", "tick 2")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Get shortId
                result = await graphql_query(client, "{ tick(index: 1) { things { shortId } } }")
                short_id = result["data"]["tick"]["things"][0]["shortId"]

                # Query history by shortId
                result = await graphql_query(
                    client,
                    f"{{ thingHistory(shortId: {short_id}) {{ shortId dataChunks {{ data }} }} }}",
                )
                history = result["data"]["thingHistory"]
                assert len(history) == 2
                assert history[0]["shortId"] == short_id
                assert history[0]["dataChunks"][0]["data"]["value"] == 0
                assert history[1]["dataChunks"][0]["data"]["value"] == 50

        await watcher.watch_and_serve_until(run)


class TestTriggerPauseByShortId:
    """Tests for triggering pauses by shortId."""

    @pytest.mark.asyncio
    async def test_trigger_pause_by_short_id(self):
        """Can trigger pause using shortId."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(TrackedObject, TrackedObjectSnapshotter())
        register_anchor_capturer(PauseAnchor, PauseAnchorCapturer())

        result_holder: list[str] = []

        async def pausing_task():
            result_holder.append("before_pause")
            anchor = PauseAnchor("test1")
            await watcher.pause(anchor)
            result_holder.append("after_pause")

        async def trigger_task():
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Get triggerable things with shortId
                result = await graphql_query(
                    client,
                    "{ latestTick { things(triggerable: true) { shortId key thingType } } }",
                )
                triggerable = result["data"]["latestTick"]["things"]
                assert len(triggerable) == 1
                short_id = triggerable[0]["shortId"]
                assert short_id >= 1

                # Trigger by shortId
                result = await graphql_query(
                    client,
                    f"mutation {{ triggerPause(shortId: {short_id}) {{ ok error }} }}",
                )
                assert result["data"]["triggerPause"]["ok"] is True

        async def run():
            await wait_for_server(port)
            import anyio

            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(trigger_task)

        await watcher.watch_and_serve_until(run)

        assert result_holder == ["before_pause", "after_pause"]

    @pytest.mark.asyncio
    async def test_trigger_pause_unknown_short_id(self):
        """Triggering unknown shortId returns error."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client, "mutation { triggerPause(shortId: 99999) { ok error } }"
                )
                assert result["data"]["triggerPause"]["ok"] is False
                assert "Unknown shortId" in result["data"]["triggerPause"]["error"]

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_triggerable_things_have_short_id(self):
        """latestTick.things(triggerable: true) includes shortId."""
        port = find_free_port()
        watcher = Watcher(port=port)
        register_anchor_capturer(PauseAnchor, PauseAnchorCapturer())

        async def pausing_task():
            anchor = PauseAnchor("test1")
            await watcher.pause(anchor)

        async def check_task():
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    "{ latestTick { things(triggerable: true) { shortId key thingType } } }",
                )
                triggerable = result["data"]["latestTick"]["things"]
                assert len(triggerable) == 1
                assert triggerable[0]["shortId"] >= 1
                assert triggerable[0]["thingType"] == "PauseAnchor"

                # Trigger to clean up
                short_id = triggerable[0]["shortId"]
                await graphql_query(
                    client,
                    f"mutation {{ triggerPause(shortId: {short_id}) {{ ok }} }}",
                )

        async def run():
            await wait_for_server(port)
            import anyio

            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(check_task)

        await watcher.watch_and_serve_until(run)


class TestValidation:
    """Tests for key/shortId validation."""

    @pytest.mark.asyncio
    async def test_must_provide_key_or_short_id_for_thing_data(self):
        """Must provide exactly one of key or shortId for thing query."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)
            tick("info", "test tick")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Neither - should error
                result = await graphql_query(client, "{ thing(atTick: 1) { shortId } }")
                assert "errors" in result

                # Both - should error
                result = await graphql_query(
                    client, '{ thing(atTick: 1, key: "dGVzdA==", shortId: 1) { shortId } }'
                )
                assert "errors" in result

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_must_provide_key_or_short_id_for_trigger_pause(self):
        """Must provide exactly one of key or shortId for triggerPause."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Neither
                result = await graphql_query(client, "mutation { triggerPause { ok error } }")
                assert result["data"]["triggerPause"]["ok"] is False
                assert "Provide exactly one" in result["data"]["triggerPause"]["error"]

                # Both
                result = await graphql_query(
                    client, 'mutation { triggerPause(key: "dGVzdA==", shortId: 1) { ok error } }'
                )
                assert result["data"]["triggerPause"]["ok"] is False
                assert "Provide exactly one" in result["data"]["triggerPause"]["error"]

        await watcher.watch_and_serve_until(run)
