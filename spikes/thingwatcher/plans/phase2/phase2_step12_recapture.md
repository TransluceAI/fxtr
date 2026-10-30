# Phase 2 Step 12: Hard Recapture

## Overview

Add a mechanism to do a complete reset and recapture of all tracked state. This is useful when the watcher's internal state may have drifted from the actual state of tracked objects (e.g., due to bugs, external changes, or to force a clean baseline).

**Two features:**
1. **Hard recapture mutation**: A GraphQL mutation (`hardRecapture`) that invalidates all live DB state, clears all in-memory tracking, and forces a fresh stateless snapshot of everything. Triggered in the UI by holding Option while clicking the manual tick button.

2. **Post-trigger tick**: Automatically do a regular tick immediately after `triggerPause` succeeds, ensuring triggers create a snapshot point (important in the presence of streaming updates which could otherwise obscure the trigger moment).

## Design

### Hard Recapture Behavior

When `hardRecapture()` is called:

1. **Clear all in-memory tracking state:**
   - `_current_things` → empty (forget what states we think exist in DB)
   - `_current_roots` → empty (forget root bindings)
   - `_owned_things` → empty (forget ownership tracking)
   - `_thing_owners` → empty (forget reverse ownership)
   - `_stateless_keys` → empty (forget stateless key tracking)
   - `_current_tick_type` → None (reset stream state)

2. **Invalidate all live records in DB:**
   - All `thing_states` with `valid_until IS NULL` → set `valid_until` to the recapture tick
   - All `links` with `valid_until IS NULL` → set `valid_until` to the recapture tick
   - All `roots` with `valid_until IS NULL` → set `valid_until` to the recapture tick

3. **Force stateless snapshot:**
   - Every `StatefulOwningSnapshotter` must provide a `stateless_snapshot()` method
   - During hard recapture, use `stateless_snapshot()` instead of `snapshot_owned()` so we get fresh ThingStates only (no deltas referencing previous state)

4. **Record as special tick type:**
   - Tick type = `"hard_recapture"` to distinguish from normal ticks

The result is that every tracked object gets a completely fresh snapshot as if it just started being tracked.

### StatefulOwningSnapshotter Extension

```python
class StatefulOwningSnapshotter(ABC):
    @abstractmethod
    def snapshot_owned(self, obj: Any) -> ThingState | ThingDelta | list[ThingState | ThingDelta]:
        """Normal snapshot - can return states or deltas."""
        ...

    @abstractmethod
    def stateless_snapshot(self, obj: Any) -> ThingState | list[ThingState]:
        """Stateless snapshot for hard recapture - must return only ThingStates.

        This is called during hard recapture to get a fresh baseline.
        Implementation should return the same keys as snapshot_owned() would,
        but always as complete ThingStates rather than deltas.
        """
        ...
```

This is a breaking change for existing `StatefulOwningSnapshotter` implementations - they must now implement both methods.

### Post-Trigger Tick

