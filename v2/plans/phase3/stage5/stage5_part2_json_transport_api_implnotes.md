# Stage 5 Part 2: JSON Transport — Implementation Notes

## Issue: `dispatch()` is sync but push requires async send

### The problem

The `RPCHandle.dispatch()` ABC is **synchronous** — it must return an `RPCPending` immediately
without awaiting. But sending a `["push", ...]` message over the JSON stream requires the async
`_send_lock` and an async `stream.send()` call.

This matters because the returned `RPCPending` needs to support **pipelining**: the caller can
immediately call `pending.as_handle()` and dispatch further calls through it. For pipelining to
work over the wire, the pending must be backed by a real import entry with a real import ID, so
that subsequent push/submit messages can reference that ID in `["pipeline", id, ...]` expressions.

### What the plan says

The plan (`stage5_part2_json_transport_api.md`) specifies:

> **`dispatch(attr_path, request)`**: Encode the call argument (if Call). Send
> `["push", ["pipeline", import_id, path, arg_expr]]`. Allocate a new positive import ID for
> the push result. Return a `JSONTransportPending` for the new import.

And for deferred release (implementation note 7):

> `discard()` on `RPCHandle`/`RPCPending` is sync per the ABC, but sending a `["release", ...]`
> message requires the async send lock. The implementation should use a deferred-send pattern.

The plan correctly identifies the sync/async tension for `discard()` → release, and prescribes a
deferred-send pattern. But it doesn't explicitly address the same tension for `dispatch()` → push.
The plan's description of dispatch reads as if encoding + sending happen synchronously, but the
actual ABC signature makes this impossible.

### First attempt (what went wrong)

My initial implementation used a `_BackgroundPending` type: `dispatch()` spawned a background task
that:
1. Sent the push message (async, under the lock)
2. Waited for the push result to resolve
3. Pulled and cloned the resolved value into a `_ResolutionFuture`

The `_BackgroundPending` wrapped this future and exposed pull/dispatch/as_handle on it.

**The bug**: `_BackgroundPending.as_handle()` created a handle backed by the future's resolved
value. But the future resolved to the *pulled value* (e.g. the integer `15`), not the push result
import entry. So `as_handle().dispatch_and_wait(["add"], Call([7]))` tried to dispatch on `15`
(not an RPCHandle), and failed with a TypeError or hang.

The fundamental issue: this approach **broke pipelining**. The whole point of `dispatch()` returning
a pending is that subsequent calls can pipeline through it — referencing the push result's import ID
in wire messages before the result has resolved. The `_BackgroundPending` approach lost the import
entry and its ID.

### Analysis: why deferred send works

Under **cooperative concurrency** (single-threaded async with anyio), no other task can run between
two synchronous operations. This means we can safely:

1. Allocate a positive import ID (synchronous counter increment)
2. Create the import entry in the table (synchronous dict insert)
3. Build the push expression (synchronous encoding)
4. Queue the message for async sending (synchronous list append)

All four steps happen atomically from the perspective of other tasks. The message won't actually
be sent until the next `await`, but:

- No other task can allocate an ID or send a message in between, so **ID ordering is preserved**
- The import entry exists immediately, so the returned `JSONTransportPending` can reference it
- Subsequent synchronous `dispatch()` calls will allocate higher IDs and queue messages after this
  one, maintaining wire order

This is the same reasoning that makes the deferred release pattern safe (plan note 7): "Deferred
releases can be sent in any order relative to other messages. The released import ID will never be
referenced by our side again."

For push messages, ordering matters more (the remote side assigns IDs based on message order), but
the cooperative concurrency guarantee means queued messages are always drained in the order they
were queued, which matches ID allocation order.

### The fix

Add a `_sync_dispatch_push` method to `JSONTransportContext` that:
1. Allocates the next positive import ID (sync)
2. Creates an import entry with a `_ResolutionFuture` (sync)
3. Builds the push expression via `_build_dispatch_expr` (sync — encoding is sync)
4. Appends the `["push", expr]` message to a `_pending_sends` queue (sync)
5. Ensures a background drain task is scheduled (sync — `tg.start_soon`)
6. Returns the import entry

Then `JSONTransportHandle.dispatch()` becomes:
```python
def dispatch(self, attr_path, request):
    entry = self._check_usable()
    if isinstance(request, Call):
        request = Call(clone_ir_value(request.arg))
    result_entry = self._ctx._sync_dispatch_push(entry.id, attr_path, request)
    return JSONTransportPending(result_entry, self._ctx)
```

