# Phase 2 Step 10.1.1: Fix Data Chunks

## Problem Summary

The current detection of whether thing data changed is broken and doesn't work correctly with ThingDeltas. This was discovered during implementation of the hook interface refactor (step 10.1).

### Current Issues

1. **Data comparison breaks after ThingDelta**: `CurrentThingInfo.data_bytes` stores the CBOR bytes of the last data, but after a ThingDelta append, it only stores the new chunk. If a ThingState follows, comparing data bytes is meaningless since we're comparing a full state to just an append chunk.

2. **ThingDelta no_diff doesn't respect link changes**: When `data_diff_kind="no_diff"`, the code reuses `prev.thingstate_id` even if links changed. This violates the thingstateId invariant.

3. **data_extends couples data to thing_states**: The append chain uses `thing_states.data_extends` pointing to another `thing_states.id`. This means we can't share data between thing_states, and changing links requires duplicating data.

4. **Unnecessary data duplication**: When links change but data doesn't, we currently write a new thing_state with duplicate data bytes instead of pointing to the same data.

## Intended Behavior

### Data Representation
- Data chunks form a backwards linked list (like git commits)
- Each data_chunk has an optional `extends` pointer to a previous chunk
- thing_states point to a data_chunk, not embed data directly
- Multiple thing_states can point to the same data_chunk

### ThingState Snapshot Rules
1. ThingState always has data bytes
2. If previous snapshot was a ThingState AND data bytes are identical → reuse same data_chunk
3. If previous was NOT a ThingState (was ThingDelta), or data mismatches → create new data_chunk (fresh chain)
4. If same data_chunk AND links unchanged → reuse existing thingstate_id
5. If links changed → create new thingstate_id (pointing to same or new data_chunk)

### ThingDelta Snapshot Rules
1. `no_diff`: reuse existing data_chunk
2. `replace`: create new data_chunk (fresh chain, no extends)
3. `append`: create new data_chunk with `extends` pointing to previous chunk
4. If same data_chunk AND links unchanged → reuse existing thingstate_id
5. If links changed → create new thingstate_id (pointing to appropriate data_chunk)

## Schema Changes

### New Table: `data_chunks`

```sql
CREATE TABLE data_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data BLOB NOT NULL,           -- CBOR encoded data for this chunk
    extends INTEGER REFERENCES data_chunks(id)  -- NULL = start of chain
);
```

### Modified Table: `thing_states`

```sql
-- Remove: data BLOB
-- Remove: data_extends INTEGER REFERENCES thing_states(id)
-- Add:
ALTER TABLE thing_states ADD COLUMN data_chunk_id INTEGER NOT NULL REFERENCES data_chunks(id);
```

The final thing_states columns:
- `id` (PK)
- `valid_from`, `valid_until`, `valid_from_seq`, `valid_to_seq` (temporal tracking)
- `thing_key` (CBOR canonical)
- `thing_type` (Text)
- `data_chunk_id` (FK to data_chunks) -- NEW, replaces data and data_extends

## In-Memory Tracking Changes

### Modified `CurrentThingInfo`

```python
@dataclass
class CurrentThingInfo:
    """In-memory tracking for a currently-valid thing in the database."""

    thingstate_id: int
    thing_type: str
    data_chunk_id: int            # Current data chunk ID
    last_was_state: bool          # True if last snapshot was ThingState
    data_bytes: bytes | None      # CBOR bytes, only valid when last_was_state=True
    links: dict[bytes, CurrentLinkInfo] = field(default_factory=dict)
```

The `data_bytes` field is only used for comparison when `last_was_state=True`. After a ThingDelta, it becomes `None` (or we just ignore it).

## Processing Logic

### `_process_thing_state` (revised)

