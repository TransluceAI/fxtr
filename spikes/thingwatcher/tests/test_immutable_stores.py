"""Tests for immutable object stores functionality."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from thingwatcher import (
    BacklinkInfo,
    ThingLink,
    ThingState,
    Watcher,
    register_immutable_store,
    tick,
    track,
    unregister_immutable_store,
)
from thingwatcher.api import NoWatcherConnectedError, StatelessSnapshotter
from thingwatcher.registry import clear_global_registries

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
    """Snapshotter that can produce links to store things."""

    def __init__(self):
        self._links: dict[int, list[ThingLink]] = {}
        self._keys: dict[int, Any] = {}

    def set_links(self, obj: Counter, links: list[ThingLink]) -> None:
        self._links[id(obj)] = links

    def set_key(self, obj: Counter, key: Any) -> None:
        self._keys[id(obj)] = key

    def snapshot(self, obj: Counter) -> ThingState:
        key = self._keys.get(id(obj), ("counter", id(obj)))
        return ThingState(
            key=key,
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=self._links.get(id(obj), []),
        )


class SimpleImmutableStore:
    """A simple in-memory immutable store for testing."""

    def __init__(self, things: dict[Any, ThingState] | None = None):
        self._things: dict[Any, ThingState] = things or {}
        self._backlinks: dict[Any, list[BacklinkInfo]] = {}

    def add_thing(self, state: ThingState) -> None:
        self._things[state.key] = state

    def add_backlink(self, to_key: Any, backlink: BacklinkInfo) -> None:
        if to_key not in self._backlinks:
            self._backlinks[to_key] = []
        self._backlinks[to_key].append(backlink)

    async def get_thing_state(self, key: Any) -> ThingState | None:
        return self._things.get(key)

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:
        return self._backlinks.get(key, [])


@pytest.fixture(autouse=True)
def clean_registries():
    """Clean global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


# =============================================================================
# Registration Tests
# =============================================================================


class TestStoreRegistration:
    """Tests for registering and unregistering immutable stores."""

    @pytest.mark.asyncio
    async def test_register_store(self):
        """Test registering a store with the watcher."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        store = SimpleImmutableStore()

        async def run():
            await wait_for_server(port)
            # Register store
            register_immutable_store("test_store", store)
            assert "test_store" in watcher._immutable_stores

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_unregister_store(self):
        """Test unregistering a store."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        store = SimpleImmutableStore()

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)
            assert "test_store" in watcher._immutable_stores

            unregister_immutable_store("test_store")
            assert "test_store" not in watcher._immutable_stores

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_register_duplicate_store_raises(self):
        """Test that registering a store with duplicate name raises."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        store1 = SimpleImmutableStore()
        store2 = SimpleImmutableStore()

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store1)

            with pytest.raises(ValueError, match="already registered"):
                register_immutable_store("test_store", store2)

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_store_raises(self):
        """Test that unregistering a nonexistent store raises."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)
            with pytest.raises(KeyError):
                unregister_immutable_store("nonexistent")

        await watcher.watch_and_serve_until(run)

    def test_register_without_watcher_raises(self):
        """Test that registering without an active watcher raises."""
        store = SimpleImmutableStore()
        with pytest.raises(NoWatcherConnectedError):
            register_immutable_store("test_store", store)


# =============================================================================
# GraphQL Query Tests
# =============================================================================


class TestStoreLookup:
    """Tests for looking up things from immutable stores via GraphQL."""

    @pytest.mark.asyncio
    async def test_lookup_thing_from_store(self):
        """Test looking up a thing that only exists in a store."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="store_thing_1",
                thing_type="StoreThing",
                thing_data={"name": "Thing from store"},
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            # Create a tick so we have a valid tick to query at
            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            data
                            sources {
                                kind
                                storeName
                                overridden
                            }
                        }
                    }
                    """,
                    {"key": "store_thing_1"},
                )
                assert result["data"]["thing"] is not None
                thing = result["data"]["thing"]
                assert thing["thingType"] == "StoreThing"
                assert thing["data"]["name"] == "Thing from store"
                assert len(thing["sources"]) == 1
                assert thing["sources"][0]["kind"] == "STORE"
                assert thing["sources"][0]["storeName"] == "test_store"
                assert thing["sources"][0]["overridden"] is False

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_lookup_nonexistent_thing_returns_null(self):
        """Test looking up a thing that doesn't exist anywhere returns null."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        store = SimpleImmutableStore()

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                        }
                    }
                    """,
                    {"key": "nonexistent_key"},
                )
                assert result["data"]["thing"] is None

        await watcher.watch_and_serve_until(run)


