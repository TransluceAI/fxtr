# Step 5: Pause/Trigger Mechanism

Implementation spec for the production `pause()` and `receive()` APIs that allow stepping through async code.

## Changes to Earlier Steps

Since Steps 1-3 are already implemented, this step requires some modifications to existing code:

1. **Remove `triggerable` column from `thing_states` table** (`db.py`):
   - The schema currently has `triggerable INTEGER` - this should be removed
   - Remove any code that reads/writes this column
   - Triggerability is now computed dynamically from watcher state

2. **Remove `triggerable` from GraphQL schema** (`schema.graphql`):
   - The `Thing` type has `triggerable: Boolean!` - this field now needs to be computed dynamically by the resolver (checking watcher state) rather than read from DB

3. **Update `write_thing_state` in `db.py`**:
   - Remove the `triggerable` parameter

These changes align the existing implementation with the new design where triggerability is ephemeral watcher state, not persisted data.

---

## Overview

This step implements the pause/trigger mechanism that allows async code to suspend and wait for external input (from the UI or an agent). When a task calls `pause()` or `receive()`, it registers as waiting on an anchor object, and resumes when that anchor is triggered via a GraphQL mutation.

The key insight is that **pause capturing is just another form of tracking**. Anchors are stored similarly to tracked objects, capturers run during the tick snapshot process, and the results are unified with regular snapshots.

## Design Decisions

These decisions were made during planning:

- **GraphQL mutation**: Use `triggerPause(key, payload)` mutation instead of REST endpoint, for consistency with `triggerTick`
- **Use existing lock**: Use `self._lock` (the tick lock) instead of a separate `_active_pauses_lock`, since pause state is part of tick snapshotting
- **Deferred capturing**: Don't run PauseCapturer immediately in `pause()` - store the anchor object with a strong reference, run the capturer during tick snapshot
- **Duplicate detection**: Disallow pausing on the same anchor object twice (by python id), but multiple anchors can produce the same thing_key
- **Unification at snapshot**: During tick, unify results from snapshotters and capturers - same key must have identical ThingState content
- **Triggerable is dynamic**: Not stored in DB; computed from watcher state. Historical ticks always show `triggerable=False`
- **Unification rules**: For each key, exactly one of: (a) one ThingDelta, or (b) one or more identical ThingStates. ThingRefs can reference either. No mixing deltas with states, no multiple deltas.
- **Conflict handling**: Keep original key, change thing_type to `"__broken__"`, set data to error object describing conflict (e.g., "multiple states with different data", "mixture of states and deltas")
- **Orphan ThingRef**: If PauseCapturer returns ThingRef but no snapshotter returned that key, create broken thing with original key
- **Liveness check**: Use `is_paused: bool` flag on PauseInfo. Trigger and triggerability queries re-check this flag.
- **Trigger persisted**: Triggers are stored in `trigger_records` table for historical querying
- **No built-in timeout**: `pause()` waits indefinitely; callers can wrap with `anyio.fail_after()` if needed

---

## Mental Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                         TICK SNAPSHOT                                │
│                                                                      │
│  ┌──────────────────┐          ┌──────────────────┐                 │
│  │  Tracked Objects │          │  Pause Anchors   │                 │
│  │  (weak refs)     │          │  (strong refs)   │                 │
│  └────────┬─────────┘          └────────┬─────────┘                 │
│           │                             │                            │
│           ▼                             ▼                            │
│  ┌──────────────────┐          ┌──────────────────┐                 │
│  │   Snapshotters   │          │  PauseCapturers  │                 │
│  └────────┬─────────┘          └────────┬─────────┘                 │
│           │                             │                            │
│           │    ThingState/ThingDelta    │   ThingState/ThingRef      │
│           └──────────┬──────────────────┘                            │
│                      ▼                                               │
│           ┌──────────────────┐                                       │
│           │   UNIFICATION    │                                       │
│           │  - Group by key  │                                       │
│           │  - Check equal   │                                       │
│           │  - Set triggerable │                                     │
│           └────────┬─────────┘                                       │
│                    ▼                                                 │
│           ┌──────────────────┐                                       │
│           │  Final Snapshot  │  + temp map: key -> paused anchors   │
│           └──────────────────┘                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

