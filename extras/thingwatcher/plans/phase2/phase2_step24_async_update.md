# Phase 2 Step 24: Async Owning Snapshotters via Streaming Ticks

## Problem

Currently, ticks are synchronous to capture a consistent point in time. However, some state checks ideally involve waiting for async operations (e.g., fetching from a slow API, database queries). We want to support async work in snapshotters while maintaining consistency.

## Solution Overview

Add a new return type `AsyncThingUpdate[T]` that both `snapshot_owned()` and `stateless_snapshot()` can return. This contains:
1. An **immediate update** (type `T`) - shown right away at the current tick (can indicate "pending" state)
2. An **async updater** - a coroutine that computes the final state, which appears in a subsequent streaming tick

The immediate update type `T` is constrained by which method returns it:
- `stateless_snapshot()`: immediate must be stateless (`ThingState | Sequence[ThingState]`)
- `snapshot_owned()`: immediate can include deltas (`ThingState | ThingDelta | Sequence[...]`)

The async updater can always return the full stateful result (including `ThingDelta`) since it pushes to a streaming tick.

The async updater is automatically cancelled if the same object is snapshotted again (via `tick()` or `streaming_update()`) before it completes. This ensures we don't apply stale async results.

## Design Details

### New Data Type

```python
# In api.py

# Type aliases for clarity
StatelessResult = ThingState | Sequence[ThingState]
StatefulResult = ThingState | ThingDelta | Sequence[ThingState | ThingDelta]

T = TypeVar('T', StatelessResult, StatefulResult)

@dataclass(frozen=True)
class AsyncThingUpdate(Generic[T]):
    """Snapshot result that requires async work to compute final state.

    The immediate_update is recorded at the current tick. The async_updater
    runs in the background and its result appears in a subsequent streaming tick.

    Generic over T (the immediate_update type):
    - For stateless_snapshot: T = ThingState | Sequence[ThingState]
    - For snapshot_owned: T = ThingState | ThingDelta | Sequence[...]

    The async_updater can always return the full stateful result (including
    ThingDelta) regardless of which method returns this, since by the time
    it completes we're doing a streaming update.

    If another tick() or streaming_update() for this object occurs before
    async_updater completes, it is cancelled and its result is discarded.
    The snapshotter should not commit changes to internal state until after
    its final await.
    """
    immediate_update: T
    async_updater: Callable[[], Awaitable[StatefulResult]]
```

Updated ABC signatures:

```python
class StatefulOwningSnapshotter(ABC):
    @abstractmethod
    def snapshot_owned(self, obj: Any) -> StatefulResult | AsyncThingUpdate[StatefulResult]:
        ...

    @abstractmethod
    def stateless_snapshot(self, obj: Any) -> StatelessResult | AsyncThingUpdate[StatelessResult]:
        ...
```

### Watcher State

Add to `Watcher.__init__`:

```python
# Pending async updates: obj_id -> CancelScope
self._pending_async_updates: dict[int, anyio.CancelScope] = {}

# Stream for async update jobs (buffered, consumed by runner task)
self._async_update_send, self._async_update_receive = anyio.create_memory_object_stream[AsyncUpdateJob](math.inf)
```

New job dataclass:

```python
@dataclass
class AsyncUpdateJob:
    """Job for running an async update in the background."""
    obj_id: int
    cancel_scope: anyio.CancelScope
    async_updater: Callable[[], Awaitable[ThingState | ThingDelta | Sequence[ThingState | ThingDelta]]]
```

### Cancellation Logic

**Rule**: Any time we call `snapshot_owned()` or `stateless_snapshot()` on a `StatefulOwningSnapshotter` for some object, we cancel any still-running async update for that object first.

- `tick()`: Before snapshotting each owning object, cancel its pending async update
- `streaming_update(obj)`: Cancel pending async update only for that specific object
- `hard_recapture()`: Cancel all pending async updates (calls `stateless_snapshot` on all)
- `untrack(obj)`: Cancel pending async update for that object (cleanup on finalization or explicit untrack)

Implementation:

```python
def _cancel_async_update_for(self, obj_id: int) -> None:
    """Cancel and remove any pending async update for this object.

    Removes from dict immediately since there may be a delay before the
    runner task actually exits.
    """
    scope = self._pending_async_updates.pop(obj_id, None)
    if scope is not None:
        scope.cancel()
```

The dict `_pending_async_updates` only contains non-cancelled, non-completed scopes. We remove immediately on cancel rather than waiting for the runner to clean up.

