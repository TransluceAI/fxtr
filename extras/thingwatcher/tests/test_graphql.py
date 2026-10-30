"""Tests for GraphQL API functionality."""

from __future__ import annotations

import asyncio
from typing import Any

import cbor2
import pytest
from httpx import AsyncClient
from thingwatcher import ThingLink, ThingState, Watcher, tick, track
from thingwatcher.api import StatelessSnapshotter
from thingwatcher.registry import clear_global_registries, configure_rendering

from .conftest import find_free_port, graphql_query, wait_for_server

# =============================================================================
# Test fixtures and helpers
# =============================================================================


class Counter:
    """Simple test class for tracking."""

    def __init__(self, value: int = 0):
        self.value = value


class CounterSnapshotter(StatelessSnapshotter):
    """Snapshotter for Counter objects."""

    def snapshot(self, obj: Counter) -> ThingState:
        return ThingState(
            key=("counter", id(obj)),
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=[],
        )


class CounterWithLinksSnapshotter(StatelessSnapshotter):
    """Snapshotter that produces links."""

    def __init__(self):
        self._targets: dict[int, list[tuple[str, Any, Any]]] = {}

    def set_links(self, obj: Counter, targets: list[tuple[str, Any, Any]]) -> None:
        """Set the link targets for an object: [(link_type, link_key, to_key), ...]."""
        self._targets[id(obj)] = targets

    def snapshot(self, obj: Counter) -> ThingState:
        targets = self._targets.get(id(obj), [])
        return ThingState(
            key=("counter", id(obj)),
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=[ThingLink(link_type=lt, link_key=lk, to_thing_key=tk) for lt, lk, tk in targets],
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


class TestLatestTick:
    """Tests for latestTick query."""

    @pytest.mark.asyncio
    async def test_latest_tick_empty(self):
        """Test latestTick returns index 0 when no ticks exist."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(client, "{ latestTick { index } }")
                assert result["data"]["latestTick"]["index"] == 0

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_latest_tick_after_ticks(self):
        """Test latestTick returns correct value after ticks."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)
            tick("info", "tick 1")
            tick("info", "tick 2")
            tick("info", "tick 3")

            # Wait for writes to complete
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(client, "{ latestTick { index } }")
                assert result["data"]["latestTick"]["index"] == 3

        await watcher.watch_and_serve_until(run)


class TestTicksQuery:
    """Tests for ticks query."""

    @pytest.mark.asyncio
    async def test_ticks_query(self):
        """Test ticks query returns all ticks."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)
            tick("debug", "debug tick")
            tick("info", "info tick")
            tick("warning", "warning tick")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        ticks {
                            index
                            level
                            type
                            desc
                        }
                    }
                    """,
                )
                ticks_data = result["data"]["ticks"]
                assert len(ticks_data) == 3
                assert ticks_data[0]["index"] == 1
                assert ticks_data[0]["level"] == "debug"
                assert ticks_data[0]["type"] == "api_tick"
                assert ticks_data[0]["desc"] == "debug tick"

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_ticks_query_with_level_filter(self):
        """Test ticks query with level filter."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)
            tick("debug", "debug 1")
            tick("info", "info 1")
            tick("debug", "debug 2")
            tick("warning", "warning 1")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($levels: [String!]) {
                        ticks(levels: $levels) {
                            index
                            level
                        }
                    }
                    """,
                    variables={"levels": ["debug"]},
                )
                ticks_data = result["data"]["ticks"]
                assert len(ticks_data) == 2
                assert all(t["level"] == "debug" for t in ticks_data)

        await watcher.watch_and_serve_until(run)


class TestThingsAtTick:
    """Tests for tick.things query."""

    @pytest.mark.asyncio
    async def test_things_at_tick(self):
        """Test tick.things returns things valid at that tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            c1 = Counter(10)
            c2 = Counter(20)
            track(c1)
            track(c2)
            tick("info", "both tracked")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($index: Int!) {
                        tick(index: $index) {
                            things {
                                key
                                thingType
                                thingstateId
                            }
                        }
                    }
                    """,
                    variables={"index": 1},
                )
                things = result["data"]["tick"]["things"]
                assert len(things) == 2
                assert all(t["thingType"] == "Counter" for t in things)
                # thingstateId should be strings like "db:{id}"
                assert all(
                    isinstance(t["thingstateId"], str) and t["thingstateId"].startswith("db:")
                    for t in things
                )

        await watcher.watch_and_serve_until(run)