### pause() Flow

```
1. Resolve typename for anchor
2. Verify PauseCapturer exists for typename (error if not)
3. Check anchor not already paused (by object id, error if duplicate)
4. Create PauseInfo with Event, is_paused=False, payload_holder=[]
5. Store anchor: _paused_anchors[id(anchor)] = (anchor, typename, pause_info)
6. If tick_after: trigger a tick
7. Set is_paused=True, then await the event
8. In finally: set is_paused=False, remove from _paused_anchors
9. Return payload if receive(), else None
```

### Tick Snapshot Flow

```
1. Acquire _lock
2. Snapshot all tracked objects (existing logic)
3. For each paused anchor:
   - Get PauseCapturer for its typename
   - Run capturer.handle(anchor) -> ThingState or ThingRef
   - If error, create broken thing
4. Build unified view by key:
   - Group all ThingState/ThingRef/ThingDelta results by key
   - For each key, validate:
     - Either exactly one ThingDelta, OR one or more ThingStates
     - If multiple ThingStates, they must be identical
     - ThingRefs just reference - they need a matching state or delta
   - On conflict (mixed delta/state, multiple deltas, different states):
     - Keep original key, set thing_type="__broken__", data={"error": "description"}
   - On orphan ThingRef (no matching state/delta):
     - Keep original key, set thing_type="__broken__", data={"error": "ThingRef has no matching state"}
   - Mark triggerable if any came from pause capturer
5. Build temp map: thing_key_bytes -> list of (anchor_id, pause_info) for live pauses
6. Store temp map as _triggerable_at_tick for trigger lookups
7. Return unified snapshot
```

### Trigger Flow (GraphQL Mutation)

```
1. triggerPause(key: String!, payload: JSON) mutation
2. Decode key to key_bytes
3. Look up _triggerable_at_tick[key_bytes]
4. Filter to entries where pause_info.is_paused == True
5. If none: error "No active pause for key"
6. For each live pause_info:
   - Store payload in payload_holder
   - Set event
7. Write trigger_record to DB
8. Return trigger record info
```

---

## Database Schema Addition

### trigger_records table

```sql
CREATE TABLE trigger_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thing_key BLOB NOT NULL,        -- The triggered thing's key (CBOR canonical)
    thingstate_id INTEGER,          -- The thingstate that was active at trigger time (nullable if thing didn't exist)
    trigger_tick INTEGER NOT NULL,  -- The tick number at trigger time
    payload BLOB,                   -- CBOR-encoded payload (nullable)
    FOREIGN KEY (trigger_tick) REFERENCES ticks(tick)
);

CREATE INDEX idx_trigger_records_key ON trigger_records(thing_key);
```

Note: `triggerable` column is NOT added to `thing_states` table - it's computed dynamically.

---

## GraphQL Schema Changes

```graphql
type Mutation {
  triggerTick(level: String = "info", desc: String = ""): Tick!

  # NEW: Trigger a paused thing
  triggerPause(key: String!, payload: JSON): TriggerResult!
}

type Query {
  # ... existing queries ...

  # Trigger history
  triggerRecords(key: String, fromTick: Int, toTick: Int): [TriggerRecord!]!
  triggerRecord(id: Int!): TriggerRecord

  # NEW: Get currently triggerable things (at latest tick)
  triggerableThings: [TriggerableThing!]!
}

# NEW types
type TriggerResult {
  ok: Boolean!
  triggerId: Int           # ID of the trigger record created
  error: String            # Error message if ok=false
}

type TriggerRecord {
  id: Int!
  thingKey: String!        # Base64-encoded CBOR
  thingstateId: Int        # May be null if thing didn't exist in DB
  triggerTick: Int!
  payload: JSON            # Decoded CBOR payload
}

type TriggerableThing {
  key: String!             # Base64-encoded CBOR
  thingType: String!
}
```

Also modify `Thing` type behavior:
- `triggerable: Boolean!` returns True only for current tick if thing is triggerable, False otherwise

