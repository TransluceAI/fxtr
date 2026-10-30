# Step 1.5: Thread Safety

Implementation spec for making Watcher thread-safe.

## Overview

Add thread safety to `track()`, `untrack()`, and `tick()` so objects can be tracked and ticks can be recorded from multiple threads.

## Design

### Lock Strategy
- Use `threading.RLock` (reentrant lock)
- RLock is required because weakref finalizers could trigger during GC while lock is held by same thread
- Hold lock for entirety of `track()`, `untrack()`, and `tick()` (including snapshotter execution)

### Scope of Locking

**Protected by lock:**
- `track()` - modifies `_tracked`
- `untrack()` - modifies `_tracked`
- `tick()` - reads/modifies `_tracked`, increments `_tick_counter`, runs snapshotters
- Weakref finalizer - modifies `_tracked`

**NOT protected (by design):**
- `register_snapshotter()` - configuration before multi-threaded use
- `set_tick_interceptor()` - configuration before multi-threaded use
- `get_snapshotter()` - read-only, safe enough under GIL
- `_log_tick()` - no state mutation
- `watch_and_serve_until()` - context management only

### Behavior

- Only one tick at a time per watcher (ticks are serialized)
- Track/untrack blocks while tick is running
- User is responsible for thread-safety of their own objects being snapshotted
- If user needs parallel ticks, they should use multiple watchers

### Limitations

1. Configuration methods should be called before multi-threaded use begins
2. Snapshotters run under the lock - slow snapshotters will block other threads
3. One tick at a time per watcher
4. Future async operations (`pause`, `receive`, DB writes) will NOT hold the lock

---

## Changes Made

### `thingwatcher/watcher.py`

1. Added import: `import threading`

2. In `__init__`, added:
   ```python
   self._lock = threading.RLock()
   ```

3. Wrapped `track()` body with `with self._lock:` (for the `_tracked` modification)

4. Wrapped `untrack()` body with `with self._lock:`

5. Wrapped entire `tick()` body with `with self._lock:`

6. Updated finalizer to acquire lock:
   ```python
   def _remove_on_finalize(ref: weakref.ref[Any]) -> None:
       with self._lock:
           self._tracked.pop(obj_id, None)
   ```

### Tests Added

In `tests/test_tracking.py`:

1. **test_concurrent_track_untrack**: 5 threads each track/untrack 100 objects
2. **test_concurrent_tick**: 5 threads each call tick 20 times, verify all 100 ticks complete with unique numbers
3. **test_track_during_tick**: One thread tracks objects while another ticks, verify no corruption

---

## Exit Criteria

- [x] `track()`, `untrack()`, `tick()` use RLock
- [x] Weakref finalizer acquires lock
- [x] Concurrent tracking tests pass
- [x] Concurrent ticking tests pass
- [x] Mixed concurrent track+tick tests pass
- [x] All existing tests still pass