class TestSnapshotOverridesStore:
    """Tests for snapshot things overriding store things."""

    @pytest.mark.asyncio
    async def test_snapshot_overrides_store_same_state(self):
        """Test when snapshot and store have the same state.

        Even when states match, the store is marked as overridden because
        the snapshot is the authoritative source.
        """
        port = find_free_port()
        watcher = Watcher(port=port)

        # Create a snapshotter that produces a specific key
        class FixedKeySnapshotter(StatelessSnapshotter):
            def snapshot(self, obj: Counter) -> ThingState:
                return ThingState(
                    key="shared_key",
                    thing_type="SharedThing",
                    thing_data={"value": obj.value},
                    links=[],
                )

        watcher.register_snapshotter(Counter, FixedKeySnapshotter())

        # Store has the same thing
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="shared_key",
                thing_type="SharedThing",
                thing_data={"value": 42},
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(42)  # Same value as store
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            data
                            sources {
                                kind
                                storeName
                                overridden
                            }
                        }
                    }
                    """,
                    {"key": "shared_key"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                assert thing["thingType"] == "SharedThing"
                # Should have both sources
                assert len(thing["sources"]) == 2
                # First source should be SNAPSHOT (authoritative)
                assert thing["sources"][0]["kind"] == "SNAPSHOT"
                assert thing["sources"][0]["overridden"] is False
                # Second source should be STORE, always marked overridden
                # (snapshot is authoritative regardless of whether states match)
                assert thing["sources"][1]["kind"] == "STORE"
                assert thing["sources"][1]["storeName"] == "test_store"
                assert thing["sources"][1]["overridden"] is True

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_snapshot_overrides_store_different_state(self):
        """Test when snapshot and store have different states."""
        port = find_free_port()
        watcher = Watcher(port=port)

        class FixedKeySnapshotter(StatelessSnapshotter):
            def snapshot(self, obj: Counter) -> ThingState:
                return ThingState(
                    key="shared_key",
                    thing_type="SharedThing",
                    thing_data={"value": obj.value},
                    links=[],
                )

        watcher.register_snapshotter(Counter, FixedKeySnapshotter())

        # Store has different value
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="shared_key",
                thing_type="SharedThing",
                thing_data={"value": 100},  # Different from snapshot
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(42)  # Different value from store
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            data
                            sources {
                                kind
                                storeName
                                overridden
                            }
                        }
                    }
                    """,
                    {"key": "shared_key"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                # Data should come from snapshot
                assert thing["data"]["value"] == 42
                # Store source should be marked as overridden
                assert len(thing["sources"]) == 2
                assert thing["sources"][0]["kind"] == "SNAPSHOT"
                assert thing["sources"][0]["overridden"] is False
                assert thing["sources"][1]["kind"] == "STORE"
                assert thing["sources"][1]["overridden"] is True

        await watcher.watch_and_serve_until(run)


class TestStoreConflicts:
    """Tests for store-to-store conflicts."""

    @pytest.mark.asyncio
    async def test_multiple_stores_same_state(self):
        """Test when multiple stores have the same thing with identical state."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        # Two stores with identical things
        store1 = SimpleImmutableStore()
        store1.add_thing(
            ThingState(
                key="multi_store_thing",
                thing_type="MultiStoreThing",
                thing_data={"name": "Same in both"},
                links=[],
            )
        )

        store2 = SimpleImmutableStore()
        store2.add_thing(
            ThingState(
                key="multi_store_thing",
                thing_type="MultiStoreThing",
                thing_data={"name": "Same in both"},
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("store1", store1)
            register_immutable_store("store2", store2)

            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            data
                            sources {
                                kind
                                storeName
                                overridden
                            }
                        }
                    }
                    """,
                    {"key": "multi_store_thing"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                assert thing["thingType"] == "MultiStoreThing"
                # Should have sources from both stores
                assert len(thing["sources"]) == 2
                store_names = {s["storeName"] for s in thing["sources"]}
                assert store_names == {"store1", "store2"}
                # Neither should be overridden (identical states)
                assert all(s["overridden"] is False for s in thing["sources"])

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_multiple_stores_conflict(self):
        """Test when multiple stores have conflicting states for the same key."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        # Two stores with different things at the same key
        store1 = SimpleImmutableStore()
        store1.add_thing(
            ThingState(
                key="conflict_thing",
                thing_type="ConflictThing",
                thing_data={"name": "From store1"},
                links=[],
            )
        )

        store2 = SimpleImmutableStore()
        store2.add_thing(
            ThingState(
                key="conflict_thing",
                thing_type="ConflictThing",
                thing_data={"name": "From store2"},  # Different!
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("store1", store1)
            register_immutable_store("store2", store2)

            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                        }
                    }
                    """,
                    {"key": "conflict_thing"},
                )
                thing = result["data"]["thing"]
                # Should return a broken thing due to conflict
                assert thing is not None
                assert thing["thingType"] == "__broken__"

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Links and Backlinks Tests
# =============================================================================


class TestStoreLinks:
    """Tests for links from store things."""

    @pytest.mark.asyncio
    async def test_store_thing_links(self):
        """Test that links from store things are resolved."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        # Store with a thing that has links
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="parent",
                thing_type="Parent",
                thing_data={"name": "Parent thing"},
                links=[
                    ThingLink(link_type="child", link_key="c1", to_thing_key="child1"),
                    ThingLink(link_type="child", link_key="c2", to_thing_key="child2"),
                ],
            )
        )
        store.add_thing(
            ThingState(
                key="child1",
                thing_type="Child",
                thing_data={"name": "Child 1"},
                links=[],
            )
        )
        store.add_thing(
            ThingState(
                key="child2",
                thing_type="Child",
                thing_data={"name": "Child 2"},
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            links {
                                linkType
                                toThing {
                                    thingType
                                    data
                                }
                            }
                        }
                    }
                    """,
                    {"key": "parent"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                assert thing["thingType"] == "Parent"
                assert len(thing["links"]) == 2
                child_names = {link["toThing"]["data"]["name"] for link in thing["links"]}
                assert child_names == {"Child 1", "Child 2"}

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_snapshot_links_to_store_thing(self):
        """Test that snapshot things can link to store things."""
        port = find_free_port()
        watcher = Watcher(port=port)

        links_snapshotter = CounterWithLinksSnapshotter()
        watcher.register_snapshotter(Counter, links_snapshotter)

        # Store with a target thing
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="store_target",
                thing_type="StoreTarget",
                thing_data={"name": "Target in store"},
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(1)
            # Set up link from counter to store thing
            links_snapshotter.set_links(
                counter,
                [
                    ThingLink(link_type="ref", link_key="r1", to_thing_key="store_target"),
                ],
            )
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # First get the counter
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                thingType
                                links {
                                    linkType
                                    toThing {
                                        thingType
                                        data
                                        sources {
                                            kind
                                            storeName
                                        }
                                    }
                                }
                            }
                        }
                    }
                    """,
                )
                things = result["data"]["latestTick"]["things"]
                counter_thing = next(t for t in things if t["thingType"] == "Counter")
                assert len(counter_thing["links"]) == 1
                link = counter_thing["links"][0]
                assert link["linkType"] == "ref"
                assert link["toThing"]["thingType"] == "StoreTarget"
                assert link["toThing"]["data"]["name"] == "Target in store"
                assert link["toThing"]["sources"][0]["kind"] == "STORE"

        await watcher.watch_and_serve_until(run)


class TestStoreBacklinks:
    """Tests for backlinks from store things."""

    @pytest.mark.asyncio
    async def test_store_backlinks(self):
        """Test that backlinks from stores are included."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        # Store with things and backlinks
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="target",
                thing_type="Target",
                thing_data={"name": "Target thing"},
                links=[],
            )
        )
        store.add_thing(
            ThingState(
                key="source",
                thing_type="Source",
                thing_data={"name": "Source thing"},
                links=[ThingLink(link_type="ref", link_key="r1", to_thing_key="target")],
            )
        )
        # Register backlink
        store.add_backlink(
            "target",
            BacklinkInfo(
                from_thing_key="source",
                link_type="ref",
                link_key="r1",
            ),
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            backlinks {
                                linkType
                                fromThing {
                                    thingType
                                    data
                                }
                            }
                        }
                    }
                    """,
                    {"key": "target"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                assert thing["thingType"] == "Target"
                assert len(thing["backlinks"]) == 1
                backlink = thing["backlinks"][0]
                assert backlink["linkType"] == "ref"
                assert backlink["fromThing"]["thingType"] == "Source"
                assert backlink["fromThing"]["data"]["name"] == "Source thing"

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_mixed_backlinks(self):
        """Test backlinks from both snapshot and store things."""
        port = find_free_port()
        watcher = Watcher(port=port)

        links_snapshotter = CounterWithLinksSnapshotter()
        watcher.register_snapshotter(Counter, links_snapshotter)

        # Store with a thing that links to a target
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="target",
                thing_type="Target",
                thing_data={"name": "Target thing"},
                links=[],
            )
        )
        store.add_thing(
            ThingState(
                key="store_source",
                thing_type="StoreSource",
                thing_data={"name": "Source from store"},
                links=[ThingLink(link_type="store_ref", link_key="sr1", to_thing_key="target")],
            )
        )
        store.add_backlink(
            "target",
            BacklinkInfo(
                from_thing_key="store_source",
                link_type="store_ref",
                link_key="sr1",
            ),
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            # Also create a snapshot thing that links to target
            counter = Counter(1)
            links_snapshotter.set_links(
                counter,
                [
                    ThingLink(link_type="snapshot_ref", link_key="snr1", to_thing_key="target"),
                ],
            )
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            backlinks {
                                linkType
                                fromThing {
                                    thingType
                                }
                            }
                        }
                    }
                    """,
                    {"key": "target"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                # Should have backlinks from both snapshot and store
                backlinks = thing["backlinks"]
                assert len(backlinks) == 2
                link_types = {bl["linkType"] for bl in backlinks}
                assert link_types == {"snapshot_ref", "store_ref"}
                from_types = {bl["fromThing"]["thingType"] for bl in backlinks}
                assert from_types == {"Counter", "StoreSource"}

        await watcher.watch_and_serve_until(run)

    @pytest.mark.asyncio
    async def test_backlink_deduplication(self):
        """Test that duplicate backlinks from DB and store are deduplicated.

        When the same backlink exists in both the snapshot DB and a store,
        only one should be returned (the DB version is authoritative).
        """
        port = find_free_port()
        watcher = Watcher(port=port)

        links_snapshotter = CounterWithLinksSnapshotter()
        watcher.register_snapshotter(Counter, links_snapshotter)

        # Store with same backlink that will also exist in snapshot
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="target",
                thing_type="Target",
                thing_data={"name": "Target thing"},
                links=[],
            )
        )
        # Add a backlink from "counter_key" to "target" with same link type/key
        # as what the snapshot will have
        store.add_backlink(
            "target",
            BacklinkInfo(
                from_thing_key="counter_key",
                link_type="ref",
                link_key="r1",
            ),
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            # Create a snapshot thing that links to target with same link type/key
            counter = Counter(1)
            links_snapshotter.set_key(counter, "counter_key")
            links_snapshotter.set_links(
                counter,
                [
                    ThingLink(link_type="ref", link_key="r1", to_thing_key="target"),
                ],
            )
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            backlinks {
                                linkType
                                linkKey
                                fromThing {
                                    key
                                    thingType
                                }
                            }
                        }
                    }
                    """,
                    {"key": "target"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                # Should have only ONE backlink (deduped)
                backlinks = thing["backlinks"]
                assert len(backlinks) == 1
                assert backlinks[0]["linkType"] == "ref"
                # fromThing should be the Counter from snapshot (DB authoritative)
                assert backlinks[0]["fromThing"]["thingType"] == "Counter"

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Sources Field Tests
# =============================================================================


class TestSourcesField:
    """Tests for the sources field on Thing."""

    @pytest.mark.asyncio
    async def test_snapshot_thing_has_snapshot_source(self):
        """Test that snapshot things have SNAPSHOT source."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        async def run():
            await wait_for_server(port)

            counter = Counter(42)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                result = await graphql_query(
                    client,
                    """
                    query {
                        latestTick {
                            things {
                                thingType
                                sources {
                                    kind
                                    storeName
                                    overridden
                                }
                            }
                        }
                    }
                    """,
                )
                things = result["data"]["latestTick"]["things"]
                counter_thing = next(t for t in things if t["thingType"] == "Counter")
                assert len(counter_thing["sources"]) == 1
                source = counter_thing["sources"][0]
                assert source["kind"] == "SNAPSHOT"
                assert source["storeName"] is None
                assert source["overridden"] is False

        await watcher.watch_and_serve_until(run)


