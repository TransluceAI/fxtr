# Cap-IR / Cap'n'Web Value and Ownership Model Correspondence

This document describes how the value models and ownership semantics of Cap-IR and Cap'n'Web
relate to each other, identifies invariants in each system, and checks whether those invariants
are correctly maintained. It also proposes changes to Cap-IR to align its ownership model with
Cap'n'Web's, closing gaps that could cause resource leaks.

## 1. Value Type Correspondence

### 1.1 User-facing types

| Cap'n'Web | Cap-IR | Role |
|-----------|--------|------|
| `RpcStub` | `RPCHandle` | Pass-by-reference capability. Cannot be pulled. |
| `RpcPromise` | `RPCPending` | Deferred computation result. Can be pulled. |
| (plain JS value) | `IRValue[Leaf]` leaf types | Primitive data (int, float, str, bool, None) |
| (JS object/array) | `list` / `dict` | Structural containers |
| `Error` | `Exception` | Error values |
| (no equivalent) | `RPCTagged` | Tagged value wrapper |

In Cap'n'Web, `RpcStub` and `RpcPromise` are both backed by a `StubHook` and distinguished by
whether the hook has a `pathIfPromise`. The user can call functions on both (via JS proxy), pull
promises (via `.then()`), and navigate properties (via JS proxy). The type system doesn't
enforce the stub/promise distinction at the language level.

In Cap-IR, `RPCHandle` and `RPCPending` are distinct ABCs with different method sets. `RPCHandle`
has `dispatch`, `dispatch_and_wait`, `clone`, `discard`, `wait_until_teardown`. `RPCPending`
adds `pull` and `as_handle` (for speculative handle conversion), and its `wait_until_teardown`
has different phase semantics (rejection is a form of teardown). The type system enforces the
distinction.

**Correspondence**: `RPCHandle` ≈ a Cap'n'Web stub that is known not to be a promise.
`RPCPending` ≈ a Cap'n'Web promise or payload hook. The distinction is: handles are live
capabilities you dispatch to; pendings are deferred value references you pull.

### 1.2 Internal hook types

| Cap'n'Web | Cap-IR | Notes |
|-----------|--------|-------|
| `RpcImportHook` (stub) | `JSONTransportHandle` | Remote handle import |
| `RpcImportHook` (promise) | `JSONTransportPending` | Remote pending import |
| `PayloadStubHook` | `LocalPending` (≈) | Holds a resolved value tree |
| `TargetStubHook` | `LocalHandle` | Wraps a live Python object |
| `PromiseStubHook` | `LocalPendingHandle` (≈) | Promise for a capability |
| `ErrorStubHook` | Rejected `_InternalFuture` | Error state |

`PayloadStubHook` is the closest analog to `LocalPending`, but with an important distinction:
in Cap'n'Web, `PayloadStubHook` explicitly owns an `RpcPayload` that tracks all stubs within.
In the current Cap-IR, `LocalPending` holds an `_InternalFuture` that resolves to an `IRValue`,
but there is no explicit payload ownership — the resolved value's handles are not tracked or
cleaned up. **This is the primary gap this document addresses.**

## 2. Ownership Model

### 2.1 Cap'n'Web's ownership model

Cap'n'Web's ownership is centered on `RpcPayload`, a linear-typed container for an IRValue tree.

**Payload states:**
- `"params"`: Value came from app as call arguments. Stubs within are NOT owned (duped on use).
- `"return"`: Value came from app as return value. Stubs within ARE owned.
- `"owned"`: Value has been deep-copied or deserialized. Stubs within are owned and tracked.

**Key invariants:**
1. **Payload tracks its stubs.** An `"owned"` payload maintains `hooks: StubHook[]` and
   `promises: LocatedPromise[]` listing all capabilities in the value tree.
2. **Disposing a payload disposes its stubs.** When a payload is disposed, all tracked stubs
   are disposed (releasing imports, cleaning up exports).
3. **Deep copy on delivery.** Before delivering a value to application code (via `deliverCall`
   or `deliverResolve`), the payload is deep-copied (`ensureDeepCopied`). This substitutes
   promises with their resolved values and creates independently-owned stub clones.
