"""Tests for roots functionality."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from thingwatcher import (
    NoWatcherConnectedError,
    ThingRef,
    ThingState,
    Watcher,
    add_root,
    remove_root,
    tick,
    track,
)
from thingwatcher.api import StatelessSnapshotter
from thingwatcher.registry import clear_global_registries

from .conftest import find_free_port, graphql_query, wait_for_server

# =============================================================================
# Test fixtures and helpers
# =============================================================================


@dataclass
class RootAnchor:
    """Simple test anchor object for root tests."""

    name: str
    value: int = 0


class RootAnchorCapturer:
    """AnchorCapturer for RootAnchor that returns ThingState."""

    def handle(self, anchor: RootAnchor) -> ThingState:
        return ThingState(
            key=("root_anchor", anchor.name),
            thing_type="root_anchor",
            thing_data={"name": anchor.name, "value": anchor.value},
            links=[],
        )


class RootAnchorRefCapturer:
    """AnchorCapturer that returns ThingRef for already-tracked objects."""

    def handle(self, anchor: RootAnchor) -> ThingRef:
        return ThingRef(key=("root_anchor", anchor.name))


class RootAnchorSnapshotter(StatelessSnapshotter):
    """Snapshotter for RootAnchor (when also tracked)."""

    def snapshot(self, obj: RootAnchor) -> ThingState:
        return ThingState(
            key=("root_anchor", obj.name),
            thing_type="root_anchor",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


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


@pytest.fixture(autouse=True)
def cleanup_registries():
    """Clear global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


# =============================================================================
# Module-level API tests
# =============================================================================


class TestModuleLevelApi:
    """Tests for module-level add_root/remove_root functions."""

    async def test_add_root_without_watcher_raises_error(self):
        """Test that add_root raises NoWatcherConnectedError without active watcher."""
        anchor = RootAnchor(name="test")
        with pytest.raises(
            NoWatcherConnectedError, match="add_root\\(\\) requires an active watcher"
        ):
            add_root("test", anchor)

    async def test_remove_root_without_watcher_raises_error(self):
        """Test that remove_root raises NoWatcherConnectedError without active watcher."""
        with pytest.raises(
            NoWatcherConnectedError, match="remove_root\\(\\) requires an active watcher"
        ):
            remove_root("test")

    async def test_add_root_with_watcher(self):
        """Test that add_root works with an active watcher."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())

        anchor = RootAnchor(name="test", value=42)

        async def run():
            add_root("myroot", anchor)
            # Should not raise

        await watcher.watch_and_serve_until(run)

    async def test_remove_root_with_watcher(self):
        """Test that remove_root works with an active watcher."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())

        anchor = RootAnchor(name="test", value=42)

        async def run():
            add_root("myroot", anchor)
            remove_root("myroot")
            # Should not raise

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Watcher method tests
# =============================================================================


class TestWatcherRootMethods:
    """Tests for watcher add_root/remove_root methods."""

    async def test_add_root_basic(self):
        """Test basic add_root functionality."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())

        anchor = RootAnchor(name="user1", value=100)

        async def run():
            watcher.add_root("currentUser", anchor)
            tick("info", "after adding root")
            await asyncio.sleep(0.1)

        await watcher.watch_and_serve_until(run)

    async def test_add_root_conflict_raises_error(self):
        """Test that add_root raises ValueError for duplicate names without replace."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())

        anchor1 = RootAnchor(name="user1")
        anchor2 = RootAnchor(name="user2")

        async def run():
            watcher.add_root("currentUser", anchor1)
            with pytest.raises(ValueError, match="root 'currentUser' already exists"):
                watcher.add_root("currentUser", anchor2)

        await watcher.watch_and_serve_until(run)

    async def test_add_root_with_replace(self):
        """Test that add_root with replace=True allows overwriting."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())

        anchor1 = RootAnchor(name="user1", value=1)
        anchor2 = RootAnchor(name="user2", value=2)

        async def run():
            watcher.add_root("currentUser", anchor1)
            # Should not raise with replace=True
            watcher.add_root("currentUser", anchor2, replace=True)
            tick("info", "after replacing root")
            await asyncio.sleep(0.1)

        await watcher.watch_and_serve_until(run)

    async def test_remove_root_basic(self):
        """Test basic remove_root functionality."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())

        anchor = RootAnchor(name="user1")

        async def run():
            watcher.add_root("currentUser", anchor)
            watcher.remove_root("currentUser")
            tick("info", "after removing root")
            await asyncio.sleep(0.1)

        await watcher.watch_and_serve_until(run)

    async def test_remove_root_nonexistent_raises_error(self):
        """Test that remove_root raises KeyError for nonexistent root."""
        watcher = Watcher(port=None)

        async def run():
            with pytest.raises(KeyError, match="no root named 'nonexistent'"):
                watcher.remove_root("nonexistent")

        await watcher.watch_and_serve_until(run)

    async def test_add_root_no_capturer_raises_error(self):
        """Test that add_root raises ValueError if no capturer is registered."""
        watcher = Watcher(port=None)

        anchor = RootAnchor(name="user1")

        async def run():
            with pytest.raises(ValueError, match="no anchor capturer registered"):
                watcher.add_root("currentUser", anchor)

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Integration tests - root tracking across ticks
# =============================================================================


