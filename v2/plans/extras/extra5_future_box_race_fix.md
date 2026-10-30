# Extra 5: Fix the resolve-vs-close race in `RemoteIRFuture.wait`

## Problem

A `wait` on a `RemoteIRFuture` can spuriously raise `LeaseBrokenError` even though the peer successfully resolved the future, when the result delivery does non-trivial async work and the client has already discarded its result lease.

Wire ordering on the racy path:

```
SERVER → CLIENT  ResolveMessage (future_id=F, expr=...)
CLIENT → SERVER  DiscardMessage (grant_id=G)            ← from the user's lease.discard() inside wait()
SERVER → CLIENT  ClosedMessage  (token_id=T)            ← server's call_scope drained
```

`_on_resolve` (lowering_message_handlers.py:553) spawns a `deliver()` task that runs
`await eval_forwardable(...)` — async, with multiple await points. While `deliver` is
mid-evaluation, `_on_closed` arrives, `_mark_token_import_ended` fires the token's
end-callbacks, and the box's `_on_token_ended` flips state to `"discarded"` and sets
`_resolved_event`. The waiter parked in `_wait_until_resolved` sees `state == "discarded"`
on wake and raises `LeaseBrokenError` (leased_cap_common.py:823). `deliver`'s eventual
`set_result` then no-ops.

### Root cause

`LocalPendingResultBox` couples its own teardown to its lifetime token via
`token.register_end_callback(...)`. That coupling is correct for `IRFutureLocalImpl` —
the producer of the result *is* the owner of the token, so token-end means "give up." It
is wrong for `RemoteIRFuture` / `FutureImportEntry`, which borrows the *peer's* token-end
signal as the box's lifetime: the peer's token can end while we are still locally
producing the result. The box discard needs to be conditioned on local delivery being
complete, not just on the remote token ending.

## Fix shape

Split the box into a token-less base + a token-aware subclass; move the discard policy
for `FutureImportEntry`'s box up into the entry itself, gated on both the remote token
having ended *and* any in-flight delivery having finished.

### New types in `fxtr2/leases/leased_cap_common.py`

```
PendingResultBox[T]
  - state machine + setters + await_resolution / await_or_forward (current body)
  - public discard(): idempotent; transitions to "discarded", releases stored leases
  - no brand B (no token — the base class doesn't constrain leases inside the box to a
    particular lifetime)
  - no weakref.finalize, no token end-callback subscription
  - setters (set_result / set_exception / resolve_from) become idempotent under
    state == "discarded": they discard the inputs' leases and return, instead of
    raising or relying on a token-ended check

LocalPendingResultBox[T, B](PendingResultBox[T])
  - constructor takes token: LifetimeToken[B] (same signature as today)
  - reinstates the existing weakref-based token-end subscription +
    _finalize_box_storage. _on_token_ended now just calls self.discard().
```

`IRFutureLocalImpl` is unchanged in shape — it still holds `_expected_grant` directly
and constructs a `LocalPendingResultBox(token)`. The `expected_grant`-on-the-box move
was incidental cleanup and would churn ~50 unit-test sites for no fix-relevant reason;
skip it. `spawn_ir_future_in_task_group`'s call sites stay as
`LocalPendingResultBox(lifetime.token)`.

### `FutureImportEntry` becomes the discard authority

In `fxtr2/rpc/lowering_state.py` `FutureImportEntry`:
- `resolution: PendingResultBox[Any] | None` (no longer `LocalPendingResultBox`).
- New fields:
  - `delivery_in_flight: bool = False` — set True before `tg.start_soon(deliver, ...)`
    in `_on_resolve`; cleared in `deliver`'s `finally`. A bool (not an int) because the
    protocol guarantees at most one resolve-or-reject per future, so at most one
    delivery is ever in flight.
  - `token_ended: bool = False` — set when the subject token import is marked ended.
  - `box_token_unsubscribe: Callable[[], None] | None = None` — deregister handle for
    the token-end subscription installed when the box is materialized.

When the box is materialized (lazily in `_on_resolve` / `_on_reject` / first `wait`),
the lowering layer subscribes to the subject token's end:

```
def _materialize_future_box(conn, future_entry):
    if future_entry.resolution is not None: return
    box = PendingResultBox()
    future_entry.resolution = box
    token = conn._get_or_create_token_proxy(future_entry.token_import_id)
    def on_token_end():
        future_entry.token_ended = True
        _maybe_discard_future_box(conn, future_entry)
    future_entry.box_token_unsubscribe = token.register_end_callback(on_token_end)
```

#### Discard policy

The gate has to handle three observable situations when the token has just ended:

| `delivery_in_flight` | `box.is_resolved()` | `local_consumer_count` | action          |
| -------------------- | ------------------- | ---------------------- | --------------- |
| True                 | (always False)      | any                    | defer           |
| False                | True                | > 0                    | defer           |
| False                | True                | 0                      | discard         |
| False                | False               | any                    | discard         |

In words:
- While delivery is in flight, never discard — the deliver task is about to write to
  `s.value`, and a discard now would make `set_result` no-op and surface
  `LeaseBrokenError` to a waiter that should have received the result.
- Once delivery has landed and the box is resolved, defer until waiters drain. Without
  this, a discard between `set_result` (which wakes the waiter via `_resolved_event`)
  and the waiter's synchronous `_descend_and_copy(s.value, ...)` would null out
  `s.value` and the waiter would see `state == "discarded"` instead of its result.
- If delivery never started (no resolve was received) and the token has ended, discard
  immediately. This is what wakes idle waiters with `LeaseBrokenError`. The well-
  behaved-protocol case for "waiter exists but no resolve" is covered by the connection
  abort path (which fires grant-broken before token-ended, so wait_break_callbacks
  wake the waiter first); but discarding here is still the right safety net.
- If neither delivery nor waiters are involved, discard is just cleanup.

```
def _maybe_discard_future_box(conn, entry):
    if entry.resolution is None: return
    if not entry.token_ended: return
    if entry.delivery_in_flight: return
    box = entry.resolution
    if box.is_resolved() and entry.local_consumer_count > 0: return
    box.discard()
    if entry.box_token_unsubscribe is not None:
        entry.box_token_unsubscribe()
        entry.box_token_unsubscribe = None
```

