"""Tests for SQLite persistence functionality."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import cbor2
import pytest
from sqlalchemy import select
from thingwatcher import POP_ALL, ThingDelta, ThingLink, ThingState, Watcher, tick, track, untrack
from thingwatcher.api import StatefulOwningSnapshotter, StatelessSnapshotter
from thingwatcher.db import Database, data_chunks, links, thing_states, ticks
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
            key=("counter", id(obj)),
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=[],
        )


class CounterWithLinksSnapshotter(StatelessSnapshotter):
    """Snapshotter that produces links."""

    def __init__(self):
        self._targets: dict[int, list[Any]] = {}

    def set_links(self, obj: Counter, targets: list[Any]) -> None:
        """Set the link targets for an object."""
        self._targets[id(obj)] = targets

    def snapshot(self, obj: Counter) -> ThingState:
        targets = self._targets.get(id(obj), [])
        return ThingState(
            key=("counter", id(obj)),
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=[
                ThingLink(
                    link_type="points_to",
                    link_key=("link", i),
                    to_thing_key=("counter", id(target)),
                )
                for i, target in enumerate(targets)
            ],
        )


class DeltaSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that can return ThingDelta instead of ThingState."""

    def __init__(self):
        self._seen: dict[int, int] = {}  # obj_id -> call count
        self._mode: str = "state"  # "state", "no_diff", "replace", "append"

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def snapshot_owned(self, obj: Counter) -> ThingState | ThingDelta:
        obj_id = id(obj)
        call_count = self._seen.get(obj_id, 0)
        self._seen[obj_id] = call_count + 1

        if call_count == 0 or self._mode == "state":
            # First call or state mode: return full state
            return ThingState(
                key=("counter", obj_id),
                thing_type="Counter",
                thing_data={"value": obj.value},
                links=[],
            )
        else:
            # Subsequent calls: return delta based on mode
            if self._mode == "no_diff":
                return ThingDelta(
                    key=("counter", obj_id),
                    # Default: no push, no pop = no data change
                )
            elif self._mode == "replace":
                return ThingDelta(
                    key=("counter", obj_id),
                    pop_chunks=POP_ALL,
                    push_chunk={"value": obj.value, "replaced": True},
                )
            elif self._mode == "append":
                return ThingDelta(
                    key=("counter", obj_id),
                    push_chunk={"appended_value": obj.value},
                )
            else:
                raise ValueError(f"Unknown mode: {self._mode}")

    def stateless_snapshot(self, obj: Counter) -> ThingState:
        """Return full state for hard recapture."""
        obj_id = id(obj)
        return ThingState(
            key=("counter", obj_id),
            thing_type="Counter",
            thing_data={"value": obj.value},
            links=[],
        )


