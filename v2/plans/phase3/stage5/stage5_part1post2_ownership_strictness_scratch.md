# Ownership Strictness — Scratch Notes

## Delayed clone via sync/async split

Daniel's insight: the clone of the arg only needs to happen at the moment of
`_resolve_pending_in_value`, and everything before that is sync (path walking, stepping
into objects). We can make `_walk_and_dispatch` a sync function that determines the
dispatch target, then handle each case separately.

### _walk_to_target (sync)

Split path walking into a sync function:

```python
@dataclass
class _DirectTarget:
    value: Any

@dataclass
class _DelegatedTarget:
    dispatchable: RPCDispatchable
    remaining_path: Sequence[str | int]

def _walk_to_target(current, attr_path) -> _DirectTarget | _DelegatedTarget:
    for i, segment in enumerate(attr_path):
        if isinstance(current, RPCDispatchable):
            return _DelegatedTarget(current, attr_path[i:])
        current = _step_into(current, segment)
    if isinstance(current, RPCDispatchable):
        return _DelegatedTarget(current, ())
    return _DirectTarget(current)
```

Same for IR values:
```python
def _walk_to_target_ir(current, attr_path) -> _DirectTarget | _DelegatedTarget:
    # Same but uses _step_into_ir
```

### How dispatch uses _walk_to_target

**LocalHandle.dispatch_and_wait()**:
```python
target = _walk_to_target(self._wrapped_obj, attr_path)
if isinstance(target, _DelegatedTarget):
    return await target.dispatchable.dispatch_and_wait(target.remaining_path, request)
    # ^ delegate handles ownership; no clone needed
if isinstance(request, GetAttribute):
    return clone_ir_value(target.value)
    # ^ clone from internal state; raw Python objects pass through
return await _execute_owned_call(target.value, request.arg)
# ^ _execute_owned_call clones arg internally
```

**LocalHandle.dispatch()**:
```python
target = _walk_to_target(self._wrapped_obj, attr_path)
if isinstance(target, _DelegatedTarget):
    return target.dispatchable.dispatch(target.remaining_path, request)
    # ^ delegate directly! No background task, no clone, no double-clone.
    # Returns the delegate's RPCPending.
if isinstance(request, GetAttribute):
    future = _InternalFuture()
    future._resolve(clone_ir_value(target.value))
    return LocalPending(future, self._ctx)
# Call: clone arg synchronously (for background task safety), spawn task
cloned_arg = clone_ir_value(request.arg)
future = _InternalFuture()
tg.start_soon(_run_dispatch, future,
    lambda: _execute_owned_call_with_cloned_arg(target.value, cloned_arg))
return LocalPending(future, self._ctx)
```

Key insight for dispatch(): when delegating, we call `dispatch()` (not
`dispatch_and_wait()`) on the inner dispatchable. This returns its RPCPending directly.
No background task needed. No double-clone.

### _execute_owned_call

Two variants:
- `_execute_owned_call(fn, arg)` — clones arg internally (for dispatch_and_wait)
- `_execute_owned_call_with_cloned_arg(fn, cloned_arg)` — arg already cloned (for dispatch)

Or we can use one function that takes ownership of the arg:

```python
async def _execute_owned_call(fn, arg_to_own):
    """Execute a call. Takes ownership of arg_to_own (will discard it)."""
    try:
        resolved = await _resolve_pending_in_value(arg_to_own)
        try:
            result = _invoke_fn(fn, resolved)
            if inspect.isawaitable(result):
                result = await result
            result_copy = clone_ir_value(result)
            discard_ir_value(result)
            return result_copy
        finally:
            discard_ir_value(resolved)
    finally:
        discard_ir_value(arg_to_own)
```

- dispatch_and_wait: `_execute_owned_call(fn, clone_ir_value(request.arg))`
- dispatch (background): `_execute_owned_call(fn, cloned_arg)` where cloned_arg was
  cloned synchronously before spawning the task

No double-clone: the clone happens once — either inline (dispatch_and_wait) or
synchronously before spawning (dispatch).

## Leak fix in _resolve_pending_in_value

The leak scenario: `_resolve_pending_in_value` is cancelled partway through, leaving
intermediate pulled values (which may contain handles) undiscarded.

Fix: discard pendings eagerly + rollback at each recursive level.

```python
async def _resolve_pending_in_value(value):
    if isinstance(value, RPCPending):
        resolved = await value.pull()  # deep copy from pull
        value.discard()                # release the pending clone
        try:
            return await _resolve_pending_in_value(resolved)
        except BaseException:
            discard_ir_value(resolved)  # clean up the pulled value
            raise

    if isinstance(value, list):
        results = [None] * len(value)
        async def _resolve_item(idx, val):
            results[idx] = await _resolve_pending_in_value(val)
        try:
            async with anyio.create_task_group() as tg:
                for i, v in enumerate(value):
                    tg.start_soon(_resolve_item, i, v)
            return results
        except BaseException:
            for item in results:
                if item is not None:
                    discard_ir_value(item)
            raise

    if isinstance(value, dict):
        resolved_dict: dict[str, Any] = {}
        async def _resolve_entry(k, v):
            resolved_dict[k] = await _resolve_pending_in_value(v)
        try:
            async with anyio.create_task_group() as tg:
                for k, v in value.items():
                    tg.start_soon(_resolve_entry, k, v)
            return resolved_dict
        except BaseException:
            for v in resolved_dict.values():
                discard_ir_value(v)
            raise

    if isinstance(value, RPCTagged):
        resolved_value = await _resolve_pending_in_value(value.value)
        # Tag is not an IRValue container, so no cleanup needed on failure
        # (or clone tag too if it could contain capabilities — unlikely)
        return RPCTagged(value.tag, resolved_value)

    return value  # leaf, handle (pass through), etc.
```

