# Stage 13 Post-1: ConnectionWorkerPool

## Context

Currently, every `run_sync_with_connection` call goes through `anyio.to_thread.run_sync`, which spawns/acquires a thread from the thread pool per call. When performing tens of thousands of small DB operations (e.g. loading precomputed data for many entities), the per-call overhead of thread dispatch becomes significant.

This plan introduces `ConnectionWorkerPool`, which uses persistent worker threads that pull tasks from a shared queue. Each worker holds a connection for its lifetime, eliminating both the thread-spawn overhead and per-call connection acquire/release overhead.

## Design

### New class: `ConnectionWorkerPool` (in `sqlite_util.py`)

**State:**
- `_db_path`, `_connection_setup`, `_connection_teardown`, `_is_memory_db` — same connection config as `ConnectionPool`
- `_max_connections: int` — maximum number of concurrent worker threads
- `_capacity_limiter: anyio.CapacityLimiter` — limits actual thread concurrency to `max_connections`
- `_queue: queue.Queue` — thread-safe queue of work items
- `_active_workers: int` — count of currently-running worker threads (for assertions)
- `_closed: bool` — set on close to reject new submissions

**Queue item tuple:**
```python
(func, args, result_box, event, cancelled_flag)
```
Where:
- `func`: the sync callable (takes `conn` as first arg)
- `args`: positional args after `conn`
- `result_box`: a `list` — worker writes `[0, value]` on success or `[1, exception]` on failure
- `event`: an `anyio.Event` to signal completion
- `cancelled_flag`: a `list[bool]` — caller sets `[True]` if cancelled before worker picks it up

**Worker thread method `_worker_loop(self)`:**
```
create connection (same as ConnectionPool._create_connection)
try:
    loop forever:
        queue.get(timeout=1.0)
        on timeout: check_cancelled(), continue
        on item: check_cancelled() — if raises, put item back, re-raise
        if cancelled_flag[0]: skip (don't signal event)
        run func(conn, *args)
        write result to result_box
        anyio.from_thread.run_sync(event.set) — swallow errors
finally:
    teardown + close connection
```

**Async method `run_worker_thread_forever(self, *, task_status)`:**
```python
async def run_worker_thread_forever(self, *, task_status):
    self._active_workers += 1
    task_status.started(None)
    try:
        await anyio.to_thread.run_sync(self._worker_loop, abandon_on_cancel=True, limiter=self._capacity_limiter)
    finally:
        self._active_workers -= 1
```

**Async method `start_all_workers(self, *, task_status)` (preferred entry point):**
Creates an internal task group with `max_connections` workers and returns the cancel scope via `task_status`. Caller cancels the scope to stop all workers.
```python
async def start_all_workers(self, *, task_status):
    async with anyio.create_task_group() as worker_tg:
        for _ in range(self._max_connections):
            await worker_tg.start(self.run_worker_thread_forever)
        task_status.started(worker_tg.cancel_scope)
```

**Public API `run_sync_with_connection(self, fn, /, *args)` (async):**
```python
assert self._active_workers > 0
assert not self._closed
cancelled_flag = [False]
result_box = []
event = anyio.Event()
self._queue.put((fn, args, result_box, event, cancelled_flag))
try:
    await event.wait()
except BaseException:
    cancelled_flag[0] = True
    raise
# unpack result_box
if result_box[0] == 0: return result_box[1]
else: raise result_box[1]
```