class TestThingData:
    """Tests for thing query."""

    @pytest.mark.asyncio
    async def test_thing_data(self):
        """Test thing query returns full thing data with data chunks."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                things_result = await graphql_query(
                    client,
                    "query { tick(index: 1) { things { key } } }",
                )
                key = things_result["data"]["tick"]["things"][0]["key"]

                result = await graphql_query(
                    client,
                    """
                    query($key: String!, $atTick: Int) {
                        thing(key: $key, atTick: $atTick) {
                            key
                            thingType
                            dataChunks {
                                id
                                data
                            }
                        }
                    }
                    """,
                    variables={"key": key, "atTick": 1},
                )
                thing_data = result["data"]["thing"]
                assert thing_data["thingType"] == "Counter"
                assert len(thing_data["dataChunks"]) == 1
                assert thing_data["dataChunks"][0]["data"] == {"value": 42}

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_thing_data_not_found(self):
        """Test thing returns null for non-existent key."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)
            tick("info", "tracked")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                import base64

                fake_key = base64.b64encode(cbor2.dumps(("nonexistent",))).decode()
                result = await graphql_query(
                    client,
                    """
                    query($key: String!, $atTick: Int) {
                        thing(key: $key, atTick: $atTick) {
                            key
                        }
                    }
                    """,
                    variables={"key": fake_key, "atTick": 1},
                )
                assert result["data"]["thing"] is None

        await watcher.watch_and_serve_until(run)


class TestChangedThings:
    """Tests for tick.changes query."""

    @pytest.mark.asyncio
    async def test_changed_things_added(self):
        """Test tick.changes detects added things."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            c1 = Counter(10)
            track(c1)
            tick("info", "first")

            c2 = Counter(20)
            track(c2)
            tick("info", "second with new thing")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 2) {
                            changes(since: 1) {
                                kind
                                thing {
                                    thingType
                                }
                            }
                        }
                    }
                    """,
                )
                changes = result["data"]["tick"]["changes"]
                added = [c for c in changes if c["kind"] == "ADDED"]
                assert len(added) == 1

        await watcher.watch_and_serve_until(run)


class TestTriggerTick:
    """Tests for triggerTick mutation."""

    @pytest.mark.asyncio
    async def test_trigger_tick(self):
        """Test triggerTick mutation creates a tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    mutation {
                        triggerTick(level: "info", desc: "remote tick") {
                            index
                            level
                            type
                        }
                    }
                    """,
                )
                tick_data = result["data"]["triggerTick"]
                assert tick_data["index"] == 1
                assert tick_data["level"] == "info"
                assert tick_data["type"] == "remote_client"

        await watcher.watch_and_serve_until(run)


class TestRenderingConfig:
    """Tests for renderingConfig query."""

    @pytest.mark.asyncio
    async def test_rendering_config(self):
        """Test renderingConfig returns configured rendering settings."""
        configure_rendering("Counter", color="#ff0000", link_colors={"owns": "#00ff00"})

        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        renderingConfig {
                            thingType
                            color
                            linkColors {
                                linkType
                                color
                            }
                        }
                    }
                    """,
                )
                config = result["data"]["renderingConfig"]
                assert len(config) == 1
                counter_config = config[0]
                assert counter_config["thingType"] == "Counter"
                assert counter_config["color"] == "#ff0000"
                assert len(counter_config["linkColors"]) == 1
                assert counter_config["linkColors"][0]["linkType"] == "owns"
                assert counter_config["linkColors"][0]["color"] == "#00ff00"

        await watcher.watch_and_serve_until(run)


class TestDebugTrigger:
    """Tests for debug trigger REST endpoint."""

    @pytest.mark.asyncio
    async def test_debug_trigger_no_waiter(self):
        """Test debug trigger returns 400 when no waiter registered."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                response = await client.post(
                    "/debug_trigger/test",
                    content=cbor2.dumps({"payload": "data"}),
                )
                assert response.status_code == 400
                assert not response.json()["ok"]

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_debug_trigger_with_waiter(self):
        """Test debug trigger wakes waiter and returns payload."""
        port = find_free_port()
        watcher = Watcher(port=port)

        received_payload = []

        async def run():
            await wait_for_server(port)

            async def waiter():
                payload = await watcher.debug_receive("test", timeout=5.0)
                received_payload.append(payload)

            async def trigger():
                await asyncio.sleep(0.1)
                async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    response = await client.post(
                        "/debug_trigger/test",
                        content=cbor2.dumps({"key": "value"}),
                    )
                    assert response.status_code == 200
                    assert response.json()["ok"]

            await asyncio.gather(waiter(), trigger())

        await watcher.watch_and_serve_until(run)
        assert len(received_payload) == 1
        assert received_payload[0] == {"key": "value"}


