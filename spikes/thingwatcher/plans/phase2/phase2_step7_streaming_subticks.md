# Phase 2 Step 7: Streaming Subticks

## Overview

Add streaming subticks - a mechanism for updating individual tracked objects in a streaming fashion without creating new conceptual ticks. This is useful for objects that update frequently (like loggers or counters) where you want to see the latest state without cluttering the timeline.

### Key Concepts

- **Stream tick**: A tick with type "stream" that can be updated in place
- **Streaming update**: An operation that snapshots a single tracked object and updates the current stream tick (or creates one)
- **Sequence number**: A global counter that increments on every operation (ticks AND streaming updates), used for full history tracking
- **Transient states**: Thing states that were created and invalidated within the same stream tick (visible in history but not at any tick query)

### Behavior Summary

1. **New API**: `streaming_update(tracked_object)` - sync, thread-safe (like `tick()`)
2. **Requirements for streaming**: Object must be tracked AND have a `StatefulOwningSnapshotter`
3. **If last tick is already "stream" type**:
   - Increment seq_counter only
   - Update the tick row's seq to the new value
   - Snapshot the object, write new states with `valid_from = current_tick, valid_from_seq = new_seq`
   - Invalidate old states owned by this object with `valid_until = current_tick, valid_to_seq = new_seq`
4. **If last tick is not "stream" type**:
   - Increment both tick_counter and seq_counter
   - Create new tick with type "stream"
   - Write states normally
5. **Things can appear/disappear during streaming**: If a snapshotter returns fewer keys than before, those things are invalidated (only for things owned by that object)
6. **Roots/pauses not re-captured**: Streaming updates only snapshot the specific object. Roots and pauses from the previous tick remain in effect.
7. **Objects not yet in a tick**: OK to streaming_update an object that hasn't appeared in a full tick yet, as long as it's tracked.

## Schema Changes

### `ticks` table

Add `seq` column:

```sql
ALTER TABLE ticks ADD COLUMN seq INTEGER NOT NULL;
```

The seq column stores the sequence number this tick maps to. For stream ticks, this gets updated on each streaming update.

### `thing_states` table

Add seq tracking columns:

```sql
ALTER TABLE thing_states ADD COLUMN valid_from_seq INTEGER NOT NULL;
ALTER TABLE thing_states ADD COLUMN valid_to_seq INTEGER;  -- NULL = still valid
```

### `links` table

Add seq tracking columns:

```sql
ALTER TABLE links ADD COLUMN valid_from_seq INTEGER NOT NULL;
ALTER TABLE links ADD COLUMN valid_to_seq INTEGER;
```

### `roots` table

Add seq tracking columns:

```sql
ALTER TABLE roots ADD COLUMN valid_from_seq INTEGER NOT NULL;
ALTER TABLE roots ADD COLUMN valid_to_seq INTEGER;
```

## Watcher Changes

### New state

```python
self._seq_counter: int = 0  # Global sequence number
self._current_tick_type: str | None = None  # Track type of most recent tick
```

### Modified `_take_snapshot` / `tick`

- Increment both `_tick_counter` and `_seq_counter`
- Pass seq to snapshot result
- Update `_current_tick_type`

### New `streaming_update` method

```python
def streaming_update(self, obj: Any) -> None:
    """Update a single tracked object in streaming fashion.

    Thread-safe: can be called from any thread.

    Args:
        obj: A tracked object with a StatefulOwningSnapshotter.

    Raises:
        ValueError: If object is not tracked or doesn't have an owning snapshotter.
    """
```

Implementation outline:
1. Acquire lock
2. Verify object is tracked
3. Verify snapshotter is StatefulOwningSnapshotter
4. Increment seq_counter
5. If last tick was "stream": reuse tick number, update seq
6. If last tick was not "stream": increment tick_counter, create new stream tick
7. Snapshot the single object
8. Build StreamingUpdateResult with:
   - New states from this object
   - Keys that disappeared (owned by this object but not in new snapshot)