```python
async def _process_thing_state(self, conn, tick, seq, state: ThingState):
    key_bytes = cbor2.dumps(state.key, canonical=True)
    new_data_bytes = cbor2.dumps(state.thing_data)
    prev = self._current_things.get(key_bytes)

    # Build new links for comparison
    new_links = {cbor2.dumps(link.link_key, canonical=True): link for link in state.links}

    if prev is None:
        # New thing - create fresh data chunk and thing_state
        data_chunk_id = await self._db.write_data_chunk(conn, state.thing_data, extends=None)
        new_id = await self._db.write_thing_state(conn, tick, seq, state.key, state.thing_type, data_chunk_id)
        new_link_infos = await self._write_links(conn, tick, seq, state.key, new_links)
    else:
        # Existing thing - determine if we can reuse data chunk
        if prev.last_was_state and prev.data_bytes == new_data_bytes:
            # Same data as previous ThingState - reuse data chunk
            data_chunk_id = prev.data_chunk_id
        else:
            # Different data or previous was ThingDelta - new chain
            data_chunk_id = await self._db.write_data_chunk(conn, state.thing_data, extends=None)

        # Check if we need a new thingstate_id
        links_changed = self._links_changed(prev.links, new_links)
        data_chunk_changed = (data_chunk_id != prev.data_chunk_id)

        if data_chunk_changed or links_changed:
            await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick, seq)
            new_id = await self._db.write_thing_state(conn, tick, seq, state.key, state.thing_type, data_chunk_id)
        else:
            new_id = prev.thingstate_id

        new_link_infos = await self._diff_links(conn, tick, seq, state.key, prev.links, new_links)

    return key_bytes, CurrentThingInfo(
        thingstate_id=new_id,
        thing_type=state.thing_type,
        data_chunk_id=data_chunk_id,
        last_was_state=True,
        data_bytes=new_data_bytes,
        links=new_link_infos,
    )
```

### `_process_thing_delta` (revised)

```python
async def _process_thing_delta(self, conn, tick, seq, delta: ThingDelta):
    key_bytes = cbor2.dumps(delta.key, canonical=True)
    prev = self._current_things.get(key_bytes)

    if prev is None:
        raise ValueError(f"ThingDelta for unknown thing: {delta.key}")

    thing_type = prev.thing_type

    # Determine data chunk
    if delta.data_diff_kind == "no_diff":
        data_chunk_id = prev.data_chunk_id
    elif delta.data_diff_kind == "replace":
        data_chunk_id = await self._db.write_data_chunk(conn, delta.new_data, extends=None)
    elif delta.data_diff_kind == "append":
        data_chunk_id = await self._db.write_data_chunk(conn, delta.new_data, extends=prev.data_chunk_id)
    else:
        raise ValueError(f"Unknown data_diff_kind: {delta.data_diff_kind}")

    # Check if links changed - easy check from delta fields
    links_changed = bool(delta.added_links or delta.removed_link_keys)

    # Handle link changes
    new_link_infos = await self._apply_link_delta(
        conn, tick, seq, delta.key, prev.links, delta.added_links, delta.removed_link_keys
    )

    # Check if we need a new thingstate_id
    data_chunk_changed = (data_chunk_id != prev.data_chunk_id)

    if data_chunk_changed or links_changed:
        await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick, seq)
        new_id = await self._db.write_thing_state(conn, tick, seq, delta.key, thing_type, data_chunk_id)
    else:
        new_id = prev.thingstate_id

    return key_bytes, CurrentThingInfo(
        thingstate_id=new_id,
        thing_type=thing_type,
        data_chunk_id=data_chunk_id,
        last_was_state=False,  # ThingDelta, so next ThingState won't compare
        data_bytes=None,       # Not valid after ThingDelta
        links=new_link_infos,
    )
```

## Database Method Changes

### New Methods

```python
async def write_data_chunk(self, conn, data: Any, extends: int | None) -> int:
    """Write a data chunk and return its id."""
    result = await conn.execute(
        data_chunks.insert().values(
            data=cbor2.dumps(data),
            extends=extends,
        )
    )
    return result.inserted_primary_key[0]

async def get_data_chunk_by_id(self, conn, chunk_id: int) -> Row | None:
    """Get a data chunk by its ID."""
    result = await conn.execute(
        select(data_chunks).where(data_chunks.c.id == chunk_id)
    )
    return result.fetchone()
```

### Modified Methods

```python
async def write_thing_state(self, conn, valid_from, valid_from_seq, thing_key, thing_type, data_chunk_id) -> int:
    """Write a thing state and return its id."""
    # No longer takes data or data_extends, takes data_chunk_id instead
    result = await conn.execute(
        thing_states.insert().values(
            valid_from=valid_from,
            valid_from_seq=valid_from_seq,
            thing_key=cbor2.dumps(thing_key, canonical=True),
            thing_type=thing_type,
            data_chunk_id=data_chunk_id,
        )
    )
    return result.inserted_primary_key[0]
```

