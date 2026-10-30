# Step 2: SQLite Persistence

Implementation spec for persisting ticks and thing states to SQLite.

## Overview

Add SQLite persistence so that ticks, thing states, and links are durably stored. Uses SQLAlchemy Core with async engine (aiosqlite driver) for database operations and anyio for the background writer task.

## Design Decisions

- **Tick schema**: Ticks have `type` and `data` fields. API ticks use `type="api_tick"` and `data=desc`.
- **Background writer**: Asyncio task using anyio memory streams. `tick()` enqueues work, writer consumes.
- **In-memory DB**: Use `file:memdb_{uuid}?mode=memory&cache=shared` URI for shared in-memory SQLite.
- **Delta storage**: Store deltas with `data_extends` pointer to previous state. Reading requires following chain.
- **SQLAlchemy Core**: Use SQLAlchemy Core (not ORM) with async engine via `aiosqlite` driver for cleaner SQL and connection handling.
- **Atomic ticks**: All writes for a single tick happen in one database transaction, so clients see consistent state.
- **No CBOR helpers**: Use `cbor2.dumps(key, canonical=True)` and `cbor2.loads(data)` directly.
- **Change detection**: Compare snapshots to previous state. Identical data = no new thing_state row. Links can change independently of data.
- **Disappearance detection**: Things not seen in a tick are invalidated. This includes objects that were garbage collected (WeakValueDictionary) or explicitly untracked.
- **Snapshotter list output**: Snapshotters can return a list of ThingState/ThingDelta. Each item is processed independently, allowing one Python object to map to multiple logical things.
- **Link from_thing_key in DB**: The `from_thing_key` column in the links table is derived from the parent thing's key (from `ThingState.key`). Users don't specify it in `ThingLink` - it's implicit.

---

## Schema

```sql
-- Ticks table
CREATE TABLE ticks (
    tick INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,  -- tick level for filtering (e.g. "debug", "info")
    type TEXT NOT NULL,   -- tick type (e.g. "api_tick", "pause_tick")
    data BLOB  -- CBOR encoded, nullable
);

-- Thing states table
CREATE TABLE thing_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    valid_from INTEGER NOT NULL,
    valid_until INTEGER,  -- NULL means still valid
    thing_key BLOB NOT NULL,  -- CBOR canonical
    thing_type TEXT NOT NULL,
    data BLOB,  -- CBOR encoded
    data_extends INTEGER,  -- FK to thing_states.id for append deltas
    triggerable INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (valid_from) REFERENCES ticks(tick),
    FOREIGN KEY (valid_until) REFERENCES ticks(tick),
    FOREIGN KEY (data_extends) REFERENCES thing_states(id)
);

-- Links table
CREATE TABLE links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_thing_key BLOB NOT NULL,  -- CBOR canonical
    valid_from INTEGER NOT NULL,
    valid_until INTEGER,  -- NULL means still valid
    link_type TEXT NOT NULL,
    link_key BLOB NOT NULL,  -- CBOR canonical
    to_thing_key BLOB NOT NULL,  -- CBOR canonical
    FOREIGN KEY (valid_from) REFERENCES ticks(tick),
    FOREIGN KEY (valid_until) REFERENCES ticks(tick)
);

-- Indexes for common queries
CREATE INDEX idx_thing_states_key ON thing_states(thing_key);
CREATE INDEX idx_thing_states_valid ON thing_states(valid_from, valid_until);
CREATE INDEX idx_links_from_key ON links(from_thing_key);
CREATE INDEX idx_links_valid ON links(valid_from, valid_until);
```

---

## File: `thingwatcher/db.py`

### Dependencies
- `sqlalchemy[aiosqlite]` for async SQLite with SQLAlchemy Core
- `cbor2` for CBOR serialization
- `anyio` for async primitives

