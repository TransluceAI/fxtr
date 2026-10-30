# Stage 5 Part 2: JSON Transport — Wire Test Debugging Notes

## Context

All 165 high-level tests pass (43 local caps + 102 transport-parameterized + 20 JSON-specific).
The remaining work is writing raw wire-level tests that send hand-crafted JSON messages to one
side of a transport, to test protocol-level behaviors: re-introduction, evaluation rollback,
encoding rollback, early cancellation, etc.

These tests are hanging during teardown. This document records what we're trying to test,
what we expect, what we observe, and hypotheses.

## The harness

The test harness creates a `JSONTransportContext` connected to one end of a memory stream pair.
The test holds the other end and sends/receives raw JSON messages. The harness is wrapped in
`async with make_harness(...)` which enters/exits the `JSONTransportContext` properly.

## Problem: tests hang on context exit

### What happens

Tests that send push messages containing `["export", -1]` expressions hang when the
`async with make_harness(...)` block exits. The context exit calls `_do_abort()` then exits
the task group. The task group exit waits for all background tasks to complete.

### What we expect

`_do_abort()` should:
1. Set `_aborted = True`
2. Send abort message (best-effort)
3. Reject all unresolved imports
4. Fire teardown events on all imports
5. Release all exports (cancel watchers, discard values)
6. Close the stream

After abort, the read loop should see the closed stream, get `EndOfStream`, exit.
The teardown watchers should be cancelled (their scopes are cancelled by `_release_export_entry`).
Then all tasks in the task group should be done, and the task group should exit.

### What we observe

The task group's `__aexit__` hangs indefinitely, suggesting at least one background task is
still running and not responding to cancellation.

### Hypotheses

**Hypothesis 1: The read loop is blocked on receive, and closing our send side doesn't unblock it.**

The `_MemoryObjectStreamPair` wraps two separate half-streams: a send stream and a receive stream.
`aclose()` closes both. The read loop iterates with `async for msg in self._json_stream`, which
calls `__anext__` → `self._recv.receive()`. If `_recv` is `r_to_t_recv` (the test→transport
direction), closing it should raise `EndOfStream`.

But `_do_abort` calls `self._json_stream.aclose()` which closes `t_to_r_send` (transport→test
send) and `r_to_t_recv` (test→transport recv). This should unblock the read loop.

However: `_do_abort` first tries to *send* the abort message via `self._json_stream.send(...)`.
If the send side (`t_to_r_send`) fills up, this blocks. The buffer is 64 items, which should be
plenty. But if the test side isn't receiving, and we've sent many messages (releases, teardown
notifications, etc.), the buffer could fill up. This seems unlikely for these simple tests.

**Hypothesis 2: A teardown watcher task is blocked on `handle.wait_until_teardown()` and not
responding to scope cancellation.**

When the context exports its main handle (export 0), it starts a teardown watcher that awaits
`handle.wait_until_teardown()`. The abort calls `_release_export_entry` which cancels the
watcher's scope and then discards the handle.

But the scope cancellation happens *before* the handle discard. Under anyio's asyncio backend,
`CancelScope.cancel()` doesn't immediately interrupt a running task — it sets a flag that's
checked at the next checkpoint (await point). If the watcher task hasn't reached a checkpoint
since the cancel was set, it keeps running until it does.

The watcher task is in `await handle.wait_until_teardown()`. For `LocalHandle`, this races
discard event against context teardown event. The context teardown event is already set
(since the `LocalRPCContext` that created the handle is still active — we're inside
`async with LocalRPCContext()` in the test). So the wait should return immediately.

Actually wait — the `LocalRPCContext` is the *test's* context. It's not torn down yet (the test
is still running). So `wait_until_teardown` is waiting for either:
- The handle's `_teardown_event` (set by discard)
- The `ctx._teardown_event` (set when `LocalRPCContext` exits)

Neither has fired when `_release_export_entry` is called. Then `_release_export_entry` calls
`discard_ir_value(entry.value)` → `handle.discard()` → sets `_teardown_event`. So the watcher
should wake up.

But the cancel scope was already set before the discard. So the task gets:
1. Scope cancelled (flag set)
2. Handle discarded (event set, waking the `wait_until_teardown`)
3. `wait_until_teardown` returns or raises
4. The watcher checks `entry.released` (True) and returns
5. The task exits

The cancel scope should take effect at step 3 or earlier. This seems like it should work.

**Hypothesis 3: There are additional push result export entries with resolve watchers that
aren't being cancelled.**

When we send `["push", ["export", -1]]`, the context creates export 1 (push result) with the
evaluated value (a `JSONTransportHandle`). This export has no watcher (push results aren't
auto-pulled). But the `["export", -1]` evaluation creates import -1, and the resulting
`JSONTransportHandle` is stored as export 1's value.