class TestInvalidBase64Key:
    """Tests for invalid base64 key error handling."""

    @pytest.mark.asyncio
    async def test_invalid_base64_returns_error(self):
        """Test that invalid base64 key returns GraphQL error."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)
            tick("info", "tracked")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        thing(key: "not-valid-base64!@#", atTick: 1) {
                            key
                        }
                    }
                    """,
                )
                assert "errors" in result
                assert "Invalid base64 key" in result["errors"][0]["message"]

        await watcher.watch_and_serve_until(run)


class TestDataChunkQuery:
    """Tests for dataChunk query."""

    @pytest.mark.asyncio
    async def test_data_chunk_by_id(self):
        """Test fetching a single data chunk by ID."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(99)
            track(counter)
            tick("info", "tracked")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                things_result = await graphql_query(
                    client,
                    "query { tick(index: 1) { things { key } } }",
                )
                key = things_result["data"]["tick"]["things"][0]["key"]

                data_result = await graphql_query(
                    client,
                    """
                    query($key: String!, $atTick: Int) {
                        thing(key: $key, atTick: $atTick) {
                            dataChunks { id }
                        }
                    }
                    """,
                    variables={"key": key, "atTick": 1},
                )
                chunk_id = data_result["data"]["thing"]["dataChunks"][0]["id"]

                chunk_result = await graphql_query(
                    client,
                    """
                    query($id: String!) {
                        dataChunk(id: $id) {
                            id
                            data
                        }
                    }
                    """,
                    variables={"id": chunk_id},
                )
                chunk = chunk_result["data"]["dataChunk"]
                assert chunk["id"] == chunk_id
                assert chunk["data"] == {"value": 99}

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_data_chunk_not_found(self):
        """Test dataChunk returns null for non-existent ID."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Test with non-existent db ID
                result = await graphql_query(
                    client,
                    'query { dataChunk(id: "db:99999") { id } }',
                )
                assert result["data"]["dataChunk"] is None

                # Test with invalid ext ID (invalid base64)
                result = await graphql_query(
                    client,
                    'query { dataChunk(id: "ext:invalid!!!") { id } }',
                )
                assert result["data"]["dataChunk"] is None

        await watcher.watch_and_serve_until(run)


