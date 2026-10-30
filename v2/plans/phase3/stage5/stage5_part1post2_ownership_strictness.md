# Stage 5 Part 1.5: Ownership Strictness

Implements the ownership model described in `stage5_part1post2_ir_correspondence.md`.
The goal is to make Cap-IR's local caps enforce the same ownership invariants as
Cap'n'Web's payload system: dispatch arguments are copied, return values have ownership
transferred, pending values own their resolved trees, and pull returns independent copies.

Scratch notes: `stage5_part1post2_ownership_strictness_scratch.md`

## Steps

### 1. Add `clone_ir_value` and `discard_ir_value` to `cap_ir.py`

Two sync functions that operate on `IRValue` trees.

**`clone_ir_value(value: IRValue[Leaf]) -> IRValue[Leaf]`:**
- Primitives (int, float, str, bool), None, Exception: pass through (immutable).
- `list`: new list with each element recursively cloned.
- `dict`: new dict with each value recursively cloned.
- `RPCTagged`: new RPCTagged with tag and value recursively cloned.
- `RPCHandle`: call `clone()`.
- `RPCPending`: call `clone()`.
- Any other type (unknown leaves): pass through (assumed immutable).

**`discard_ir_value(value: IRValue[Leaf]) -> None`:**
- `RPCHandle`: call `discard()`. Must be idempotent (already-discarded = no-op).
- `RPCPending`: call `discard()`. Must be idempotent.
- `list`: recurse into each element.
- `dict`: recurse into each value.
- `RPCTagged`: recurse into tag and value.
- All other types: no-op.

Both functions treat unrecognized types as opaque leaves (pass through / no-op).

Status: **done**

### 2. Add refcounting to `_InternalFuture`

Add `_ref_count: int` (starts at 1), `_add_ref()`, `_release()`.

**`_add_ref()`**: Increment refcount. Raise if refcount was already 0.

**`_release()`**: Decrement refcount. If it hits 0 and the state is `FutureDone`, call
`discard_ir_value` on the resolved value. Use a `FutureDone(None)` sentinel (or a
separate `FutureCleaned` state) to mark that cleanup has occurred, so `wait()` after
cleanup can raise an appropriate error.

**`_resolve()` change**: If `_ref_count` is already 0 when `_resolve` is called (all
clones discarded before the computation finished), immediately call
`discard_ir_value(value)` on the incoming value and store the sentinel. Still set the
`_done_event` (so any racing `_wait_or_discard` completes).

Status: **done**

### 3. Update `LocalPending` and `LocalPendingHandle` for refcounting

**LocalPending:**
- `clone()`: call `future._add_ref()` before creating the new instance.
- `as_handle()`: call `future._add_ref()` before creating the `LocalPendingHandle`.
- `discard()`: call `future._release()` (in addition to the existing
  `self._future = None` and `self._teardown_event.set()`). Must handle the case where
  the pending was already discarded (idempotent: if `_future` is already None, skip
  release).

**LocalPendingHandle:**
- `clone()`: call `future._add_ref()`.
- `discard()`: call `future._release()` (in addition to existing cleanup). Same
  idempotency requirement.

Status: **done**

### 4. Update `LocalPending.pull()` to return deep copy

Change from `return await future.wait()` to
`return clone_ir_value(await future.wait())`.

This ensures the caller gets an independently-owned copy. The future retains its
internal copy for other clones to pull.

Status: **done**

### 5. Restructure path walking into sync `_walk_to_target`

Split `_walk_and_dispatch` into a sync path walker and separate action handling.

**`_walk_to_target(current, attr_path)`** — sync function that walks the path:
- If an `RPCDispatchable` is encountered mid-walk (or at end of path): return
  `_DelegatedTarget(dispatchable, remaining_path)`.
- Otherwise: return `_DirectTarget(value)` with the value at the end of the path.

Similarly, **`_walk_to_target_ir(current, attr_path)`** — same but uses `_step_into_ir`
(only lists and dicts, no getattr).

`_step_into` and `_step_into_ir` are unchanged. `_walk_and_dispatch` and
`_walk_and_dispatch_ir` are replaced by the callers using `_walk_to_target` /
`_walk_to_target_ir` + action handling (see steps 6 and 8).

Status: **done**

### 6. Add ownership logic to `LocalHandle` dispatch

**New helper: `_execute_owned_call(fn, arg_to_own)`** — async function that takes
ownership of the arg and handles the full call lifecycle:
1. `resolved = await _resolve_pending_in_value(arg_to_own)` (see step 7)
2. Invoke `fn` with `resolved` (using the existing None/list/dict calling convention).
3. `result_copy = clone_ir_value(result)` — clone the return value.
4. `discard_ir_value(result)` — ownership transfer: discard the original.
5. In finally: `discard_ir_value(resolved)`, then `discard_ir_value(arg_to_own)`.
6. Return `result_copy`.