### Table Definitions (SQLAlchemy Core)
```python
import cbor2
from sqlalchemy import (
    MetaData, Table, Column, Integer, Float, Text, LargeBinary,
    ForeignKey, Index, create_engine
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection

metadata = MetaData()

ticks = Table(
    "ticks", metadata,
    Column("tick", Integer, primary_key=True),
    Column("timestamp", Float, nullable=False),
    Column("level", Text, nullable=False),  # tick level for filtering
    Column("type", Text, nullable=False),   # tick type (e.g. "api_tick")
    Column("data", LargeBinary),  # CBOR encoded, nullable
)

thing_states = Table(
    "thing_states", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("valid_from", Integer, ForeignKey("ticks.tick"), nullable=False),
    Column("valid_until", Integer, ForeignKey("ticks.tick")),  # NULL = still valid
    Column("thing_key", LargeBinary, nullable=False),  # CBOR canonical
    Column("thing_type", Text, nullable=False),
    Column("data", LargeBinary),  # CBOR encoded
    Column("data_extends", Integer, ForeignKey("thing_states.id")),  # for append deltas
    Column("triggerable", Integer, nullable=False, default=0),
    Index("idx_thing_states_key", "thing_key"),
    Index("idx_thing_states_valid", "valid_from", "valid_until"),
)

links = Table(
    "links", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("from_thing_key", LargeBinary, nullable=False),  # CBOR canonical
    Column("valid_from", Integer, ForeignKey("ticks.tick"), nullable=False),
    Column("valid_until", Integer, ForeignKey("ticks.tick")),  # NULL = still valid
    Column("link_type", Text, nullable=False),
    Column("link_key", LargeBinary, nullable=False),  # CBOR canonical
    Column("to_thing_key", LargeBinary, nullable=False),  # CBOR canonical
    Index("idx_links_from_key", "from_thing_key"),
    Index("idx_links_valid", "valid_from", "valid_until"),
)
```

### Database Class
```python
class Database:
    def __init__(self, db_path: str | None):
        """
        Args:
            db_path: Path to SQLite file, or None for in-memory.
        """
        self._db_path = db_path
        self._engine: AsyncEngine | None = None

    async def connect(self) -> None:
        """Open database connection and create schema if needed."""
        if self._db_path is None:
            # In-memory with shared cache
            uri = f"sqlite+aiosqlite:///file:memdb_{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
        else:
            uri = f"sqlite+aiosqlite:///{self._db_path}"

        self._engine = create_async_engine(uri)

        # Create tables
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def close(self) -> None:
        """Close database connection."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    def begin(self) -> AsyncConnection:
        """Return an async connection context manager for a transaction.

        Usage:
            async with db.begin() as conn:
                # all operations here are in one transaction
                await conn.execute(...)
        """
        return self._engine.begin()
```

### Transaction-Based Write Methods
All write operations take a connection parameter and execute within the caller's transaction:

```python
async def write_tick(
    self, conn: AsyncConnection,
    tick: int, timestamp: float, level: str, type: str, data: Any
) -> None:
    """Write a tick record within a transaction."""
    data_blob = cbor2.dumps(data) if data is not None else None
    await conn.execute(
        ticks.insert().values(
            tick=tick, timestamp=timestamp, level=level, type=type, data=data_blob
        )
    )

async def write_thing_state(
    self, conn: AsyncConnection,
    valid_from: int,
    thing_key: Any,
    thing_type: str,
    data: Any,
    data_extends: int | None = None,
    triggerable: bool = False,
) -> int:
    """Write a thing state and return its id."""
    result = await conn.execute(
        thing_states.insert().values(
            valid_from=valid_from,
            thing_key=cbor2.dumps(thing_key, canonical=True),
            thing_type=thing_type,
            data=cbor2.dumps(data),
            data_extends=data_extends,
            triggerable=int(triggerable),
        )
    )
    return result.lastrowid

async def invalidate_thing_state(
    self, conn: AsyncConnection, state_id: int, valid_until: int
) -> None:
    """Set valid_until on a thing state."""
    await conn.execute(
        thing_states.update()
        .where(thing_states.c.id == state_id)
        .values(valid_until=valid_until)
    )

async def write_link(
    self, conn: AsyncConnection,
    valid_from: int,
    from_thing_key: Any,
    link_type: str,
    link_key: Any,
    to_thing_key: Any,
) -> int:
    """Write a link and return its id."""
    result = await conn.execute(
        links.insert().values(
            from_thing_key=cbor2.dumps(from_thing_key, canonical=True),
            valid_from=valid_from,
            link_type=link_type,
            link_key=cbor2.dumps(link_key, canonical=True),
            to_thing_key=cbor2.dumps(to_thing_key, canonical=True),
        )
    )
    return result.lastrowid

async def invalidate_link(
    self, conn: AsyncConnection, link_id: int, valid_until: int
) -> None:
    """Set valid_until on a link."""
    await conn.execute(
        links.update()
        .where(links.c.id == link_id)
        .values(valid_until=valid_until)
    )

async def find_link_id(
    self, conn: AsyncConnection,
    from_thing_key: Any,
    link_key: Any,
) -> int | None:
    """Find a currently-valid link by from_key and link_key."""
    result = await conn.execute(
        links.select()
        .where(links.c.from_thing_key == cbor2.dumps(from_thing_key, canonical=True))
        .where(links.c.link_key == cbor2.dumps(link_key, canonical=True))
        .where(links.c.valid_until.is_(None))
    )
    row = result.fetchone()
    return row.id if row else None
```

**[Implementation note]**: `find_link_id` was not implemented because the in-memory `CurrentLinkInfo` tracking makes it unnecessary - we already have link IDs cached, so no DB lookup is needed.

---

## File: `thingwatcher/watcher.py` Changes

### New Imports
```python
import cbor2
import anyio
from dataclasses import dataclass
from anyio.streams.memory import MemoryObjectSendStream, MemoryObjectReceiveStream
from sqlalchemy.ext.asyncio import AsyncConnection
from .db import Database
```

### In-Memory State Tracking

We need to store enough info to:
1. Compare new snapshots to previous state (detect no-change)
2. Track which things we saw (detect disappearances)
3. Diff links (detect link additions/removals)

```python
@dataclass
class CurrentLinkInfo:
    """Info about a currently-valid link."""
    link_id: int
    link_type: str
    to_thing_key: bytes  # CBOR canonical

@dataclass
class CurrentThingInfo:
    """In-memory tracking for a currently-valid thing."""
    thingstate_id: int
    thing_type: str
    data_bytes: bytes  # CBOR-encoded data for comparison
    # Maps link_key (CBOR bytes) -> CurrentLinkInfo
    links: dict[bytes, CurrentLinkInfo]
```

### New State in `__init__`
```python
# DB state (initialized in watch_and_serve_until)
self._db: Database | None = None
self._write_stream: MemoryObjectSendStream | None = None

# In-memory tracking: thing_key (CBOR bytes) -> CurrentThingInfo
self._current_things: dict[bytes, CurrentThingInfo] = {}
```

### WriteJob Dataclass
```python
@dataclass
class WriteJob:
    """A batch of writes for one tick."""
    tick_number: int
    timestamp: float
    level: str      # tick level for filtering (passed from tick() call)
    tick_type: str  # e.g. "api_tick"
    tick_data: Any  # e.g. desc string for api_tick
    states: list[ThingState | ThingDelta]
```

### Modified `tick()` Method
After collecting snapshots (still under lock), enqueue the write job:
```python
# After creating snapshot_result, before releasing lock:
if self._write_stream is not None:
    job = WriteJob(
        tick_number=tick_num,
        timestamp=timestamp,
        level=level,  # from tick(level, desc) call
        tick_type="api_tick",
        tick_data=desc,
        states=states,
    )
    # Send without blocking (anyio memory stream)
    self._write_stream.send_nowait(job)
```

### Background Writer Task