class TestThingHistory:
    """Tests for thingHistory query."""

    @pytest.mark.asyncio
    async def test_thing_history(self):
        """Test thingHistory returns all historical states."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(1)
            track(counter)
            tick("info", "v1")

            counter.value = 2
            tick("info", "v2")

            counter.value = 3
            tick("info", "v3")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                things_result = await graphql_query(
                    client,
                    "query { tick(index: 3) { things { key } } }",
                )
                key = things_result["data"]["tick"]["things"][0]["key"]

                result = await graphql_query(
                    client,
                    """
                    query($key: String!) {
                        thingHistory(key: $key) {
                            dataChunks { data }
                        }
                    }
                    """,
                    variables={"key": key},
                )
                history = result["data"]["thingHistory"]
                assert len(history) == 3
                assert history[0]["dataChunks"][0]["data"] == {"value": 1}
                assert history[1]["dataChunks"][0]["data"] == {"value": 2}
                assert history[2]["dataChunks"][0]["data"] == {"value": 3}

        await watcher.watch_and_serve_until(run)


class TestLinkOnlyChange:
    """Tests for link-only changes being detected as MODIFIED."""

    @pytest.mark.asyncio
    async def test_link_only_change_is_modified(self):
        """Test that changing only links (not data) is detected as MODIFIED."""
        port = find_free_port()
        watcher = Watcher(port=port)
        snapshotter = CounterWithLinksSnapshotter()
        watcher.register_snapshotter(Counter, snapshotter)

        async def run():
            await wait_for_server(port)

            # Create counter with link to "target_a"
            counter = Counter(42)
            snapshotter.set_links(counter, [("owns", "link1", ("target", "a"))])
            track(counter)
            tick("info", "with link to a")

            # Change link to "target_b" but keep same data
            snapshotter.set_links(counter, [("owns", "link1", ("target", "b"))])
            tick("info", "with link to b")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 2) {
                            changes(since: 1) {
                                kind
                                thing {
                                    thingType
                                }
                            }
                        }
                    }
                    """,
                )
                changes = result["data"]["tick"]["changes"]
                # Should be detected as MODIFIED even though data didn't change
                assert len(changes) == 1
                assert changes[0]["kind"] == "MODIFIED"

        await watcher.watch_and_serve_until(run)


class TestDebugReceiveErrors:
    """Tests for debug_receive error conditions."""

    @pytest.mark.asyncio
    async def test_debug_receive_timeout(self):
        """Test debug_receive raises TimeoutError on timeout."""
        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)

            import anyio

            with pytest.raises(TimeoutError):
                with anyio.fail_after(0.5):
                    await watcher.debug_receive("never_triggered", timeout=0.1)

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_debug_receive_duplicate_waiter(self):
        """Test debug_receive raises DebugTriggerError for duplicate waiters."""
        from thingwatcher.watcher import DebugTriggerError

        port = find_free_port()
        watcher = Watcher(port=port)

        async def run():
            await wait_for_server(port)

            # Start first waiter
            async def first_waiter():
                try:
                    await watcher.debug_receive("test_trigger", timeout=2.0)
                except TimeoutError:
                    pass  # Expected - we won't trigger it

            async def second_waiter():
                await asyncio.sleep(0.1)  # Let first waiter register
                with pytest.raises(DebugTriggerError, match="Another waiter already registered"):
                    await watcher.debug_receive("test_trigger", timeout=0.1)

            await asyncio.gather(first_waiter(), second_waiter())

        await watcher.watch_and_serve_until(run)