@pytest.fixture(autouse=True)
def clean_registries():
    """Clean global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


async def wait_for_writes():
    """Wait for background writes to complete."""
    # Give the background writer time to process
    await asyncio.sleep(0.1)


# =============================================================================
# Tests
# =============================================================================


class TestBasicPersistence:
    """Tests for basic tick and thing state persistence."""

    @pytest.mark.asyncio
    async def test_tick_persisted_with_file_db(self):
        """Test tick persistence with a file-based database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                counter = Counter(42)
                track(counter)
                tick("info", "test tick")
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Now read from the DB file directly
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(ticks))
                    rows = result.fetchall()
                    assert len(rows) == 1
                    row = rows[0]
                    assert row.tick == 1
                    assert row.level == "info"
                    assert row.type == "api_tick"
                    assert cbor2.loads(row.data) == "test tick"
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_thing_state_persisted(self):
        """Test that thing states are persisted to the database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                counter = Counter(42)
                track(counter)
                tick("info", "test")
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(thing_states))
                    rows = result.fetchall()
                    assert len(rows) == 1
                    row = rows[0]
                    assert row.valid_from == 1
                    assert row.valid_until is None  # Still valid
                    assert row.thing_type == "Counter"
                    # Get data from data_chunks table
                    chunk_result = await conn.execute(
                        select(data_chunks).where(data_chunks.c.id == row.data_chunk_id)
                    )
                    chunk = chunk_result.fetchone()
                    assert cbor2.loads(chunk.data) == {"value": 42}
            finally:
                await db.close()


class TestChangeDetection:
    """Tests for change detection - unchanged data should not create new rows."""

    @pytest.mark.asyncio
    async def test_unchanged_data_no_new_row(self):
        """Test that unchanged data doesn't create a new thing_states row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                counter = Counter(42)
                track(counter)
                tick("info", "tick 1")
                tick("info", "tick 2")  # Same data
                tick("info", "tick 3")  # Same data
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # Should have 3 ticks
                    result = await conn.execute(select(ticks))
                    tick_rows = result.fetchall()
                    assert len(tick_rows) == 3

                    # But only 1 thing_state (unchanged)
                    result = await conn.execute(select(thing_states))
                    state_rows = result.fetchall()
                    assert len(state_rows) == 1
                    assert state_rows[0].valid_until is None  # Still valid
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_changed_data_creates_new_row(self):
        """Test that changed data creates a new thing_states row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                counter = Counter(0)
                track(counter)
                tick("info", "tick 1")  # value=0
                counter.increment()
                tick("info", "tick 2")  # value=1
                counter.increment()
                tick("info", "tick 3")  # value=2
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()
                    assert len(rows) == 3

                    # Helper to get data from data_chunk
                    async def get_chunk_data(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        chunk = chunk_result.fetchone()
                        return cbor2.loads(chunk.data)

                    # First two should be invalidated
                    assert rows[0].valid_from == 1
                    assert rows[0].valid_until == 2
                    assert await get_chunk_data(rows[0].data_chunk_id) == {"value": 0}

                    assert rows[1].valid_from == 2
                    assert rows[1].valid_until == 3
                    assert await get_chunk_data(rows[1].data_chunk_id) == {"value": 1}

                    # Last one should still be valid
                    assert rows[2].valid_from == 3
                    assert rows[2].valid_until is None
                    assert await get_chunk_data(rows[2].data_chunk_id) == {"value": 2}
            finally:
                await db.close()


class TestLinkPersistence:
    """Tests for link persistence."""

    @pytest.mark.asyncio
    async def test_links_persisted(self):
        """Test that links are persisted to the database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = CounterWithLinksSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                track(c1)
                track(c2)
                snapshotter.set_links(c1, [c2])
                tick("info", "test")
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(links))
                    rows = result.fetchall()
                    assert len(rows) == 1
                    row = rows[0]
                    assert row.link_type == "points_to"
                    assert row.valid_from == 1
                    assert row.valid_until is None
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_link_change_without_data_change(self):
        """Test that link changes are tracked even when data doesn't change."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = CounterWithLinksSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                c3 = Counter(3)
                track(c1)
                track(c2)
                track(c3)

                # First tick: c1 -> c2
                snapshotter.set_links(c1, [c2])
                tick("info", "tick 1")

                # Second tick: c1 -> c3 (link changed, but c1's data same)
                snapshotter.set_links(c1, [c3])
                tick("info", "tick 2")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # Check thing_states - c1 should have 2 rows (links changed)
                    # This tests the thingstateId invariant: a new thing_state is created
                    # when links change, even if data doesn't change
                    result = await conn.execute(
                        select(thing_states).where(thing_states.c.thing_type == "Counter")
                    )
                    state_rows = result.fetchall()
                    # c1: 2 states (link changed), c2: 1 state, c3: 1 state = 4 total
                    assert len(state_rows) == 4

                    # Check links - should have 2 (old invalidated, new created)
                    result = await conn.execute(select(links).order_by(links.c.id))
                    link_rows = result.fetchall()
                    assert len(link_rows) == 2

                    # First link should be invalidated
                    assert link_rows[0].valid_from == 1
                    assert link_rows[0].valid_until == 2

                    # Second link should be valid
                    assert link_rows[1].valid_from == 2
                    assert link_rows[1].valid_until is None
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_links_unchanged(self):
        """Test that unchanged links don't create new rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = CounterWithLinksSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                track(c1)
                track(c2)
                snapshotter.set_links(c1, [c2])

                tick("info", "tick 1")
                tick("info", "tick 2")  # Same links
                tick("info", "tick 3")  # Same links

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(links))
                    rows = result.fetchall()
                    # Only 1 link row (unchanged across ticks)
                    assert len(rows) == 1
                    assert rows[0].valid_until is None
            finally:
                await db.close()


