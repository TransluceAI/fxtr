# Stage 5 Part 1: Local Capability Implementation

## Overview

Implement the local capability objects for Cap-IR. These wrap Python objects into IRValue
handles so they can be accessed through the IR interface. There is no "local transport" per se —
the local caps are just the mechanism for creating handles from Python objects. The identity
transport (for testing) simply passes a handle through unchanged.

Files:
- `fxtr2/rpc/cap_ir.py` — already exists, defines IR ABCs
- `fxtr2/rpc/basic_local_caps.py` — already exists with stubs, will be fleshed out
- `tests/test_rpc/test_local_caps.py` — unit tests for local cap internals
- `tests/test_rpc/test_transport.py` — transport-parameterized integration tests

## Implementation

### Step 1: `_InternalFuture` ✅

The backing primitive for all local pending values. Has `FuturePending`/`FutureDone`/
`FutureFailed` state types and an `anyio.Event`.

```
_resolve(value):  assert still pending, set state = FutureDone(value), fire event
_reject(exc):     assert still pending, set state = FutureFailed(factory), fire event
wait():           await event, return value or raise factory-produced exception
```

`_reject` builds an exception factory (via `_make_rejection`) rather than storing the
exception directly. Each call to `wait()` invokes the factory, producing a fresh exception
instance. This avoids sharing a single mutable exception object (and its traceback) across
multiple consumers of the same future (e.g. clones). The factory wraps application
exceptions in `RPCError` (with the original as `__cause__`), and re-creates framework
exceptions (`RPCError` subclasses) as new instances of the same type.

Should raise `RuntimeError` if resolved/rejected when already settled (indicates a bug in the
implementation, not a user error).

`_InternalFuture` does not need a reference to the context. It is a standalone resolve-once
primitive. The future is *always* settled by the task that owns it — the task wrapper
catches both normal exceptions and cancellation:

```python
async def _run_dispatch(future, coro):
    try:
        result = await coro
        future._resolve(result)
    except Exception as exc:
        future._reject(exc)
    except BaseException:
        future._reject(RPCResourceBrokenError("RPC context was torn down"))
        raise
```

This means `wait()` is just `await _done_event.wait()` + invoke factory. No racing against
context teardown needed — the task lifecycle handles it:
- Normal completion → resolved
- Application exception → rejected, `wait()` raises `RPCError` wrapping original
- Context exits abnormally → task cancelled → rejected with RPCResourceBrokenError
- Context exits normally → task group waits for tasks to finish, futures already settled

### Step 2: `LocalRPCContext` teardown ✅

The context needs to signal teardown to all handles/futures when its task group exits. Add:

- A `_teardown_event: anyio.Event` that fires when the context's `__asynccontextmanager__`
  exits (in the `finally` block, before clearing `_tg`)
- A `_torn_down: bool` flag so newly created handles can immediately detect teardown
- An `_teardown_exception: BaseException | None` — set to the exception if the task group
  was cancelled/crashed, `None` if it exited cleanly

Handles check the context's teardown state rather than tracking a list of handles. This
avoids the context needing to maintain a set of weak references. Futures do not need context
awareness — they are always settled by their owning task (see Step 1).

### Step 3: `LocalHandle` ✅

#### `discard()`
Set `_discarded = True`, drop `_wrapped_obj = None`. Future calls raise
`RPCResourceDiscardedError`. In-flight calls in the task group are unaffected (they already
captured the object reference).

#### `dispatch_and_wait(attr_path, request)`
The core operation. Steps:
1. Check not discarded, check context not torn down
2. Walk `attr_path` to resolve the target object. At each step:
   - If current value is an `RPCDispatchable` (i.e. `RPCHandle` or `RPCPending`): delegate
     the *remaining* path + request to it via `obj.dispatch_and_wait(remaining_path, request)`
     and return its result. This enables walking through nested handles and pipelining through
     pending values without pulling them first.
   - `list` + `int` key → index
   - `dict` + `str` key → key lookup
   - other object + `str` key (not starting with `_`) → `getattr`
   - raise on invalid path segments