9. Enqueue write job (different from SnapshotResult - see below)

### New `StreamingUpdateResult` dataclass

```python
@dataclass
class StreamingUpdateResult:
    """Result of a streaming update operation."""
    tick_number: int
    seq_number: int
    timestamp: float
    obj_id: int  # The tracked object that was updated
    states: list[ThingState | ThingDelta]
    disappeared_keys: set[bytes]  # Keys to invalidate (previously owned by this obj)
    on_complete: Callable[[dict[str, Any]], None] | None = None
```

## Database Changes

### Modified write methods

All write methods need to accept and store seq:

```python
async def write_tick(self, conn, tick, timestamp, level, tick_type, data, seq) -> None:
    ...

async def write_thing_state(self, conn, valid_from, valid_from_seq, thing_key, ...) -> int:
    ...

async def invalidate_thing_state(self, conn, state_id, valid_until, valid_to_seq) -> None:
    ...

# Similar for links and roots
```

### New method: `update_tick_seq`

```python
async def update_tick_seq(self, conn, tick: int, new_seq: int, new_timestamp: float) -> None:
    """Update the seq and timestamp of an existing tick (for streaming updates)."""
```

### Query methods

No changes needed for basic queries - the `valid_from <= tick AND (valid_until IS NULL OR valid_until > tick)` pattern naturally excludes transient states where `valid_from == valid_until`.

For historical/debugging queries that want full seq history, we'd query by valid_from_seq/valid_to_seq.

## Background Writer Changes

### Handle `StreamingUpdateResult`

The writer needs to handle streaming updates differently from full snapshots:

1. If tick already exists (stream update to existing stream tick):
   - Update tick row's seq and timestamp
   - For the specific object's states: invalidate old, write new
   - Handle disappeared keys (invalidate those states)
2. If tick doesn't exist (first stream update creating new stream tick):
   - Write tick row
   - Write states normally

### `_process_streaming_update` method

```python
async def _process_streaming_update(self, update: StreamingUpdateResult) -> None:
    """Process a streaming update atomically."""
    async with self._db.begin() as conn:
        tick = update.tick_number
        seq = update.seq_number

        # Check if tick exists
        existing_tick = await self._db.get_tick(conn, tick)

        if existing_tick:
            # Update existing stream tick
            await self._db.update_tick_seq(conn, tick, seq, update.timestamp)
        else:
            # Create new stream tick
            await self._db.write_tick(conn, tick, update.timestamp, "stream", "stream", None, seq)

        # Process states for this object
        for item in update.states:
            # ... (invalidate old state for this key, write new state)

        # Invalidate disappeared things
        for key_bytes in update.disappeared_keys:
            await self._invalidate_thing(conn, tick, seq, key_bytes)
```

## Subscription Changes

### Modified `newTick` subscription

Add parameter to control whether streaming updates trigger the subscription:

```graphql
type Subscription {
    newTick(includeStreamingUpdates: Boolean = false): Tick!
}
```

When `includeStreamingUpdates` is true, fire on both normal ticks and streaming updates.

The subscription callback receives the tick info as usual - for streaming updates, it's the same tick number with updated timestamp.

## Frontend Considerations

For this step, minimal frontend changes:
- Timeline continues to show ticks as before
- Stream ticks appear as single ticks that update in place
- When subscribed with `includeStreamingUpdates: true`, the UI refetches tick data

Future enhancement: could show a visual indicator that a tick is a "stream" tick and is actively updating.

## Implementation Steps

- [x] 1. Schema changes in `db.py`:
  - [x] Add seq column to ticks table
  - [x] Add valid_from_seq/valid_to_seq to thing_states, links, roots
  - [x] Update table definitions and indexes

- [x] 2. Update Database write methods:
  - [x] Modify `write_tick` to accept seq
  - [x] Modify `write_thing_state` to accept valid_from_seq
  - [x] Modify `invalidate_thing_state` to accept valid_to_seq
  - [x] Same for links and roots methods
  - [x] Add `update_tick_seq` method
  - [x] Add `get_tick` method (to check if tick exists)