Why this works with cancellation:

When a task in a task group is cancelled (e.g., because another item failed), the
cancellation exception (BaseException) propagates through the recursive
`_resolve_pending_in_value` calls. At each level:
1. The pending (if any) was already discarded (before the try block)
2. The except block catches BaseException and discards the intermediate pulled value
3. The exception re-raises to the next level

anyio guarantees that cancelled tasks' cleanup handlers run before the task group's
exception propagates. So all inner cleanup completes before outer cleanup.

## Cleanup ordering in _execute_owned_call

After the function call:
1. `clone_ir_value(result)` — create caller's independent copy FIRST
2. `discard_ir_value(result)` — discard function's return (ownership transfer)
3. `discard_ir_value(resolved)` — discard resolved arg (in finally)
4. `discard_ir_value(arg_to_own)` — discard original cloned arg (in finally)

The result and resolved may share handle references (if the function returned an
argument handle). Steps 2 and 3 may discard the same handle — safe due to idempotent
discard. Step 1 creates independent copies before any discards happen.

On failure (function raises):
1. No clone of result
2. finally: discard resolved, then discard arg_to_own

The resolved tree's handles are cleaned up. The arg_to_own's remaining pendings (already
discarded by _resolve_pending_in_value) are no-ops. Handles shared between resolved and
arg_to_own are discarded once (idempotent second discard).

## _InternalFuture refcounting

```python
class _InternalFuture:
    def __init__(self):
        self._state = FuturePending()
        self._done_event = anyio.Event()
        self._ref_count = 1

    def _add_ref(self):
        if self._ref_count <= 0:
            raise RuntimeError("Cannot add ref to released future")
        self._ref_count += 1

    def _release(self):
        self._ref_count -= 1
        if self._ref_count == 0:
            self._cleanup_if_resolved()

    def _cleanup_if_resolved(self):
        if isinstance(self._state, FutureDone) and self._state.value is not None:
            discard_ir_value(self._state.value)
            self._state = FutureDone(None)  # sentinel: already cleaned up

    def _resolve(self, value):
        if not isinstance(self._state, FuturePending):
            raise RuntimeError("Future has already been settled")
        if self._ref_count <= 0:
            # Nobody will ever observe this value — discard immediately
            discard_ir_value(value)
            self._state = FutureDone(None)
        else:
            self._state = FutureDone(value)
        self._done_event.set()
```

`wait()` returns the raw shared value (for internal use). `pull()` calls
`clone_ir_value(await future.wait())` for the user-facing deep copy.

## LocalPending changes

```python
class LocalPending:
    def __init__(self, future, ctx):
        self._future = future      # reference to _InternalFuture
        self._ctx = ctx
        self._teardown_event = anyio.Event()

    def clone(self):
        future = self._get_future()
        future._add_ref()
        return LocalPending(future, self._ctx)

    def as_handle(self):
        future = self._get_future()
        future._add_ref()
        return LocalPendingHandle(future, self._ctx)

    async def pull(self):
        future = self._get_future()
        value = await future.wait()
        return clone_ir_value(value)

    def discard(self):
        if self._future is not None:
            self._future._release()
        self._future = None
        self._teardown_event.set()
```

Similarly for LocalPendingHandle.

## LocalPending dispatch changes

**dispatch_and_wait()**: No upfront clone. Await future, then:
- Dispatchable → delegate (handles ownership)
- IR walk → clone result from internal state
- Call → delegate to dispatchable (handles ownership) or raise TypeError

**dispatch()**: Clone arg synchronously. Spawn background task. Task awaits future, then:
- Dispatchable → call its dispatch_and_wait() (double-clone for arg, acceptable)
- IR walk → clone result
- For Call that reaches a dispatchable mid-walk: dispatch_and_wait handles ownership

Note: for LocalPending.dispatch(), we can't avoid the double-clone in the delegate case
because we need to await the future before knowing the target. The background task must
call dispatch_and_wait (not dispatch) to get the value for the output future. Acceptable
trade-off.

## Test changes

### Modified tests

- `test_pending_handle_teardown_propagates_handle_discard`: Pull returns a copy, so
  discarding it doesn't affect the internal handle. Update test to verify this behavior
  and instead test teardown via own discard.

### New tests

- Pull returns independent copy (pull twice, verify handles are independent)
- Dispatch arg is cloned (mutate arg after dispatch, computation unaffected)
- Handles in dispatch arg are cleaned up after dispatch returns
- Discarding pending cleans up handles in resolved value
- Clone pending shares future; only last clone triggers cleanup
- "Returning transfers ownership" convention works correctly

## Behavioral note: "returning transfers ownership"

Functions wrapped in LocalHandle must follow the convention: returning a handle (or value
containing handles) from dispatch transfers ownership. If the function stores a handle
AND returns it, the dispatch mechanism will discard the original return value (after
cloning it for the caller). The function's stored copy becomes invalid.

Functions that want to keep a handle must clone it before returning. This is a new
constraint relative to the current model.

This convention matches Cap'n'Web's `fromAppReturn()` behavior.