class TestRootTracking:
    """Tests for root tracking across ticks."""

    async def test_root_appears_in_tick(self):
        """Test that root appears in tick when added."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor = RootAnchor(name="task1", value=42)

        async def run():
            await wait_for_server(port)

            watcher.add_root("mainTask", anchor)
            counter = Counter(1)
            track(counter)
            tick("info", "tick with root")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            index
                            roots {
                                name
                                thing {
                                    thingType
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                tick_data = result["data"]["latestTick"]
                assert tick_data["index"] == 1

                roots = tick_data["roots"]
                assert len(roots) == 1
                assert roots[0]["name"] == "mainTask"
                assert roots[0]["thing"]["thingType"] == "root_anchor"
                assert roots[0]["thing"]["data"]["name"] == "task1"
                assert roots[0]["thing"]["data"]["value"] == 42

        await watcher.watch_and_serve_until(run)

    async def test_multiple_roots(self):
        """Test multiple roots in same tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor1 = RootAnchor(name="user1", value=1)
        anchor2 = RootAnchor(name="task1", value=2)

        async def run():
            await wait_for_server(port)

            watcher.add_root("currentUser", anchor1)
            watcher.add_root("mainTask", anchor2)
            counter = Counter(1)
            track(counter)
            tick("info", "tick with roots")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            roots {
                                name
                            }
                        }
                    }
                    """,
                )
                roots = result["data"]["latestTick"]["roots"]
                root_names = {r["name"] for r in roots}
                assert root_names == {"currentUser", "mainTask"}

        await watcher.watch_and_serve_until(run)

    async def test_root_persists_across_ticks(self):
        """Test that root persists across multiple ticks."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor = RootAnchor(name="persistent", value=99)

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            watcher.add_root("persist", anchor)
            tick("info", "tick 1")
            await asyncio.sleep(0.05)
            tick("info", "tick 2")
            await asyncio.sleep(0.05)
            tick("info", "tick 3")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Check all three ticks have the root
                for tick_idx in [1, 2, 3]:
                    result = await graphql_query(
                        client,
                        """
                        query($tick: Int!) {
                            tick(index: $tick) {
                                roots {
                                    name
                                    thing {
                                        data
                                    }
                                }
                            }
                        }
                        """,
                        variables={"tick": tick_idx},
                    )
                    roots = result["data"]["tick"]["roots"]
                    assert len(roots) == 1
                    assert roots[0]["name"] == "persist"
                    assert roots[0]["thing"]["data"]["value"] == 99

        await watcher.watch_and_serve_until(run)

    async def test_root_change_detection(self):
        """Test that changing root target creates new binding."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor1 = RootAnchor(name="first", value=1)
        anchor2 = RootAnchor(name="second", value=2)

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            watcher.add_root("changing", anchor1)
            tick("info", "tick 1")
            await asyncio.sleep(0.1)

            # Change root target
            watcher.add_root("changing", anchor2, replace=True)
            tick("info", "tick 2")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Tick 1 should point to first anchor
                result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 1) {
                            roots {
                                name
                                thing {
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                roots = result["data"]["tick"]["roots"]
                assert len(roots) == 1
                assert roots[0]["thing"]["data"]["name"] == "first"

                # Tick 2 should point to second anchor
                result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 2) {
                            roots {
                                name
                                thing {
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                roots = result["data"]["tick"]["roots"]
                assert len(roots) == 1
                assert roots[0]["thing"]["data"]["name"] == "second"

        await watcher.watch_and_serve_until(run)

    async def test_root_removal_history(self):
        """Test that removed roots don't appear in subsequent ticks."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor = RootAnchor(name="removable", value=42)

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            watcher.add_root("temp", anchor)
            tick("info", "tick 1 - with root")
            await asyncio.sleep(0.1)

            watcher.remove_root("temp")
            tick("info", "tick 2 - without root")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Tick 1 should have the root
                result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 1) {
                            roots {
                                name
                            }
                        }
                    }
                    """,
                )
                assert len(result["data"]["tick"]["roots"]) == 1

                # Tick 2 should not have the root
                result = await graphql_query(
                    client,
                    """
                    query {
                        tick(index: 2) {
                            roots {
                                name
                            }
                        }
                    }
                    """,
                )
                assert len(result["data"]["tick"]["roots"]) == 0

        await watcher.watch_and_serve_until(run)


# =============================================================================
# ThingRef tests - root pointing to tracked thing
# =============================================================================


class TestRootWithThingRef:
    """Tests for roots using ThingRef to reference tracked objects."""

    async def test_root_with_thing_ref(self):
        """Test that root can use ThingRef to point to tracked thing."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorRefCapturer())
        watcher.register_snapshotter(RootAnchor, RootAnchorSnapshotter())

        anchor = RootAnchor(name="tracked", value=100)

        async def run():
            await wait_for_server(port)

            # Track the anchor as a thing
            track(anchor)
            # Add it as a root using the ref capturer
            watcher.add_root("myRoot", anchor)
            tick("info", "tick with root ref")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            roots {
                                name
                                thing {
                                    thingType
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                roots = result["data"]["latestTick"]["roots"]
                assert len(roots) == 1
                assert roots[0]["name"] == "myRoot"
                assert roots[0]["thing"]["thingType"] == "root_anchor"
                assert roots[0]["thing"]["data"]["name"] == "tracked"
                assert roots[0]["thing"]["data"]["value"] == 100

        await watcher.watch_and_serve_until(run)

    async def test_root_with_thing_ref_to_untracked_creates_broken_thing(self):
        """Test that ThingRef to untracked thing creates a broken thing."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorRefCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor = RootAnchor(name="untracked", value=999)

        async def run():
            await wait_for_server(port)

            # Add as root but don't track - this creates a broken thing
            counter = Counter(1)
            track(counter)
            watcher.add_root("broken", anchor)
            tick("info", "tick with broken root")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            roots {
                                name
                                thing {
                                    thingType
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                roots = result["data"]["latestTick"]["roots"]
                assert len(roots) == 1
                assert roots[0]["name"] == "broken"
                # Broken thing has type "__broken__"
                assert roots[0]["thing"]["thingType"] == "__broken__"

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Query.root tests
# =============================================================================


class TestQueryRoot:
    """Tests for Query.root resolver."""

    async def test_query_root_by_name(self):
        """Test querying a specific root by name."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor = RootAnchor(name="special", value=77)

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            watcher.add_root("mySpecialRoot", anchor)
            tick("info", "tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        root(name: "mySpecialRoot") {
                            name
                            thing {
                                thingType
                                data
                            }
                        }
                    }
                    """,
                )
                root = result["data"]["root"]
                assert root is not None
                assert root["name"] == "mySpecialRoot"
                assert root["thing"]["thingType"] == "root_anchor"
                assert root["thing"]["data"]["value"] == 77

        await watcher.watch_and_serve_until(run)

    async def test_query_root_at_tick(self):
        """Test querying a root at a specific tick."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        anchor1 = RootAnchor(name="v1", value=1)
        anchor2 = RootAnchor(name="v2", value=2)

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            watcher.add_root("versioned", anchor1)
            tick("info", "tick 1")
            await asyncio.sleep(0.1)

            watcher.add_root("versioned", anchor2, replace=True)
            tick("info", "tick 2")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # Query at tick 1
                result = await graphql_query(
                    client,
                    """
                    query {
                        root(name: "versioned", atTick: 1) {
                            thing {
                                data
                            }
                        }
                    }
                    """,
                )
                assert result["data"]["root"]["thing"]["data"]["name"] == "v1"

                # Query at tick 2
                result = await graphql_query(
                    client,
                    """
                    query {
                        root(name: "versioned", atTick: 2) {
                            thing {
                                data
                            }
                        }
                    }
                    """,
                )
                assert result["data"]["root"]["thing"]["data"]["name"] == "v2"

        await watcher.watch_and_serve_until(run)

    async def test_query_nonexistent_root_returns_null(self):
        """Test querying a nonexistent root returns null."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            tick("info", "tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        root(name: "nonexistent") {
                            name
                        }
                    }
                    """,
                )
                assert result["data"]["root"] is None

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Multiple roots pointing to same thing
# =============================================================================