- [x] 3. Update Watcher state and normal tick flow:
  - [x] Add `_seq_counter`
  - [x] Add `_current_tick_type`
  - [x] Update `_take_snapshot` to increment both counters and include seq in result
  - [x] Update `SnapshotResult` to include seq
  - [x] Update `_process_snapshot` to pass seq to DB methods

- [x] 4. Implement `streaming_update` in Watcher:
  - [x] Validation (tracked, owning snapshotter)
  - [x] Counter logic (seq always increments, tick only if needed)
  - [x] Single-object snapshot
  - [x] Detect disappeared keys
  - [x] Build and enqueue StreamingUpdateResult

- [x] 5. Implement `_process_streaming_update` in Watcher:
  - [x] Handle existing vs new stream tick
  - [x] Process states with proper seq tracking
  - [x] Handle disappeared things
  - [x] Notify subscribers

- [x] 6. Update GraphQL subscription:
  - [x] Add `includeStreamingUpdates` parameter to `newTick`
  - [x] Modify subscription resolver to respect the parameter

- [x] 7. Add tests:
  - [x] Basic streaming update
  - [x] Multiple streaming updates to same tick
  - [x] Thing appearing during streaming
  - [x] Thing disappearing during streaming
  - [x] Transient states (created and removed within same stream tick)
  - [x] Normal tick after stream tick
  - [x] Stream update on object not yet in any tick
  - [x] Ownership validation (must have owning snapshotter)
  - [x] Thread safety

- [x] 8. Update `examples/demo_multi_chunk.py`:
  - [x] Change CounterSnapshotter to StatefulOwningSnapshotter (required for streaming_update)
  - [x] Replace the single tick loop with separate async tasks for counter and logger
  - [x] Each task calls streaming_update at its own rate (de-synced streams)
  - [x] Add module-level streaming_update function to api.py

- [x] 9. Manual testing with graphqurl:
  - [x] Run the updated demo
  - [x] Query `ticks` and verify stream ticks appear but are deduplicated (not showing every seq)
  - [x] Query `latestTick { things { ... } }` and verify we see the most recent streaming state
  - [x] Use `triggerTick` mutation to capture a history point (creates a new non-stream tick)
  - [x] Verify streaming continues after the manual tick, updating the new stream tick
  - [x] Query historical tick to verify old state is preserved
  - [x] Test subscription with `includeStreamingUpdates: true` fires on stream updates
  - Note: graphqurl doesn't support the graphql-transport-ws protocol; tested subscriptions with Python websockets instead

- [x] 10. Update CODE_OVERVIEW.md

## API Summary

### Python API

```python
# Module-level function (like tick())
def streaming_update(obj: Any) -> None:
    """Update a single tracked object in streaming fashion.

    The object must be tracked and have a StatefulOwningSnapshotter.
    Creates or updates a stream tick with just this object's new state.

    Raises:
        ValueError: If object is not tracked or doesn't have an owning snapshotter.
    """

# Also available on Watcher instance
watcher.streaming_update(obj)
```

### GraphQL

No new queries/mutations needed. The subscription gets a new parameter:

```graphql
subscription {
    newTick(includeStreamingUpdates: true) {
        index
        timestamp
        level
        type
    }
}
```

The subscription just notifies that the tick was updated; frontend queries for the actual changes.

## Edge Cases and Notes

1. **Ownership conflicts during streaming**: If the owning snapshotter emits a key that conflicts with a stateless source from the previous tick, this is an error (broken thing). The `_stateless_keys` set tracks this.

2. **Root/pause pointing to streamed object via ThingRef**: This is fine - the ThingRef just references the key, and the key's data gets updated. The root/pause binding remains valid.