class TestDisappearance:
    """Tests for thing disappearance detection."""

    @pytest.mark.asyncio
    async def test_untracked_thing_invalidated(self):
        """Test that untracked things are invalidated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                track(c1)
                track(c2)
                tick("info", "both tracked")

                untrack(c2)
                tick("info", "c2 untracked")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()
                    assert len(rows) == 2

                    # Helper to get data from data_chunk
                    async def get_chunk_data(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        chunk = chunk_result.fetchone()
                        return cbor2.loads(chunk.data)

                    # Find rows by their data values
                    c1_row = None
                    c2_row = None
                    for r in rows:
                        data = await get_chunk_data(r.data_chunk_id)
                        if data["value"] == 1:
                            c1_row = r
                        elif data["value"] == 2:
                            c2_row = r

                    # c1 should still be valid
                    assert c1_row is not None
                    assert c1_row.valid_until is None

                    # c2 should be invalidated at tick 2
                    assert c2_row is not None
                    assert c2_row.valid_until == 2
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_disappeared_thing_links_invalidated(self):
        """Test that links from disappeared things are also invalidated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = CounterWithLinksSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                track(c1)
                track(c2)
                snapshotter.set_links(c1, [c2])
                tick("info", "both tracked with link")

                untrack(c1)
                tick("info", "c1 untracked")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(links))
                    rows = result.fetchall()
                    assert len(rows) == 1
                    # Link should be invalidated when c1 disappeared
                    assert rows[0].valid_until == 2
            finally:
                await db.close()