class TestNonJsonCborError:
    """Tests for non-JSON CBOR data error handling."""

    @pytest.mark.asyncio
    async def test_non_json_cbor_returns_error(self):
        """Test that CBOR data with bytes raises GraphQL error."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class BytesCounter:
            """Counter that stores bytes in its data."""

            def __init__(self, value: int):
                self.value = value
                self.binary_data = b"\x00\x01\x02\x03"

        class BytesCounterSnapshotter(StatelessSnapshotter):
            """Snapshotter that includes bytes in thing_data."""

            def snapshot(self, obj: BytesCounter) -> ThingState:
                return ThingState(
                    key=("bytes_counter", id(obj)),
                    thing_type="BytesCounter",
                    thing_data={"value": obj.value, "binary": obj.binary_data},
                    links=[],
                )

        watcher.register_snapshotter(BytesCounter, BytesCounterSnapshotter())

        async def run():
            await wait_for_server(port)

            counter = BytesCounter(99)
            track(counter)
            tick("info", "with bytes data")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # First get the key
                things_result = await graphql_query(
                    client,
                    "query { tick(index: 1) { things { key } } }",
                )
                key = things_result["data"]["tick"]["things"][0]["key"]

                # Now try to get thing data - should error due to bytes
                result = await graphql_query(
                    client,
                    """
                    query($key: String!, $atTick: Int) {
                        thing(key: $key, atTick: $atTick) {
                            dataChunks { data }
                        }
                    }
                    """,
                    variables={"key": key, "atTick": 1},
                )
                # Should have an error about non-JSON-compatible CBOR
                assert "errors" in result
                assert "non-JSON-compatible" in result["errors"][0]["message"]

        await watcher.watch_and_serve_until(run)


class TestNewTickSubscription:
    """Tests for newTick WebSocket subscription."""

    @pytest.mark.asyncio
    async def test_new_tick_subscription(self):
        """Test newTick subscription receives tick data via WebSocket."""
        import json

        import websockets

        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        received_ticks: list[dict] = []

        async def run():
            await wait_for_server(port)

            # Use raw websockets with graphql-transport-ws protocol
            uri = f"ws://127.0.0.1:{port}/graphql/"
            async with websockets.connect(uri, subprotocols=["graphql-transport-ws"]) as ws:
                # Send ConnectionInit per graphql-transport-ws protocol
                await ws.send(json.dumps({"type": "connection_init"}))
                ack = json.loads(await ws.recv())
                assert ack["type"] == "connection_ack"

                # Subscribe to newTick (returns NewTickPayload with tick and triggerableKeys)
                await ws.send(
                    json.dumps(
                        {
                            "id": "1",
                            "type": "subscribe",
                            "payload": {
                                "query": "subscription { newTick { tick { index level type } triggerableKeys } }"
                            },
                        }
                    )
                )

                # Trigger a tick after subscription is set up
                async def trigger_after_delay():
                    await asyncio.sleep(0.1)
                    counter = Counter(1)
                    track(counter)
                    tick("info", "test tick")

                async def receive_tick():
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                    assert msg["type"] == "next"
                    received_ticks.append(msg["payload"]["data"]["newTick"])

                await asyncio.gather(receive_tick(), trigger_after_delay())

            # Verify we received the tick (now in NewTickPayload format)
            assert len(received_ticks) == 1
            assert received_ticks[0]["tick"]["index"] == 1
            assert received_ticks[0]["tick"]["level"] == "info"
            assert received_ticks[0]["tick"]["type"] == "api_tick"
            assert isinstance(received_ticks[0]["triggerableKeys"], list)

        await watcher.watch_and_serve_until(run)


class TestThingDataField:
    """Tests for the Thing.data convenience field."""

    @pytest.mark.asyncio
    async def test_thing_data_field(self):
        """Test the data convenience field returns merged data."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                shortId
                                thingType
                                data
                            }
                        }
                    }
                    """,
                )
                things = result["data"]["latestTick"]["things"]
                assert len(things) == 1
                assert things[0]["data"] == {"value": 42}

        await watcher.watch_and_serve_until(run)


class TestLinkToThing:
    """Tests for Link.toThing field."""

    @pytest.mark.asyncio
    async def test_link_to_thing(self):
        """Test Link.toThing resolves the target thing."""
        port = find_free_port()
        watcher = Watcher(port=port)
        snapshotter = CounterWithLinksSnapshotter()
        watcher.register_snapshotter(Counter, snapshotter)

        async def run():
            await wait_for_server(port)

            # Create two counters, one linking to the other
            c1 = Counter(10)
            c2 = Counter(20)
            track(c1)
            track(c2)

            # c1 links to c2
            snapshotter.set_links(c1, [("owns", "link1", ("counter", id(c2)))])
            tick("info", "with link")

            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                data
                                links {
                                    linkType
                                    toThing {
                                        thingType
                                        data
                                    }
                                }
                            }
                        }
                    }
                    """,
                )
                things = result["data"]["latestTick"]["things"]
                # Find the thing with links
                thing_with_link = next((t for t in things if t["links"]), None)
                assert thing_with_link is not None
                assert thing_with_link["data"] == {"value": 10}
                assert len(thing_with_link["links"]) == 1
                link = thing_with_link["links"][0]
                assert link["linkType"] == "owns"
                assert link["toThing"]["thingType"] == "Counter"
                assert link["toThing"]["data"] == {"value": 20}

        await watcher.watch_and_serve_until(run)