---

## Watcher State Additions

```python
@dataclass
class PauseInfo:
    """Tracking info for an active pause."""
    event: Event                    # Event to signal on trigger
    payload_holder: list[Any]       # Mutable holder for payload
    is_paused: bool                 # True while actually awaiting
    typename: str                   # Resolved typename for capturer lookup


@dataclass
class TriggerResult:
    """Result of a trigger_pause call."""
    ok: bool
    trigger_id: int | None = None   # ID of trigger record if successful
    error: str | None = None        # Error message if failed

# In Watcher.__init__:

# Paused anchors: id(anchor) -> (anchor_obj, PauseInfo)
# Strong reference to anchor to keep it alive during pause
self._paused_anchors: dict[int, tuple[Any, PauseInfo]] = {}

# Temp map rebuilt each tick: thing_key_bytes -> list of (anchor_id, PauseInfo)
# Used for trigger lookups between ticks
self._triggerable_at_tick: dict[bytes, list[tuple[int, PauseInfo]]] = {}
```

---

## Implementation Details

### File: `thingwatcher/watcher.py` - pause/receive

```python
async def pause(self, anchor, typename=None, tick_after=True):
    await self._pause_or_receive(anchor, typename, tick_after, want_payload=False)

async def receive(self, anchor, typename=None, tick_after=True):
    return await self._pause_or_receive(anchor, typename, tick_after, want_payload=True)

async def _pause_or_receive(self, anchor, typename, tick_after, want_payload):
    # Resolve typename
    resolved = resolve_typename(typename if typename else type(anchor))

    # Verify capturer exists
    capturer = get_pause_capturer(resolved)
    if capturer is None:
        raise ValueError(f"no pause capturer registered for {resolved}")

    anchor_id = id(anchor)

    # Check not already paused (under lock)
    with self._lock:
        if anchor_id in self._paused_anchors:
            raise ValueError("cannot pause on same anchor object twice")

        # Create pause info and register
        event = Event()
        pause_info = PauseInfo(
            event=event,
            payload_holder=[],
            is_paused=False,
            typename=resolved,
        )
        self._paused_anchors[anchor_id] = (anchor, pause_info)

    try:
        # Tick if requested (builds _triggerable_at_tick including this pause)
        if tick_after:
            self.tick("pause", f"Paused: {resolved}")

        # Mark as paused and await
        pause_info.is_paused = True
        await event.wait()

        # Return payload
        if want_payload:
            return pause_info.payload_holder[0] if pause_info.payload_holder else None
        return None
    finally:
        # Cleanup
        pause_info.is_paused = False
        with self._lock:
            self._paused_anchors.pop(anchor_id, None)
```

### File: `thingwatcher/watcher.py` - Modified _take_snapshot