3. If `GetAttribute` (and we didn't delegate to a sub-handle): return the resolved value
4. If `Call`: resolve any `RPCPending` values in the arg (see "Resolving pending args"
   below), then call the resolved object:
   - `None` arg → `obj()` (no arguments)
   - `list` arg → `obj(*arg)` (positional arguments)
   - `dict` arg → `obj(**arg)` (keyword arguments)
   - other → TypeError (arg must be None, list, or dict)
5. If the result is awaitable, await it
6. Return the result

Note on return values: LocalHandle assumes the wrapped Python object already speaks
IRValues. Unrecognized return values are treated as leaf values for whatever transport the
handle is used with. LocalHandle does not enforce leaf type restrictions — that's the
transport's job at serialization time. Since the identity "transport" accepts any Python
object as a leaf, this is always valid locally. If the same handle is later sent through a
JSON transport and returns a non-JSON-leaf value, the error surfaces at the transport layer,
not here.

#### `dispatch(attr_path, request)`
1. Create an `_InternalFuture`
2. Start a background task in the context's task group:
   - Call `dispatch_and_wait(attr_path, request)`
   - On success: `future._resolve(result)`
   - On exception: `future._reject(exc)`
3. Return `LocalPending(future)`

#### `wait_until_teardown()`
Wait for any of:
- `_discarded` becomes True → raise `RPCResourceDiscardedError`
- Context's teardown event fires → raise `RPCResourceBrokenError` or return normally
  depending on whether the context exited cleanly

Implementation: give `LocalHandle` its own `_teardown_event: anyio.Event` and have
`discard()` fire it. Then `wait_until_teardown` races the handle's event against the
context's teardown event:

```
async def wait_until_teardown(self):
    # Wait for whichever fires first
    async with anyio.create_task_group() as tg:
        async def wait_discard():
            await self._teardown_event.wait()
            tg.cancel_scope.cancel()
        async def wait_ctx():
            await self._ctx._teardown_event.wait()
            tg.cancel_scope.cancel()
        tg.start_soon(wait_discard)
        tg.start_soon(wait_ctx)
    # After the group exits, determine which fired and raise/return appropriately
```

### Step 4: `LocalPending` ✅

#### `pull()`
Delegate to `self._future.wait()`. If discarded (`_future is None`), raise
`RPCResourceDiscardedError`.

#### `dispatch_and_wait(attr_path, request)`
1. Wait for the future to resolve (via `_future.wait()`)
2. If the resolved value is `RPCDispatchable`, delegate to it directly
3. Otherwise walk attr_path on the resolved value using the restricted IR walker
   (`_walk_and_dispatch_ir`), which only supports structural navigation into lists
   and dicts plus delegation to any `RPCDispatchable` encountered mid-walk. Unlike
   `LocalHandle`, it does not do `getattr` on arbitrary Python objects or call
   non-dispatchable values — only `LocalHandle` establishes the rules for how
   arbitrary Python objects participate in the IR system.

#### `dispatch(attr_path, request)`
Same pattern as `LocalHandle.dispatch`: create future, spawn background task running
`dispatch_and_wait`, return `LocalPending`.

#### `wait_until_teardown()`
The pending value's lifetime is independent of whatever it resolves to. Teardown
means the pending itself can no longer be used — discard, context break, or future
rejection. If it resolves to a handle that later breaks, that's not the pending's
concern; that error surfaces when you pull/dispatch. If you need to track the
resolved handle's lifetime, use `as_handle()` to get a `LocalPendingHandle`.

1. Race the future settling against discard (via `_wait_or_discard` helper). If
   discarded before the future settles, raise `RPCResourceDiscardedError` immediately.
   If rejected → raise the rejection exception.
2. Future settled (resolved to any value, including a handle). Wait for discard or
   context teardown (via `_wait_for_discard_or_ctx_teardown`).

### Step 5: `LocalPendingHandle` ✅

#### `dispatch_and_wait(attr_path, request)`
1. Wait for the future to resolve
2. Verify the result is an `RPCHandle` (raise if not)
3. Delegate to `handle.dispatch_and_wait(attr_path, request)`

#### `dispatch(attr_path, request)`
Same spawn-background-task pattern.

#### `wait_until_teardown()`
Two-phase approach with proper racing against own lifecycle events:

**Phase 1**: Wait for the future to resolve, racing against discard (via
`_wait_or_discard` helper). If rejected, the exception propagates. If discarded,
raises `RPCResourceDiscardedError`. Verify the result is an `RPCHandle`.

**Phase 2**: Race three things in a task group:
- `handle.wait_until_teardown()` — propagate its result/exception
- Own discard event → `RPCResourceDiscardedError`
- Context teardown event → `RPCResourceBrokenError` or normal return

Priority for ties: discard > context teardown > handle teardown.

### Resolving pending args ✅

When a `Call` arg contains `RPCPending` values nested inside it, LocalHandle needs to resolve
them before making the actual Python call. This is a **local caps concern**, not a transport
concern: a JSON transport just forwards commands to the other side, where the receiving
LocalHandle (or equivalent) handles resolution. Only the code that actually executes the call
on a real Python object needs to unwrap pending values.

```
_resolve_pending_in_value(value):
    RPCPending → pull, recurse into result
    list → resolve all elements concurrently (via anyio task group)
    dict → resolve all values concurrently (via anyio task group)
    RPCTagged → recurse into wrapped value
    other → return as-is
```

List and dict elements are resolved concurrently so that multiple pending values in a
single call arg don't serialize their network round-trips. Recurses into the result of
`pull()` because a pending value might resolve to a structure containing more pending
values (e.g. a list with pending elements).

### Step 6: Path-walking helpers ✅

Two separate walkers, reflecting the different access rules for wrapped Python objects vs
IR values:

- **`_walk_and_dispatch` / `_step_into`** — used by `LocalHandle`. Walks an arbitrary
  Python object: list indexing, dict key lookup, `getattr` (no underscore-prefixed attrs),
  and calling as a Python callable. This is the code that defines how raw Python objects
  participate in the IR system.

- **`_walk_and_dispatch_ir` / `_step_into_ir`** — used by `LocalPending` (for non-handle
  resolved values). Walks an IR value: only lists and dicts can be stepped into, and only
  `RPCDispatchable` values can be called. Other value types are opaque at the IR level.

Both walkers delegate to `RPCDispatchable` when encountered mid-walk.

### Step 7: Update CODE_OVERVIEW.md ✅

Add a section for `fxtr2/rpc/` describing cap_ir.py and basic_local_caps.py.

---

## Implementation notes

During implementation, a few things came up that differ from the original plan:

- **Calling convention**: Changed from "any non-list/non-dict value is a single positional
  arg" to a stricter convention: `None` → no args, `list` → `*args`, `dict` → `**kwargs`,
  anything else → `TypeError`. This avoids confusion about `Call(None)` meaning "pass None
  as an arg" vs "call with no args."
- **Context manager ExceptionGroup collapsing**: `LocalRPCContext.__asynccontextmanager__`
  uses `create_task_group(collapse_exc_group=True)` from `fxtr2.util.anyio_util` to avoid
  anyio 4.x wrapping single body exceptions in `ExceptionGroup`.
- **`RPCError` base class and exception factory**: Added `RPCError` as a base class for all
  RPC exceptions in `cap_ir.py`. `_InternalFuture` stores an exception factory (built by
  `_make_rejection`) rather than the raw exception, so each `wait()` call gets a fresh
  instance. Application exceptions are wrapped in `RPCError`; framework exceptions
  (`RPCError` subclasses) are re-created with the same type.
- **Restricted IR walker for LocalPending**: `LocalPending` uses a separate
  `_walk_and_dispatch_ir` that only walks into lists and dicts, and always delegates calls
  to `RPCDispatchable`. This enforces that only `LocalHandle` establishes the rules for how
  arbitrary Python objects participate in the IR system.
- **Eager object capture in `LocalHandle.dispatch`**: The lambda passed to `_run_dispatch`
  captures `self._wrapped_obj` eagerly (via a local variable) to avoid races with `discard()`.
- **Parallel pending resolution**: `_resolve_pending_in_value` resolves list and dict elements
  concurrently via an anyio task group.
- **Shared teardown helpers**: `_wait_for_discard_or_ctx_teardown` races a local discard event
  against the context's teardown event, used by `LocalHandle` and `LocalPending`.
  `_wait_or_discard` (module-level) races `future.wait()` against a discard event, used by
  both `LocalPending` and `LocalPendingHandle` in `wait_until_teardown`.
- **Separated teardown lifetimes**: `LocalPending.wait_until_teardown` tracks only the
  pending's own lifetime (discard, context break, future rejection) — it does NOT follow
  through to the resolved value's lifetime, even if the value is a handle. To track the
  resolved handle's lifetime, use `as_handle()` to get a `LocalPendingHandle`, whose
  `wait_until_teardown` races the underlying handle's teardown against its own discard and
  context teardown.

---

## Testing ✅

All 80 tests pass (35 unit + 45 transport integration).

### Architecture

Two categories of tests:

#### 1. Unit tests (`test_local_caps.py`) — 35 tests

Directly test the implementation of `basic_local_caps.py` internals:
- `_InternalFuture` resolve/reject/wait mechanics
- `LocalRPCContext` lifecycle and teardown signaling
- `LocalHandle` path walking, call conventions, discard behavior
- `LocalPending` pull, as_handle, discard
- `LocalPendingHandle` delegation to resolved handle

These test the specific logic of local caps and are not parameterized.

#### 2. Transport integration tests (`test_transport.py`) — 45 tests

Test that the behavior of capabilities is correct when accessed through a transport. These
use local caps to construct the handles, then send them through a parameterized transport,
and verify behavior on the other side.

The transport fixture signature:

```python
TransportFixture = Callable[[RPCHandle], AsyncContextManager[RPCHandle]]
```

Takes an `RPCHandle` in, gives an `RPCHandle` out on the other side of the "connection".
The two handles should be logically equivalent but may be different objects.

Transports:
- **Identity**: returns the same handle unchanged. This is the only transport for now.
- **JSON loopback** (Part 2): sets up a bidirectional message queue, exports the handle on
  one side, yields the imported remote handle from the other side.

The test flow is: construct a Python test object → wrap in `LocalHandle` via
`LocalRPCContext` → pass to the transport fixture → get back a handle → exercise it
through the IR interface.

The goal of adding these now (with only the identity transport) is to:
(a) verify the system works end-to-end even without remote transports,
(b) establish the test structure so adding a new transport is just adding a fixture param.

### Unit test cases (test_local_caps.py)

#### _InternalFuture
- [x] resolve then wait → returns value
- [x] reject then wait → raises exception
- [x] wait then resolve → returns value (concurrent)
- [x] wait then reject → raises exception (concurrent)
- [x] double resolve → RuntimeError
- [x] double reject → RuntimeError
- [x] resolve then reject → RuntimeError
- [x] rejection produces independent exception instances per wait()

#### LocalRPCContext
- [x] wrap fails outside context (no active task group)
- [x] context exit fires teardown event
- [x] context exit with exception sets teardown_exception
- [x] context normal exit → handle's wait_until_teardown completes normally
- [x] context normal exit → pending's wait_until_teardown completes normally
- [x] context exception exit → handle's wait_until_teardown raises RPCResourceBrokenError
- [x] context exception exit → pending's wait_until_teardown raises RPCResourceBrokenError
- [x] context exception exit → in-flight dispatch (pending not yet resolved) raises RPCResourceBrokenError on pull

#### LocalHandle specifics
- [x] discard sets flag, drops wrapped object reference
- [x] discard fires teardown event for wait_until_teardown waiters
- [x] clone produces independent handle sharing same wrapped object
- [x] clone of discarded handle → error

#### LocalPending specifics
- [x] pull delegates to future.wait
- [x] pull on discarded pending → RPCResourceDiscardedError
- [x] as_handle returns LocalPendingHandle sharing same future
- [x] as_handle on discarded pending → error
- [x] clone shares same future, independent discard
- [x] dispatch walks into dict through pending (IR walker allows structural types)
- [x] dispatch walks into list through pending (IR walker allows structural types)
- [x] dispatch rejects getattr on non-IR type through pending → TypeError
- [x] dispatch rejects call on non-dispatchable through pending → TypeError

#### LocalPendingHandle specifics
- [x] dispatch_and_wait waits for future, verifies result is handle, delegates
- [x] dispatch_and_wait when future resolves to non-handle → error
- [x] wait_until_teardown on already-discarded pending handle → raises immediately
- [x] wait_until_teardown delegates to resolved handle's teardown (handle discarded → propagates)

### Transport integration test cases (test_transport.py)

#### Basic operations
- [x] Call a method via dispatch_and_wait (list arg → positional, dict arg → kwargs)
- [x] Call with None arg (no arguments)
- [x] Get an attribute via dispatch_and_wait + GetAttribute (object attr, dict key, list index)
- [x] Walk a multi-segment attr_path
- [x] Call with empty attr_path (call the object itself)
- [x] Call an async method
- [x] Return various value types: primitives, lists, dicts, None

#### Path walking with handles
- [x] attr_path walks into a value that is an RPCHandle → delegates remaining path
- [x] attr_path walks through RPCPending → delegates remaining path (pipelining, no pull)
- [x] Method returns an RPCHandle, subsequent dispatch_and_wait on it works

#### Error handling
- [x] Access nonexistent attribute → exception
- [x] Invalid attr_path segment type → exception
- [x] Attribute starting with underscore → rejected
- [x] Called method raises → exception propagated through dispatch_and_wait
- [x] Called method raises → exception propagated through dispatch + pull
- [x] dispatch_and_wait on discarded handle → RPCResourceDiscardedError

#### Pipelining
- [x] dispatch returns pending, pull returns the result
- [x] dispatch → as_handle → dispatch_and_wait chains (promise pipelining)
- [x] Pass a pending value as a Call arg → resolved before delivery
- [x] Nested pending values in list/dict args → all resolved
- [x] Pending arg that resolves to structure with more pendings → all resolved recursively

#### Lifecycle
- [x] clone handle, discard original, clone still works
- [x] clone pending, discard original, pull from clone works
- [x] discard handle → subsequent dispatch raises RPCResourceDiscardedError
- [x] discard pending → subsequent pull raises RPCResourceDiscardedError

#### Teardown
- [x] wait_until_teardown on handle, then discard → raises RPCResourceDiscardedError
- [x] wait_until_teardown on handle, context exits normally → completes normally
- [x] wait_until_teardown on handle, context exits with exception → raises RPCResourceBrokenError
- [x] wait_until_teardown on already-discarded handle → raises immediately
- [x] wait_until_teardown on pending that rejects → raises the rejection exception
- [x] wait_until_teardown on pending that resolves to handle — pending's teardown is independent (handle discard does NOT wake pending; pending only wakes on own discard/ctx exit)
- [x] wait_until_teardown on pending that resolves to plain value → waits until discard/ctx exit
- [x] wait_until_teardown on pending that resolved to plain value, then discard → raises RPCResourceDiscardedError
- [x] wait_until_teardown on pending whose future hasn't settled, then discard → raises RPCResourceDiscardedError immediately
- [x] wait_until_teardown on pending, context exits with exception → raises RPCResourceBrokenError

#### PendingHandle teardown
- [x] wait_until_teardown on pending_handle, resolved handle discarded → propagates discard
- [x] wait_until_teardown on pending_handle, then discard pending_handle → raises RPCResourceDiscardedError
- [x] wait_until_teardown on pending_handle, context exits with exception → raises RPCResourceBrokenError
- [x] wait_until_teardown on pending_handle, context exits normally (future already resolved) → completes normally

#### Context teardown breaks in-flight operations
- [x] context exits with exception while dispatch is in flight → pull raises RPCResourceBrokenError
- [x] context exits with exception while dispatch_and_wait is running → raises RPCResourceBrokenError
- [x] context exits normally while dispatch is in flight → pull raises RPCResourceBrokenError (task was cancelled)
