# Step 1: Core Tracking + Log Output

Implementation spec for the core tracking mechanism with JSON log output.

## Overview

This step implements the fundamental tracking loop:
1. User calls `track(obj)` to register objects
2. User calls `tick(level, desc)` to snapshot all tracked objects
3. Watcher invokes registered snapshotters and logs JSON output

No persistence, no server—just the core model working end-to-end.

---

## Data Structures

### TrackedObject (internal)
```
typename: str           # resolved type name
obj_ref: weakref.ref    # weak reference to the object
```

### SnapshotResult (internal, for tick output)
```
tick_number: int
timestamp: float
level: str
desc: str | None
states: list[ThingState | ThingDelta]  # includes broken things
```

---

## File: `thingwatcher/context.py`

Global context management using contextvars.

```
_current_watcher: ContextVar[Watcher | None] = ContextVar(default=None)

get_current_watcher() -> Watcher | None
set_current_watcher(w: Watcher | None) -> Token
```

Simple get/set for the context variable. The token is returned so `watch_and_serve_until` can restore the previous state on exit.

---

## File: `thingwatcher/registry.py`

Global registries for snapshotters and rendering config. Separate from watcher so they can be configured before watcher exists.

```
_snapshotters: dict[str, StateSnapshotter] = {}
_rendering_config: dict[str, RenderingConfig] = {}

register_snapshotter(typename: str | type, snapshotter: StateSnapshotter)
    - if type, convert to "module.qualname" string
    - store in _snapshotters

get_snapshotter(typename: str) -> StateSnapshotter | None

register_rendering(...) / get_rendering_config(...)
    - similar pattern, stored for later use by UI
```

---

## File: `thingwatcher/watcher.py`

The main Watcher class.

### State
```
_tracked: dict[int, TrackedObject]  # keyed by id(obj)
_tick_counter: int = 0
_local_snapshotters: dict[str, StateSnapshotter]  # watcher-local overrides
_tick_interceptor: Callable[[SnapshotResult], None] | None  # for testing
_db: Database | None = None  # for Step 2
_write_queue: Queue | None = None  # for Step 2
```

### Methods

**`__init__(db_path=None, port=8765)`**
```
self._tracked = {}
self._tick_counter = 0
self._local_snapshotters = {}
self._tick_interceptor = None
self.db_path = db_path
self.port = port
```

**`register_snapshotter(typename, snapshotter)`**
```
# Watcher-local registration (overrides global)
resolve typename to string if it's a type
self._local_snapshotters[typename] = snapshotter
```

**`get_snapshotter(typename) -> StateSnapshotter | None`**
```
# Check local first, then global
if typename in self._local_snapshotters:
    return self._local_snapshotters[typename]
return registry.get_snapshotter(typename)
```

**`set_tick_interceptor(fn: Callable[[SnapshotResult], None] | None)`**
```
# For testing: intercept tick results before logging
self._tick_interceptor = fn
```

**`track(obj, typename=None)`**
```
resolve typename:
    if typename is None:
        typename = type(obj).__module__ + "." + type(obj).__qualname__
    elif isinstance(typename, type):
        typename = ... same resolution ...

# Validate snapshotter exists at track time
snapshotter = self.get_snapshotter(typename)
if snapshotter is None:
    raise ValueError(f"no snapshotter registered for {typename}")

create weak reference with ensure obj is weak-referenceable:
    try:
        ref = weakref.ref(obj, finalizer_callback)
    except TypeError:
        raise ValueError("object must be weakref-referenceable")

store TrackedObject in self._tracked[id(obj)]
```

The finalizer callback removes the entry from `_tracked` when the object is GC'd.

**`untrack(obj)`**
```
del self._tracked[id(obj)]  # KeyError if not tracked, that's fine
```

**`tick(level, desc=None)`**
```
self._tick_counter += 1
tick_num = self._tick_counter
timestamp = time.time()

states = []
for obj_id, tracked in list(self._tracked.items()):
    obj = tracked.obj_ref()
    if obj is None:
        # already finalized, clean up and skip
        del self._tracked[obj_id]
        continue

    # Snapshotter guaranteed to exist (checked at track time)
    # but could have been unregistered, so handle gracefully
    snapshotter = self.get_snapshotter(tracked.typename)
    if snapshotter is None:
        states.append(make_broken_thing(obj_id, tick_num, "snapshotter removed for " + tracked.typename))
        continue

    try:
        result = snapshotter.snapshot(obj)
        if isinstance(result, list):
            states.extend(result)
        else:
            states.append(result)
    except Exception as e:
        states.append(make_broken_thing(obj_id, tick_num, str(e)))

result = SnapshotResult(tick_num, timestamp, level, desc, states)

# Call interceptor if set (for testing)
if self._tick_interceptor is not None:
    self._tick_interceptor(result)

self._log_tick(result)

# Step 2 will add: self._enqueue_write(result)
```