```python
def _take_snapshot(self, level, desc, tick_type="api_tick", on_complete=None):
    with self._lock:
        # ... existing tick counter and timestamp ...

        # 1. Snapshot tracked objects (existing logic)
        tracked_results: list[tuple[Any, ThingState|ThingDelta]] = []
        for obj_id, tracked in list(self._tracked.items()):
            obj = tracked.obj_ref()
            if obj is None:
                del self._tracked[obj_id]
                continue
            snapshotter = self.get_snapshotter(tracked.typename)
            # ... run snapshotter, handle errors, collect results ...

        # 2. Capture pause anchors
        pause_results: list[tuple[int, ThingState|ThingRef]] = []  # (anchor_id, result)
        for anchor_id, (anchor, pause_info) in self._paused_anchors.items():
            capturer = get_pause_capturer(pause_info.typename)
            if capturer is None:
                # Create broken thing
                pause_results.append((anchor_id, _make_broken_pause(anchor_id, tick_num, "capturer removed")))
                continue
            try:
                result = capturer.handle(anchor)
                pause_results.append((anchor_id, result))
            except Exception as e:
                pause_results.append((anchor_id, _make_broken_pause(anchor_id, tick_num, str(e))))

        # 3. Unify by key
        unified, triggerable_map = self._unify_snapshots(
            tick_num, tracked_results, pause_results
        )

        # 4. Store triggerable map for trigger lookups
        self._triggerable_at_tick = triggerable_map

        return SnapshotResult(
            tick_number=tick_num,
            timestamp=timestamp,
            level=level,
            desc=desc,
            states=unified,
            tick_type=tick_type,
            on_complete=on_complete,
        )

def _unify_snapshots(self, tick_num, tracked_results, pause_results):
    """Unify tracked and pause results, checking for conflicts.

    Rules:
    - For each key: either exactly one ThingDelta, OR one or more identical ThingStates
    - ThingRefs just reference - need a matching state or delta to exist
    - Conflicts produce a broken thing with original key, type="__broken__", data=error
    """

    # Group by key
    by_key: dict[bytes, list[ThingState|ThingDelta]] = {}
    pause_keys: dict[bytes, list[tuple[int, PauseInfo]]] = {}  # key -> [(anchor_id, pause_info)]
    ref_keys: set[bytes] = set()  # keys referenced by ThingRefs

    # Process tracked results
    for _, item in tracked_results:
        if isinstance(item, (ThingState, ThingDelta)):
            key_bytes = cbor2.dumps(item.key, canonical=True)
            by_key.setdefault(key_bytes, []).append(item)

    # Process pause results
    for anchor_id, item in pause_results:
        anchor, pause_info = self._paused_anchors[anchor_id]

        if isinstance(item, ThingRef):
            key_bytes = cbor2.dumps(item.key, canonical=True)
            ref_keys.add(key_bytes)
            pause_keys.setdefault(key_bytes, []).append((anchor_id, pause_info))
        else:  # ThingState (pause capturers don't return ThingDelta)
            key_bytes = cbor2.dumps(item.key, canonical=True)
            by_key.setdefault(key_bytes, []).append(item)
            pause_keys.setdefault(key_bytes, []).append((anchor_id, pause_info))

    # Check for orphan ThingRefs and add broken things
    for key_bytes in ref_keys:
        if key_bytes not in by_key:
            # Create broken thing with original key
            original_key = cbor2.loads(key_bytes)
            broken = ThingState(
                key=original_key,
                thing_type="__broken__",
                thing_data={"error": "ThingRef has no matching state"},
                links=[],
            )
            by_key[key_bytes] = [broken]

    # Validate and build final list
    final_states: list[ThingState|ThingDelta] = []
    for key_bytes, items in by_key.items():
        thing_states = [i for i in items if isinstance(i, ThingState)]
        thing_deltas = [i for i in items if isinstance(i, ThingDelta)]
        original_key = cbor2.loads(key_bytes)

        # Check for invalid combinations
        error_msg = None

        if thing_states and thing_deltas:
            error_msg = "mixture of ThingState and ThingDelta for same key"
        elif len(thing_deltas) > 1:
            error_msg = "multiple ThingDeltas for same key"
        elif len(thing_states) > 1:
            # Check all states are equal
            first = thing_states[0]
            for other in thing_states[1:]:
                if not _thing_states_equal(first, other):
                    error_msg = "multiple ThingStates with different data for same key"
                    break

        if error_msg:
            # Create broken thing with original key
            logger.error(f"Snapshot conflict at tick {tick_num}: {error_msg}")
            broken = ThingState(
                key=original_key,
                thing_type="__broken__",
                thing_data={"error": error_msg},
                links=[],
            )
            final_states.append(broken)
        elif thing_deltas:
            # Exactly one delta
            final_states.append(thing_deltas[0])
        elif thing_states:
            # One or more identical states - use first
            final_states.append(thing_states[0])

    return final_states, pause_keys
```

### File: `thingwatcher/watcher.py` - Trigger Method