Called from:
- the `on_token_end` callback above,
- `deliver()`'s `finally` (after clearing `delivery_in_flight`),
- `_on_reject` (after `set_exception`),
- `_decrement_local_consumer` (after decrementing the count — covers the "waiters
  drain after a deferred discard" trigger).

`_on_resolve` and `_on_reject` are symmetric in every respect except the async window:
both materialize via `_materialize_future_box`, both end with a
`_maybe_discard_future_box` call so a token-end that arrives before/during the handler
flushes through cleanly. The only asymmetry is that `_on_resolve` toggles
`delivery_in_flight` around its spawned `deliver` task, because that's the actual race
window the flag exists to guard. `_on_reject` is fully synchronous (`eval_static`, not
`eval_forwardable`), so by the time it returns `set_exception` has already committed
and there's no in-flight delivery to track.

### `_on_resolve` adjustments

- Materialize the box via `_materialize_future_box` (replaces the inline construction).
- Set `entry.delivery_in_flight = True` *before* `tg.start_soon(deliver, ...)`.
- In `deliver`'s `finally` (alongside the existing `_decrement_local_consumer` loop):
  set `entry.delivery_in_flight = False` and call `_maybe_discard_future_box`.

### Wait-wakeup signal (no waiter changes needed)

`RemoteIRFuture.wait` already parks in `box.await_resolution` and wakes on
`_resolved_event`. `box.discard()` sets `_resolved_event` (same as the current
`_on_token_ended` does), so an idle wait still wakes promptly with `LeaseBrokenError`
when the entry decides to discard. The sole behavioural change is that *if* delivery is
in flight when the token ends, the discard is deferred and the wait correctly receives
the value via `set_result`.

## Implementation steps

- [ ] **Red test.** Add `TestResolveSurvivesPeerCloseRace` to `tests/test_rpc/test_lowering.py`.
  Shape: B dispatches a method on A whose result contains an `IRHandle` reference (so
  `eval_forwardable` on B has to walk an `ImportedIRHandleExpr` — actual async work).
  Use a sniffing transport with a controllable gate that holds the inbound `ResolveMessage`
  on B until B is parked in `wait`, then releases it; ensure B's eval is stalled long
  enough that A's `ClosedMessage` lands first. Assert the wait returns the resolved
  value rather than raising `LeaseBrokenError`. Verify it fails on `main`.
  - The cleanest stall is to place a `ImportedIRFutureExpr` in the result that B has to
    `WalkFutureExpr` on a future B can keep pending. If that turns out hard to wire,
    fall back to monkey-patching `eval_forwardable` for the test or inserting an
    `anyio.Event`-gated step in a custom `_run_request_async`-side payload.

- [ ] **Refactor `LocalPendingResultBox` → `PendingResultBox` + `LocalPendingResultBox`.**
  In `fxtr2/leases/leased_cap_common.py`:
  - Pull state machine + setters + await methods up into `PendingResultBox[T]` (no
    brand, no token).
  - Setters become idempotent on `"discarded"` (discard inputs' leases and return).
  - Add public `discard()` (idempotent).
  - `LocalPendingResultBox[T, B](PendingResultBox[T])` keeps the existing
    `(token: LifetimeToken[B])` constructor and the token-end subscription +
    `_finalize_box_storage` (`_on_token_ended` becomes `self.discard()`).
  - `IRFutureLocalImpl` is untouched.
  - Add direct `PendingResultBox` unit tests covering the explicit-discard path and
    idempotent-set-after-discard. Existing `LocalPendingResultBox(lt.token)` test
    sites in `tests/test_leases/test_leased_cap_common.py` stay valid.

- [ ] **Move discard authority into `FutureImportEntry`.**
  - Add `delivery_in_flight: bool`, `token_ended: bool`, `box_token_unsubscribe:
    Callable[[], None] | None` to `FutureImportEntry`. Widen the `resolution` field
    to `PendingResultBox[Any] | None`.
  - Add `_materialize_future_box` and `_maybe_discard_future_box` helpers (in
    `lowering.py` is probably cleanest since they touch connection internals — token-
    proxy lookup and the entry's bookkeeping fields).
  - Replace inline box construction in `_on_resolve` and `_on_reject` with the helper.
  - Set/clear `delivery_in_flight` around the spawned `deliver` in `_on_resolve`; call
    `_maybe_discard_future_box` from `deliver`'s `finally`.
  - Call `_maybe_discard_future_box` at the end of `_on_reject` (after
    `set_exception`), symmetric with the resolve path's deliver-finally hook. Reject
    doesn't need `delivery_in_flight` since `eval_static` is synchronous.
  - Hook `_maybe_discard_future_box` into `_decrement_local_consumer` so a draining
    waiter triggers the deferred-discard path.
  - On connection abort: the existing abort path already fires
    `_mark_grant_import_broken` (waking waiters via `wait_break_callbacks`) before
    `_mark_token_import_ended`, so by the time our token-end callback fires the
    waiters are unwinding through the grant-broken raise. Verify the
    `_decrement_local_consumer` hook closes the loop and the box is discarded by the
    time abort returns.

- [ ] **Verify the red test goes green** and the full `tests/test_rpc/` suite still passes.

- [ ] Update `v2/fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` (mention the new discard-policy
  responsibility on `FutureImportEntry`) and the top-level `v2/CODE_OVERVIEW.md` if it
  references `LocalPendingResultBox` directly.

## Open questions / things to revisit

- The `box.is_resolved() AND consumer_count > 0 → defer` half of the gate is what
  prevents a waiter from racing the discard between `set_result` (waker) and its
  synchronous `_descend_and_copy(s.value, ...)` read. Worth a unit test that exercises
  that ordering directly so a future refactor doesn't quietly drop it.
- The connection-abort path is expected to wake waiters via grant-broken before our
  token-end callback fires, so the new discard machinery isn't load-bearing for abort
  semantics. Confirm during implementation that `tests/test_rpc/test_lowering.py`'s
  abort-related tests still pass without further changes.