class TestTriggerableFilter:
    """Tests for things(triggerable: true) filter."""

    @pytest.mark.asyncio
    async def test_triggerable_filter(self):
        """Test filtering things by triggerable status."""
        from thingwatcher.api import AnchorCapturer, register_anchor_capturer

        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        class WaitPoint:
            pass

        class WaitPointCapturer(AnchorCapturer):
            def handle(self, anchor: WaitPoint) -> ThingState:
                return ThingState(
                    key=("waitpoint", id(anchor)),
                    thing_type="WaitPoint",
                    thing_data={"waiting": True},
                    links=[],
                )

        register_anchor_capturer(WaitPoint, WaitPointCapturer())

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)

            # Start a pause that creates a triggerable thing
            async def pauser():
                wp = WaitPoint()
                await watcher.receive(wp)

            async def checker():
                await asyncio.sleep(0.2)  # Let pause register
                async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    # Get all things
                    all_result = await graphql_query(
                        client,
                        "query { latestTick { things { thingType } } }",
                    )
                    all_things = all_result["data"]["latestTick"]["things"]

                    # Get only triggerable things using filter
                    trig_result = await graphql_query(
                        client,
                        "query { latestTick { things(triggerable: true) { thingType } } }",
                    )
                    trig_things = trig_result["data"]["latestTick"]["things"]

                    # Should have Counter (not triggerable) and WaitPoint (triggerable)
                    assert len(all_things) == 2
                    assert len(trig_things) == 1
                    assert trig_things[0]["thingType"] == "WaitPoint"

                    # Trigger to let pauser finish
                    await graphql_query(
                        client,
                        "mutation { triggerPause(shortId: 2) { ok } }",
                    )

            await asyncio.gather(pauser(), checker())

        await watcher.watch_and_serve_until(run)


# =============================================================================
# keyJSON Tests
# =============================================================================