4. **Argument cleanup.** `deliverCall` disposes the argument payload in a `finally` block after
   the function returns. This cleans up argument stubs that the function didn't capture.
5. **Return ownership transfer.** `fromAppReturn(result)` wraps the function's return value
   and takes ownership of all stubs within. The function has transferred ownership by returning.
6. **Pull returns shared reference.** The internal `hook.pull()` returns the payload's own
   value (shared, not copied). The caller and hook share the same payload. Only one dispose is
   needed.
7. **User-facing pull copies.** The user-facing `await promise` path goes through
   `deliverResolve`, which deep-copies the value and attaches a disposer. The user receives
   an independently-owned result.

**Dispatch lifecycle in Cap'n'Web (`deliverCall`):**
```
1. ensureDeepCopied() — deep copy the argument payload, resolving promises, duping stubs
2. func.apply(this, value) — call the function with the deep-copied args
3. fromAppReturn(result) — wrap return value, take ownership
4. finally: this.dispose() — dispose the argument payload (all duped stubs cleaned up)
```

The convention is: **returning from a dispatch transfers ownership**. If the function stores a
stub and also returns it, the argument disposal will invalidate the stored copy. The function
must clone before returning if it wants to keep a reference.

### 2.2 Cap-IR's current ownership model

Cap-IR currently has **no explicit payload ownership**. Handles and pendings are Python objects
with clone/discard lifecycle methods, but:
- `LocalPending` holds an `_InternalFuture` that resolves to an `IRValue`. When the pending is
  discarded, `self._future = None` — the Python reference is dropped. There is no explicit
  cleanup of handles/pendings within the resolved value.
- `LocalHandle.dispatch()` passes the raw `Call.arg` to `_walk_and_dispatch` without cloning.
  The caller's handles are shared with the dispatch mechanism.
- `dispatch_and_wait()` returns the raw result. No deep copy. No ownership tracking.
- `pull()` returns `await future.wait()` — the raw resolved value. No deep copy.

**Consequence:** Handles in dispatch arguments, resolved values, and pulled values can leak.
If a dispatch creates import entries (e.g., evaluating `["export", id]` on a transport) and
the computation fails, those import entries persist without anyone tracking them. The remote
side holds corresponding exports that are never released.

For local-only usage (no transport), this isn't visible because `LocalHandle` is a lightweight
wrapper around a Python object, and Python's GC handles the memory. But for transport handles,
explicit cleanup is required.

### 2.3 Proposed Cap-IR ownership model

The proposed model adds payload-style ownership to Cap-IR, making `RPCPending` the owner of its
resolved value tree (and of dispatch argument trees during computation).

**Core principles:**

1. **RPCPending owns a payload.** An `RPCPending` conceptually owns an IRValue tree. When the
   last clone of a pending is discarded, the owned tree is cleaned up: all `RPCHandle` and
   `RPCPending` values within it are recursively discarded.

2. **Deep copy on dispatch (arguments).** When dispatching `Call(arg)`, the dispatch mechanism
   clones the entire `arg` tree (cloning handles and pendings within). The caller's originals
   are untouched. The clones are used for the computation and cleaned up afterward.

3. **Deep copy on dispatch (results).** After the computation completes, the result is cloned
   to make it independent from the argument clones and from the function's internal state.
   The original result and argument clones are then discarded. The clone is stored as the
   pending's owned value.

4. **Ownership transfer on return.** The convention for functions exposed via `RPCHandle`:
   returning a value transfers ownership. If a function creates a handle, stores it internally,
   and also returns it, the dispatch mechanism will discard the returned original (after cloning
   it for the result). The function must clone before returning if it wants to keep a copy.

5. **`pull()` returns a deep copy.** `RPCPending.pull()` returns an independently-owned clone
   of the resolved value. The caller owns the clone. The pending retains its internal copy.
   Multiple pulls return independent copies.

6. **`dispatch_and_wait()` transfers to caller.** The result is deep-copied and returned to the
   caller with ownership. The caller is responsible for discarding handles in the result.