The returned `JSONTransportPending` wraps a real import entry. `as_handle()` increments the
entry's `local_refcount` and returns a `JSONTransportPendingHandle`. Pipelining works because
subsequent dispatch calls reference the entry's real import ID.

The `_pending_sends` queue is drained by any code path that acquires the `_send_lock`:
`_drain_pending_sends()` sends queued messages first (in order), then queued releases. This is
called at the start of `_send_push`, `_send_submit`, `_send_queued_releases`, and any other
lock-acquiring method.

### What was changed

1. Added `_pending_sends: list[JSONTypes]` to `JSONTransportContext.__init__`
2. Added `_queue_send()`, `_ensure_send_drain()`, `_drain_pending_sends()` methods
3. Modified `_drain_pending_releases()` to call `_drain_pending_sends()` (so all lock-acquiring
   paths drain both queues)
4. Added `_sync_dispatch_push()` method
5. Rewrote `JSONTransportHandle._dispatch_impl()` to use `_sync_dispatch_push` → return
   `JSONTransportPending`
6. Rewrote `JSONTransportPending._dispatch_wire()` similarly

### What remains to verify

- The `_BackgroundPending` and `_BackgroundPendingHandle` types are now unused by the main
  dispatch path but may still be used by `JSONTransportPendingHandle.dispatch()`. Need to check
  whether those types should also be converted, or whether they're still appropriate for the
  pending-handle case (where we must wait for resolution before dispatching).
- The `_send_push` / `_send_submit` async methods still exist and are used by
  `dispatch_and_wait()` (which is async and can use the lock directly). These are fine.
- Need to verify that the `_send_push_for_dispatch` / `_send_submit_for_dispatch` methods (which
  check resolution under the lock) are still needed for the pending dispatch path, or if they
  can be simplified.
- All the transport-parameterized tests need to pass.

## Issue: `JSONTransportPendingHandle` must pipeline, not wait for resolution

### The problem

My initial `JSONTransportPendingHandle.dispatch_and_wait()` awaited the import entry's
resolution future (waiting for the push result to resolve), then dispatched locally on the
resolved value. This **deadlocks** because:

1. The push result's resolution requires the server to send `["resolve", id, ...]`
2. The server only sends resolve when the export is pulled (push results aren't auto-pulled)
3. Nobody sends `["pull", id]` because `dispatch_and_wait` is waiting, not pulling

The whole point of `as_handle()` is to enable **pipelining** — dispatching through an unresolved
pending by referencing its import ID in wire expressions.

### The fix

`JSONTransportPendingHandle.dispatch_and_wait()` sends `["submit", ["pipeline", entry.id, ...]]`
— exactly like `JSONTransportHandle.dispatch_and_wait()`. The server evaluates this by dispatching
on its corresponding export (which may still be pending), chaining through the path.

Same for `dispatch()`: uses `_sync_dispatch_push` referencing the entry's ID.

This means `JSONTransportPendingHandle` has exactly the same dispatch behavior as
`JSONTransportHandle` — both pipeline through their import entry. The difference is only in
`wait_until_teardown()` semantics (two-phase: wait for resolution, then wait for resolved
handle's teardown).

## Issue: Exception types are lost across the wire

### The problem

Tests expect specific exception types (`AttributeError`, `TypeError`, `ValueError`) to propagate
through `dispatch_and_wait`. But the wire protocol serializes all exceptions as
`["error", typeName, message]` and the evaluator reconstructs them as `RPCError`.

This is **by design** — PROTOCOL.md says: "Cap-IR uses the type name for informational purposes
and always constructs an RPCError on the receiving side." And `cap_ir.py` says: "exceptions may
not be round-trip convertable across transports."

### How to handle in tests

The transport-parameterized tests check for specific exception types because the identity
transport preserves them. For the JSON transport, these tests need to accept `RPCError` wrapping
the original. Options:

1. **Modify tests to accept either**: e.g. `pytest.raises((AttributeError, RPCError))`
2. **Skip JSON transport for exception-type-specific tests**
3. **Parameterize the assertion**: the transport fixture indicates whether exceptions preserve types

Option 1 is simplest and reflects the actual contract: over a wire transport, you always get
`RPCError`. The test should check the message contains the original error info.

There's also a subtlety: `_make_rejection` wraps non-`RPCError` exceptions in `RPCError` with
the original as `__cause__`. But for wire-transported errors, the `__cause__` is None because the
original exception was serialized as `["error", ...]` and reconstructed as a fresh `RPCError` on
the other side. This means `test_method_raises_via_dispatch_pull`'s assertion
`isinstance(exc_info.value.__cause__, ValueError)` will fail for JSON transport.

### The fix

Modify the error handling tests to accept the behavior of wire transports:
- Tests that check `pytest.raises(AttributeError)` should also accept `RPCError` with the
  original type name in the message.
- Tests that check `__cause__` should be skipped or adapted for wire transports.

## Issue: `wait_until_teardown` on push result pendings deadlocks without pull

### The problem

`JSONTransportPending.wait_until_teardown()` has two phases (matching `LocalPending`):
- **Phase 1**: Wait for the resolution future to settle (resolve or reject). If rejected,
  propagate the exception immediately — this is how `wait_until_teardown` detects failed
  computations.
- **Phase 2**: Future resolved successfully. Wait for discard or connection abort.

For push result imports, the resolution future is only settled when the server sends
`["resolve", ...]` or `["reject", ...]`. But per the protocol, the server only starts the
resolve/reject process when it receives a `["pull", id]` message. Push results are not
auto-pulled — this is intentional in the protocol because it allows the client to pipeline
through a push result without ever pulling the actual value.

Before the fix, if the client called `wait_until_teardown()` on a push result pending *without*
having called `pull()` first, phase 1 would wait for the resolution future which would never be
settled because no `["pull", id]` was ever sent. The connection would just hang.

### Why this doesn't happen in the local transport

In the local transport, `LocalPending`'s `_InternalFuture` is settled directly by the background
dispatch task (via `_run_dispatch`), which runs unconditionally once `dispatch()` is called. There
is no pull/resolve round-trip — the future is settled as soon as the computation completes or
fails, regardless of whether anyone is waiting for it.

### What the code did before

```python
# Phase 1: wait for future to settle, racing against discard
async with create_task_group(...) as tg:
    # wait_future: await entry.resolution_future.wait()  -- hangs here
    # wait_discard: await self._teardown_event.wait()
    ...
```

Phase 1 entered directly without sending pull. For a push result where `pull()` was never called,
the resolution future was never settled, so `wait_future` blocked forever.

### Tests that hit this

- **`test_teardown_pending_rejected`**: Calls `h.dispatch(["fail"], Call(None))` to get a pending
  for a computation that will fail, then immediately calls `pending.wait_until_teardown()` without
  ever calling `pull()`. Expects to receive the rejection exception. Without the fix, this hung
  because the server never started resolving the push result (no pull was sent), so the reject
  message was never sent back.

- **`test_teardown_pending_plain_value_waits`**: Calls `pull()` first (so it worked even without
  the fix), but the same pattern would apply to any test that calls `wait_until_teardown` on an
  unpulled push result pending.

The first test (`test_teardown_pending_rejected`) was the one that actually hung during the test
run and led to discovering this issue.

### The fix

Before entering phase 1, `wait_until_teardown` sends `["pull", id]` if the entry is not
auto-pulled and pull hasn't been sent yet:

```python
if not entry.auto_pulled and not entry.pull_sent:
    entry.pull_sent = True
    # send ["pull", entry.id]
```

This is the same logic `pull()` uses. It ensures the server will start the resolve process and
eventually send resolve/reject, allowing phase 1 to complete. For auto-pulled entries (submit
results, promise imports), this is a no-op because the server already starts resolving
immediately.

### Alternative considered

Instead of sending pull eagerly, we could have made `wait_until_teardown` skip phase 1 entirely
for unresolved push results and go straight to phase 2 (wait for discard/abort). This would be
semantically correct — if the pending hasn't been pulled, there's no rejection to observe, so
phase 1 has nothing to do. But this would diverge from the local transport's behavior where
`wait_until_teardown` on a rejected pending *always* raises the rejection, regardless of whether
`pull()` was called. Sending pull preserves the uniform behavior: if the computation failed, you
learn about it through `wait_until_teardown` even if you never called `pull()`.

This is worth revisiting — the implicit pull is a side effect that the caller might not expect.
For example, if a caller is using `wait_until_teardown` purely for liveness monitoring (not
caring about the result), the pull forces the server to serialize and send the full result value,
which could be wasteful. An alternative design might be to add a dedicated "watch" or "subscribe
to rejection" mechanism in the protocol that doesn't require the full pull round-trip.

## Issue: `JSONTransportHandle.wait_until_teardown` doesn't race against discard

### The problem

The initial `wait_until_teardown` implementation only awaited `entry.teardown_event.wait()`. This
event fires on teardown/broken messages from the remote or on connection abort. But it never fires
when the handle is locally discarded — so `wait_until_teardown` would block forever if `discard()`
was called while it was waiting.

The local transport handles this correctly: `LocalHandle.wait_until_teardown` races the handle's
own `_teardown_event` (set by discard) against the context's teardown event via
`_wait_for_discard_or_ctx_teardown`.

### The fix

Added a `_discard_event` to `JSONTransportHandle` (set by `discard()`). Changed
`wait_until_teardown` to race `_discard_event` against `entry.teardown_event` using a task group.
If discard wins, raises `RPCResourceDiscardedError`. If teardown wins, returns the teardown
reason or raises the teardown error.

## Cleanup / follow-up items

### Dead code: `_BackgroundPending`, `_BackgroundPendingHandle`, `_send_push_for_dispatch`

After the sync push redesign, nothing outside these classes creates `_BackgroundPending` or
`_BackgroundPendingHandle` instances. They were the original dispatch mechanism (spawn background
task → send push → wait for result → resolve future) and are now fully replaced by
`_sync_dispatch_push` → `JSONTransportPending`. About 370 lines of dead code.

`_send_push_for_dispatch` (the async method that checked resolution under the lock before sending
a push) is also unused — `_send_submit_for_dispatch` is still used by
`JSONTransportPending.dispatch_and_wait()`, but the push variant has no callers.

### `_TrivialPending.clone()` deep-clones instead of sharing

`_TrivialPending.clone()` creates a completely independent instance by deep-cloning the value
(including cloning any handles inside it). This contrasts with `LocalPending.clone()` which
shares the `_InternalFuture` via refcounting — multiple clones point at the same resolved value,
and only the last discard triggers cleanup.

The deep-clone approach is correct (tests pass, ownership invariants hold) but more expensive:
each clone of a `_TrivialPending` containing N handles creates N new handle clones. For most
practical uses this is fine since `_TrivialPending` wraps small values, but it's worth noting
the asymmetry with the local transport.

### Push error handling creates a convoluted `_TrivialPending`

When expression evaluation fails during an incoming push (read loop, line ~884), the code creates
a `_TrivialPending(None, self)` and then separately rejects its `_future`. This is roundabout —
the export entry just needs something that will produce a rejection when the resolve process
pulls it. A simpler approach would be to store a pre-rejected `_ResolutionFuture` directly, or
use a dedicated "failed export" sentinel, rather than constructing a full `_TrivialPending` with
a None value.

### `JSONTransportPendingHandle` dispatch duplicates `JSONTransportHandle` dispatch

After the pipelining fix, `JSONTransportPendingHandle.dispatch()` and `dispatch_and_wait()` have
identical behavior to `JSONTransportHandle` — both pipeline through their import entry via
`_sync_dispatch_push` / `_send_submit`. The only difference between the two classes is
`wait_until_teardown()` semantics (two-phase for pending handles: wait for resolution then
delegate to resolved handle's teardown; single-phase for regular handles: wait for
teardown/broken/abort).

This suggests the dispatch logic could be shared via a base class or mixin, reducing duplication.
The `clone()` and `discard()` methods are also very similar (both increment/decrement
`local_refcount` on the import entry).

## Planned: Remove implicit pull from `wait_until_teardown`

### Rationale

`JSONTransportPending.wait_until_teardown()` currently sends an implicit `["pull", id]` before
entering phase 1 (lines 1504-1509). This was added to match the local transport's behavior where
rejection is detected eagerly (the future settles as a side effect of the dispatch task running).

After discussion, this implicit pull is the wrong design:

- **Side-effect semantics**: `wait_until_teardown` is documented as a liveness monitor, not a way
  to observe results. Silently sending a pull forces the server to compute, serialize, and transmit
  the full result value — expensive and surprising.
- **Violates protocol intent**: Push results are intentionally not auto-pulled. The protocol
  separates "dispatch" from "observe result" so that intermediate pipeline results never need to be
  pulled. The implicit pull undermines this.
- **The spec allows transport-dependent eagerness**: `cap_ir.py` says "the exact behavior may
  depend on the transport" for teardown proactivity. Wire transports not detecting rejection
  eagerly (without a pull) is explicitly allowed.
- **Use case analysis**: If you want to know whether a computation failed, `pull()` is the
  designated API. `wait_until_teardown` is for connection liveness and cleanup coordination —
  events that the wire transport can already detect without a pull (discard, abort, remote
  teardown).

After removing the implicit pull, rejection is still a teardown event — if `pull()` has been called
(making the rejection observable on the wire), `wait_until_teardown` will fire immediately. The
only change is that the wire transport no longer goes out of its way to discover rejections that
nobody asked to observe.

### Changes to `json_transport.py`

1. **`JSONTransportPending.wait_until_teardown()`**: Remove the implicit pull block (lines
   1504-1509). Add `entry.teardown_event` as a third racer in phase 1, alongside
   `resolution_future` and `self._teardown_event` (discard). This is needed because without the
   implicit pull, a remote `["teardown", id, ...]` message (which sets `entry.teardown_event` but
   does NOT reject the resolution future) would otherwise go undetected in phase 1. Connection
   abort (`_do_abort`) already rejects all unresolved futures, so that path is covered, but normal
   remote-initiated teardown is not.

2. **`JSONTransportPendingHandle.wait_until_teardown()`**: Same fix — add `entry.teardown_event`
   as a third racer in phase 1. This class never had the implicit pull, but has the same gap: if
   the remote tears down the export normally before the future settles, phase 1 would hang
   indefinitely with only the resolution future and discard as racers.

### Changes to `test_transport.py`

3. **`test_teardown_pending_rejected`**: Change to call `pull()` first (catching the expected
   `RPCError`), then call `wait_until_teardown()`. This tests the same semantic property
   (rejection = teardown) without requiring the transport to eagerly discover rejection on unpulled
   pendings. After `pull()` triggers the server's resolve/reject cycle, the resolution future is
   settled, so `wait_until_teardown()` detects it immediately in phase 1.

4. **New test: `test_teardown_pending_unpulled_context_exception`**: Dispatch a call (no pull),
   crash the context, then check `wait_until_teardown()` raises `RPCResourceBrokenError`. This
   validates that `_do_abort` rejecting the resolution future wakes phase 1 without needing an
   implicit pull. The existing `test_teardown_pending_context_exception` calls `pull()` first, so
   it doesn't cover this path.

### `as_handle()` sends an implicit pull

`as_handle()` is more than just a pipelining shortcut — you can already pipeline dispatch directly
on a pending without `as_handle()`. The `RPCPending.wait_until_teardown` docstring says:

> If you need to track the lifetime of a handle that a pending value resolves to, convert it to
> an RPCHandle via `as_handle()`; the resulting handle's `wait_until_teardown` will reflect the
> underlying handle's lifetime.

This means `as_handle()` is an explicit opt-in to handle semantics, including lifecycle tracking.
It can be read as: "I know this resolves to a handle, and I want to pull it, but I don't want to
wait for it to arrive — I want to start dispatching immediately while the resolution is in
flight." On the wire transport, this requires a pull so the server sends back the resolution
(which lets `JSONTransportPendingHandle.wait_until_teardown()` reach phase 2 and delegate to the
resolved handle's lifecycle).

**Implementation**: `JSONTransportPending.as_handle()` should send a deferred pull via
`_queue_send(["pull", entry.id])` (setting `entry.pull_sent = True` if not already set). This
uses the same deferred-send mechanism that `dispatch()` uses for push messages. The pull is
sent at the next drain opportunity. Since `as_handle()` is sync, this is the only option — and
it matches the pattern.

**Contrast with plain pending**: `RPCPending.wait_until_teardown()` does NOT send an implicit
pull. If you haven't pulled and haven't called `as_handle()`, you haven't expressed interest in
the resolved value, so the wire transport doesn't go out of its way to discover rejection. The
difference is justified: `as_handle()` is an explicit opt-in to resolution, while
`wait_until_teardown()` on a plain pending is a liveness monitor.

### Changes to `test_transport.py` (additional)

5. **New test: `test_pending_handle_teardown_no_explicit_pull`**: Call `as_handle()` on a pending
   (without calling `pull()` separately), then verify that `wait_until_teardown()` correctly
   tracks the resolved handle's lifecycle. This validates that `as_handle()` sends the implicit
   pull. Should include a comment explaining that this behavior differs from plain pending
   `wait_until_teardown()` — it works because `as_handle()` is an explicit opt-in to resolution.

### Compatibility with the plan

The original plan (`stage5_part2_json_transport_api.md`) specifies `wait_until_teardown` as
"await resolution_future, racing against discard" — it never mentions an implicit pull. The
implicit pull was an implementation addition. Removing it is consistent with the plan. Adding
`entry.teardown_event` to phase 1 is a minor extension (the plan only lists discard as a racer),
justified by correctness: without it, remote-initiated teardown on unresolved imports would go
undetected in phase 1.