The processing flow is:
1. Write tick record
2. Process all snapshots, tracking which keys we saw
3. For each snapshot: compare to previous, write only changes
4. Invalidate things that disappeared (weren't seen this tick)

```python
async def _db_writer(self, receive_stream: MemoryObjectReceiveStream[WriteJob]) -> None:
    """Background task that writes to the database."""
    async for job in receive_stream:
        await self._process_write_job(job)

async def _process_write_job(self, job: WriteJob) -> None:
    """Process a single write job atomically in one transaction.

    Flow:
    1. Write tick record
    2. Process each snapshot, comparing to previous state
    3. Invalidate things that disappeared
    """
    tick = job.tick_number
    seen_keys: set[bytes] = set()

    async with self._db.begin() as conn:
        # 1. Write tick record
        await self._db.write_tick(
            conn, tick, job.timestamp, job.level, job.tick_type, job.tick_data
        )

        # 2. Process each snapshot
        for item in job.states:
            if isinstance(item, ThingState):
                key_bytes = await self._process_thing_state(conn, tick, item)
            else:  # ThingDelta
                key_bytes = await self._process_thing_delta(conn, tick, item)
            seen_keys.add(key_bytes)

        # 3. Invalidate things that disappeared
        for key_bytes in list(self._current_things.keys()):
            if key_bytes not in seen_keys:
                await self._invalidate_thing(conn, tick, key_bytes)

        # Transaction commits automatically
```

### Processing ThingState (with change detection)

```python
async def _process_thing_state(
    self, conn: AsyncConnection, tick: int, state: ThingState
) -> bytes:
    """Process a ThingState snapshot, comparing to previous.

    Returns the thing_key as CBOR bytes.
    """
    key_bytes = cbor2.dumps(state.key, canonical=True)
    new_data_bytes = cbor2.dumps(state.thing_data)
    prev = self._current_things.get(key_bytes)

    # Build new links map for comparison
    new_links: dict[bytes, ThingLink] = {}
    for link in state.links:
        link_key_bytes = cbor2.dumps(link.link_key, canonical=True)
        new_links[link_key_bytes] = link

    if prev is None:
        # New thing - write state and all links
        new_id = await self._db.write_thing_state(
            conn,
            valid_from=tick,
            thing_key=state.key,
            thing_type=state.thing_type,
            data=state.thing_data,
        )
        new_link_infos = await self._write_links(conn, tick, state.key, new_links)
        self._current_things[key_bytes] = CurrentThingInfo(
            thingstate_id=new_id,
            thing_type=state.thing_type,
            data_bytes=new_data_bytes,
            links=new_link_infos,
        )
    else:
        # Existing thing - check for changes
        data_changed = (new_data_bytes != prev.data_bytes)

        if data_changed:
            # Invalidate old state, write new state
            await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick)
            new_id = await self._db.write_thing_state(
                conn,
                valid_from=tick,
                thing_key=state.key,
                thing_type=state.thing_type,
                data=state.thing_data,
            )
        else:
            # Data unchanged, keep existing state
            new_id = prev.thingstate_id

        # Diff and update links (regardless of data change)
        new_link_infos = await self._diff_links(
            conn, tick, state.key, prev.links, new_links
        )

        self._current_things[key_bytes] = CurrentThingInfo(
            thingstate_id=new_id,
            thing_type=state.thing_type,
            data_bytes=new_data_bytes,
            links=new_link_infos,
        )

    return key_bytes
```

### Processing ThingDelta (trusting the diff)

```python
async def _process_thing_delta(
    self, conn: AsyncConnection, tick: int, delta: ThingDelta
) -> bytes:
    """Process a ThingDelta, trusting the snapshotter's diff.

    Returns the thing_key as CBOR bytes.
    """
    key_bytes = cbor2.dumps(delta.key, canonical=True)
    prev = self._current_things.get(key_bytes)

    if prev is None:
        # ThingDelta requires the thing to already exist
        # (new things must use ThingState, not ThingDelta)
        raise ValueError(f"ThingDelta for unknown thing: {delta.key}")

    thing_type = prev.thing_type

    # Handle data changes based on diff_kind
    if delta.data_diff_kind == "no_diff":
        # Data unchanged
        new_id = prev.thingstate_id
        new_data_bytes = prev.data_bytes

    elif delta.data_diff_kind == "replace":
        # Full replacement
        await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick)
        new_id = await self._db.write_thing_state(
            conn,
            valid_from=tick,
            thing_key=delta.key,
            thing_type=thing_type,
            data=delta.new_data,
        )
        new_data_bytes = cbor2.dumps(delta.new_data)

    elif delta.data_diff_kind == "append":
        # Append to previous data
        await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick)
        new_id = await self._db.write_thing_state(
            conn,
            valid_from=tick,
            thing_key=delta.key,
            thing_type=prev.thing_type,
            data=delta.new_data,
            data_extends=prev.thingstate_id,
        )
        # For comparison, we'd need to track the full data chain
        # For now, store just the new chunk (appends always "change")
        new_data_bytes = cbor2.dumps(delta.new_data)
        thing_type = prev.thing_type

    # Handle link changes from delta
    prev_links = prev.links if prev else {}
    new_link_infos = await self._apply_link_delta(
        conn, tick, delta.key, prev_links, delta.added_links, delta.removed_link_keys
    )

    self._current_things[key_bytes] = CurrentThingInfo(
        thingstate_id=new_id,
        thing_type=thing_type,
        data_bytes=new_data_bytes,
        links=new_link_infos,
    )

    return key_bytes
```

### Link Diffing Helpers

```python
async def _write_links(
    self, conn: AsyncConnection, tick: int, from_key: Any,
    links: dict[bytes, ThingLink]
) -> dict[bytes, CurrentLinkInfo]:
    """Write all links for a new thing. Returns link tracking info."""
    result = {}
    for link_key_bytes, link in links.items():
        link_id = await self._db.write_link(
            conn,
            valid_from=tick,
            from_thing_key=from_key,
            link_type=link.link_type,
            link_key=link.link_key,
            to_thing_key=link.to_thing_key,
        )
        result[link_key_bytes] = CurrentLinkInfo(
            link_id=link_id,
            link_type=link.link_type,
            to_thing_key=cbor2.dumps(link.to_thing_key, canonical=True),
        )
    return result

async def _diff_links(
    self, conn: AsyncConnection, tick: int, from_key: Any,
    old_links: dict[bytes, CurrentLinkInfo],
    new_links: dict[bytes, ThingLink],
) -> dict[bytes, CurrentLinkInfo]:
    """Diff old and new links, writing changes. Returns new link tracking info."""
    result = {}

    # Check each new link
    for link_key_bytes, link in new_links.items():
        old_info = old_links.get(link_key_bytes)
        new_to_bytes = cbor2.dumps(link.to_thing_key, canonical=True)

        if old_info is None:
            # New link
            link_id = await self._db.write_link(
                conn,
                valid_from=tick,
                from_thing_key=from_key,
                link_type=link.link_type,
                link_key=link.link_key,
                to_thing_key=link.to_thing_key,
            )
            result[link_key_bytes] = CurrentLinkInfo(
                link_id=link_id,
                link_type=link.link_type,
                to_thing_key=new_to_bytes,
            )
        elif old_info.to_thing_key != new_to_bytes or old_info.link_type != link.link_type:
            # Link changed - invalidate old, write new
            await self._db.invalidate_link(conn, old_info.link_id, tick)
            link_id = await self._db.write_link(
                conn,
                valid_from=tick,
                from_thing_key=from_key,
                link_type=link.link_type,
                link_key=link.link_key,
                to_thing_key=link.to_thing_key,
            )
            result[link_key_bytes] = CurrentLinkInfo(
                link_id=link_id,
                link_type=link.link_type,
                to_thing_key=new_to_bytes,
            )
        else:
            # Link unchanged - keep old info
            result[link_key_bytes] = old_info

    # Invalidate removed links
    for link_key_bytes, old_info in old_links.items():
        if link_key_bytes not in new_links:
            await self._db.invalidate_link(conn, old_info.link_id, tick)

    return result

async def _apply_link_delta(
    self, conn: AsyncConnection, tick: int, from_key: Any,
    old_links: dict[bytes, CurrentLinkInfo],
    added_links: list[ThingLink],
    removed_link_keys: list[Any],
) -> dict[bytes, CurrentLinkInfo]:
    """Apply explicit link delta. Returns new link tracking info."""
    result = dict(old_links)  # Start with existing

    # Remove links
    for link_key in removed_link_keys:
        link_key_bytes = cbor2.dumps(link_key, canonical=True)
        if link_key_bytes in result:
            await self._db.invalidate_link(conn, result[link_key_bytes].link_id, tick)
            del result[link_key_bytes]

    # Add links
    for link in added_links:
        link_key_bytes = cbor2.dumps(link.link_key, canonical=True)
        # If link_key already exists, invalidate old first
        if link_key_bytes in result:
            await self._db.invalidate_link(conn, result[link_key_bytes].link_id, tick)

        link_id = await self._db.write_link(
            conn,
            valid_from=tick,
            from_thing_key=from_key,
            link_type=link.link_type,
            link_key=link.link_key,
            to_thing_key=link.to_thing_key,
        )
        result[link_key_bytes] = CurrentLinkInfo(
            link_id=link_id,
            link_type=link.link_type,
            to_thing_key=cbor2.dumps(link.to_thing_key, canonical=True),
        )

    return result
```

### Invalidating Disappeared Things

```python
async def _invalidate_thing(
    self, conn: AsyncConnection, tick: int, key_bytes: bytes
) -> None:
    """Invalidate a thing that disappeared from tracking."""
    info = self._current_things.pop(key_bytes)

    # Invalidate thing state
    await self._db.invalidate_thing_state(conn, info.thingstate_id, tick)

    # Invalidate all its links
    for link_info in info.links.values():
        await self._db.invalidate_link(conn, link_info.link_id, tick)
```

### Modified `watch_and_serve_until`
```python
async def watch_and_serve_until(self, done: Callable[[], Awaitable[Any]]) -> Any:
    token = set_current_watcher(self)
    try:
        # Initialize database
        self._db = Database(self.db_path)
        await self._db.connect()

        # Create write stream and start writer task
        send_stream, receive_stream = anyio.create_memory_object_stream[WriteJob](max_buffer_size=100)
        self._write_stream = send_stream

        async with anyio.create_task_group() as tg:
            tg.start_soon(self._db_writer, receive_stream)

            try:
                result = await done()
            finally:
                # Signal writer to stop
                await send_stream.aclose()

            # Writer will exit when stream is closed

        return result
    finally:
        if self._db:
            await self._db.close()
        self._write_stream = None
        self._current_things.clear()
        reset_current_watcher(token)
```

---

## Change Detection Semantics

### ThingState Processing
When a snapshotter returns a `ThingState`:
1. Compare `thing_data` to previous (via CBOR bytes)
2. If data identical: **no new thing_state row**, previous stays valid
3. If data changed: invalidate previous, write new thing_state
4. Always diff links independently (links can change without data changing)

### ThingDelta Processing
When a snapshotter returns a `ThingDelta`, we trust the declared `data_diff_kind`:

#### `data_diff_kind="no_diff"`
- No new thing_state row
- Previous state remains current (valid_until stays NULL)
- Only apply link changes (added_links, removed_link_keys)

#### `data_diff_kind="replace"`
- Invalidate previous state (set valid_until)
- Create new state with full new_data
- No data_extends pointer
- Apply link changes

#### `data_diff_kind="append"`
- Invalidate previous state (set valid_until) - it's no longer "current"
- Create new state with new_data and data_extends pointing to previous
- The new state becomes the "current" state (valid_until=NULL)
- Reader reconstructs full data by following data_extends chain backward
- Apply link changes

### Disappearance Detection
Things that were tracked in the previous tick but NOT seen in the current tick are **invalidated**:
- Their thing_state gets valid_until set
- All their links get valid_until set
- They are removed from in-memory tracking

Note: `valid_until` means "this state was current/active until tick N". It does NOT prevent following `data_extends` chains - readers can follow `data_extends` into states that have `valid_until` set.

---

## Tests: `tests/test_persistence.py`

### Test: basic tick persistence
```
- Create watcher with temp file DB
- Track object, tick
- Query DB directly, verify tick record exists
```

### Test: thing state persistence
```
- Track object, tick
- Verify thing_states row exists with correct key, type, data
```

### Test: unchanged data skips new row
```
- Track object, tick
- Tick again WITHOUT modifying object
- Verify only ONE thing_states row exists (valid_until still NULL)
- Verify tick count is 2 (ticks are recorded even if nothing changed)
```

### Test: changed data creates new row
```
- Track object, tick
- Modify object, tick again
- Verify first state has valid_until set, second state has valid_until NULL
```

### Test: link persistence
```
- Track object with links, tick
- Verify links table has correct entries
```

### Test: link change without data change
```
- Track object with links, tick
- Change only links (not data), tick again
- Verify thing_states row is SAME (no new row)
- Verify old link has valid_until set, new link exists
```

### Test: link unchanged
```
- Track object with links, tick
- Tick again without changes
- Verify same link row (no valid_until, no new row)
```

### Test: thing disappearance
```
- Track object A and B, tick
- Untrack object B, tick again
- Verify B's thing_state has valid_until set
- Verify B's links have valid_until set
- Verify A is unchanged
```

### Test: delta no_diff
```
- Create snapshotter that returns ThingDelta with no_diff
- Track, tick multiple times
- Verify only one thing_states row (no new rows for no_diff)
```

### Test: delta append
```
- Create snapshotter that returns ThingDelta with append
- Track, tick multiple times
- Verify chain of states with data_extends pointers
- Verify previous states have valid_until set
```

### Test: delta replace
```
- Create snapshotter that returns ThingDelta with replace
- Track, tick
- Verify old state invalidated, new state has no data_extends
```

### Test: in-memory database
```
- Create watcher with db_path=None
- Verify it works without errors
```

### Test: concurrent ticks still work
```
- Existing thread safety tests should still pass
- DB writes happen in background, don't block tick()
```

### Test: tick writes are atomic
```
- Start a transaction, write partial data, then fail
- Verify no partial data is visible in DB
- Alternatively: query mid-tick and verify either full tick or no tick visible
```

---

## Dependencies to Add

```bash
uv add cbor2 "sqlalchemy[aiosqlite]" anyio
```

---

## Implementation Order

1. Add dependencies (`cbor2`, `sqlalchemy[aiosqlite]`, `anyio`)
2. Create `thingwatcher/db.py` with SQLAlchemy table definitions
3. Implement Database class with `connect()`, `close()`, `begin()`
4. Implement transaction-based write methods
5. Update `watcher.py` with WriteJob, stream, writer task
6. Modify `watch_and_serve_until` to start DB and writer
7. Modify `tick()` to enqueue write jobs
8. Implement `_process_write_job` with atomic transaction wrapper
9. Write tests
10. Run tests, iterate

---

## Exit Criteria

- [ ] Database schema created on connect via SQLAlchemy
- [ ] Ticks persisted with type and data
- [ ] ThingState creates thing_states row (first time)
- [ ] Unchanged ThingState does NOT create new row
- [ ] Changed ThingState invalidates previous, creates new row
- [ ] ThingDelta with no_diff does NOT create new row
- [ ] ThingDelta with append creates chain via data_extends
- [ ] ThingDelta with replace invalidates previous
- [ ] Links persisted and invalidated correctly
- [ ] Links can change independently of thing data
- [ ] Unchanged links are preserved (no new rows)
- [ ] Disappeared things are invalidated
- [ ] In-memory mode works
- [ ] Background writer doesn't block tick()
- [ ] Each tick's writes are atomic (single transaction)
- [ ] All existing tests still pass
- [ ] New persistence tests pass
