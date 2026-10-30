# Phase 2 Step 13: Pop Chunks for ThingDelta

## Overview

Redesign the `ThingDelta` data interface to support "popping" chunks from the extends chain before pushing new data. This enables use cases like:
- Rolling windows (keep last N entries)
- Undo/redo systems (revert to earlier state)
- Compaction (consolidate multiple append chunks into one)

## Current Interface

```python
@dataclass
class ThingDelta:
    key: CborableTypes
    data_diff_kind: Literal["replace", "append", "no_diff"]
    new_data: CborableTypes  # must be None if data_diff_kind is "no_diff"
    added_links: list[ThingLink]
    removed_link_keys: list[CborableTypes]
```

## New Interface

```python
class NoPushedChunk:
    """Sentinel indicating no chunk should be pushed."""
    pass

NO_PUSHED_CHUNK = NoPushedChunk()

class PopAll:
    """Sentinel indicating all chunks should be popped."""
    pass

POP_ALL = PopAll()

@dataclass
class ThingDelta:
    key: CborableTypes

    # Data operations
    push_chunk: CborableTypes | NoPushedChunk = NO_PUSHED_CHUNK
    pop_chunks: int | PopAll = 0

    # Link operations (unchanged)
    added_links: list[ThingLink]
    removed_link_keys: list[CborableTypes]
```

## Behavior Mapping

| Operation | Old Interface | New Interface |
|-----------|---------------|---------------|
| No data change | `data_diff_kind="no_diff"` | `push_chunk=NO_PUSHED_CHUNK, pop_chunks=0` |
| Append chunk | `data_diff_kind="append"` | `push_chunk=data, pop_chunks=0` |
| Replace (fresh chain) | `data_diff_kind="replace"` | `push_chunk=data, pop_chunks=POP_ALL` |
| Pop N then append | (not possible) | `push_chunk=data, pop_chunks=N` |

## Validation Rules

1. **Must always have at least one chunk**: After all operations, a thing must have at least one data chunk.

2. **Pop without push is valid** (if chunks remain): `pop_chunks=2` on a 5-chunk chain without push → valid, 3 chunks remain. The thing_state simply points to an earlier chunk.