class TestMultipleRootsSameThing:
    """Tests for multiple roots pointing to the same thing."""

    async def test_multiple_roots_same_thing(self):
        """Test that multiple roots can point to the same thing."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_anchor_capturer(RootAnchor, RootAnchorCapturer())
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        # Both roots will use the same anchor
        anchor = RootAnchor(name="shared", value=50)

        async def run():
            await wait_for_server(port)

            counter = Counter(1)
            track(counter)
            watcher.add_root("alias1", anchor)
            watcher.add_root("alias2", anchor)
            tick("info", "tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            roots {
                                name
                                thing {
                                    shortId
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                roots = result["data"]["latestTick"]["roots"]
                assert len(roots) == 2

                # Both should have the same shortId (same underlying thing)
                root_map = {r["name"]: r for r in roots}
                assert (
                    root_map["alias1"]["thing"]["shortId"] == root_map["alias2"]["thing"]["shortId"]
                )
                assert root_map["alias1"]["thing"]["data"]["name"] == "shared"
                assert root_map["alias2"]["thing"]["data"]["name"] == "shared"

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Root + tracked object same key tests
# =============================================================================


class SharedKeySnapshotter(StatelessSnapshotter):
    """Snapshotter that produces a specific key for testing key collisions."""

    def __init__(self, key: tuple, data: dict):
        self._key = key
        self._data = data

    def snapshot(self, obj: RootAnchor) -> ThingState:
        return ThingState(
            key=self._key,
            thing_type="shared_type",
            thing_data=self._data,
            links=[],
        )


class SharedKeyCapturer:
    """AnchorCapturer that produces a specific key for testing key collisions."""

    def __init__(self, key: tuple, data: dict):
        self._key = key
        self._data = data

    def handle(self, anchor: RootAnchor) -> ThingState:
        return ThingState(
            key=self._key,
            thing_type="shared_type",
            thing_data=self._data,
            links=[],
        )


class TestRootAndTrackedObjectSameKey:
    """Tests for when root anchor and tracked object resolve to the same key."""

    async def test_root_and_tracked_object_identical_state(self):
        """Test that root + tracked object with identical ThingState works fine."""
        port = find_free_port()
        watcher = Watcher(port=port)

        shared_key = ("shared", "key")
        shared_data = {"value": 42}

        # Both snapshotter and capturer produce identical ThingState
        watcher.register_snapshotter(RootAnchor, SharedKeySnapshotter(shared_key, shared_data))
        watcher.register_anchor_capturer(RootAnchor, SharedKeyCapturer(shared_key, shared_data))

        anchor = RootAnchor(name="test")

        async def run():
            await wait_for_server(port)

            # Track the anchor AND add it as a root
            track(anchor)
            watcher.add_root("myRoot", anchor)
            tick("info", "tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                thingType
                                data
                            }
                            roots {
                                name
                                thing {
                                    thingType
                                    data
                                }
                            }
                        }
                    }
                    """,
                )
                tick_data = result["data"]["latestTick"]

                # Should have exactly one thing (unified, not duplicated)
                things = tick_data["things"]
                assert len(things) == 1
                assert things[0]["thingType"] == "shared_type"
                assert things[0]["data"]["value"] == 42

                # Root should resolve to that same thing
                roots = tick_data["roots"]
                assert len(roots) == 1
                assert roots[0]["name"] == "myRoot"
                assert roots[0]["thing"]["thingType"] == "shared_type"
                assert roots[0]["thing"]["data"]["value"] == 42

        await watcher.watch_and_serve_until(run)

    async def test_root_and_tracked_object_conflicting_state(self):
        """Test that root + tracked object with conflicting ThingState creates broken thing."""
        port = find_free_port()
        watcher = Watcher(port=port)

        shared_key = ("shared", "key")
        tracked_data = {"source": "tracked", "value": 1}
        root_data = {"source": "root", "value": 2}

        # Snapshotter and capturer produce different data for same key
        watcher.register_snapshotter(RootAnchor, SharedKeySnapshotter(shared_key, tracked_data))
        watcher.register_anchor_capturer(RootAnchor, SharedKeyCapturer(shared_key, root_data))

        anchor = RootAnchor(name="test")

        async def run():
            await wait_for_server(port)

            # Track the anchor AND add it as a root - they will conflict
            track(anchor)
            watcher.add_root("myRoot", anchor)
            tick("info", "tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                thingType
                                data
                            }
                            roots {
                                name
                                thing {
                                    thingType
                                }
                            }
                        }
                    }
                    """,
                )
                tick_data = result["data"]["latestTick"]

                # Should have a broken thing due to conflict
                things = tick_data["things"]
                assert len(things) == 1
                assert things[0]["thingType"] == "__broken__"

                # Root should resolve to the broken thing
                roots = tick_data["roots"]
                assert len(roots) == 1
                assert roots[0]["thing"]["thingType"] == "__broken__"

        await watcher.watch_and_serve_until(run)
