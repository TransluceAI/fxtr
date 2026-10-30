# Stage 5 Part 1 Post 1: Teardown Reason

## Overview

Change `wait_until_teardown` on `RPCHandle` and `RPCPending` to return an `IRValue[Leaf]`
representing the reason for teardown, instead of returning `None`. The application determines
the meaning of this value. Exception paths (discard, broken) are unchanged — they still raise.

Only the "normal teardown" path changes: instead of `-> None`, it returns an `IRValue[Leaf]`
reason. For the local implementation, this is the string `"exited LocalRPCContext"`.

## Changes

### Step 1: Update ABCs in `cap_ir.py` ✅

Change the return type of `wait_until_teardown` on both `RPCHandle` and `RPCPending`:

```
async def wait_until_teardown(self) -> IRValue[Leaf]:
```

Update the docstrings to mention that the return value is a teardown reason whose meaning
is application-defined.

### Step 2: Add `_teardown_reason` to `LocalRPCContext` ✅

Add a `_teardown_reason: IRValue` field to `LocalRPCContext`, hardcoded to
`"exited LocalRPCContext"` in `__init__`. This is the reason returned by all local
handles/pendings when the context exits cleanly.

### Step 3: Update `_wait_for_discard_or_ctx_teardown` ✅

This helper currently returns `None` on clean context exit. Change it to return
`IRValue` — specifically `ctx._teardown_reason`. The discard and broken paths
still raise as before.

New signature:

```
async def _wait_for_discard_or_ctx_teardown(
    discard_event: anyio.Event,
    ctx: LocalRPCContext,
) -> IRValue:
```

### Step 4: Update `LocalHandle.wait_until_teardown` ✅

Currently the method returns `None` in two places:
- Context already torn down cleanly (early return)
- Delegated to `_wait_for_discard_or_ctx_teardown` (normal path)

Both now return `ctx._teardown_reason`.

The already-torn-down early-return path becomes:
```
return self._ctx._teardown_reason
```

The delegated path already gets the value from the updated helper.

### Step 5: Update `LocalPending.wait_until_teardown` ✅

Phase 1 (waiting for future to settle) is unchanged — rejection still raises,
discard still raises. Phase 2 delegates to `_wait_for_discard_or_ctx_teardown`,
which now returns the reason. Propagate that return value.

### Step 6: Update `LocalPendingHandle.wait_until_teardown` ✅

Phase 1 (waiting for future to resolve) is unchanged.

Phase 2 races three things. The result handling changes:
- `("handle_normal", reason)` — propagate the reason from the resolved handle
- `("handle_error", exc)` — raise (unchanged)
- `("discarded",)` — raise (unchanged)
- `("ctx_teardown",)` — return `self._ctx._teardown_reason` (was `return` / `return None`)

The `wait_handle` task needs to capture the return value from `handle.wait_until_teardown()`:
```
async def wait_handle():
    try:
        reason = await handle.wait_until_teardown()
        if not result:
            result.append(("handle_normal", reason))
    except Exception as exc:
        ...
```

### Step 7: Update tests ✅

Tests that check for normal teardown completion need to also assert the returned
reason value.

**Unit tests (`test_local_caps.py`):**
- `test_normal_exit_handle_teardown_waiter` — assert reason == `"exited LocalRPCContext"`
- `test_exception_exit_handle_teardown_raises_broken` — unchanged (exception path)
- `test_exception_exit_pending_teardown_raises_broken` — unchanged (exception path)

**Transport tests (`test_transport.py`):**
- `test_teardown_on_discard` — unchanged (exception path)
- `test_teardown_on_normal_context_exit` — assert returned reason == `"exited LocalRPCContext"`
- `test_teardown_on_exception_context_exit` — unchanged (exception path)
- `test_teardown_already_discarded` — unchanged (exception path)
- `test_teardown_pending_rejected` — unchanged (exception path)
- `test_teardown_pending_resolves_to_handle_independent` — unchanged (tests discard path)
- `test_teardown_pending_plain_value_waits` — assert returned reason == `"exited LocalRPCContext"`
- `test_teardown_pending_plain_value_discard` — unchanged (tests discard path)
- `test_teardown_pending_unresolved_discard` — unchanged (tests discard path)
- `test_teardown_pending_context_exception` — unchanged (exception path)
- `test_pending_handle_teardown_propagates_handle_discard` — unchanged (tests discard path)
- `test_pending_handle_teardown_own_discard` — unchanged (tests discard path)
- `test_pending_handle_teardown_context_exception` — unchanged (exception path)
- `test_pending_handle_teardown_context_normal_exit` — assert returned reason == `"exited LocalRPCContext"`

Additionally, tests where the waiter captures `"normal"` on success should also capture
and assert the reason value, to verify propagation works.

### Step 8: Create `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` and simplify root CODE_OVERVIEW.md ✅

Refactor the RPC documentation into two tiers:

**`fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`** — detailed documentation for developers working on
the RPC system. Includes:
- Full description of `cap_ir.py` types and their semantics (IRValue, RPCHandle, RPCPending,
  RPCTagged, Call, GetAttribute, RPCDispatchable, exception types)
- Detailed description of `basic_local_caps.py` internals: `_InternalFuture` (resolve-once
  primitive, exception factory pattern), `LocalRPCContext` (task group, teardown signaling,
  teardown reason), `LocalHandle` (path walking rules, pending arg resolution, calling
  convention), `LocalPending` (pipelining, two-phase teardown), `LocalPendingHandle`
  (delegation to resolved handle, two-phase teardown with reason propagation)
- Private helpers: `_walk_and_dispatch` / `_step_into` (Python object walker),
  `_walk_and_dispatch_ir` / `_step_into_ir` (IR value walker), `_resolve_pending_in_value`
  (concurrent pending resolution), `_run_dispatch` (task wrapper),
  `_wait_for_discard_or_ctx_teardown`, `_wait_or_discard`
- Teardown behavior: which paths return reasons vs raise exceptions, reason propagation
  through `LocalPendingHandle`

**Root `CODE_OVERVIEW.md` rpc/ section** — trim to a high-level overview that focuses on:
- What Cap-IR is (one paragraph: capability-passing RPC, extensible variant of Cap'n'Web)
- The public entry points: `LocalRPCContext` (async context manager, `ctx.wrap(obj)`) and
  `LocalHandle` (the resulting handle)
- Key design points: tag-based extensibility, single-argument calling convention, explicit
  pending values, teardown with application-defined reason
- Point to `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` for details

Remove from root overview: private helpers (`_InternalFuture`, `_walk_and_dispatch`, etc.),
internal implementation details of `LocalPending`/`LocalPendingHandle` (these are implied
by the Cap-IR ABCs), detailed path walking rules.
