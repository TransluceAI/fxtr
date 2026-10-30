# Phase 2 Step 13 - Pop Chunks Scratch

## New ThingDelta Design

### Current design (to be replaced)
```python
@dataclass
class ThingDelta:
    key: CborableTypes
    data_diff_kind: Literal["replace", "append", "no_diff"]
    new_data: CborableTypes  # must be None if data_diff_kind is "no_diff"
    added_links: list[ThingLink]
    removed_link_keys: list[CborableTypes]
```

### New design
```python
# Sentinel types
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

    # Data operations - new interface
    push_chunk: CborableTypes | NoPushedChunk = NO_PUSHED_CHUNK  # data to append, or sentinel
    pop_chunks: int | PopAll = 0  # number of chunks to pop first, or POP_ALL

    # Link operations - unchanged
    added_links: list[ThingLink]
    removed_link_keys: list[CborableTypes]
```

## Behavior Mapping

| Old `data_diff_kind` | New equivalent |
|---------------------|----------------|
| `no_diff` | `push_chunk=NO_PUSHED_CHUNK, pop_chunks=0` |
| `append` | `push_chunk=data, pop_chunks=0` |
| `replace` | `push_chunk=data, pop_chunks=POP_ALL` |
| (new) pop N then append | `push_chunk=data, pop_chunks=N` |

## Validation Rules

1. **Must always have at least one chunk after operations**:
   - A thing must always have at least one data chunk

2. **Pop without push is valid** (if chunks remain):
   - `pop_chunks=2` on a 5-chunk chain without push → valid, 3 chunks remain
   - The thing_state just repoints to an earlier chunk in the chain

3. **Pop all without push is invalid**:
   - `pop_chunks=POP_ALL` requires `push_chunk` to have data
   - Otherwise there would be zero chunks

4. **Pop more than exists is an error**:
   - If `pop_chunks > chain_length`, raise error (even with push)
   - If `pop_chunks >= chain_length` AND no push, raise error (would leave 0 chunks)
   - If `pop_chunks == chain_length` AND has push, valid (pops all, pushes 1)

5. **Push without pop is append**:
   - `push_chunk=data, pop_chunks=0` creates new chunk extending current head

6. **No push, no pop is valid**:
   - `push_chunk=NO_PUSHED_CHUNK, pop_chunks=0` means no data change
   - Can still have link changes

## Implementation Details

### Finding ancestor chunk (for pop_chunks=N)

When we have `pop_chunks=N`, we need to find the chunk N steps back in the extends chain:

```python
async def get_ancestor_chunk_id(
    self, conn: AsyncConnection, current_chunk_id: int, steps_back: int
) -> int | None:
    """Walk back N steps in the extends chain, return the chunk ID to extend from.

    Returns None if steps_back == number of chunks (i.e., pop entire chain).
    Raises ValueError if steps_back > number of chunks.
    """
    chunk_id = current_chunk_id
    for _ in range(steps_back):
        chunk = await self.get_data_chunk_by_id(conn, chunk_id)
        if chunk is None:
            raise ValueError("Cannot pop: chunk not found in DB")
        if chunk.extends is None:
            # We've reached the start of the chain but still have more to pop
            raise ValueError(f"Cannot pop {steps_back} chunks: only reached chain start")
        chunk_id = chunk.extends

    # Now chunk_id is the ancestor we should extend from
    # But we need its extends value (the parent of what we're keeping)
    chunk = await self.get_data_chunk_by_id(conn, chunk_id)
    return chunk.extends  # Could be None if popping down to 1 chunk
```

Wait, let me think about this more carefully...

If chain is: A → B → C → D (where D is head, D.extends = C.id, etc.)

If we pop_chunks=2 and push new data E:
- We want E.extends = B.id (skip C and D)
- So we need to walk back 2 steps from D to find B

Actually simpler: walk back pop_chunks steps, that's the new extends target.

```python
async def get_extends_target_after_pop(
    self, conn: AsyncConnection, current_head_id: int, pop_count: int
) -> int | None:
    """Find the chunk ID to use as extends after popping pop_count chunks.

    If pop_count=0, returns current_head_id (normal append).
    If pop_count > 0, walks back that many steps and returns the ancestor's ID.
    Returns None if we pop the entire chain.

    Raises ValueError if pop_count > chain length.
    """
    if pop_count == 0:
        return current_head_id

    chunk_id = current_head_id
    steps_remaining = pop_count

    while steps_remaining > 0:
        chunk = await self.get_data_chunk_by_id(conn, chunk_id)
        if chunk is None:
            raise ValueError(f"Cannot pop: chunk {chunk_id} not found")

        extends_id = chunk.extends
        if extends_id is None:
            # We're at the start of the chain
            if steps_remaining == 1:
                # This is the last step, so we pop this chunk too
                return None  # New chain starts fresh
            else:
                raise ValueError(f"Cannot pop {pop_count} chunks: only {pop_count - steps_remaining + 1} exist")

        chunk_id = extends_id
        steps_remaining -= 1

    return chunk_id
```

### Processing ThingDelta in watcher.py

The `_process_thing_delta` method needs to be updated:

```python
async def _process_thing_delta(
    self, conn: AsyncConnection, tick: int, seq: int, delta: ThingDelta
) -> tuple[bytes, CurrentThingInfo]:
    key_bytes = cbor2.dumps(delta.key, canonical=True)
    prev = self._current_things.get(key_bytes)

    if prev is None:
        raise ValueError(f"ThingDelta for unknown thing: {delta.key}")

    thing_type = prev.thing_type

    # Validate: can't pop without push
    is_no_push = isinstance(delta.push_chunk, NoPushedChunk)
    is_pop = delta.pop_chunks != 0 and not isinstance(delta.pop_chunks, int) or (
        isinstance(delta.pop_chunks, int) and delta.pop_chunks > 0
    ) or isinstance(delta.pop_chunks, PopAll)

    # Simpler check:
    has_push = not isinstance(delta.push_chunk, NoPushedChunk)
    has_pop = (isinstance(delta.pop_chunks, PopAll) or
               (isinstance(delta.pop_chunks, int) and delta.pop_chunks > 0))

    if has_pop and not has_push:
        raise ValueError("Cannot pop chunks without pushing new data")

    # Determine extends target
    if isinstance(delta.pop_chunks, PopAll):
        extends_id = None  # Fresh chain
    elif delta.pop_chunks > 0:
        extends_id = await self._db.get_extends_target_after_pop(
            conn, prev.data_chunk_id, delta.pop_chunks
        )
    else:
        # No pop
        if has_push:
            extends_id = prev.data_chunk_id  # Append
        else:
            extends_id = None  # No data change, reuse existing chunk
            data_chunk_id = prev.data_chunk_id

    # Create new chunk if pushing
    if has_push:
        data_chunk_id = await self._db.write_data_chunk(
            conn, delta.push_chunk, extends=extends_id
        )
    else:
        # No push, no pop = no data change
        data_chunk_id = prev.data_chunk_id

    # ... rest of method (links, thingstate write, etc.)
```

### In-memory tracking consideration

Currently `CurrentThingInfo` has `data_chunk_id` pointing to head of chain. When popping, we need to look up the chain in the DB. This should be fine since:
1. Pop operations are probably not super common
2. The chain is typically short
3. We could cache if needed later

### Frontend implications

The frontend already follows the extends chain via `useChunkResolver`. When chunks are popped:
- The new thingstate points to a new chunk
- That chunk's extends points further back
- Old popped chunks are orphaned but still exist
- Historical ticks still work correctly (they point to old thingstates which point to old chunk chains)

No frontend changes needed! The existing dataChunks resolution just works.

### GraphQL implications

No changes needed - the schema already exposes `dataChunks` which follows the extends chain.

## Migration / Breaking Change

This is a breaking change to the ThingDelta interface. The old fields are:
- `data_diff_kind: Literal["replace", "append", "no_diff"]`
- `new_data: CborableTypes`

The new fields are:
- `push_chunk: CborableTypes | NoPushedChunk`
- `pop_chunks: int | PopAll`

Options:
1. **Clean break**: Remove old fields, add new fields. Update all demos and clydra.
2. **Backward compat**: Keep old fields as deprecated, support both, eventually remove.

Given the comment "this can be a breaking change, there are no other users", let's do a clean break.

## Test Cases

1. **No push, no pop**: `push_chunk=NO_PUSHED_CHUNK, pop_chunks=0`
   - Data unchanged, same data_chunk_id

2. **Push without pop (append)**: `push_chunk=data, pop_chunks=0`
   - New chunk with extends=prev_chunk_id

3. **Pop all and push (replace)**: `push_chunk=data, pop_chunks=POP_ALL`
   - New chunk with extends=None

4. **Pop N and push**: `push_chunk=data, pop_chunks=N`
   - New chunk with extends=ancestor_chunk_id
   - Error if N > chain length

5. **Pop N without push (repoint)**: `push_chunk=NO_PUSHED_CHUNK, pop_chunks=2` on 5-chunk chain
   - Valid! Repoints to ancestor chunk (3 chunks remain)

6. **Pop N without push (too many)**: `push_chunk=NO_PUSHED_CHUNK, pop_chunks=5` on 5-chunk chain
   - Error: would leave 0 chunks

7. **Pop more than exists**: `pop_chunks=10` when chain is 3 long
   - Error (even with push)

8. **Pop exactly chain length with push**: `pop_chunks=4, push_chunk=data` on 4-chunk chain
   - Valid: pops all, pushes 1 (fresh start)

9. **Pop all on single-chunk thing**: `pop_chunks=POP_ALL, push_chunk=data`
   - Valid, new chunk with extends=None

10. **Pop all without push**: `pop_chunks=POP_ALL, push_chunk=NO_PUSHED_CHUNK`
    - Error: would leave 0 chunks

## Summary

Changes needed:
1. **api.py**: Update ThingDelta dataclass, add sentinel types
2. **db.py**: Add helper methods `walk_chunk_chain` and `count_chunk_chain`
3. **watcher.py**: Update `_process_thing_delta` to handle new interface
4. **tests**: Add tests for pop_chunks functionality
5. **demos**: Update demo_multi_chunk to use new interface
6. **clydra**: Update any snapshotters using ThingDelta
7. **CODE_OVERVIEW.md**: Document the new interface