7. **`GetAttribute` results are deep-copied.** Consistent with Cap'n'Web's `get()` which
   returns `deepCopyFrom(value)`. Reading an attribute that returns a handle gives the caller
   an independent clone.

8. **Refcounted shared internal state.** `RPCPending.clone()` produces a new pending that
   shares the same `_InternalFuture`. The future tracks a refcount. When the last clone
   discards, the future's resolved value is cleaned up.

**Proposed dispatch lifecycle:**
```
dispatch(Call(arg)):
  1. cloned_arg = clone_ir_value(arg)               — clone argument handles/pendings
  2. resolved_arg = resolve_pending_in(cloned_arg)   — pull cloned pendings (returns copies)
  3. raw_result = call_function(resolved_arg)         — execute
  4. result = clone_ir_value(raw_result)              — clone result for independence
  5. discard_ir_value(raw_result)                     — discard function's return (ownership transfer)
  6. discard_ir_value(resolved_arg)                   — discard resolved arg handles
  7. discard_ir_value(cloned_arg)                     — discard pending clones
  8. future._resolve(result)                          — future owns the independent result
```

Steps 5-7 use idempotent discard, so overlapping handles (e.g., a handle that was in both the
argument and the return value) are safely handled.

## 3. Helper Operations

Two fundamental operations on IRValue trees support the ownership model:

**`clone_ir_value(value: IRValue) -> IRValue`** — Deep copy an IRValue tree.
- Primitive types (int, float, str, bool, None): pass through (immutable).
- `list`: recursively clone each element, produce a new list.
- `dict`: recursively clone each value, produce a new dict.
- `RPCTagged`: clone tag and value, produce a new RPCTagged.
- `Exception`: pass through (treated as immutable for ownership purposes).
- `RPCHandle`: call `clone()`, producing an independent handle.
- `RPCPending`: call `clone()`, producing an independent pending.
- Non-IRValue types (raw Python objects from LocalHandle): pass through.

**`discard_ir_value(value: IRValue) -> None`** — Recursively discard all capabilities.
- `RPCHandle`: call `discard()`.
- `RPCPending`: call `discard()`.
- `list`, `dict`, `RPCTagged`: recurse into children.
- All other types: no-op.
- **Must be idempotent**: discarding an already-discarded handle/pending is a no-op.