3. **Root/pause pointing to streamed object via ThingState**: This would be a conflict since the anchor capturer would emit the same key as the owning snapshotter. Should already be caught as ownership conflict.

4. **Concurrent streaming updates**: Same thread-safety as tick() - only one runs at a time under the lock.

5. **Streaming update while pause is active**: The pause anchor's state from the previous tick remains. If the paused object itself is streamed, and its anchor capturer returned ThingRef, the referenced data updates. If it returned ThingState, that's an ownership conflict.

6. **Error handling**: `streaming_update()` raises `ValueError` immediately if:
   - Object is not tracked
   - Object's snapshotter is not a `StatefulOwningSnapshotter`

# Frontend changes

- [x] 11. Update frontend for streaming updates:
  - [x] Update `NEW_TICK_SUBSCRIPTION` query to add `includeStreamingUpdates: true`
  - [x] Add live mode check to subscription handler (skip refetch when not live)
  - [x] Add `.stream` CSS class to tick markers when `level === "stream"` (dotted style)
  - [x] Reserve "stream" level in watcher.py `tick()` and `_async_tick()`
  - [x] Test with demo_multi_chunk.py to verify real-time updates appear
  - [x] Update LOCAL_CODE_OVERVIEW.md

## Frontend Changes for Streaming Updates

### Overview

The frontend needs to subscribe to streaming updates so that users can see real-time changes when `streaming_update()` is called. Currently, the subscription only fires on normal ticks. With the `includeStreamingUpdates: true` parameter, the subscription will also fire when individual objects are updated via streaming.

### Key Insight: Use `level` Field

Stream ticks already have `level: "stream"` (set by the backend). The `GET_TICKS` query already returns `level`, so we can use this for styling without adding `type` to the query. The level "stream" is reserved for streaming updates.

### Files to Modify

#### 1. `frontend/graphql/queries.ts`

Update the subscription to include the new parameter:

```typescript
export const NEW_TICK_SUBSCRIPTION = gql`
  subscription OnNewTick {
    newTick(includeStreamingUpdates: true) {
      index
      timestamp
      level
      type
      desc
    }
  }
`;
```

#### 2. `frontend/App.tsx`

Skip refetch when not in live mode (optimization):

```typescript
useSubscription({
  query: NEW_TICK_SUBSCRIPTION,
}, (_prev, data) => {
  // Only refetch if in live mode - no point updating when viewing history
  if (state.liveMode) {
    refetchLatest({ requestPolicy: 'network-only' });
    refetchTicks({ requestPolicy: 'network-only' });
    reexecuteTriggerRecords({ requestPolicy: 'network-only' });
  }
  return data;
});
```

#### 3. `frontend/components/Timeline/Timeline.tsx`

Add visual distinction for stream ticks using dotted border style:

- Check `tick.level === "stream"` to apply `.stream` CSS class
- Use dotted border to distinguish from solid "checkpoint" ticks

#### 4. `frontend/components/Timeline/Timeline.module.css`

```css
.tickMarker.stream {
  background: transparent;
  border: 2px dotted currentColor;
}
```

#### 5. `thingwatcher/watcher.py` (backend)

Reserve "stream" level - prevent users from using it in normal ticks:

```python
def tick(self, level: str = "info", desc: str | None = None) -> None:
    if level == "stream":
        raise ValueError("Level 'stream' is reserved for streaming updates")
    # ... rest of tick implementation
```

Also update `_async_tick` similarly.

### Behavior Notes

1. **Collapsing**: Consecutive stream updates already collapse naturally since they update the same tick number - no special handling needed.

2. **Live mode optimization**: Skip refetch when not in live mode to avoid unnecessary network traffic while scrubbing through history.

3. **Timeline position**: Stream ticks appear as single ticks that "update in place". Multiple streaming updates to the same tick don't create new timeline entries.

4. **Reserved level**: The level "stream" is reserved and cannot be used by `tick()` calls.

## Future Work / Known Gaps

No known gaps - all test cases are complete.