When abort cleans up exports, `_release_export_entry(export_1)` discards the value, which calls
`discard()` on the `JSONTransportHandle`. This decrements import -1's `local_refcount`, which
queues a release. But we're already in abort, so `_queue_release` starts a background task
(`_drain_send_queues_bg`) that tries to acquire the send lock and send the release. But abort
is happening, and the abort code might be holding the send lock... wait, `_do_abort` doesn't
hold the send lock. The abort sends the abort message directly (not through the lock). So the
drain task should be able to acquire the lock.

But the drain task was just spawned via `tg.start_soon`, and the task group is being exited.
Under anyio, `start_soon` during task group exit might not schedule the task, or the task
might start and then be immediately cancelled.

Actually, I think the issue might be simpler: `_do_abort` is called, which sets `_aborted = True`.
Then `_queue_release` calls `_ensure_send_drain` which checks `if self._tg is not None and
not self._aborted` — and since `_aborted` is now True, it does NOT start the drain task. So the
release is queued but never sent. That's fine (we're aborting anyway, no need to send releases).

But the task group exit still waits for all tasks to finish. What tasks are still running?

**Hypothesis 4: The task group exit itself is the problem — the `create_task_group`
in the context manager doesn't support cancellation properly in edge cases.**

The `JSONTransportContext.__aexit__` calls `_do_abort()` then delegates to
`self._tg_cm.__aexit__()`. The `_tg_cm` is `create_task_group(collapse_exc_group=True)`.
Its `__aexit__` exits the `anyio.create_task_group()` context manager, which waits for all
tasks. But what if one of those tasks is blocked in a way that doesn't respond to
cancellation?

Under anyio's asyncio backend, task cancellation is cooperative — tasks must reach a checkpoint.
If a task is stuck in a non-cancellable operation (like a sync loop), it won't respond. But
all our tasks are async and use `await` at various points, so they should respond.

**Hypothesis 5 (most likely): The raw test harness's stream pair doesn't close cleanly when
the context aborts.**

When `_do_abort` closes `self._json_stream`, it closes the `_MemoryObjectStreamPair`. This
closes `t_to_r_send` and `r_to_t_recv`. The read loop is iterating on `r_to_t_recv` (the
receive half), which is now closed. This should cause `EndOfStream` → `StopAsyncIteration`
→ the `async for` loop exits.

But: there's a subtlety. `_do_abort` sends the abort message *before* closing the stream:
```python
await self._json_stream.send(["abort", error_expr])  # line 1069
```

If the *send* side (`t_to_r_send`) is full (64-item buffer), this blocks. The test side isn't
receiving (it's inside the `async with` exit path). But 64 items should be plenty.

Unless... the teardown watcher for export 0 sends teardown/broken messages before abort
completes. The watcher runs as a background task. When the `LocalRPCContext`'s handle is
discarded (step 5 of abort), the watcher wakes up and tries to send `["broken", 0, ...]`.
This sends through the lock. But `_do_abort` doesn't hold the lock. So the watcher task
and the abort can interleave:

1. Abort: set `_aborted = True`
2. Abort: send abort message (succeeds)
3. Watcher: wakes up (handle was discarded), tries to send broken
4. Watcher: acquires lock, calls `_send_message` — but `_aborted` is True, so it returns
5. Abort: closes stream

This seems fine. The watcher returns after step 4.

## What to try next

I think the cleanest approach would be to:

1. Add some debug logging to identify exactly which task is hanging during exit.
2. Alternatively, send `["abort", ...]` from the test side before exiting the `async with`
   block, so the context gets a clean abort signal from the wire before we tear down the
   context. This gives the context a chance to process the abort before `__aexit__` tries.
3. Alternatively, use `anyio.fail_after` around the context exit to at least not hang forever,
   and investigate from the error.
4. Check if there's a simpler harness design — maybe using a full two-sided transport (like
   the json_loopback fixture) but with a message-intercepting proxy that records wire messages
   for assertion. This avoids the half-connection teardown issues.

## Tests we want to write

For reference, these are the planned test scenarios:

| Test | What it validates |
|------|-------------------|
| **Re-introduction** | Send `["export", -1]`, release, send `["export", -1]` again. Verify refcount. |
| **Evaluation rollback** | Push expression with valid export + invalid sub-expr. Verify import is released. |
| **Encoding rollback** | Encode value with handle + unencodable. Verify export is cleaned up. |
| **Teardown during pipelining** | Abort while slow call in flight. Verify context aborts. |
| **Submit early cancellation** | Cancel `dispatch_and_wait` mid-flight. Verify import cleaned up. |
| **Push → pull → resolve** | Basic wire round-trip verification. |
| **Submit auto-resolve** | Submit auto-pulls and auto-releases. |
| **Reject on failure** | Failed call produces reject message. |
| **Unknown opcode** | Unknown message aborts. |
| **Malformed message** | Non-list message aborts. |

The encoding rollback test works (it doesn't touch the wire — just calls `_encode_expression`
directly). The protocol edge case tests (push/pull/resolve, submit, reject, unknown opcode,
malformed) worked in the first pass (they were passing before the reintroduction test hung).
The issue is specifically with tests that create import/export entries via `["export", -1]`
expressions, making teardown more complex.
