# Stage 5 Part 5: Discard Notification for Local Handles

## Goal

Add a mechanism to `LocalRPCContext` / `LocalHandle` for notifying the application when all
references to a wrapped handle have been discarded, or when the context tears down. This is
needed by the fxtr entity bridge (stage 6) to clean up per-call scopes when the caller releases
the corresponding pending, but is a general-purpose Cap-IR enhancement useful for any resource
lifecycle management.

## Motivation

When the fxtr bridge wraps an arena as a `LocalHandle` capability, it needs to know when the
remote side is done with that arena so the per-call scope can be closed. Currently,
`LocalHandle.discard()` sets `_discarded = True` and fires the teardown event, but there is no
way for the creator of the handle to be notified.

The transport layer handles this for remote handles: when the remote side releases all
references, a `["release"]` message arrives and the export entry is cleaned up. But for locally
wrapped handles, there is no equivalent notification. The wrapped object is silently dropped.

The consumer generally wants to know "this object is no longer reachable through RPC" regardless
of whether that's because all clones were explicitly discarded or the context tore down. So we
notify on both events, with a flag to distinguish them.

## Design

### Option A: Callback on `wrap()`

Add an optional `on_released` callback to `LocalRPCContext.wrap()`:

```python
def wrap(
    self,
    obj: Any,
    on_released: Callable[[str], None] | None = None,
) -> LocalHandle:
    ...
```

The callback receives a string indicating the reason: `"discarded"` when all clones have been
explicitly discarded, or `"teardown"` when the context exits while clones still exist.

### Option B: Method on the wrapped object

If the wrapped object has a `_on_all_refs_discarded()` method, `LocalHandle` calls it when
refcount reaches 0.

### Recommendation: Option A

Option A is cleaner — it doesn't couple the wrapped object's interface to Cap-IR lifecycle
management. The callback is provided by the code that creates the handle, which is the code
that needs to react to release.

## Implementation

### `_HandleRefTracker`

Shared refcount tracker across all clones of a handle. The context owns a registration set of
active trackers; the tracker deregisters itself when all refs are released, and the context
notifies remaining trackers on teardown.

```python
class _HandleRefTracker:
    def __init__(
        self,
        ctx: LocalRPCContext,
        on_released: Callable[[str], None],
    ) -> None:
        self._ref_count = 1
        self._on_released = on_released
        self._ctx = ctx
        ctx._active_trackers.add(self)

    def add_ref(self) -> None:
        if self._ref_count <= 0:
            raise RuntimeError("Cannot add ref to a handle with refcount 0")
        self._ref_count += 1

    def release(self) -> None:
        if self._ref_count <= 0:
            raise RuntimeError("Cannot release a handle with refcount 0")
        self._ref_count -= 1
        if self._ref_count == 0:
            self._ctx._active_trackers.discard(self)
            self._on_released("discarded")

    def context_teardown(self) -> None:
        self._on_released("teardown")
```

### Changes to `LocalRPCContext`

The context maintains a set of active trackers and notifies them on teardown:

```python
class LocalRPCContext:
    def __init__(self) -> None:
        ...
        self._active_trackers: set[_HandleRefTracker] = set()

    # In __asynccontextmanager__ finally block, before setting _torn_down:
    for tracker in list(self._active_trackers):
        tracker.context_teardown()
    self._active_trackers.clear()
```

The `wrap()` method gains an `on_released` parameter:

```python
def wrap(
    self,
    obj: Any,
    on_released: Callable[[str], None] | None = None,
) -> LocalHandle:
    self._check_active()
    tracker = _HandleRefTracker(self, on_released) if on_released else None
    return LocalHandle(obj, self, _tracker=tracker)
```

### Changes to `LocalHandle`

```python
class LocalHandle(RPCHandle[Any]):
    def __init__(
        self,
        wrapped_obj: Any,
        ctx: LocalRPCContext,
        _tracker: _HandleRefTracker | None = None,
    ) -> None:
        self._wrapped_obj = wrapped_obj
        self._ctx = ctx
        self._discarded = False
        self._teardown_event = anyio.Event()
        self._tracker = _tracker

    def clone(self) -> LocalHandle:
        if self._discarded:
            raise RPCResourceDiscardedError("Cannot clone a discarded handle")
        if self._tracker is not None:
            self._tracker.add_ref()
        return LocalHandle(self._wrapped_obj, self._ctx, _tracker=self._tracker)

    def discard(self) -> None:
        if not self._discarded and self._tracker is not None:
            self._tracker.release()
        self._discarded = True
        self._wrapped_obj = None
        self._teardown_event.set()
```

The `_discarded` check in `discard()` guards against double-release. Each clone has its own
`_discarded` flag, so each clone only calls `tracker.release()` once. The tracker's internal
assertions are an invariant check, not the primary guard.

### Interaction notes

**Context teardown**: When the `LocalRPCContext` exits, it iterates active trackers and calls
`context_teardown()` on each, then clears the set. This fires the callback with `"teardown"`.
The callback fires *before* `_torn_down` is set, so the callback can still inspect context
state if needed.

**Transport export**: When a `LocalHandle` is exported via a JSON transport, the transport
clones the handle (incrementing the refcount). When the remote side releases the export, the
transport discards its clone. If that was the last clone, the notification fires with
`"discarded"`. This is exactly the desired behavior — the notification tells us "nobody holds
a reference to this handle anymore, including remote references."

**Sync callback**: The callback is synchronous. If the implementer needs async work, they can
`start_soon` on a task group they own. The callback is a notification, not a place for complex
async logic.

## Implementation Steps

- [x] Add `_active_trackers` set to `LocalRPCContext.__init__`
- [x] Add tracker teardown iteration to `LocalRPCContext.__asynccontextmanager__` finally block
- [x] Implement `_HandleRefTracker`
- [x] Add `_tracker` parameter to `LocalHandle.__init__`
- [x] Update `LocalHandle.clone()` to increment refcount
- [x] Update `LocalHandle.discard()` to decrement refcount (guarded by `_discarded` check)
- [x] Add `on_released` parameter to `LocalRPCContext.wrap()`
- [x] Tests for discard notification
- [x] Update `rpc/LOCAL_CODE_OVERVIEW.md`

## Testing

- [x] Basic: wrap with callback, discard → callback fires with `"discarded"`
- [x] Multiple clones: clone twice, discard all three → callback fires once with `"discarded"` on last discard
- [x] Partial discard: clone twice, discard two of three → callback does NOT fire
- [x] No callback: wrap without callback → discard works as before (no regression)
- [x] Context teardown with live handles: context exits → callback fires with `"teardown"`
- [x] Context teardown after full discard: discard all clones, then context exits → callback fires once with `"discarded"`, NOT again with `"teardown"`
- [x] Double discard: calling discard twice on the same handle doesn't double-release
- [x] Transport integration: export handle, remote releases → callback fires with `"discarded"`
  - Tests both JSON and simple JSON loopback transports
- [x] Clone after discard: verify clone of discarded handle still raises

## Notes

- This is a backwards-compatible change. Existing code that calls `wrap()` without the callback
  parameter is unaffected.
- The `_HandleRefTracker` is intentionally simple — no async, no thread safety beyond what
  cooperative concurrency provides. This matches the existing patterns in `basic_local_caps.py`.
- The callback receives a string reason rather than a bool or enum, for readability at call
  sites and easy extensibility if more reasons are needed later.