```python
async def trigger_pause(self, key_bytes: bytes, payload: Any) -> TriggerResult:
    """Trigger pauses for a thing key. Returns TriggerResult."""

    with self._lock:
        pause_list = self._triggerable_at_tick.get(key_bytes, [])

        # Filter to actually paused entries
        live_pauses = [(aid, pi) for aid, pi in pause_list if pi.is_paused]

        if not live_pauses:
            return TriggerResult(ok=False, error="No active pause for this key")

        # Trigger all
        for anchor_id, pause_info in live_pauses:
            pause_info.payload_holder.append(payload)
            pause_info.event.set()

        # Clear from triggerable map (already triggered this tick)
        self._triggerable_at_tick[key_bytes] = [
            (aid, pi) for aid, pi in pause_list if not pi.is_paused
        ]

    # Write trigger record to DB
    async with self._db.begin() as conn:
        latest_tick = await self._db.get_latest_tick(conn) or 0
        thing = await self._db.get_thing_data(conn, latest_tick, key_bytes)
        thingstate_id = thing.id if thing else None

        record_id = await self._db.write_trigger_record(
            conn, key_bytes, thingstate_id, latest_tick, payload
        )

    return TriggerResult(ok=True, trigger_id=record_id)
```

### File: `thingwatcher/server.py` - GraphQL Resolver

```python
@mutation.field("triggerPause")
async def resolve_trigger_pause(_, info, key: str, payload=None):
    watcher: Watcher = info.context["watcher"]
    key_bytes = decode_key(key)
    result = await watcher.trigger_pause(key_bytes, payload)
    # Convert TriggerResult dataclass to dict for GraphQL
    return {
        "ok": result.ok,
        "triggerId": result.trigger_id,
        "error": result.error,
    }

@query.field("triggerableThings")
async def resolve_triggerable_things(_, info):
    watcher: Watcher = info.context["watcher"]
    result = []
    with watcher._lock:
        for key_bytes, pause_list in watcher._triggerable_at_tick.items():
            live = [pi for _, pi in pause_list if pi.is_paused]
            if live:
                # Get thing type from DB or pause info
                # ... lookup thing_type ...
                result.append({
                    "key": encode_key(key_bytes),
                    "thingType": thing_type,
                })
    return result
```

### File: `thingwatcher/server.py` - Modified Thing.triggerable resolver

```python
# When resolving Thing.triggerable for thingsAtTick:
# Only return True if:
# 1. This is the latest tick
# 2. The key is in watcher._triggerable_at_tick with is_paused=True entries

def resolve_thing_triggerable(thing_dict, info, tick):
    watcher: Watcher = info.context["watcher"]
    # Check if this is latest tick
    # Check if key is in _triggerable_at_tick with live pauses
    # Return True only if both conditions met
```

---

## Edge Cases

### Unification Conflicts

For any conflict (different ThingStates, mixed states/deltas, multiple deltas):
- Keep original key
- Set `thing_type = "__broken__"`
- Set `thing_data = {"error": "description of conflict"}`
- Log error message
- Thing is still triggerable if any pause capturer produced it (broken things can be triggered to unblock)

### Orphan ThingRef

When PauseCapturer returns ThingRef for a key with no ThingState/ThingDelta:
- Keep original key
- Set `thing_type = "__broken__"`
- Set `thing_data = {"error": "ThingRef has no matching state"}`
- Thing is still triggerable

### Cancellation

When task calling pause/receive is cancelled:
- `finally` block sets `is_paused = False` and removes from `_paused_anchors`
- Trigger lookups re-check `is_paused`, so will see no live pauses
- Next tick won't include this anchor in snapshot

### Historical Triggerability

- `Thing.triggerable` always returns False for non-latest ticks
- Only current watcher state determines triggerability
- This matches reality: you can only trigger things that are actually waiting

---

## Tests: `tests/test_pause.py`

### Test: basic pause and trigger
```
- Register PauseCapturer that returns ThingState
- Start task that calls pause(anchor)
- Query thingsAtTick - verify thing present with triggerable=True
- Call triggerPause mutation
- Verify task resumes
- Verify thing gone from next tick (or triggerable=False)
```

### Test: receive returns payload
```
- Start task that calls receive(anchor)
- Call triggerPause(key, payload={"value": 42})
- Verify receive() returns {"value": 42}
```

### Test: trigger record persisted
```
- Complete a pause/trigger cycle
- Query triggerRecords via GraphQL
- Verify record exists with correct payload
```