class TestKeyJson:
    """Tests for keyJSON output and input fields."""

    @pytest.mark.asyncio
    async def test_key_json_output(self):
        """Thing.keyJSON returns decoded JSON for JSON-compatible keys."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                key
                                keyJSON
                            }
                        }
                    }
                    """,
                )

                things = result["data"]["latestTick"]["things"]
                assert len(things) == 1
                # The key is ("counter", id(obj)) which is JSON-compatible
                assert things[0]["keyJSON"] is not None
                assert things[0]["key"] is not None

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_key_json_null_for_bytes_key(self):
        """Thing.keyJSON returns null for non-JSON-compatible keys (bytes)."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class BytesKeyObject:
            """Object with bytes in its key."""

            def __init__(self, data: bytes):
                self.data = data

        class BytesKeySnapshotter(StatelessSnapshotter):
            """Snapshotter that uses bytes in the key."""

            def snapshot(self, obj: BytesKeyObject) -> ThingState:
                return ThingState(
                    key=obj.data,  # bytes are not JSON-compatible
                    thing_type="BytesKeyThing",
                    thing_data={"length": len(obj.data)},
                    links=[],
                )

        watcher.register_snapshotter(BytesKeyObject, BytesKeySnapshotter())

        async def run():
            await wait_for_server(port)
            obj = BytesKeyObject(b"\x00\x01\x02\x03")
            track(obj)
            tick("info", "tracked")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                key
                                keyJSON
                                thingType
                            }
                        }
                    }
                    """,
                )

                things = result["data"]["latestTick"]["things"]
                assert len(things) == 1
                thing = things[0]
                assert thing["thingType"] == "BytesKeyThing"
                # keyJSON should be null for bytes key
                assert thing["keyJSON"] is None
                # But base64 key should still be available
                assert thing["key"] is not None

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_thing_query_by_key_json(self):
        """thing() query accepts keyJSON parameter."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # First get the keyJSON
                list_result = await graphql_query(
                    client,
                    "query { latestTick { things { keyJSON } } }",
                )
                key_json = list_result["data"]["latestTick"]["things"][0]["keyJSON"]

                # Query by keyJSON
                result = await graphql_query(
                    client,
                    """
                    query GetThing($keyJSON: JSON) {
                        thing(keyJSON: $keyJSON) {
                            keyJSON
                            thingType
                            data
                        }
                    }
                    """,
                    variables={"keyJSON": key_json},
                )

                thing = result["data"]["thing"]
                assert thing is not None
                assert thing["keyJSON"] == key_json
                assert thing["thingType"] == "Counter"
                assert thing["data"] == {"value": 42}

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_key_json_null_rejection(self):
        """Error when keyJSON is explicitly null."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query GetThing($keyJSON: JSON) {
                        thing(keyJSON: $keyJSON) {
                            shortId
                        }
                    }
                    """,
                    variables={"keyJSON": None},
                )

                assert "errors" in result
                error_msg = str(result["errors"][0]).lower()
                assert "null" in error_msg

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_link_key_json(self):
        """Link.linkKeyJSON returns decoded JSON for link keys."""
        port = find_free_port()
        snapshotter = CounterWithLinksSnapshotter()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, snapshotter)

        async def run():
            await wait_for_server(port)
            counter_a = Counter(1)
            counter_b = Counter(2)
            # Set up link: counter_a -> counter_b
            snapshotter.set_links(
                counter_a, [("points_to", "link_key_1", ("counter", id(counter_b)))]
            )
            track(counter_a)
            track(counter_b)
            tick("info", "with link")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                data
                                links {
                                    linkKey
                                    linkKeyJSON
                                    toThing {
                                        keyJSON
                                    }
                                }
                            }
                        }
                    }
                    """,
                )

                things = result["data"]["latestTick"]["things"]
                # Find the thing with links
                thing_with_links = next((t for t in things if t["links"]), None)
                assert thing_with_links is not None
                link = thing_with_links["links"][0]
                assert link["linkKeyJSON"] == "link_key_1"
                assert link["linkKey"] is not None
                assert link["toThing"]["keyJSON"] is not None

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_thing_history_by_key_json(self):
        """thingHistory() query accepts keyJSON parameter."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(10)
            track(counter)
            tick("info", "v1")
            counter.value = 20
            tick("info", "v2")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # First get the keyJSON
                list_result = await graphql_query(
                    client,
                    "query { latestTick { things { keyJSON } } }",
                )
                key_json = list_result["data"]["latestTick"]["things"][0]["keyJSON"]

                # Query history by keyJSON
                result = await graphql_query(
                    client,
                    """
                    query GetHistory($keyJSON: JSON) {
                        thingHistory(keyJSON: $keyJSON) {
                            keyJSON
                            data
                        }
                    }
                    """,
                    variables={"keyJSON": key_json},
                )

                history = result["data"]["thingHistory"]
                assert len(history) == 2
                # Both should have same keyJSON
                assert history[0]["keyJSON"] == key_json
                assert history[1]["keyJSON"] == key_json

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_error_multiple_key_params(self):
        """Error when providing both shortId and keyJSON."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query GetThing($keyJSON: JSON) {
                        thing(shortId: 1, keyJSON: $keyJSON) {
                            shortId
                        }
                    }
                    """,
                    variables={"keyJSON": {"test": 1}},
                )

                assert "errors" in result
                error_msg = str(result["errors"][0]).lower()
                assert "exactly one" in error_msg or "key" in error_msg

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_error_no_key_params(self):
        """Error when providing no key params to thing query."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            counter = Counter(42)
            track(counter)
            tick("info", "tracked")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        thing(atTick: 1) {
                            shortId
                        }
                    }
                    """,
                )

                assert "errors" in result
                error_msg = str(result["errors"][0]).lower()
                assert "exactly one" in error_msg or "none" in error_msg

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_trigger_pause_by_key_json(self):
        """triggerPause() mutation accepts keyJSON parameter."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class WaitPoint:
            """Triggerable wait point."""

            def __init__(self, name: str):
                self.name = name

        class WaitPointSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj: WaitPoint) -> ThingState:
                return ThingState(
                    key=("waitpoint", obj.name),
                    thing_type="WaitPoint",
                    thing_data={"name": obj.name},
                    links=[],
                )

        class WaitPointCapturer:
            def handle(self, anchor: WaitPoint) -> ThingState:
                return ThingState(
                    key=("waitpoint", anchor.name),
                    thing_type="WaitPoint",
                    thing_data={"name": anchor.name},
                    links=[],
                )

        watcher.register_snapshotter(WaitPoint, WaitPointSnapshotter())
        watcher.register_anchor_capturer(WaitPoint, WaitPointCapturer())

        received_payload = []

        async def run():
            await wait_for_server(port)

            async def pauser():
                wp = WaitPoint("test")
                result = await watcher.receive(wp, WaitPoint)
                received_payload.append(result)

            async def trigger():
                await asyncio.sleep(0.2)  # Give pauser time to register
                tick("info", "before trigger")
                await asyncio.sleep(0.1)

                async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    # Get the keyJSON first
                    list_result = await graphql_query(
                        client,
                        "query { latestTick { things { keyJSON thingType } } }",
                    )
                    things = list_result["data"]["latestTick"]["things"]
                    wait_thing = next(t for t in things if t["thingType"] == "WaitPoint")
                    key_json = wait_thing["keyJSON"]

                    # Trigger by keyJSON
                    result = await graphql_query(
                        client,
                        """
                        mutation TriggerPause($keyJSON: JSON, $payload: JSON) {
                            triggerPause(keyJSON: $keyJSON, payload: $payload) {
                                ok
                                error
                            }
                        }
                        """,
                        variables={"keyJSON": key_json, "payload": {"msg": "hello"}},
                    )

                    assert result["data"]["triggerPause"]["ok"] is True

            await asyncio.gather(pauser(), trigger())
            assert received_payload == [{"msg": "hello"}]

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_trigger_records_by_key_json(self):
        """triggerRecords() query accepts keyJSON filter parameter."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class WaitPoint:
            """Triggerable wait point."""

            def __init__(self, name: str):
                self.name = name

        class WaitPointSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj: WaitPoint) -> ThingState:
                return ThingState(
                    key=("waitpoint", obj.name),
                    thing_type="WaitPoint",
                    thing_data={"name": obj.name},
                    links=[],
                )

        class WaitPointCapturer:
            def handle(self, anchor: WaitPoint) -> ThingState:
                return ThingState(
                    key=("waitpoint", anchor.name),
                    thing_type="WaitPoint",
                    thing_data={"name": anchor.name},
                    links=[],
                )

        watcher.register_snapshotter(WaitPoint, WaitPointSnapshotter())
        watcher.register_anchor_capturer(WaitPoint, WaitPointCapturer())

        async def run():
            await wait_for_server(port)

            async def pauser():
                wp = WaitPoint("record_test")
                await watcher.receive(wp, WaitPoint)

            async def trigger_and_check():
                await asyncio.sleep(0.2)  # Give pauser time to register
                tick("info", "before trigger")
                await asyncio.sleep(0.1)

                async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    # Get the keyJSON first
                    list_result = await graphql_query(
                        client,
                        "query { latestTick { things { keyJSON thingType } } }",
                    )
                    things = list_result["data"]["latestTick"]["things"]
                    wait_thing = next(t for t in things if t["thingType"] == "WaitPoint")
                    key_json = wait_thing["keyJSON"]

                    # Trigger by shortId (simpler for this test)
                    await graphql_query(
                        client,
                        """
                        mutation TriggerPause($keyJSON: JSON, $payload: JSON) {
                            triggerPause(keyJSON: $keyJSON, payload: $payload) { ok }
                        }
                        """,
                        variables={"keyJSON": key_json, "payload": {"data": 123}},
                    )
                    await asyncio.sleep(0.1)

                    # Now query trigger records by keyJSON
                    result = await graphql_query(
                        client,
                        """
                        query GetRecords($keyJSON: JSON) {
                            triggerRecords(keyJSON: $keyJSON) {
                                payload
                                thing { keyJSON }
                            }
                        }
                        """,
                        variables={"keyJSON": key_json},
                    )

                    records = result["data"]["triggerRecords"]
                    assert len(records) == 1
                    assert records[0]["payload"] == {"data": 123}
                    assert records[0]["thing"]["keyJSON"] == key_json

            await asyncio.gather(pauser(), trigger_and_check())

        await watcher.watch_and_serve_until(run)
