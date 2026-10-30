"""Tests for pop_chunks functionality in ThingDelta.

Tests the new push_chunk/pop_chunks interface for ThingDelta:
- No push, no pop (data unchanged)
- Push without pop (append)
- Pop all and push (replace)
- Pop N and push (partial pop)
- Pop N without push (repoint to earlier chunk)
- Error cases: pop all without push, pop >= chain_length without push
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from thingwatcher import POP_ALL, ThingDelta, ThingState, Watcher, tick, track
from thingwatcher.api import StatefulOwningSnapshotter, _PopAll
from thingwatcher.db import Database, data_chunks, thing_states
from thingwatcher.registry import clear_global_registries


class ChunkedThing:
    """A thing that can produce different delta operations."""

    def __init__(self, name: str):
        self.name = name
        self.value = 0


class ChunkedThingSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that can produce different delta operations for testing."""

    def __init__(self):
        self._seen: dict[int, bool] = {}
        # Control what delta to produce
        self.push_data: dict | None = None  # Data to push, or None for NO_PUSHED_CHUNK
        self.pop_count: int | _PopAll = 0  # Number of chunks to pop, or POP_ALL

    def snapshot_owned(self, obj: ChunkedThing) -> ThingState | ThingDelta:
        obj_id = id(obj)
        if obj_id not in self._seen:
            # First snapshot: return full state
            self._seen[obj_id] = True
            return ThingState(
                key=("chunked", obj.name),
                thing_type="ChunkedThing",
                thing_data={"name": obj.name, "value": obj.value},
                links=[],
            )
        else:
            # Subsequent: return delta based on settings
            if self.push_data is not None:
                return ThingDelta(
                    key=("chunked", obj.name),
                    pop_chunks=self.pop_count,
                    push_chunk=self.push_data,
                )
            else:
                return ThingDelta(
                    key=("chunked", obj.name),
                    pop_chunks=self.pop_count,
                    # push_chunk=NO_PUSHED_CHUNK is the default
                )

    def stateless_snapshot(self, obj: ChunkedThing) -> ThingState:
        return ThingState(
            key=("chunked", obj.name),
            thing_type="ChunkedThing",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


async def wait_for_writes():
    """Wait for background writes to complete."""
    await asyncio.sleep(0.1)


class TestPushWithoutPop:
    """Test push without pop (append)."""

    @pytest.mark.asyncio
    async def test_append_creates_extending_chunk(self):
        """Push without pop creates a new chunk that extends the previous."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)

                # First tick: ThingState creates first chunk
                tick("info", "first")

                # Second tick: append new chunk
                snapshotter.push_data = {"appended": True}
                snapshotter.pop_count = 0
                tick("info", "append")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Check database has two chunks in chain
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(data_chunks))
                    chunks = result.fetchall()
                    assert len(chunks) == 2

                    # Find the extending chunk (the one with extends != None)
                    extending_chunks = [c for c in chunks if c.extends is not None]
                    assert len(extending_chunks) == 1
            finally:
                await db.close()


class TestPopAllAndPush:
    """Test POP_ALL + push (replace)."""

    @pytest.mark.asyncio
    async def test_pop_all_creates_fresh_chain(self):
        """POP_ALL + push creates a new chunk with extends=None."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)

                # First tick: ThingState creates first chunk
                tick("info", "first")

                # Second tick: replace (POP_ALL + push)
                snapshotter.push_data = {"replaced": True}
                snapshotter.pop_count = POP_ALL
                tick("info", "replace")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Check database - both chunks should have extends=None
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(data_chunks))
                    chunks = result.fetchall()
                    assert len(chunks) == 2
                    # Both chunks have extends=None (first is initial, second is replaced)
                    assert all(c.extends is None for c in chunks)
            finally:
                await db.close()