### Test: ThingRef marks existing thing triggerable
```
- Track an object normally
- Register PauseCapturer that returns ThingRef(object.key)
- Call pause() with different anchor that produces same key
- Verify existing thing has triggerable=True
- Trigger it, verify both pause resumes
```

### Test: duplicate anchor raises error
```
- Call pause(anchor)
- Try to call pause(anchor) again with same object
- Verify ValueError raised
```

### Test: multiple anchors same key
```
- Two different anchors that produce same key via capturer
- Both call pause()
- Single trigger wakes both
```

### Test: key conflict creates broken thing
```
- Anchor A produces ThingState(key=K, data="x")
- Anchor B produces ThingState(key=K, data="y")
- Both pause
- Tick produces single broken thing with:
  - key=K (original key preserved)
  - thing_type="__broken__"
  - thing_data={"error": "multiple ThingStates with different data..."}
- Broken thing is still triggerable
```

### Test: mixed delta/state creates broken thing
```
- Tracked object produces ThingDelta(key=K)
- Anchor produces ThingState(key=K)
- Tick produces broken thing with error "mixture of ThingState and ThingDelta"
```

### Test: orphan ThingRef creates broken thing
```
- PauseCapturer returns ThingRef for untracked key K
- Tick produces broken thing with:
  - key=K (original key preserved)
  - thing_type="__broken__"
  - thing_data={"error": "ThingRef has no matching state"}
- Broken thing is still triggerable
```

### Test: cancellation cleanup
```
- Start task that calls pause()
- Cancel the task
- Verify triggerPause returns error
- Verify anchor removed from state
```

### Test: historical triggerable is False
```
- Pause, tick, trigger
- Query thingsAtTick for old tick
- Verify triggerable=False
```

### Test: triggerableThings query
```
- Multiple pauses active
- Query triggerableThings
- Verify returns correct list
```

---

## Implementation Order

1. Add `trigger_records` table to `db.py` (no `triggerable` column on thing_states)
2. Add DB methods for trigger records
3. Add `PauseInfo` dataclass and watcher state (`_paused_anchors`, `_triggerable_at_tick`)
4. Implement `_pause_or_receive()` in watcher
5. Modify `_take_snapshot()` for unification logic
6. Add `trigger_pause()` method to watcher
7. Update GraphQL schema with mutation and new types
8. Add GraphQL resolvers
9. Modify `Thing.triggerable` resolution for dynamic check
10. Update `api.py` pause/receive to delegate to watcher
11. Write tests
12. Run tests, iterate

---

## Exit Criteria

- [ ] `pause(anchor)` suspends until triggered
- [ ] `receive(anchor)` suspends and returns trigger payload
- [ ] Pause things appear in ticks, computed as triggerable dynamically
- [ ] `triggerPause` mutation wakes waiting tasks
- [ ] Multiple anchors with same key all wake on single trigger
- [ ] Trigger records are persisted to DB
- [ ] `triggerRecords` query returns trigger history
- [ ] `triggerableThings` query returns currently triggerable things
- [ ] Duplicate pause on same anchor object raises error
- [ ] Key conflicts produce broken thing (original key, type=`__broken__`, data=error)
- [ ] Mixed delta/state produces broken thing
- [ ] Multiple deltas produces broken thing
- [ ] Orphan ThingRef produces broken triggerable thing
- [ ] Cancellation cleans up pause state correctly
- [ ] Historical ticks show `triggerable=False`
- [ ] All existing tests still pass
- [ ] New pause tests pass

---

## Files to Modify

- `thingwatcher/db.py` - Add trigger_records table and methods
- `thingwatcher/watcher.py` - Add pause/receive, modify tick snapshot, add trigger method
- `thingwatcher/server.py` - Add GraphQL resolvers, modify triggerable resolution
- `thingwatcher/schema.graphql` - Add triggerPause mutation, TriggerResult, TriggerRecord, triggerableThings
- `thingwatcher/api.py` - Update pause/receive to delegate to watcher
- `tests/test_pause.py` - New test file