The `trigger_pause()` flow changes to:
1. Set payloads on live pauses (but don't wake them yet)
2. **Do a tick** with level=`"post_trigger"`, type=`"post_trigger"` (captures state at trigger moment)
3. Write trigger record to DB (associated with the post_trigger tick)
4. Set the event (wake the paused task)

The tick happens *before* the trigger record is written, so the trigger record is associated with the new post_trigger tick (not the previous tick). This ensures the frontend fetches fresh trigger data when viewing the post_trigger tick.

Post-trigger ticks look like normal ticks in the timeline (no special styling).

This ensures:
- The trigger moment has a clear snapshot point in the timeline
- Trigger records are associated with the tick they created (avoids caching issues)
- Streaming updates don't obscure when the trigger happened
- The triggered task's state changes appear in subsequent ticks, not mixed into the trigger tick

### GraphQL Changes

```graphql
type Mutation {
  # ... existing mutations ...

  # Hard recapture - reset all state and recapture fresh
  hardRecapture: Tick!
}
```

### Frontend Changes

**Manual tick button behavior:**
- Normal click → `triggerTick` mutation (existing)
- Option+click (Mac) / Alt+click (Windows/Linux) → `hardRecapture` mutation

**Visual feedback:**
- When Option/Alt is held, the tick button changes appearance (e.g., different color, icon, or label) to indicate it will do something different
- Release modifier → button returns to normal appearance
- This lets users preview the action before clicking

**Timeline styling:**
- Hard recapture ticks (`type === "hard_recapture"`) appear orange in the timeline
- Distinguishes them from normal ticks and stream ticks (dotted)

## Implementation Steps

### Backend

- [x] **1. Extend StatefulOwningSnapshotter ABC**
  - Add abstract `stateless_snapshot()` method
  - Update docstrings explaining when each method is used

- [x] **2. Update existing StatefulOwningSnapshotter implementations**
  - Update snapshotters in examples (demo_multi_chunk, etc.)
  - Each must implement `stateless_snapshot()` returning ThingStates only

- [x] **3. Add DB method to bulk-invalidate live records**
  - `async def invalidate_all_live(self, conn, tick: int, seq: int) -> None`
  - Invalidates all thing_states, links, and roots with `valid_until IS NULL`

- [x] **4. Implement `_hard_recapture()` in Watcher**
  - Clear all in-memory tracking maps
  - Call `invalidate_all_live()`
  - Take snapshot using `stateless_snapshot()` for owning snapshotters
  - Create tick with type `"hard_recapture"`
  - Process normally through `_process_snapshot()`

- [x] **5. Add `hard_recapture()` public API**
  - `async def hard_recapture(self) -> dict[str, Any]` on Watcher
  - Returns tick data like `_async_tick()`

- [x] **6. Add GraphQL mutation**
  - Add `hardRecapture: Tick!` to schema
  - Add resolver that calls `watcher.hard_recapture()`

- [x] **7. Implement post-trigger tick**
  - Modify `trigger_pause()` to do tick AFTER writing trigger record but BEFORE setting event
  - Use level=`"post_trigger"`, type=`"post_trigger"`, and include triggered thing info in description
  - This requires making `trigger_pause` do the tick synchronously before waking the task

- [x] **8. Write tests**
  - Test hard recapture clears in-memory state
  - Test hard recapture invalidates all DB records
  - Test hard recapture creates fresh snapshots
  - Test owning snapshotters use `stateless_snapshot()` during recapture
  - Test hard recapture tick has correct type (`"hard_recapture"`)
  - Test post-trigger tick is created with type `"post_trigger"`
  - Test post-trigger tick happens BEFORE paused task resumes (use debug_receive to verify ordering)

### Frontend

- [x] **9. Update Timeline tick button with modifier key detection**
  - Track Option/Alt key state (keydown/keyup on window)
  - When modifier held: change button appearance (color, label like "Hard Recapture")
  - When modifier released: return to normal appearance
  - On click: check modifier state and call appropriate mutation

- [x] **10. Add hardRecapture mutation to GraphQL queries**
  - Add mutation definition and useHardRecapture hook in useTrigger.ts
  - Export from hooks/index.ts
  - Wire up to button click handler in Timeline.tsx

- [x] **11. Style hard_recapture ticks in Timeline**
  - Add `.hardRecaptureMode` CSS class for capture button (orange)
  - Orange color for hard_recapture tick markers via data-level
  - Orange level badge styling

- [x] **12. Update CODE_OVERVIEW.md**

## Edge Cases

### Objects Added During Recapture

If an object is tracked while recapture is in progress, it will be included in the snapshot normally. The clear happens before snapshot.

### Streaming Updates During Recapture

Since recapture clears `_current_tick_type`, any streaming updates after recapture will create a new stream tick. The recapture tick itself is not a stream tick.

### Multiple Snapshotters for Same Object

The logic is the same as normal snapshot - stateless snapshotters use their regular `snapshot()`, only `StatefulOwningSnapshotter` switches to `stateless_snapshot()`.

### Anchor Capturers

Anchor capturers (for pauses and roots) always produce ThingState or ThingRef, so they're already stateless. No changes needed.

### Active Pauses During Recapture

Active pauses (`_paused_anchors`) remain paused during hard recapture. Their anchor capturers are re-run to capture fresh state, but the paused tasks are not woken. The recapture is purely a state reset, not an execution reset.

### Empty Tracked Set

If no objects are tracked, hard recapture still:
- Invalidates any remaining DB records (cleanup)
- Creates a recapture tick (for audit trail)
- Results in an empty state snapshot

## Notes

- Hard recapture is intentionally rare - it's a "break glass" operation
- The UI hint (Option+click) should be discoverable but not accidental
- Post-trigger tick ensures trigger boundaries are visible in the timeline even with streaming