# =============================================================================
# Data Chunk Fetching Tests
# =============================================================================


class TestStoreDataChunks:
    """Tests for fetching data chunks from stores."""

    @pytest.mark.asyncio
    async def test_fetch_store_data_chunk_by_id(self):
        """Test that ext: data chunk IDs can be used to fetch store data."""
        port = find_free_port()
        watcher = Watcher(port=port)
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        # Store with a thing
        store = SimpleImmutableStore()
        store.add_thing(
            ThingState(
                key="store_thing",
                thing_type="StoreThing",
                thing_data={"name": "From store", "value": 123},
                links=[],
            )
        )

        async def run():
            await wait_for_server(port)
            register_immutable_store("test_store", store)

            counter = Counter(1)
            track(counter)
            tick("info", "test tick")
            await asyncio.sleep(0.1)

            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # First get the store thing and its dataChunks
                result = await graphql_query(
                    client,
                    """
                    query($key: JSON!) {
                        thing(keyJSON: $key) {
                            thingType
                            dataChunks { id data }
                        }
                    }
                    """,
                    {"key": "store_thing"},
                )
                thing = result["data"]["thing"]
                assert thing is not None
                assert len(thing["dataChunks"]) == 1
                chunk_id = thing["dataChunks"][0]["id"]
                assert chunk_id.startswith("ext:")

                # Now fetch the chunk directly by its ID
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
                    {"id": chunk_id},
                )
                chunk = chunk_result["data"]["dataChunk"]
                assert chunk is not None
                assert chunk["id"] == chunk_id
                assert chunk["data"]["name"] == "From store"
                assert chunk["data"]["value"] == 123

        await watcher.watch_and_serve_until(run)
