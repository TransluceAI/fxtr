# Phase 0 Step 3b: Fix /clear Not Clearing Events

## Problem

When `/clear` is issued, the conversation events don't clear in the frontend. The old messages remain visible.

**Root cause**: The `ClydraManagerSnapshotter` uses event count comparison to detect changes:
- It tracks `last_event_count` per manager
- It emits append deltas when `current_count > last_count`
- When events are cleared, `current_count` (0) < `last_count` (N), which falls through to either a no-op or an empty append
- The frontend never receives a signal to clear its accumulated chunks

## Solution

Implement a cooperative tracking system between `ClydraManager` and `ClydraManagerSnapshotter`:

1. **Manager** maintains a log of uncommitted operations (`_pending_ops`)
2. **Snapshotter** tracks how many events are in each emitted chunk
3. At snapshot time, snapshotter consumes the ops and computes the minimal `ThingDelta` (pop_chunks + push_chunk)

### Operations to Support

- `append` - add events to end
- `clear` - remove all events
- `pop(n)` - remove N events from end

### StreamEvent Optimization

StreamEvents are partial message deltas used for live display during streaming. Once a non-stream event arrives (like AssistantMessage), the preceding StreamEvents are superseded and can be popped to keep the events list shorter. The renderer already filters them out of `displayEvents` once superseded.

## Files to Change

### 1. `clydra/manager.py`

**Changes to `ClydraManager` dataclass:**
- Rename `events` → `_events` (private)
- Add `_pending_ops: list[dict[str, Any]]` field (default empty list, not included in snapshots)
- Add methods:
  - `append_event(event: dict)` - appends to `_events`, records `{"op": "push", "count": 1}`
  - `append_events(events: list[dict])` - extends `_events`, records `{"op": "push", "count": len(events)}`
  - `clear_events()` - sets `_events = []`, records `{"op": "clear"}`
  - `pop_events(n: int)` - pops n events from end of `_events`, records `{"op": "pop", "count": n}`
  - `get_events() -> list[dict]` - read-only access to `_events`
  - `_consume_pending_ops() -> list[dict]` - returns and clears `_pending_ops` (private, for snapshotter use only)

### 2. `clydra/main.py`

**Update all call sites:**
- `manager.events.append(...)` → `manager.append_event(...)`
- `manager.events = []` → `manager.clear_events()`
- `manager.events = history + manager.events` → `manager.clear_events()` then `manager.append_events(history)` then append current
- Read access `manager.events` → `manager.get_events()`

Call sites to update:
- `run_session()`: appending user_message and SDK events
- `model_worker()`: appending ClydraInternalError
- `worker()`: clearing events on session restart
- `on_user_prompt` hook: prepending transcript history

**StreamEvent popping:**
In `run_session()`, when receiving SDK messages:
- Track count of consecutive trailing StreamEvents
- When a non-StreamEvent arrives, pop the trailing StreamEvents before appending the new event
- This keeps the events list trimmed during streaming

### 3. `clydra/snapshotter.py`

**Changes to `ClydraManagerSnapshotter`:**

Replace count-based tracking with chunk-aware tracking:

**State per manager (keyed by `id(manager)`):**
- `_chunk_event_counts: list[int]` - number of events in each emitted chunk
- `_initialized: bool` - whether first snapshot has been done

**`snapshot_owned()` algorithm:**
```
1. If not initialized:
   - Return stateless_snapshot() (which handles initialization)

2. Consume pending ops from manager via _consume_pending_ops()

3. If no ops:
   - Check if state changed (for state-only updates)
   - Return appropriate delta or no-diff

4. Process ops to compute (chunks_to_pop, suffix_len):
   chunks_to_pop = 0
   suffix_len = 0

   For each op:
     Push(n):
       suffix_len += n

     Pop(n):
       # "Borrow" from chunks while suffix is insufficient
       while suffix_len < n:
         chunks_to_pop += 1
         suffix_len += chunk_event_counts[-chunks_to_pop]
       suffix_len -= n

     Clear:
       # Pop all existing chunks, reset suffix
       chunks_to_pop = len(chunk_event_counts)
       suffix_len = 0

5. Get events to push:
   - If suffix_len > 0: buffer_events = manager.get_events()[-suffix_len:]
   - Else: buffer_events = []

6. Emit delta:
   - If chunks_to_pop > 0 or buffer_events:
       ThingDelta(pop_chunks=chunks_to_pop, push_chunk={events: buffer_events, state, session_id})
   - Else: ThingDelta() (no-diff)

7. Update chunk tracking:
   - Remove `chunks_to_pop` entries from end of _chunk_event_counts
   - If buffer_events: append len(buffer_events) to _chunk_event_counts
```

The key insight: `suffix_len` represents "events we're responsible for pushing". When a pop exceeds it, we pop a chunk and absorb its events into our suffix (semantically a no-op since we'll re-push them).

**`stateless_snapshot()` changes:**
- Call `manager._consume_pending_ops()` to clear any pending ops (they're captured in full state)
- Reset `_chunk_event_counts` to `[len(manager.get_events())]`
- Set `_initialized = True`
- Return `ThingState` as before

## Implementation Order

1. [x] Update `manager.py` - add private events, pending ops, and methods (append_event, append_events, clear_events, pop_events, get_events, _consume_pending_ops)
2. [x] Update `main.py` - change all call sites to use new methods
3. [x] Update `main.py` - add StreamEvent popping logic in run_session()
4. [x] Update `snapshotter.py` - implement op-based delta calculation with pop support
5. [x] Test `/clear` command
6. [x] Test streaming with StreamEvent popping
7. [x] Test `/resume` command (prepend history)
8. [x] Update CODE_OVERVIEW.md

## Design Decisions

- **Always include state/session_id in push_chunk**: Simplifies frontend logic; it always reads from the last chunk
- **First snapshot is always ThingState**: Provides clean baseline; stateless_snapshot clears pending ops
- **Operations are consumed on snapshot**: Pending ops list is cleared when snapshotter reads it; if no snapshot happens, ops accumulate
- **Ops record counts, not events**: The pending ops just track counts (`{"op": "push", "count": N}`), not the actual events. The snapshotter computes a suffix slice from manager.get_events() based on the algorithm.
- **No POP_ALL sentinel**: Clear is handled by setting `chunks_to_pop = len(chunk_event_counts)`, keeping the mechanism consistent
- **suffix_len tracks "events we owe"**: When pop exceeds suffix_len, we borrow from chunks by popping them and adding their size to suffix_len, then subtract the pop amount. This elegantly handles partial chunk pops.
- **StreamEvent trimming**: Pop StreamEvents immediately when a non-stream event arrives, reducing memory and chunk complexity