Same signature as `ConnectionPool.run_sync_with_connection` but without `**kwargs` (the existing code never uses kwargs, and they're messier to pass through the queue).

**`close(self)`:**
- Sets `_closed = True`
- Drains the queue: for each pending item, writes a "pool closed" error to its result box and signals its event

### New class: `ConnectionWorkerPoolGroup` (in `sqlite_util.py`)

Dataclass mirroring `ConnectionPoolGroup`:
```python
@dataclasses.dataclass
class ConnectionWorkerPoolGroup:
    write_pool: ConnectionWorkerPool
    read_pool: ConnectionWorkerPool
    memory_keep_alive_connection: sqlite3.Connection | None = None
```

With `close()` method that closes both pools and the keep-alive connection.

### Changes to `sqlite_ops.py`

Add `setup_database_and_create_worker_pool_group()` (and async variant) alongside the existing `setup_database_and_create_pool_group()`. Same setup logic (schema, migrations, garbage session), but creates `ConnectionWorkerPool` instances instead of `ConnectionPool` instances, and returns a `ConnectionWorkerPoolGroup`.

### Changes to `sqlite_namespace.py`

- `_SharedSqlitePoolGroupManager`: change to create and return `ConnectionWorkerPoolGroup` instead of `ConnectionPoolGroup`
- `SQLiteFxtrSession.__init__`: accept `ConnectionWorkerPoolGroup` instead of `ConnectionPoolGroup`
- `SQLiteFxtrSession.attach_to_connection_pool_group`: after creating the session and before yielding, start worker threads into the task group. On exit, close the worker pools (which causes workers to drain and eventually exit when the cancel scope cancels them).
- All `self._session._pool_group.write_pool.run_sync_with_connection(...)` and `read_pool.run_sync_with_connection(...)` calls stay the same — the API is the same.

### Worker lifecycle

The intended lifecycle is:

1. `_SharedSqlitePoolGroupManager.get_pool_group()` creates a `ConnectionWorkerPoolGroup`
2. `SQLiteFxtrSession.attach_to_connection_pool_group()` starts worker threads into the session's task group via `tg.start_soon(pool.run_worker_in_cancel_scope, tg.cancel_scope)` for each pool (write and read)
3. Workers run until the task group exits (cancel scope cancels them, they detect via `check_cancelled()` and exit)
4. `close()` on the pool group drains any remaining queued items with errors

## Implementation Steps

- [x] **Step 1: Implement `ConnectionWorkerPool`** in `sqlite_util.py`
  - Class with queue, worker loop, run_sync_with_connection, close
  - No changes to existing `ConnectionPool` (kept for flexibility)

- [x] **Step 2: Implement `ConnectionWorkerPoolGroup`** in `sqlite_util.py`
  - Dataclass with write_pool, read_pool, memory_keep_alive_connection, close()

- [x] **Step 3: Write tests** for `ConnectionWorkerPool` in `test_sqlite_util.py`
  - Basic operation (submit work, get result)
  - Multiple workers with concurrency
  - Cancellation flag skips queued work
  - Close drains queue with errors
  - Worker exits cleanly on cancel scope cancellation

- [x] **Step 4: Add `setup_database_and_create_worker_pool_group`** to `sqlite_ops.py`
  - Same setup logic, creates ConnectionWorkerPool instances
  - Add async variant

- [x] **Step 5: Update `sqlite_namespace.py`**
  - `_SharedSqlitePoolGroupManager`: use worker pool group
  - `SQLiteFxtrSession`: accept worker pool group, start workers in task group
  - Verify all `run_sync_with_connection` call sites work unchanged

- [x] **Step 6: Run full test suite** to verify no regressions (737 passed)

- [x] **Step 7: Update LOCAL_CODE_OVERVIEW.md and CODE_OVERVIEW.md**

## Verification

- Run `uv run pytest tests/test_sqlite_store/ -x -q` — all tests pass
- Run `uv run pytest tests/ -x -q` — full suite passes
- Verify no pyright/basedpyright errors in modified files

## Open Questions / Notes

- The `ConnectionPool` class is kept around but no longer used by `sqlite_namespace.py`. It may be useful for other callers or testing, so we keep it.
- `**kwargs` support is dropped from `run_sync_with_connection` on the worker pool. No existing call site uses kwargs — they all pass positional args.

## Shutdown Design

Two shutdown pathways:

1. **Cancel scope cancellation** (used by `attach_to_connection_pool_group`): Cancels the async tasks immediately. Threads are abandoned (`abandon_on_cancel=True`) and exit shortly after via `check_cancelled()`. The `CapacityLimiter` ensures at most `max_connections` threads hold connections at any time, so abandoned threads don't cause unbounded connection growth.

2. **`aclose()`** (used by `_SharedSqlitePoolGroupManager.get_pool_group` when tearing down the pool group): Sends shutdown sentinels (None) to all active threads, then waits for threads to actually exit and close their connections. This ensures all file descriptors are released before the pool group is discarded. Uses a thread-side counter (`_active_threads` with `threading.Lock` + `threading.Event`) since the async-side `_active_workers` may already be 0 from prior cancel scope cancellations.