## GraphQL Changes

### `dataChunk(id:)` Query

Currently queries thing_states by ID and follows data_extends. After this change:
- Takes a data_chunk ID (not thing_state ID)
- Queries data_chunks table directly
- Follows `extends` chain in data_chunks table

### Thing Data Resolution

When resolving a Thing's data:
1. Get thing_state at tick
2. Get data_chunk via `data_chunk_id`
3. Return chunk data (or follow extends chain for full data)

## Implementation Steps

- [x] Update `db.py`: Add `data_chunks` table schema
- [x] Update `db.py`: Modify `thing_states` table (remove data/data_extends, add data_chunk_id)
- [x] Update `db.py`: Add `write_data_chunk` method
- [x] Update `db.py`: Add `get_data_chunk_by_id` method
- [x] Update `db.py`: Modify `write_thing_state` signature and implementation
- [x] Update `watcher.py`: Modify `CurrentThingInfo` dataclass
- [x] Update `watcher.py`: Rewrite `_process_thing_state` with new logic
- [x] Update `watcher.py`: Rewrite `_process_thing_delta` with new logic
- [x] Update `server.py`: Update `dataChunk` resolver to query data_chunks table
- [x] Update `server.py`: Update thing data resolution (anywhere that reads thing_state.data)
- [x] Update tests: Fix any tests that check thing_states.data directly
- [x] Update tests: Add tests for the new behavior (ThingState after ThingDelta, link-only changes, etc.)
- [x] Run all tests and verify
- [x] Update implementation notes

## Testing Considerations

- ThingState after ThingState with same data → same data_chunk, same thingstate_id (if links unchanged)
- ThingState after ThingState with different data → new data_chunk, new thingstate_id
- ThingState after ThingDelta → always new data_chunk (no comparison)
- ThingDelta no_diff with no link changes → same data_chunk, same thingstate_id
- ThingDelta no_diff with link changes → same data_chunk, NEW thingstate_id
- ThingDelta append → new data_chunk with extends, new thingstate_id
- ThingDelta replace → new data_chunk (no extends), new thingstate_id
- Link-only change on ThingState → same data_chunk, new thingstate_id

## Implementation Notes

Implementation went smoothly. All 156 tests pass.

### Key Changes Made

**db.py:**
- Added `data_chunks` table with `id`, `data`, and `extends` columns
- Modified `thing_states` table: removed `data` and `data_extends`, added `data_chunk_id`
- Added `write_data_chunk()` and `get_data_chunk_by_id()` methods
- Modified `write_thing_state()` to take `data_chunk_id` instead of `data`/`data_extends`

**watcher.py:**
- Extended `CurrentThingInfo` with `data_chunk_id`, `last_was_state`, and made `data_bytes` nullable
- Rewrote `_process_thing_state` with new data chunk logic:
  - Reuses data chunk if `last_was_state=True` and data bytes match
  - Creates new chunk if data changed or previous was ThingDelta
- Rewrote `_process_thing_delta` with new data chunk logic:
  - `no_diff`: reuses existing chunk, checks links for thingstate change
  - `replace`: creates new chunk (no extends)
  - `append`: creates new chunk with `extends` pointing to previous

**server.py:**
- Updated `_get_data_chunks_chain()` to follow `extends` in data_chunks table
- Updated `resolve_data_chunk()` to query data_chunks directly
- Updated `resolve_thing_data_chunks()` and `resolve_thing_data()` to use `data_chunk_id`

**tests/test_persistence.py:**
- Updated tests to get data from `data_chunks` table via `data_chunk_id`
- Updated append tests to check `data_chunks.extends` instead of `thing_states.data_extends`

### Added Tests

New test class `TestDataChunkBehavior` in `tests/test_persistence.py`:

1. `test_thingstate_same_data_reuses_chunk` - ThingState with unchanged data reuses same data_chunk (and only creates 1 thing_state)
2. `test_link_only_change_reuses_data_chunk` - Link-only changes create new thingstate_id but reuse data_chunk
3. `test_thingstate_after_thingdelta_creates_new_chunk` - ThingState after ThingDelta always creates fresh data_chunk (no comparison)
4. `test_thingdelta_no_diff_with_link_changes_new_thingstate` - ThingDelta no_diff with link changes creates new thingstate_id but shares data_chunk

All 160 tests pass.