class TestPopNAndPush:
    """Test pop N chunks then push."""

    @pytest.mark.asyncio
    async def test_pop_one_and_push(self):
        """Pop 1 chunk and push creates chunk extending grandparent."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)

                # Tick 1: ThingState (chunk A)
                tick("info", "first")

                # Tick 2: Append (chunk B extends A)
                snapshotter.push_data = {"tick": 2}
                snapshotter.pop_count = 0
                tick("info", "second")

                # Tick 3: Append (chunk C extends B)
                snapshotter.push_data = {"tick": 3}
                tick("info", "third")

                # Tick 4: Pop 1 and push (chunk D extends B, skipping C)
                snapshotter.push_data = {"tick": 4, "popped_one": True}
                snapshotter.pop_count = 1
                tick("info", "pop_one")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Verify: D should extend B
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(data_chunks).order_by(data_chunks.c.id))
                    chunks = result.fetchall()

                    # Should have 4 chunks: A, B, C, D
                    assert len(chunks) == 4

                    # D (last) should extend B (second)
                    chunk_d = chunks[3]
                    chunk_b = chunks[1]
                    assert chunk_d.extends == chunk_b.id
            finally:
                await db.close()


class TestPopWithoutPush:
    """Test pop without push (repoint to earlier chunk)."""

    @pytest.mark.asyncio
    async def test_pop_one_without_push(self):
        """Pop 1 without push repoints to earlier chunk."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)

                # Build chain: A <- B <- C
                tick("info", "first")  # A

                snapshotter.push_data = {"tick": 2}
                snapshotter.pop_count = 0
                tick("info", "second")  # B

                snapshotter.push_data = {"tick": 3}
                tick("info", "third")  # C

                # Pop 1 without push: should repoint to B
                snapshotter.push_data = None  # NO_PUSHED_CHUNK
                snapshotter.pop_count = 1
                tick("info", "pop_one")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Verify: current thing_state points to B, not C
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    # Get latest thing_state (valid_to is None)
                    result = await conn.execute(
                        select(thing_states).where(thing_states.c.valid_until.is_(None))
                    )
                    latest_state = result.fetchone()
                    assert latest_state is not None

                    # Get chunk B (second chunk)
                    result = await conn.execute(select(data_chunks).order_by(data_chunks.c.id))
                    chunks = result.fetchall()
                    chunk_b = chunks[1]

                    # Latest state should point to B
                    assert latest_state.data_chunk_id == chunk_b.id
            finally:
                await db.close()


class TestValidationErrors:
    """Test validation error cases."""

    @pytest.mark.asyncio
    async def test_pop_all_without_push_produces_broken(self):
        """POP_ALL without push produces broken thing (tick doesn't raise)."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)
                tick("info", "first")

                # Try POP_ALL without push - produces broken thing, doesn't raise
                snapshotter.push_data = None
                snapshotter.pop_count = POP_ALL
                tick("info", "invalid")  # Should NOT raise

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Verify a broken thing was created
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(
                        select(thing_states).where(thing_states.c.thing_type == "__broken__")
                    )
                    broken = result.fetchall()
                    assert len(broken) >= 1
                    # Error message should mention "cannot pop all chunks"
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_negative_pop_chunks_produces_broken(self):
        """Negative pop_chunks produces broken thing (tick doesn't raise)."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)
                tick("info", "first")

                snapshotter.push_data = {"data": True}
                snapshotter.pop_count = -1
                tick("info", "invalid")  # Should NOT raise

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Verify a broken thing was created
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(
                        select(thing_states).where(thing_states.c.thing_type == "__broken__")
                    )
                    broken = result.fetchall()
                    assert len(broken) >= 1
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_pop_more_than_chain_length_produces_broken(self):
        """Pop more chunks than exist produces broken thing."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)

                # Create chain with 1 chunk
                tick("info", "first")

                # Try to pop 5 chunks from a 1-chunk chain
                snapshotter.push_data = {"data": True}
                snapshotter.pop_count = 5
                tick("info", "too_many_pops")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Should produce broken thing (async validation)
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(
                        select(thing_states).where(thing_states.c.valid_until.is_(None))
                    )
                    latest_state = result.fetchone()
                    assert latest_state is not None
                    assert latest_state.thing_type == "__broken__"
            finally:
                await db.close()


class TestNoPushNoPop:
    """Test no push, no pop (data unchanged)."""

    @pytest.mark.asyncio
    async def test_no_change_reuses_chunk(self):
        """No push, no pop reuses the same chunk."""
        clear_global_registries()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            watcher = Watcher(db_path=db_path, port=None)
            snapshotter = ChunkedThingSnapshotter()
            watcher.register_snapshotter(ChunkedThing, snapshotter)

            async def run():
                thing = ChunkedThing("test")
                track(thing)

                tick("info", "first")

                # Second tick: no change
                snapshotter.push_data = None
                snapshotter.pop_count = 0
                tick("info", "no_change")

                await wait_for_writes()

            await watcher.watch_and_serve_until(run)

            # Should only have 1 data chunk
            db = Database(db_path)
            await db.connect()
            try:
                async with db.begin() as conn:
                    result = await conn.execute(select(func.count()).select_from(data_chunks))
                    count = result.scalar()
                    assert count == 1
            finally:
                await db.close()