Both operations only need to handle the IRValue union type. For values that are raw Python
objects (e.g., a Calculator instance returned by LocalHandle's GetAttribute), they pass through
unchanged. *[Daniel note: this should not generally occur with a value such as "a Calculator instance". any unrecognized raw Python objects should be assumed to be immutable leaf primitives, not as instances that could be dispatched to. a more realistic example would be something like a raw CBOR decode result, which would be passed around as a primitive.]*

## 4. Invariant Comparison

### 4.1 Invariants that match

| # | Invariant | Cap'n'Web | Cap-IR (proposed) |
|---|-----------|-----------|-------------------|
| 1 | Stubs are not shared across dispatch boundaries | Deep copy via `ensureDeepCopied` | Deep copy via `clone_ir_value` |
| 2 | Argument stubs are cleaned up after dispatch | `deliverCall` `finally` block | Dispatch thunk `finally` discards cloned args |
| 3 | Return value ownership is transferred | `fromAppReturn` takes ownership | Convention: return = transfer. Result cloned, original discarded. |
| 4 | Pending/payload disposal cleans up contained stubs | `RpcPayload.dispose()` | `_InternalFuture._release()` calls `discard_ir_value` when refcount hits 0 |
| 5 | Clone produces independent lifecycle | `dup()` creates new hook with shared entry | `clone()` creates new wrapper, increments refcount on shared future |
| 6 | Dispose/discard is idempotent | Hook sets `entry = undefined` | Handle sets `_discarded = True`, pending sets `_future = None` |
| 7 | Remote import refcounting | `localRefcount` on `ImportTableEntry` | `local_refcount` on `ImportEntry` |
| 8 | Export refcounting for re-export dedup | `refcount` on `ExportTableEntry` | `refcount` on `ExportEntry` |
| 9 | Connection abort cleans up all state | `abort()` iterates imports/exports | Abort sequence discards all imports/exports |

### 4.2 Invariants where Cap-IR improves on Cap'n'Web

| # | Area | Cap'n'Web behavior | Cap-IR improvement |
|---|------|--------------------|--------------------|
| A | Remote refcount on re-introduction | `remoteRefcount` starts at 1, never incremented for re-introductions | `remote_refcount` correctly incremented on each `["export", id]` re-introduction |
| B | Handle teardown notification | No proactive teardown notification; only `onBroken` on connection break | `teardown`/`broken` messages sent when handle exports tear down |
| C | Type safety for handle vs. pending | Runtime check (`isPromise` flag, duck typing) | Distinct ABCs (`RPCHandle`, `RPCPending`) enforced by type system |
| D | User-facing pull semantics | `pull()` returns shared reference; user must know disposal rules | `pull()` returns deep copy; independently owned, no shared-state confusion |

### 4.3 Invariants that were missing in Cap-IR (now addressed)

| # | Gap | How it manifests | Proposed fix |
|---|-----|------------------|--------------|
| 1 | No payload ownership on RPCPending | Handles in resolved values leak when pending is discarded | RPCPending owns resolved value; last clone triggers `discard_ir_value` |
| 2 | No argument cloning on dispatch | Caller's handles shared with dispatch; function could invalidate caller's references | Deep copy argument before dispatch |
| 3 | No result cloning on dispatch | Result handles shared between function internals and caller | Clone result after function returns |
| 4 | No argument cleanup after dispatch | Handles in arguments not cleaned up on failure | Discard argument clones in `finally` block |
| 5 | No result cleanup convention | Functions can return handles without transferring ownership | Convention: returning = transfer. Dispatch discards original. |
| 6 | No refcounting on shared futures | Multiple clones of a pending don't coordinate cleanup | Add refcount to `_InternalFuture`; last release triggers cleanup |

### 4.4 Intentional divergences from Cap'n'Web

| # | Area | Cap'n'Web | Cap-IR | Rationale |
|---|------|-----------|--------|-----------|
| 1 | Promise resolution before delivery | Promises in values MUST be resolved before delivery to app | Promises (RPCPending) delivered as-is; resolution handled at application/IR boundary | Keeps transport layer simple; LocalHandle resolves before calling Python functions |
| 2 | Payload as explicit type | `RpcPayload` is a first-class type passed between subsystems | No explicit payload type; ownership tracked implicitly via RPCPending + refcounting | Simpler Python API; RPCPending IS the payload container |
| 3 | Internal pull semantics | `hook.pull()` returns shared reference (for pipelining efficiency) | `RPCPending.pull()` returns deep copy | Safer user API. Transport can use internal methods for efficiency. |
| 4 | Deep copy timing | Deep copy happens lazily (on `ensureDeepCopied`, often at delivery) | Deep copy happens eagerly (at dispatch boundaries) | Simpler reasoning about when copies happen |

## 5. Lifecycle Diagrams

### 5.1 dispatch() lifecycle

```
Caller                          LocalHandle                        Function
  |                                 |                                 |
  |-- dispatch(Call(arg)) --------->|                                 |
  |                                 |-- clone_ir_value(arg) --------> cloned_arg
  |                                 |-- spawn task ------------------>|
  |<-- return LocalPending ---------|                                 |
  |                                 |   [in background task:]         |
  |                                 |   resolve_pending(cloned_arg)   |
  |                                 |   result = func(resolved_arg)   |
  |                                 |   result_copy = clone(result)   |
  |                                 |   discard(result)               |
  |                                 |   discard(resolved_arg)         |
  |                                 |   discard(cloned_arg)           |
  |                                 |   future.resolve(result_copy)   |
  |                                                                   |
  |-- pull() on pending ----------->|                                 |
  |<-- clone_ir_value(resolved) ----|                                 |
```

### 5.2 dispatch_and_wait() lifecycle

```
Caller                          LocalHandle                        Function
  |                                 |                                 |
  |-- dispatch_and_wait(Call(arg))->|                                 |
  |                                 |-- clone_ir_value(arg) --------> cloned_arg
  |                                 |-- resolve_pending(cloned_arg)   |
  |                                 |-- result = func(resolved_arg) ->|
  |                                 |<- result = return value --------|
  |                                 |-- result_copy = clone(result)   |
  |                                 |-- discard(result)               |
  |                                 |-- discard(resolved_arg)         |
  |                                 |-- discard(cloned_arg)           |
  |<-- return result_copy ---------|                                 |
  |   (caller owns result_copy)     |                                 |
```

### 5.3 Pending clone lifecycle

```
  pending = handle.dispatch(...)      -- refcount = 1
  clone1 = pending.clone()            -- refcount = 2
  clone2 = pending.clone()            -- refcount = 3

  v1 = await pending.pull()           -- returns clone_ir_value(resolved), refcount unchanged
  v2 = await clone1.pull()            -- returns another clone_ir_value(resolved)

  pending.discard()                   -- refcount = 2
  clone1.discard()                    -- refcount = 1
  clone2.discard()                    -- refcount = 0 → discard_ir_value(resolved_value)

  # v1 and v2 are independently owned by caller, unaffected by pending cleanup
```

## 6. Impact on _InternalFuture

The `_InternalFuture` currently has three states (`FuturePending`, `FutureDone`, `FutureFailed`)
and an event. Under the proposed model, it also needs:

- **`_ref_count: int`** — starts at 1 on creation. Incremented by `LocalPending.clone()`,
  `LocalPending.as_handle()`, `LocalPendingHandle.clone()`. Decremented by their `discard()`.
- **Cleanup on last release**: when `_ref_count` reaches 0 and state is `FutureDone`,
  call `discard_ir_value(state.value)`.

The refcount tracks how many `LocalPending` / `LocalPendingHandle` instances reference this
future. The future's resolved value is "alive" as long as any clone exists. When the last clone
discards, the resolved value and all its capabilities are cleaned up.

Note: `_InternalFuture.wait()` is unchanged. It returns the raw resolved value (shared
reference). The deep copy happens in `LocalPending.pull()`, which calls `wait()` and then
`clone_ir_value()`. This separation keeps the internal future simple and allows the transport
to use `wait()` directly for efficient access when it knows it owns the future.

## 7. Impact on Dispatch Helpers

### _resolve_pending_in_value

Unchanged in interface. Currently resolves RPCPending values by calling `pull()`. Under the new
model, `pull()` returns a deep copy, so the resolved values in the returned tree are
independently owned.

The pending clones in the original (cloned) argument tree are NOT discarded by this function —
they're cleaned up by the dispatch mechanism's `finally` block via `discard_ir_value(cloned_arg)`.

*[Daniel note: Seems like a slightly tricky invariant here. We should make sure that across the whole `call` dispatch, including both `_resolve_pending_in_value` and the argument cloning, we always clone everything and then clean up all the clones. so if the original arg contains a pending value, and we pull it, at some point we need to both (a) release the original pending, (b) release all values inside the deep-clone pulled result. one way to do this would be to immediately release each pending when we pull it, and then have the finally-block cleanup dispose the fully-resolved argument]*

### _walk_and_dispatch (LocalHandle)

Changes:
- `GetAttribute` at end of path: `return clone_ir_value(current)` instead of `return current`.
  For raw Python primitives, this is a no-op. For raw Python objects (Calculator *[Daniel note: not quite, see above]*), this passes
  through (not an IRValue type to clone). For IRValue types (RPCHandle in a dict), this clones.
- `Call`: argument resolution and cleanup handled by the dispatch mechanism (caller of
  `_walk_and_dispatch`), not by this function itself.

### _walk_and_dispatch_ir (LocalPending)

Changes:
- `GetAttribute` at end of path: `return clone_ir_value(current)` instead of `return current`.
  Since the current value is part of the pending's owned resolved value tree, the caller gets
  an independent clone.

## 8. Impact on Transport Design

The JSON transport's `JSONTransportHandle` and `JSONTransportPending` already have clone/discard
semantics via import entry refcounting. The proposed ownership model aligns naturally:

- **Expression evaluation** creates import entries (via `["export", id]` and `["promise", id]`).
  These entries are "owned" by whatever context uses the evaluation result.
- **Evaluation rollback** (proposed): if expression evaluation fails partway through, all import
  entries created during the evaluation are discarded. This prevents leaks from partial
  evaluations (matching Cap'n'Web's `Evaluator.evaluate()` catch-and-dispose pattern).
- **Encoding rollback** (already designed): if expression encoding fails, all export entries
  created during the encoding pass are rolled back.
- **Dispatch on exports**: when an incoming push/submit evaluates to a dispatch on a local
  export, the dispatch follows the same clone-on-dispatch rules. The result is stored as the
  export entry's value. Cleanup of dispatch arguments happens as described above.

The proposed ownership model means the transport doesn't need its own separate "payload tracking"
mechanism — the IRValue-level clone/discard/refcount operations provide the same guarantees.

## 9. Summary of Required Changes

### cap_ir.py
- Document ownership rules in `RPCPending.pull()`, `RPCDispatchable.dispatch()`,
  `RPCDispatchable.dispatch_and_wait()` docstrings.
- Add `clone_ir_value()` and `discard_ir_value()` helper functions.

### basic_local_caps.py
- Add `_ref_count` to `_InternalFuture` with `_add_ref()` / `_release()` methods.
- On last release of a resolved future: `discard_ir_value(resolved_value)`.
- `LocalPending.clone()`: call `_add_ref()`.
- `LocalPending.as_handle()`: call `_add_ref()`.
- `LocalPending.discard()`: call `_release()`.
- `LocalPendingHandle.clone()`: call `_add_ref()`.
- `LocalPendingHandle.discard()`: call `_release()`.
- `LocalPending.pull()`: return `clone_ir_value(await future.wait())`.
- `LocalHandle.dispatch()`: clone arg, dispatch with clone, clone result, discard originals.
- `LocalHandle.dispatch_and_wait()`: clone arg, dispatch, clone result, discard originals.
- `_walk_and_dispatch`: clone result for `GetAttribute`.
- `_walk_and_dispatch_ir`: clone result for `GetAttribute`.

### tests
- Existing tests should continue to pass (the deep copy is transparent for primitives and the
  tests already don't rely on object identity for handles).
- New tests for ownership behavior: verify that discarding a pending cleans up handles in the
  resolved value, verify that pull() returns independent copies, verify that argument handles
  survive dispatch (caller's originals are untouched).

## 10. Open Questions

1. **`RPCConcretePayload` helper**: Should we provide a common `RPCPending` implementation that
   holds concrete (already-resolved) values and discards them when itself is discarded? This
   would be useful for the transport's "trivially resolved pending" and for wrapping concrete
   results. (Likely yes, but can be added when needed.)
   => Resolution: Defer until needed.

2. **Efficiency for transport internals**: The transport's resolve watcher needs to encode the
   resolved value of an export. Under the proposed model, `pull()` returns a deep copy. The
   watcher could instead use an internal `_InternalFuture.wait()` to access the raw value
   directly (without copying), since the watcher owns the clone and will discard it after
   encoding. This is an implementation detail, not a design concern.
   => Resolution: Defer until needed.

3. **Exception values in resolve trees**: When a pending rejects, the rejection is an exception,
   not an IRValue tree. Exceptions don't contain handles/pendings (they're terminal values).
   So the cleanup on rejection is a no-op. If we later allow exceptions to contain IRValues
   (e.g., error context), we'd need to extend the cleanup.
   => Resolution: Yes, cleanup of exceptions should be free, they cannot contain IRValues.

4. **Depth limit for clone/discard**: `clone_ir_value` and `discard_ir_value` recurse into
   lists, dicts, and tagged values. Deeply nested structures could cause stack overflow.
   Consider adding a depth limit matching the transport's encoding/decoding limit.
   => Resolution: Defer until needed.
