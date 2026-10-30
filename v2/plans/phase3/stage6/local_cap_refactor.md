# Local Cap Handle Refactor: Composition-Based Unified Lifecycle

## Motivation

Three RPCHandle implementations (`LocalHandle`, `_FxtrInternalCapIRRPCHandle`,
`ArenaCapabilityHandle`) and three RPCPending implementations (`LocalPending`,
`_FxtrLocalPending`, `_ArenaCapPending`) duplicate lifecycle management code:
refcounting, clone/discard, wait_until_teardown, arg cloning, and background task
spawning.

This duplication has caused real bugs — the release_scope-on-error deadlock, the
arena ID partitioning issue, and the inconsistent arg cloning in dispatch_and_wait
all stem from parallel implementations drifting apart. Three ref-tracking classes
(`_HandleRefTracker`, `_FxtrHandleRefTracker`, `_ArenaCapSharedState`) implement
the same pattern with minor variations.

The goal: separate "how to invoke the target" (the impl) from "lifecycle management"
(the handle wrapper), using composition. The impl should be as minimal as possible.
Everything else — refcounting, clone, discard, arg cloning, pending creation — lives
in one place.

## Design

### Architecture

```
ManagedHandle[Leaf] — RPCHandle[Leaf] implementation
  ├── lifecycle: clone, discard, wait_until_teardown, refcounting
  ├── dispatch wrapper: _check_usable, arg cloning, task spawning, pending creation
  ├── ctx: LocalCapContext  ← required, provides task group + teardown signals
  └── _impl: DispatchImpl[Leaf]  ← only this part varies
        └── async execute(attr_path, request) -> IRValue[Leaf]
```

The key insight: the subclass-specific dispatch logic is just one async function.
Everything else is identical across all three handle types.

### DispatchImpl protocol

The impl knows how to execute a dispatch. It does NOT know about refcounts,
contexts, pendings, or arg cloning. It receives an already-cloned request and
returns an owned result.

```python
class DispatchImpl[Leaf](abc.ABC):
    @abc.abstractmethod
    async def execute(
        self,
        attr_path: Sequence[str | int],
        request: Call[Leaf] | GetAttribute,
    ) -> IRValue[Leaf]:
        """Execute a dispatch, returning an owned result.

        The request arg has already been cloned by the caller. The returned
        value is owned by the caller — the impl must not keep references to it.
        """
        ...
```

Three implementations:

**`LocalDispatchImpl`** (replaces LocalHandle's dispatch logic):
- Wraps a Python object
- Path walks via `_walk_to_target`, calls functions via `_execute_owned_call`
- Handles delegation to RPCDispatchable targets

**`FxtrDispatchImpl`** (replaces _FxtrInternalCapIRRPCHandle's dispatch logic):
- Wraps a Python object + LocalFxtrRPCContext
- Path walks, validates fxtr:call tag, runs `_run_fxtr_call`

**`ArenaDispatchImpl`** (replaces ArenaCapabilityHandle's dispatch logic):
- Wraps a ReadableArena
- Routes to load_data / release_scope
- Holds domain-specific state (on_scope_released, scope_released)

### Context protocol (required)

All handles require a context. Both `LocalRPCContext` and `LocalFxtrRPCContext`
already have the needed fields. Extract a protocol:

```python
class LocalCapContext(Protocol):
    _torn_down: bool
    _teardown_event: anyio.Event
    _teardown_exception: BaseException | None
    _teardown_reason: IRValue
    _active_trackers: set[HandleRefTracker]

    def _check_active(self) -> anyio.abc.TaskGroup: ...
```

Both existing context classes already satisfy this.

**`RemoteFxtrRPCContext` gets a task group.** Currently it only wraps a scope.
Adding a task group follows the same pattern as `LocalFxtrRPCContext._open()` and
makes it a proper `LocalCapContext`, so arena caps created through it can use
`dispatch()` and have proper teardown semantics. The task group also provides a
natural place for background work if RemoteFxtrRPCContext grows features later.

```python
class RemoteFxtrRPCContext(_FxtrRPCContextBase, anyio.abc.AsyncResource):
    # ... existing fields ...

    @contextlib.asynccontextmanager
    async def _open(self) -> AsyncGenerator[Self]:
        self._scope = FxtrScope(name="fxtr_rpc_remote_connection")
        async with self._scope:
            async with anyio.create_task_group() as tg:
                self._tg = tg
                try:
                    yield self
                except BaseException as exc:
                    self._teardown_exception = exc
                    raise
                finally:
                    # Same teardown pattern as LocalFxtrRPCContext
                    for tracker in list(self._active_trackers):
                        tracker.context_teardown()
                    self._active_trackers.clear()
                    self._torn_down = True
                    self._teardown_event.set()
                    self._tg = None
                    tg.cancel_scope.cancel()
```

The `_FxtrRPCContextBase` default `_create_arena_cap` can then always pass `self`
as the context, since all concrete subclasses are proper contexts.

### ManagedHandle[Leaf]

One concrete class replaces all three handle types. Context is always required.

```python
class ManagedHandle[Leaf](RPCHandle[Leaf]):
    def __init__(
        self,
        impl: DispatchImpl[Leaf],
        ctx: LocalCapContext,
        on_released: Callable[[str], None] | None = None,
    ) -> None:
        self._impl = impl
        self._ctx = ctx
        self._discarded = False
        self._teardown_event = anyio.Event()
        self._tracker = HandleRefTracker(ctx, on_released) if on_released else None

    def _check_usable(self) -> None:
        if self._discarded:
            raise RPCResourceDiscardedError(...)
        if self._ctx._torn_down:
            raise RPCResourceBrokenError(...)

    def clone(self) -> ManagedHandle[Leaf]:
        if self._discarded:
            raise RPCResourceDiscardedError(...)
        if self._tracker is not None:
            self._tracker.add_ref()
        return ManagedHandle(self._impl, self._ctx, _tracker=self._tracker)

    def discard(self) -> None:
        if not self._discarded and self._tracker is not None:
            self._tracker.release()
        self._discarded = True
        self._teardown_event.set()

    def dispatch(
        self, attr_path, request: Call[Leaf] | GetAttribute
    ) -> RPCPending[Leaf, IRValue[Leaf]]:
        self._check_usable()
        tg = self._ctx._check_active()
        cloned_request = _clone_request(request)
        future = _InternalFuture()
        captured_path = tuple(attr_path)
        tg.start_soon(
            _run_dispatch, future,
            lambda: self._impl.execute(captured_path, cloned_request),
        )
        return LocalPending(future, self._ctx)

    async def dispatch_and_wait(
        self, attr_path, request: Call[Leaf] | GetAttribute
    ) -> IRValue[Leaf]:
        self._check_usable()
        cloned_request = _clone_request(request)
        return await self._impl.execute(attr_path, cloned_request)

    async def wait_until_teardown(self) -> IRValue[Leaf]:
        self._check_usable()
        return await _wait_for_discard_or_ctx_teardown(
            self._teardown_event, self._ctx
        )
```

Notes:
- No None checks on `self._ctx` — it's always present.
- `dispatch()` and `dispatch_and_wait()` both clone the request arg. This is
  consistent with the Cap-IR contract and fixes the existing bug where
  `_FxtrInternalCapIRRPCHandle.dispatch_and_wait` doesn't clone.
- `dispatch()` always spawns a background task. This loses the sync fast path
  for GetAttribute (immediate resolution without a task), but correctness >
  micro-optimization. See "Follow-up optimizations" below.
- The `clone()` constructor takes a `_tracker` kwarg (not `on_released`) so
  clones share the same tracker without creating a new one.

### Unified HandleRefTracker

Replace all three ref-tracking classes with one:

```python
class HandleRefTracker:
    def __init__(
        self,
        ctx: LocalCapContext,
        on_released: Callable[[str], None] | None = None,
    ):
        self._ref_count = 1
        self._on_released = on_released
        self._ctx = ctx
        ctx._active_trackers.add(self)

    def add_ref(self) -> None: ...

    def release(self) -> None:
        # decrement; if 0: deregister from ctx, fire on_released("discarded")

    def context_teardown(self) -> None:
        # fire on_released("teardown")
```

- `on_released` is optional (handles without callbacks just refcount)
- Context is always provided (tracker always registers/deregisters)
- Replaces `_HandleRefTracker`, `_FxtrHandleRefTracker`, and `_ArenaCapSharedState`

### DispatchImpl implementations

#### LocalDispatchImpl

```python
class LocalDispatchImpl(DispatchImpl[Any]):
    def __init__(self, wrapped_obj: Any) -> None:
        self._obj = wrapped_obj

    async def execute(self, attr_path, request):
        target = _walk_to_target(self._obj, attr_path)
        if isinstance(target, _DelegatedTarget):
            return await target.dispatchable.dispatch_and_wait(
                target.remaining_path, request
            )
        if isinstance(request, GetAttribute):
            return clone_ir_value(target.value)
        if isinstance(target.value, RPCDispatchable):
            return await target.value.dispatch_and_wait((), request)
        return await _execute_owned_call(target.value, request.arg)
```

Note: delegation uses `dispatch_and_wait` (not `dispatch`). This loses the
pipelining-through-delegation optimization. See "Follow-up optimizations".

#### FxtrDispatchImpl

```python
class FxtrDispatchImpl(DispatchImpl[JSONLeaf]):
    def __init__(self, wrapped_obj: Any, ctx: LocalFxtrRPCContext) -> None:
        self._obj = wrapped_obj
        self._fxtr_ctx = ctx

    async def execute(self, attr_path, request):
        target = _walk_to_target(self._obj, attr_path)
        if isinstance(target, _DelegatedTarget):
            return await target.dispatchable.dispatch_and_wait(
                target.remaining_path, request
            )
        if isinstance(request, GetAttribute):
            return clone_ir_value(target.value)
        if isinstance(target.value, RPCDispatchable):
            return await target.value.dispatch_and_wait((), request)
        # Fxtr-specific: validate fxtr:call tag, run scope pipeline
        fn = target.value
        arg = request.arg
        if not (isinstance(arg, RPCTagged) and arg.tag == FXTR_CALL_TAG):
            raise TypeError(...)
        return await _run_fxtr_call(self._fxtr_ctx, fn, arg.value)
```

`_run_fxtr_call_in_background` becomes `_run_fxtr_call` — a normal async function
that returns an `IRValue` or raises. The `_InternalFuture` management is no longer
needed because `ManagedHandle.dispatch()` handles that via `_run_dispatch`. This
is a significant simplification: the try/except/future._resolve/future._reject
plumbing goes away entirely.

#### ArenaDispatchImpl

```python
class ArenaDispatchImpl(DispatchImpl[JSONLeaf]):
    def __init__(self, arena: ReadableArena) -> None:
        self._arena = arena
        self._on_scope_released: Callable | None = None
        self._scope_released = False

    def set_on_scope_released(self, callback): ...

    async def execute(self, attr_path, request):
        if isinstance(request, GetAttribute):
            raise TypeError(...)
        method = attr_path[0] if attr_path else None
        if method == "release_scope":
            self._do_release_scope()
            return None
        if method != "load_data" and attr_path:
            raise AttributeError(...)
        # ... extract id_string, call _load_data ...
```

Domain-specific state (`on_scope_released`, `scope_released`) lives on the impl.
Callers access it via `handle._impl`:
```python
handle = ManagedHandle(ArenaDispatchImpl(arena), ctx=ctx, on_released=cleanup)
handle._impl.set_on_scope_released(release_keep_alive)
```

### Pending unification

**Replace `_FxtrLocalPending` and `_ArenaCapPending` with `LocalPending`.**

`LocalPending` already does everything needed. Change:
- Accept `LocalCapContext` instead of `LocalRPCContext` (protocol, not concrete type)

`_FxtrLocalPending` is functionally identical to `LocalPending` in practice — the
dispatch restriction (RPCDispatchable-only vs IR navigation) doesn't matter since
fxtr call results are always RPCDispatchable. `_ArenaCapPending` was just a
future-backed pending, same as `LocalPending`.

One pending class covers all three use cases.

### Helper: _clone_request

Small utility used by `ManagedHandle`:

```python
def _clone_request(request: Call[Leaf] | GetAttribute) -> Call[Leaf] | GetAttribute:
    if isinstance(request, Call):
        return Call(clone_ir_value(request.arg))
    return request
```

## What this eliminates

**Deleted classes (7):**
- `_FxtrHandleRefTracker` (replaced by unified `HandleRefTracker`)
- `_FxtrLocalPending` (replaced by `LocalPending`)
- `_ArenaCapSharedState` (replaced by unified `HandleRefTracker` + impl state)
- `_ArenaCapPending` (replaced by `LocalPending`)
- `_FxtrInternalCapIRRPCHandle` (replaced by `ManagedHandle` + `FxtrDispatchImpl`)
- `ArenaCapabilityHandle` (replaced by `ManagedHandle` + `ArenaDispatchImpl`)
- `LocalHandle` (replaced by `ManagedHandle` + `LocalDispatchImpl`)

**Simplified:**
- `_run_fxtr_call_in_background` loses future/exception plumbing, becomes a normal
  async function `_run_fxtr_call`
- `RemoteFxtrRPCContext` gets a task group, becomes a proper `LocalCapContext`
- `_FxtrRPCContextBase._create_arena_cap` can always pass `self` as context

**Bug fixes for free:**
- `dispatch_and_wait` always clones args (fixes the inconsistency in
  `_FxtrInternalCapIRRPCHandle`)
- One implementation of refcounting (no more drift between three versions)
- Pending cleanup on discard is consistent everywhere
- Arena cap `wait_until_teardown` properly races against context teardown

## What stays the same

- `_run_fxtr_call` internal scope/entity logic is unchanged
- `LocalRPCContext` is unchanged (already satisfies `LocalCapContext`)
- `LocalFxtrRPCContext` is unchanged (already satisfies `LocalCapContext`)
- External APIs (`FxtrRPCHandle`, `FxtrRPCPending`, etc.) are unchanged
- Wire encoding, IR conversion — unchanged
- All tests exercise the same code paths

## `_FxtrRPCContextBase` satisfies `LocalCapContext`

Since both `LocalFxtrRPCContext` and `RemoteFxtrRPCContext` will be proper
`LocalCapContext`s, the base class should declare this. Add the protocol fields
as abstract properties/methods on `_FxtrRPCContextBase`. Then `_create_arena_cap`
can pass `self` as the context without a cast.

## `_run_fxtr_call` simplification

Currently `_run_fxtr_call_in_background` manages an `_InternalFuture` internally
(resolves/rejects it) and has a complex try/except with `future._resolve()` /
`future._reject()`. In the new design, `ManagedHandle.dispatch()` spawns the
impl's `execute()` inside `_run_dispatch`, which already handles the
try/except/BaseException pattern.

`_run_fxtr_call_in_background` becomes `_run_fxtr_call` — a normal async function
that returns an `IRValue[JSONLeaf]` or raises. The future/exception plumbing goes
away entirely. The `finally` block that can mask the handler's exception (to_revisit
#10) also goes away since exception propagation is natural.

## `on_scope_released` as a constructor parameter

Currently `on_scope_released` is set mutably via `set_on_scope_released()` after
the arena cap is created. This requires an `isinstance` check and reaching into
the handle to set the callback — an antipattern.

There's no reason for this to be mutable. The callback is always set immediately
after construction in the same function (`_dispatch_on_inner`). With the
composition design, `ArenaDispatchImpl` takes it as a constructor parameter:

```python
impl = ArenaDispatchImpl(
    arena,
    on_scope_released=lambda _: keep_alive_token.release(),
)
handle = ManagedHandle(impl, ctx=ctx, on_released=cleanup)
```

No mutation, no `isinstance`, no `handle._impl` access. The `_create_arena_cap`
factory passes it through:

```python
def _create_arena_cap(self, arena, on_scope_released=None, on_released=None):
    impl = ArenaDispatchImpl(arena, on_scope_released=on_scope_released)
    return ManagedHandle(impl, ctx=self, on_released=on_released)
```

## Follow-up optimizations (not in this refactor)

**Delegation pipelining**: `LocalDispatchImpl.execute` uses `dispatch_and_wait`
for delegation, losing the ability to pipeline through delegated RPCDispatchables.
To restore this, `DispatchImpl` could grow an optional `try_fast_dispatch` method
that returns a pending synchronously for delegation cases. `ManagedHandle.dispatch`
would try this before spawning a background task.

**Sync GetAttribute fast path**: Currently `LocalHandle.dispatch()` resolves
GetAttribute synchronously (no background task). In the unified model, all
dispatches go through background tasks. To restore the fast path, `execute` could
check for GetAttribute + non-dispatchable target and return immediately; the
background task overhead is minimal (cooperative scheduling) but avoidable.

## Risk assessment

**Low risk:**
- The lifecycle logic is purely mechanical — easy to verify
- All existing tests exercise the same code paths
- The dispatch logic (the complex part) moves into impl classes but doesn't change

**Medium risk:**
- `basic_local_caps.py` changes affect 454 tests. Mitigated by: `LocalHandle`
  becomes `ManagedHandle` + `LocalDispatchImpl` with identical behavior.
- `LocalPending` accepting `LocalCapContext` protocol instead of `LocalRPCContext`
  is a type annotation change only. All existing callers pass a context.
- `RemoteFxtrRPCContext` gaining a task group changes its exit behavior (cancels
  background tasks on exit). Currently it has no background tasks, so this is
  safe. Tests will verify.

## Implementation steps

1. [x] Add `LocalCapContext` ABC to `basic_local_caps.py`
2. [x] Add `DispatchImpl[Leaf]` ABC to `basic_local_caps.py`
3. [x] Unify `HandleRefTracker` (context required, callback optional)
4. [x] Implement `ManagedHandle[Leaf]` with dispatch/dispatch_and_wait/clone/discard/
   wait_until_teardown
5. [x] Add `_clone_request` helper
6. [x] Implement `LocalDispatchImpl` (extract from current `LocalHandle`)
7. [x] Refactor `LocalHandle` → `ManagedHandle` + `LocalDispatchImpl` (`LocalHandle`
   kept as type alias)
8. [x] Make `LocalPending` accept `LocalCapContext`
9. [x] Run full test suite — all 454 tests pass
10. [x] Add task group + teardown to `RemoteFxtrRPCContext`, make it satisfy
    `LocalCapContext`
11. [x] Implement `FxtrDispatchImpl` (extract from current `_FxtrInternalCapIRRPCHandle`)
12. [x] Simplify `_run_fxtr_call_in_background` → `_run_fxtr_call` (drop future plumbing)
13. [x] Replace `_FxtrInternalCapIRRPCHandle` with `ManagedHandle` + `FxtrDispatchImpl`
14. [x] Delete `_FxtrLocalPending`, `_FxtrHandleRefTracker`
15. [x] Run full test suite — all 454 tests pass
16. [x] Implement `ArenaDispatchImpl` (extract from current `ArenaCapabilityHandle`)
17. [x] Replace `ArenaCapabilityHandle` with `ManagedHandle` + `ArenaDispatchImpl`
18. [x] Delete `_ArenaCapPending`, `_ArenaCapSharedState`, remove `start_soon` plumbing
19. [x] Run full test suite — all 454 tests pass
20. [x] Update LOCAL_CODE_OVERVIEW.md and CODE_OVERVIEW.md