The ordering matters: step 3 (clone result) happens before steps 4-5 (discards), so the
caller's copy is created while all values are still alive.

**LocalHandle.dispatch_and_wait():**
```
target = _walk_to_target(self._wrapped_obj, attr_path)
if _DelegatedTarget → await target.dispatchable.dispatch_and_wait(remaining_path, request)
  (delegate handles ownership; no clone needed)
if GetAttribute → return clone_ir_value(target.value)
if Call → return await _execute_owned_call(target.value, clone_ir_value(request.arg))
```

No upfront arg clone — the clone happens at the `_execute_owned_call` boundary, which is
still before any async context switch (the `clone_ir_value` call is sync). This avoids
double-clone in the delegation case.

**LocalHandle.dispatch():**
```
target = _walk_to_target(self._wrapped_obj, attr_path)
if _DelegatedTarget → return target.dispatchable.dispatch(remaining_path, request)
  (delegate directly! Returns the delegate's RPCPending. No background task, no clone.)
if GetAttribute → resolve immediately:
  future = _InternalFuture()
  future._resolve(clone_ir_value(target.value))
  return LocalPending(future, self._ctx)
if Call → clone arg synchronously, spawn background task:
  cloned_arg = clone_ir_value(request.arg)
  tg.start_soon(_run_dispatch, future, lambda: _execute_owned_call(fn, cloned_arg))
  return LocalPending(future, self._ctx)
```

For Call: the arg is cloned synchronously before the task starts (necessary because the
caller can mutate/discard after `dispatch()` returns). `_execute_owned_call` receives the
already-cloned arg and takes ownership of it. No double-clone.

For delegation: we call `dispatch()` on the inner dispatchable, NOT
`dispatch_and_wait()`. This returns the inner pending directly — no background task, no
intermediate future, no double-clone. The inner dispatchable handles its own arg cloning.

Status: **done**

### 7. Update `_resolve_pending_in_value` for ownership and rollback

Two changes: (a) discard each pending after pulling, (b) clean up intermediate values
on failure.

**Pending case:**
```
resolved = await value.pull()   # deep copy from pull
value.discard()                 # release the pending clone immediately
try:
    return await _resolve_pending_in_value(resolved)
except BaseException:
    discard_ir_value(resolved)  # clean up the pulled value on failure
    raise
```

**List case:**
```
results = [None] * len(value)
try:
    # resolve items concurrently (existing task group pattern)
    return results
except BaseException:
    for item in results:
        if item is not None:
            discard_ir_value(item)
    raise
```

Same pattern for dict and tagged values.

**Why this handles cancellation correctly:** When a task in a task group is cancelled,
the cancellation exception (BaseException) propagates through the recursive calls. At
each level, the except block discards intermediate values. anyio guarantees that
cancelled tasks' cleanup runs before the task group's exception propagates, so inner
cleanup completes before outer cleanup.

Status: **done**

### 8. Update `LocalPending` dispatch for ownership

**LocalPending.dispatch_and_wait() / _dispatch_and_wait_impl:**

After `await future.wait()`:
- If resolved is RPCDispatchable: delegate to `dispatch_and_wait()`. The dispatchable
  handles ownership. No cloning needed.
- Otherwise: `_walk_to_target_ir(resolved, attr_path)`.
  - `_DelegatedTarget`: delegate to `dispatch_and_wait()`.
  - `_DirectTarget` + GetAttribute: `return clone_ir_value(target.value)` — clone from
    the pending's internal state.
  - `_DirectTarget` + Call: raise TypeError (cannot call non-dispatchable through pending,
    same as current behavior).

**LocalPending.dispatch():**

Clone arg synchronously (necessary for background task). Spawn background task that
awaits the future, then follows the same logic as `dispatch_and_wait` above.

Note: for the delegation case inside the background task, we call `dispatch_and_wait()`
(not `dispatch()`) because we need the result to resolve the output future. This means
the arg gets cloned twice (once by us, once by the delegate). This is acceptable — it
only happens when a pending resolves to a dispatchable that is called through, which is
uncommon. The alternative (restructuring to chain futures) adds complexity without
meaningful benefit.

**LocalPendingHandle.dispatch_and_wait():**

Unchanged in structure (await future, assert RPCHandle, delegate). The resolved handle's
`dispatch_and_wait` handles ownership. No cloning needed on our side.