3. **Pop all without push is invalid**: `pop_chunks=POP_ALL` requires `push_chunk` to have data. Otherwise there would be zero chunks. This is validated **immediately** (doesn't require DB access).

4. **Pop more than exists is an error**: If `pop_chunks` is an integer greater than or equal to chain length AND there's no push, raise `ValueError` (would leave zero chunks).

5. **Pop more than exists WITH push is valid up to chain length**: `pop_chunks=5, push_chunk=data` on a 5-chunk chain → valid (pops all, pushes 1). But `pop_chunks=6` on a 5-chunk chain → error.

6. **No change is valid**: `push_chunk=NO_PUSHED_CHUNK, pop_chunks=0` means no data change (but can still have link changes).

7. **Negative pop_chunks is an error**: `pop_chunks` must be >= 0 (or `POP_ALL`).

## Error Handling

All validation errors produce broken things rather than raising exceptions. This ensures `tick()` never fails due to invalid ThingDelta values - the error is captured and visible in the UI.

- **Early validation** (checked in `_take_snapshot` / `_take_streaming_update`, no DB needed):
  - `pop_chunks=POP_ALL` without push
  - Negative `pop_chunks`

  These are validated inside the snapshotter try/except block, so errors produce broken things with the error message preserved.

- **Late validation** (checked during DB write in `_process_thing_delta`):
  - `pop_chunks > chain_length`
  - `pop_chunks >= chain_length` without push

  These also produce broken things and log a warning, since validation requires DB access.

## Chain Walking Example

Given chunk chain: `A → B → C → D` (D is head, D.extends=C, C.extends=B, etc.)

### With push_chunk=data (new chunk E created)

| `pop_chunks` | New head | E.extends | Effect |
|--------------|----------|-----------|--------|
| 0 | E | D | Normal append: E → D → C → B → A |
| 1 | E | C | Pop D: E → C → B → A |
| 2 | E | B | Pop D,C: E → B → A |
| 3 | E | A | Pop D,C,B: E → A |
| 4 | E | None | Pop all: E (fresh start) |
| 5 | Error | - | Can't pop more than 4 |
| `POP_ALL` | E | None | Pop all: E (fresh start) |

### With push_chunk=NO_PUSHED_CHUNK (no new chunk, just repoint)

| `pop_chunks` | New head | Effect |
|--------------|----------|--------|
| 0 | D | No change |
| 1 | C | Repoint to C: C → B → A |
| 2 | B | Repoint to B: B → A |
| 3 | A | Repoint to A |
| 4 | Error | Would leave 0 chunks |
| `POP_ALL` | Error | Would leave 0 chunks |

## Implementation Changes

### 1. api.py - Update ThingDelta

- Add `NoPushedChunk` singleton class and `NO_PUSHED_CHUNK` instance
- Add `PopAll` singleton class and `POP_ALL` instance
- Replace `data_diff_kind` and `new_data` with `push_chunk` and `pop_chunks`
- Export new types from `__all__`
- Docstring for `push_chunk` should note: `None` is valid CBOR data and will push a chunk containing `None`. Use `NO_PUSHED_CHUNK` sentinel to indicate no chunk should be pushed.

### 2. db.py - Add chain walking helper

```python
async def walk_chunk_chain(
    self,
    conn: AsyncConnection,
    head_chunk_id: int,
    steps: int,
) -> int | None:
    """Walk back N steps in the extends chain.

    Args:
        conn: Database connection
        head_chunk_id: The current head chunk ID
        steps: Number of steps to walk back

    Returns:
        Chunk ID after walking back N steps, or None if we walked
        exactly to the end of the chain (steps == chain length).

    Raises:
        ValueError: If steps > chain length (can't walk that far).
    """
```

### 3. watcher.py - Update delta handling

**In `_take_snapshot` and `_take_streaming_update`**: Add immediate validation after snapshotter returns a ThingDelta:
- Check for POP_ALL without push → raise ValueError
- Check for negative pop_chunks → raise ValueError

**In `_process_thing_delta`**: Update the async processing logic:
1. Validate pop/push combination against actual chain length (must leave at least one chunk)
2. Walk chain to find new head or extends target
3. Either repoint to existing chunk (pop-only) or create new chunk (push)
4. Handle links as before
5. On validation failure, produce broken thing + log warning

Key logic (in `_process_thing_delta`):
```python
has_push = not isinstance(delta.push_chunk, NoPushedChunk)
is_pop_all = isinstance(delta.pop_chunks, PopAll)
pop_count = 0 if is_pop_all else delta.pop_chunks

# Note: POP_ALL without push and negative pop_chunks are already validated
# in _take_snapshot before we get here

try:
    if is_pop_all:
        # Pop everything, push new
        data_chunk_id = await self._db.write_data_chunk(conn, delta.push_chunk, extends=None)

    elif pop_count > 0:
        # Walk the chain - returns int if chunks remain, None if popped all, raises if too many
        walked_id = await self._db.walk_chunk_chain(conn, prev.data_chunk_id, pop_count)

        if has_push:
            # Pop N, then push: walked_id can be None (fresh start) or int (extends)
            data_chunk_id = await self._db.write_data_chunk(conn, delta.push_chunk, extends=walked_id)
        else:
            # Pop N, no push: walked_id must be int (can't leave 0 chunks)
            if walked_id is None:
                raise ValueError(f"Cannot pop {pop_count} chunks without push: would leave 0")
            data_chunk_id = walked_id

    elif has_push:
        # Normal append (pop_count == 0)
        data_chunk_id = await self._db.write_data_chunk(conn, delta.push_chunk, extends=prev.data_chunk_id)

    else:
        # No change (no push, no pop)
        data_chunk_id = prev.data_chunk_id

except ValueError as e:
    # Async validation failed - produce broken thing
    logger.warning(f"ThingDelta validation failed for {delta.key}: {e}")
    # Create broken thing with fresh data chunk
    data_chunk_id = await self._db.write_data_chunk(conn, {"error": str(e)}, extends=None)
    thing_type = "__broken__"
```

### 4. Update consumers

**examples/demo_multi_chunk/demo.py** - Update `LogAccumulatorSnapshotter`:
```python
# Old
ThingDelta(key=..., data_diff_kind="append", new_data=entries, ...)
ThingDelta(key=..., data_diff_kind="no_diff", new_data=None, ...)

# New
ThingDelta(key=..., push_chunk=entries, pop_chunks=0, ...)
ThingDelta(key=..., push_chunk=NO_PUSHED_CHUNK, pop_chunks=0, ...)
```

**clydra/** - Update any snapshotters that use ThingDelta

### 5. Tests

Add tests in `tests/test_pop_chunks.py`:
- No push, no pop (data unchanged)
- Push without pop (append)
- Pop all and push (replace)
- Pop N and push (partial pop)
- Pop N without push (repoint to earlier chunk)
- Error: pop all without push
- Error: pop N >= chain_length without push
- Error: pop N > chain_length (even with push)
- Pop exactly chain_length with push (valid, fresh start)
- Pop all on single-chunk thing with push
- Pop with streaming_update
- Verify historical ticks still see old chunk chains

### 6. Documentation

Update CODE_OVERVIEW.md to document new interface.

## Frontend/GraphQL Impact

**None.** The frontend already follows the extends chain via `useChunkResolver`. When chunks are popped:
- The new thingstate points to a new chunk
- That chunk's extends points further back
- Orphaned chunks remain in DB (historical ticks still reference them)
- The existing `Thing.dataChunks` field just works

## Migration

This is a **breaking change** to the public API. The phase 2 overview notes: "this can be a breaking change, there are no other users" besides the demos and clydra.

Update order:
1. Update api.py with new interface
2. Update watcher.py processing
3. Update db.py with helper
4. Fix demo_multi_chunk
5. Fix clydra (in sibling directory)
6. Add tests
7. Update CODE_OVERVIEW.md

## Implementation Steps

- [x] 1. Add sentinel types and update ThingDelta in api.py
- [x] 2. Add `walk_chunk_chain` helper to db.py
- [x] 3. Update `_take_snapshot` / `_take_streaming_update` with immediate validation
- [x] 4. Update `_process_thing_delta` in watcher.py with new logic
- [x] 5. Update demo_multi_chunk to use new interface
- [x] 6. Update clydra snapshotters to use new interface
- [x] 7. Write tests for pop_chunks functionality
- [x] 8. Update CODE_OVERVIEW.md