### Async Update Runner

A background task consumes async update jobs and runs them concurrently:

```python
async def _async_update_runner(self):
    """Background task that runs async update jobs."""
    async with anyio.create_task_group() as tg:
        async for job in self._async_update_receive:
            tg.start_soon(self._run_single_async_update, job)

async def _run_single_async_update(self, job: AsyncUpdateJob):
    """Execute a single async update job."""
    # Check if already cancelled before we started
    # (scope was removed from dict when cancelled, so just check the flag)
    if job.cancel_scope.cancel_called:
        return

    try:
        with job.cancel_scope:
            result = await job.async_updater()

            # Validate: cannot return another AsyncThingUpdate
            # Validate: must be ThingState/ThingDelta/Sequence

            # Push result to streaming tick (bypassing snapshotter)
            self._push_async_update_result(job.obj_id, result)

    except anyio.get_cancelled_exc_class():
        pass  # Cancelled, result not applied
    except Exception as e:
        # Error in async_updater - push broken thing
        self._push_async_update_error(job.obj_id, str(e))
    finally:
        # Clean up from dict if we're still the current entry
        # (might not be if a new async update was queued for this obj)
        with self._lock:
            if self._pending_async_updates.get(job.obj_id) is job.cancel_scope:
                del self._pending_async_updates[job.obj_id]
```

### Processing AsyncThingUpdate

When `snapshot_owned()` or `stateless_snapshot()` returns an `AsyncThingUpdate`:

1. Use `immediate_update` for the current tick's state (process normally)
2. Create a new `CancelScope` and store in `_pending_async_updates[obj_id]`
3. Queue an `AsyncUpdateJob` to the stream

Note: `_cancel_async_update_for(obj_id)` must have been called **before** the snapshot method was invoked. We assert this invariant rather than cancelling here.

```python
# In _take_snapshot, after calling snapshot_owned or stateless_snapshot:
if isinstance(result, AsyncThingUpdate):
    # Invariant: async update was cancelled before snapshot method was called
    assert obj_id not in self._pending_async_updates

    # Process immediate_update as the current state
    immediate = result.immediate_update
    # ... normalize to list, validate, add to states ...

    # Queue async work
    cancel_scope = anyio.CancelScope()
    self._pending_async_updates[obj_id] = cancel_scope
    job = AsyncUpdateJob(obj_id, cancel_scope, result.async_updater)
    self._async_update_send.send_nowait(job)
```

### Shared Helper for Streaming Results

Extract shared logic between `streaming_update()` and async completion:

```python
def _push_streaming_states(
    self,
    obj_id: int,
    states: Sequence[ThingState | ThingDelta],
) -> None:
    """Push states to a streaming tick.

    Used by both streaming_update() (after calling the snapshotter) and
    async update completion (with pre-computed results). Creates or updates
    a stream tick with the given states for the specified object.
    """
    with self._lock:
        # Same seq/tick management as _take_streaming_update
        self._seq_counter += 1
        seq_num = self._seq_counter
        timestamp = time.time()

        if self._current_tick_type == "stream":
            tick_num = self._tick_counter
            is_new_tick = False
        else:
            self._tick_counter += 1
            tick_num = self._tick_counter
            is_new_tick = True
            self._current_tick_type = "stream"

        # Detect disappeared keys
        prev_owned = self._owned_things.get(obj_id, set())
        current_keys = {cbor2.dumps(s.key, canonical=True) for s in states}
        disappeared_keys = prev_owned - current_keys

        result = StreamingUpdateResult(
            tick_number=tick_num,
            seq_number=seq_num,
            timestamp=timestamp,
            is_new_tick=is_new_tick,
            obj_id=obj_id,
            states=list(states),
            disappeared_keys=disappeared_keys,
        )

        self._write_stream.send_nowait(result)
```

### Startup with Task Group

In `watching_and_serving()`:

```python
async with anyio.create_task_group() as tg:
    tg.start_soon(self._db_writer, self._receive_stream)
    tg.start_soon(self._async_update_runner)  # NEW
    # ... rest of setup
```

Jobs queued before `watching_and_serving()` is called will be buffered in the stream and processed once the runner starts. The runner checks `cancel_scope.cancel_called` at startup to handle the case where the job was cancelled before processing began.

### Shutdown

During shutdown, cancel all pending async updates and close the stream:

```python
# In watching_and_serving cleanup:
for scope in list(self._pending_async_updates.values()):
    scope.cancel()
self._pending_async_updates.clear()
await self._async_update_send.aclose()
```

## Demo: AsyncSnapshottedThing

Add a new object to `demo_multi_chunk` that demonstrates async updates:

```python
class AsyncSnapshottedThing:
    """Demonstrates async snapshot completion.

    When snapshotted, immediately returns a "pending" state,
    then after 5 seconds, an async result with "completed" status.

    The object has an async getter that simulates slow async work
    (e.g., fetching from an API). The snapshotter uses this to
    get the delayed state.
    """
    def __init__(self, name: str):
        self.name = name

    async def get_delayed_state(self) -> dict:
        """Simulate fetching state from a slow async source.

        Returns a random value after a delay, without modifying
        the object itself.
        """
        await anyio.sleep(5.0)
        return {
            "fetched_value": random.randint(1, 1000),
            "fetched_at": time.time(),
        }


class AsyncSnapshottedThingSnapshotter(StatefulOwningSnapshotter):
    def snapshot_owned(self, obj: AsyncSnapshottedThing) -> AsyncThingUpdate:
        request_id = str(uuid.uuid4())[:8]

        immediate = ThingState(
            key=("async_thing", obj.name),
            thing_type="AsyncSnapshottedThing",
            thing_data={
                "name": obj.name,
                "status": "pending",
                "request_id": request_id,
            },
            links=[],
        )

        async def complete_async():
            result = await obj.get_delayed_state()
            return ThingState(
                key=("async_thing", obj.name),
                thing_type="AsyncSnapshottedThing",
                thing_data={
                    "name": obj.name,
                    "status": "completed",
                    "request_id": request_id,  # Same ID - shows continuity
                    **result,
                },
                links=[],
            )

        return AsyncThingUpdate(
            immediate_update=immediate,
            async_updater=complete_async,
        )

    def stateless_snapshot(self, obj: AsyncSnapshottedThing) -> ThingState:
        """For hard recapture - return current state as unknown."""
        return ThingState(
            key=("async_thing", obj.name),
            thing_type="AsyncSnapshottedThing",
            thing_data={
                "name": obj.name,
                "status": "unknown",
            },
            links=[],
        )
```

The demo shows:
1. Initial tick shows `status: "pending"` with a request_id
2. After 5 seconds, streaming tick updates to `status: "completed"` with same request_id plus the fetched value
3. If user triggers another tick within 5 seconds, the pending async is cancelled (no completed update appears for that request_id)

## Implementation Steps

- [x] 1. Add `AsyncThingUpdate` dataclass, `StatelessResult` and `StatefulResult` type aliases to `api.py` and export them
- [x] 2. Add `AsyncUpdateJob` dataclass to `watcher.py`
- [x] 3. Add watcher state: `_pending_async_updates` dict, async update send/receive streams
- [x] 4. Add `_cancel_async_update_for(obj_id)` method
- [x] 5. Add `_async_update_runner()` and `_run_single_async_update()` methods
- [x] 6. Add `_push_streaming_states()` helper and refactor `_take_streaming_update()` to use it
- [x] 7. Modify `_take_snapshot()` to handle `AsyncThingUpdate` return type from both `snapshot_owned()` and `stateless_snapshot()`
- [x] 8. Modify `_take_streaming_update()` to cancel pending async for the object before calling snapshotter
- [x] 9. Modify `hard_recapture()` to cancel all pending async updates
- [x] 10. Modify `untrack()` to cancel pending async for the object (handles explicit untrack and weak ref finalization)
- [x] 11. Start `_async_update_runner` in `watching_and_serving()`
- [x] 12. Add shutdown cleanup for async updates
- [x] 13. Add tests:
    - [x] Basic async update flow (immediate → wait → completed)
    - [x] AsyncThingUpdate from stateless_snapshot (hard_recapture path)
    - [x] Cancellation on new tick
    - [x] Cancellation on streaming_update
    - [x] Cancellation on hard_recapture
    - [x] Cancellation on untrack
    - [x] Error handling in async_updater
    - [x] AsyncThingUpdate not allowed from async_updater
- [x] 14. Add `AsyncSnapshottedThing` demo to `demo_multi_chunk/`
    - [x] Add `AsyncSnapshottedThing` class and snapshotter
    - [x] Add renderer (`AsyncSnapshottedThing.tsx`)
    - [x] Update `demo.py` to include it
- [x] 15. Update CODE_OVERVIEW.md