**LocalPendingHandle.dispatch():**

Same structure (await future, assert RPCHandle, delegate to `dispatch()`). The resolved
handle's `dispatch()` handles ownership.

Wait — `LocalPendingHandle.dispatch()` currently spawns a background task that awaits the
future and then calls `dispatch_and_wait` on the resolved handle. To avoid double-clone,
we could instead: await the future, get the resolved handle, call `dispatch()` on it,
return its pending. But this requires the background task to return an RPCPending, not an
IRValue. The current `_run_dispatch` pattern resolves a future.

For now, keep the existing pattern (background task + `dispatch_and_wait`). The
double-clone is bounded and acceptable. We can optimize later if needed.

Status: **done**

### 9. Update tests

**Modified tests:**

- `test_pending_handle_teardown_propagates_handle_discard`: Under the new model,
  `pull()` returns a copy, so discarding it does NOT propagate to the pending_handle.
  Update to verify that discarding a pulled copy does not wake the pending_handle, and
  instead verify teardown via own discard.

**New tests (in `test_local_caps.py`):**

- `test_pull_returns_independent_copy`: Pull the same pending twice. Verify the two
  results don't share handle identity (if the resolved value contains a handle, the two
  pulls should return different handle objects).
- `test_discard_pending_cleans_up_resolved_handles`: Create a pending that resolves to a
  value containing a handle. Discard the pending. Verify the handle inside was discarded.
- `test_last_clone_discard_triggers_cleanup`: Create a pending, clone it. Discard one —
  internal value should still be alive. Discard the other — internal value should be
  cleaned up (verify via handle discard).
- `test_resolve_after_all_clones_discarded`: All clones discarded before computation
  completes. Verify the resolved value is immediately cleaned up (not leaked).

**New tests (in `test_transport.py`):**

- `test_dispatch_arg_independent_after_return`: Dispatch a call, then mutate the arg
  list. Verify the computation is unaffected (it used a copy).
- `test_dispatch_cleans_up_arg_handles`: Dispatch a call where the arg contains a
  handle. After the dispatch completes, verify the arg handle clone was cleaned up.
- `test_dispatch_result_independent_from_pending_internal`: A function returns a value
  containing a handle. The dispatch produces a pending. Verify that:
  (a) the function's original handle (accessible via a side channel, e.g. stored on the
  function object) was discarded by the machinery (ownership transfer convention), and
  (b) pulling the pending returns a copy that is independent — mutating the pulled
  result (e.g. appending to a list) does not affect subsequent pulls from the same
  pending.
- `test_dispatch_result_handles_discarded_by_machinery`: A function returns a value
  containing a handle. Verify that the raw handle the function returned (observable via
  a side channel) is discarded after the dispatch machinery clones it — even though the
  pending's internal cloned copy remains alive. This tests the "returning transfers
  ownership" convention end-to-end.

Status: **done**

### 10. Update docs

- Update `LOCAL_CODE_OVERVIEW.md` to describe the ownership model, `_walk_to_target`,
  `_execute_owned_call`, refcounting on `_InternalFuture`, and the modified
  `_resolve_pending_in_value` rollback behavior.
- Update `CODE_OVERVIEW.md` if the ownership model warrants a high-level mention.

Status: **done**

## Notes

### "Returning transfers ownership" convention

Functions wrapped in `LocalHandle` must follow the convention: returning a value from
a dispatch transfers ownership of any handles/pendings in that value. If a function
stores a handle internally AND returns it, the dispatch mechanism will discard the
original return value (after cloning it for the caller). The function's stored copy
becomes invalid. Functions that want to keep a handle must clone it before returning.

This matches Cap'n'Web's `fromAppReturn()` behavior and is consistent with the general
principle that dispatch boundaries are ownership boundaries.

### Known limitation: LocalPending.dispatch() double-clone on delegation

When `LocalPending.dispatch()` spawns a background task that delegates to a resolved
dispatchable, the arg is cloned twice (once synchronously before the task, once by the
delegate's dispatch_and_wait). This is acceptable for v1 — it's bounded and only happens
in the uncommon case of dispatching through a pending that resolves to a dispatchable.

### Deferred: RPCConcretePayload

A common `RPCPending` implementation that wraps an already-resolved value, useful for
trivially-resolved pendings. Not needed for this change but will be useful for the
JSON transport. Can be added later.

### Deferred: depth limits

`clone_ir_value` and `discard_ir_value` recurse into nested structures. Deeply nested
or cyclic structures could cause stack overflow. Add a depth limit when implementing
the transport (which needs encoding/decoding depth limits anyway).