class TestThingDelta:
    """Tests for ThingDelta handling."""

    @pytest.mark.asyncio
    async def test_delta_no_diff(self):
        """Test that no_diff delta doesn't create new thing_states row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = DeltaSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                counter = Counter(42)
                track(counter)

                # First tick: full state
                tick("info", "tick 1")

                # Subsequent ticks: no_diff
                snapshotter.set_mode("no_diff")
                tick("info", "tick 2")
                tick("info", "tick 3")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(thing_states))
                    rows = result.fetchall()
                    # Only 1 row (no_diff doesn't create new rows)
                    assert len(rows) == 1
                    assert rows[0].valid_until is None
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_delta_replace(self):
        """Test that replace delta creates new thing_states row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = DeltaSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                counter = Counter(42)
                track(counter)

                # First tick: full state
                tick("info", "tick 1")

                # Second tick: replace
                snapshotter.set_mode("replace")
                tick("info", "tick 2")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()
                    assert len(rows) == 2

                    # Helper to get chunk and data
                    async def get_chunk(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        return chunk_result.fetchone()

                    # First row should be invalidated
                    assert rows[0].valid_until == 2
                    chunk0 = await get_chunk(rows[0].data_chunk_id)
                    assert cbor2.loads(chunk0.data) == {"value": 42}

                    # Second row should have replaced data (new chain, no extends)
                    assert rows[1].valid_until is None
                    chunk1 = await get_chunk(rows[1].data_chunk_id)
                    assert cbor2.loads(chunk1.data) == {"value": 42, "replaced": True}
                    assert chunk1.extends is None
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_delta_append(self):
        """Test that append delta creates chain via data_chunks.extends."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = DeltaSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                counter = Counter(42)
                track(counter)

                # First tick: full state
                tick("info", "tick 1")

                # Subsequent ticks: append
                snapshotter.set_mode("append")
                counter.increment()
                tick("info", "tick 2")
                counter.increment()
                tick("info", "tick 3")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()
                    assert len(rows) == 3

                    # Helper to get chunk
                    async def get_chunk(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        return chunk_result.fetchone()

                    # Get all chunks
                    chunk0 = await get_chunk(rows[0].data_chunk_id)
                    chunk1 = await get_chunk(rows[1].data_chunk_id)
                    chunk2 = await get_chunk(rows[2].data_chunk_id)

                    # First chunk: base state (no extends)
                    assert chunk0.extends is None
                    assert rows[0].valid_until == 2

                    # Second chunk: extends first chunk
                    assert chunk1.extends == chunk0.id
                    assert rows[1].valid_until == 3
                    assert cbor2.loads(chunk1.data) == {"appended_value": 43}

                    # Third chunk: extends second chunk
                    assert chunk2.extends == chunk1.id
                    assert rows[2].valid_until is None
                    assert cbor2.loads(chunk2.data) == {"appended_value": 44}
            finally:
                await db.close()


class TestInMemoryDatabase:
    """Tests for in-memory database mode."""

    @pytest.mark.asyncio
    async def test_in_memory_works(self):
        """Test that in-memory database mode works without errors."""
        watcher = Watcher(port=None)  # No db_path = in-memory
        watcher.register_snapshotter(Counter, CounterSnapshotter())

        captured_ticks = []
        watcher.set_tick_interceptor(captured_ticks.append)

        async def run():
            counter = Counter(42)
            track(counter)
            tick("info", "tick 1")
            counter.increment()
            tick("info", "tick 2")
            await wait_for_writes()

        await watcher.watch_and_serve_until(run)

        # Should have captured 2 ticks
        assert len(captured_ticks) == 2


class TestAtomicity:
    """Tests for transaction atomicity."""

    @pytest.mark.asyncio
    async def test_multiple_things_in_single_tick_atomic(self):
        """Test that all writes for a tick happen atomically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                counters = [Counter(i) for i in range(10)]
                for c in counters:
                    track(c)
                tick("info", "all 10 counters")
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # All 10 should be there
                    result = await conn.execute(select(thing_states))
                    rows = result.fetchall()
                    assert len(rows) == 10

                    # All should have the same valid_from (same tick)
                    valid_froms = {r.valid_from for r in rows}
                    assert valid_froms == {1}
            finally:
                await db.close()


class TestDataChunkBehavior:
    """Tests for data chunk sharing and creation behavior."""

    @pytest.mark.asyncio
    async def test_thingstate_same_data_reuses_chunk(self):
        """Test that ThingState with same data reuses data_chunk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            watcher.register_snapshotter(Counter, CounterSnapshotter())

            async def run():
                counter = Counter(42)  # Same value throughout
                track(counter)
                tick("info", "tick 1")
                tick("info", "tick 2")  # Same data
                tick("info", "tick 3")  # Same data
                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # Should have only 1 thing_state (data unchanged)
                    result = await conn.execute(select(thing_states))
                    rows = result.fetchall()
                    assert len(rows) == 1

                    # Should have only 1 data_chunk
                    chunk_result = await conn.execute(select(data_chunks))
                    chunks = chunk_result.fetchall()
                    assert len(chunks) == 1
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_link_only_change_reuses_data_chunk(self):
        """Test that link-only changes reuse the data_chunk but create new thingstate_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = CounterWithLinksSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                c3 = Counter(3)
                track(c1)
                track(c2)
                track(c3)

                # Tick 1: c1 -> c2
                snapshotter.set_links(c1, [c2])
                tick("info", "tick 1")

                # Tick 2: c1 -> c3 (link changed, data same)
                snapshotter.set_links(c1, [c3])
                tick("info", "tick 2")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # c1 should have 2 thing_states (link changed)
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()
                    # c1: 2 states, c2: 1 state, c3: 1 state = 4 total
                    assert len(rows) == 4

                    # Find c1's states (they should have the same data_chunk_id)
                    c1_states = [r for r in rows if r.valid_from == 1 or r.valid_until == 2]

                    # Filter to only c1 by checking they share a data_chunk that has value=1
                    async def get_chunk_data(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        chunk = chunk_result.fetchone()
                        return cbor2.loads(chunk.data)

                    c1_data_chunk_ids = []
                    for r in rows:
                        data = await get_chunk_data(r.data_chunk_id)
                        if data.get("value") == 1:
                            c1_data_chunk_ids.append(r.data_chunk_id)

                    # c1's two thing_states should share the same data_chunk_id
                    assert len(c1_data_chunk_ids) == 2
                    assert c1_data_chunk_ids[0] == c1_data_chunk_ids[1]
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_thingstate_after_thingdelta_creates_new_chunk(self):
        """Test that ThingState after ThingDelta always creates a new data_chunk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = DeltaSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                counter = Counter(42)
                track(counter)

                # Tick 1: ThingState
                tick("info", "tick 1")

                # Tick 2: ThingDelta (append)
                snapshotter.set_mode("append")
                counter.increment()
                tick("info", "tick 2")

                # Tick 3: Back to ThingState (should create new chunk even if data same)
                snapshotter.set_mode("state")
                tick("info", "tick 3")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # Should have 3 thing_states
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()
                    assert len(rows) == 3

                    # Each should have a different data_chunk_id
                    chunk_ids = [r.data_chunk_id for r in rows]
                    assert len(set(chunk_ids)) == 3  # All different

                    # Verify the chunks
                    async def get_chunk(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        return chunk_result.fetchone()

                    chunk1 = await get_chunk(rows[0].data_chunk_id)
                    chunk2 = await get_chunk(rows[1].data_chunk_id)
                    chunk3 = await get_chunk(rows[2].data_chunk_id)

                    # First chunk: no extends
                    assert chunk1.extends is None

                    # Second chunk (append): extends first
                    assert chunk2.extends == chunk1.id

                    # Third chunk (ThingState after ThingDelta): NEW chain, no extends
                    assert chunk3.extends is None
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_thingdelta_no_diff_with_link_changes_new_thingstate(self):
        """Test that ThingDelta no_diff with link changes creates new thingstate_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)

            # Custom snapshotter that returns ThingDelta with link changes
            class DeltaWithLinksSnapshotter(StatefulOwningSnapshotter):
                def __init__(self):
                    self._seen: dict[int, int] = {}  # obj_id -> call count
                    self._targets: dict[int, list[Counter]] = {}  # obj_id -> targets

                def set_link_targets(self, obj: Counter, targets: list[Counter]) -> None:
                    self._targets[id(obj)] = targets

                def snapshot_owned(self, obj: Counter) -> ThingState | ThingDelta:
                    obj_id = id(obj)
                    call_count = self._seen.get(obj_id, 0)
                    self._seen[obj_id] = call_count + 1

                    targets = self._targets.get(obj_id, [])
                    links = [
                        ThingLink(
                            link_key=("link", i),
                            link_type="points_to",
                            to_thing_key=("counter", id(t)),
                        )
                        for i, t in enumerate(targets)
                    ]

                    if call_count == 0:
                        # First call for this object: full state
                        return ThingState(
                            key=("counter", obj_id),
                            thing_type="Counter",
                            thing_data={"value": obj.value},
                            links=links,
                        )
                    else:
                        # Subsequent: delta with no data diff but link changes
                        # Need to compute added/removed links
                        return ThingDelta(
                            key=("counter", obj_id),
                            # Default: pop_chunks=0, push_chunk=NO_PUSHED_CHUNK = no data change
                            added_links=links,  # New links
                            removed_link_keys=[("link", 0)],  # Remove old link
                        )

                def stateless_snapshot(self, obj: Counter) -> ThingState:
                    """Return full state for hard recapture."""
                    obj_id = id(obj)
                    targets = self._targets.get(obj_id, [])
                    links = [
                        ThingLink(
                            link_key=("link", i),
                            link_type="points_to",
                            to_thing_key=("counter", id(t)),
                        )
                        for i, t in enumerate(targets)
                    ]
                    return ThingState(
                        key=("counter", obj_id),
                        thing_type="Counter",
                        thing_data={"value": obj.value},
                        links=links,
                    )

            snapshotter = DeltaWithLinksSnapshotter()
            watcher.register_snapshotter(Counter, snapshotter)

            async def run():
                c1 = Counter(1)
                c2 = Counter(2)
                c3 = Counter(3)
                track(c1)
                track(c2)
                track(c3)

                # Tick 1: c1 -> c2
                snapshotter.set_link_targets(c1, [c2])
                tick("info", "tick 1")

                # Tick 2: c1 -> c3 (link changed via ThingDelta no_diff)
                snapshotter.set_link_targets(c1, [c3])
                tick("info", "tick 2")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # Get c1's thing_states
                    result = await conn.execute(select(thing_states).order_by(thing_states.c.id))
                    rows = result.fetchall()

                    # Find c1's states by data value
                    async def get_chunk_data(chunk_id):
                        chunk_result = await conn.execute(
                            select(data_chunks).where(data_chunks.c.id == chunk_id)
                        )
                        chunk = chunk_result.fetchone()
                        return cbor2.loads(chunk.data)

                    c1_states = []
                    for r in rows:
                        data = await get_chunk_data(r.data_chunk_id)
                        if data.get("value") == 1:
                            c1_states.append(r)

                    # c1 should have 2 thing_states (link changed)
                    assert len(c1_states) == 2

                    # But they should share the same data_chunk_id (no_diff)
                    assert c1_states[0].data_chunk_id == c1_states[1].data_chunk_id

                    # First should be invalidated, second should be valid
                    assert c1_states[0].valid_until == 2
                    assert c1_states[1].valid_until is None
            finally:
                await db.close()