**`make_broken_thing(obj_id, tick_num, error_msg) -> ThingState`**
```
return ThingState(
    key=("__broken__", obj_id, tick_num),
    thing_type="__broken__",
    thing_data={"error": error_msg},
    links=[]
)
```

**`_log_tick(result: SnapshotResult)`**
```
import json, logging
logger = logging.getLogger("thingwatcher")

output = {
    "tick": result.tick_number,
    "timestamp": result.timestamp,
    "level": result.level,
    "desc": result.desc,
    "things": [serialize_thing(s) for s in result.states]
}
logger.info(json.dumps(output))
```

Where `serialize_thing` converts ThingState/ThingDelta to a dict. Keys that aren't JSON-serializable get repr'd for logging purposes (full CBOR comes in Step 2).

**`watch_and_serve_until(done: Callable[[], Awaitable])`**
```
token = set_current_watcher(self)
try:
    return await done()
finally:
    set_current_watcher(token)  # restore previous
```

For Step 1, this just sets context and runs the coroutine. Steps 2-3 will add DB and server startup here.

---

## File: `thingwatcher/api.py`

Update the stub implementations to delegate to context.

**`track(obj, typename=None)`**
```
watcher = get_current_watcher()
if watcher is not None:
    watcher.track(obj, typename)
# else: no-op
```

**`untrack(obj)`**
```
watcher = get_current_watcher()
if watcher is not None:
    watcher.untrack(obj)
```

**`tick(level, desc=None)`**
```
watcher = get_current_watcher()
if watcher is not None:
    watcher.tick(level, desc)
```

**`register_snapshotter(typename, snapshotter)`**
```
# delegate to registry (not watcher-specific)
from .registry import register_snapshotter as _register
_register(typename, snapshotter)
```

Same pattern for `register_pause_capturer` and `configure_rendering`.

---

## File: `thingwatcher/__init__.py`

Ensure all public API is exported:
```
from .api import (
    track, untrack, tick, pause, receive,
    register_snapshotter, register_pause_capturer, configure_rendering,
    ThingState, ThingDelta, ThingLink, ThingRef,
    StateSnapshotter, PauseCapturer,
    Watcher, NoWatcherConnectedError,
)
```

---

## File: `tests/test_tracking.py`

### Test: basic tracking and tick
```
- Create a simple Counter class with value attribute
- Create a CounterSnapshotter that returns ThingState with key=id(obj), data={"value": obj.value}
- Create Watcher
- watcher.register_snapshotter(Counter, CounterSnapshotter())
- Set up interceptor to capture ticks: captured = []; watcher.set_tick_interceptor(captured.append)
- Use watch_and_serve_until
- Inside: create Counter, track it, tick
- Assert captured[0] has expected tick_number, level, states
```

### Test: watcher-local snapshotter overrides global
```
- Register global snapshotter for SomeType
- Create Watcher, register different local snapshotter for SomeType
- Track object, tick
- Assert the local snapshotter was used (via interceptor)
```

### Test: weakref cleanup
```
- Track an object
- Delete the object, force gc
- Tick should not crash, object should not appear in output
```

### Test: snapshotter error handling
```
- Register a snapshotter that raises
- Track object, tick
- Output should contain broken thing with error message
```

### Test: track without snapshotter raises
```
- Try to track object with no registered snapshotter
- Assert raises ValueError with "no snapshotter registered"
```

### Test: untrack
```
- Track object, tick (appears in interceptor), untrack, tick (doesn't appear)
```

### Test: no watcher active
```
- Call module-level track/tick without watcher -> no-op, no crash
```

---

## Implementation Order

1. Create `thingwatcher/context.py` with context variable
2. Create `thingwatcher/registry.py` with snapshotter registry
3. Create `thingwatcher/watcher.py` with Watcher class
4. Update `thingwatcher/api.py` to wire everything up
5. Update `thingwatcher/__init__.py` exports
6. Write tests in `tests/test_tracking.py`
7. Run tests, iterate

---

## Dependencies

None new for Step 1. Uses only stdlib (weakref, contextvars, json, logging, time).

---

## Exit Criteria

- `track`, `untrack`, `tick` work with active watcher
- `track` raises if no snapshotter is registered for the typename
- Watcher-local snapshotters override global registry
- Snapshotters are invoked on tick
- JSON is logged with tick info + thing states
- Snapshotter errors produce broken thing states (not crash)
- Objects are automatically untracked on GC
- Tick interceptor can capture results for testing
- No-op behavior when no watcher is active (module-level API)
- All tests pass
