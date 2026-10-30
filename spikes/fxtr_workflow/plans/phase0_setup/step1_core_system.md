# Step 1: Core Workflow System

## Goal

Implement the minimum viable workflow system: external steps, cache slots (write + resume), step context, and top-level entrypoint. No replay references, no checkpoints, no `run_fallible`, no `run_uncached`, no managed steps. After this, it should be possible to run a simple multi-step workflow, kill it partway through, and resume it with finished sub-steps reused from the cache.

## What's in scope

- `Pending` type (awaitable future wrapper)
- `StepHandle` (external only, backed by a callable + fxtr entity key)
- `CacheSlot` state machine (Empty / Split / Partial / Finished)
- `StepContext` (dispatch sub-steps via cache slots, access internal cache slots)
- `TopLevelEntrypoint` (create handles, open cache slots)
- Persistent storage of cache state via fxtr2 `SQLiteFxtrNamespace`
- Deep-walk arg resolution (resolve nested `Pending` values before dispatch)
- Cache key computation via fxtr2 entity hashing
- Error propagation as `BaseException` (abort semantics), with poisoning flag
- Cache-slot-arg replay check (detect stale results when slot contents changed between runs)
- Tests for all of the above

## What's NOT in scope

- `run_uncached` (deferred to step 2 — not needed for core caching story and the in-memory throwaway slot is a distraction)
- `run_fallible`, `save_checkpoint`, `load_checkpoint` (step 2)
- Managed steps, replay references, migration/provenance
- Returning `CacheSlot` from a step is not allowed (hard to get right, disallowed by design). `StepHandle` in results may be supported eventually as a plain data object but not in phase 1.

---

## Design Decisions

### Storage layout

Each cache slot maps to a single pin key in a `SQLiteFxtrNamespace`. The pin's entity is a tagged union (Rust enum style) encoding the slot's state and all associated data:

- **Empty**: no pin exists for this prefix
- **Split**: entity of type `SlotSplit` (no fields) — subslots exist but no run in this slot
- **Partial**: entity of type `SlotPartial(cache_key_id: FxtrID)` — a run is in progress or was interrupted. `cache_key_id` is the FxtrID of the cache key entity (which itself encodes step identity + args, so step_handle_key_id is derivable from it).
- **Finished**: entity of type `SlotFinished(cache_key_id: FxtrID, result: <serialized>, slot_deps: <snapshot>)` — run completed. `result` is the serialized return value. `slot_deps` records cache-slot-arg dependency state for the replay check.

These are fxtr2 entity types (dataclasses). The slot state is encoded in which type is stored, not as a field.

Pin key scheme:
- `<prefix>` → the slot's state entity
- Subslots (from `CacheSlot.subslot`): `<prefix>/s/<key>`
- Internal cache slots (from `StepContext.cache_slot` inside a run): `<prefix>/c/<key>`

### Cache key computation

A cache key entity encodes the step identity + all resolved args:
- The step handle contributes its key entity's FxtrID
- Primitives are serialized directly
- Lists/dicts are serialized recursively
- Cache slot args are replaced by a reserved placeholder entity (an internal type that marks "a cache slot was here at this position")
- Step handle args contribute their identity FxtrID

The entity is stored via `scope.store_cbor()` to get its content-addressed FxtrID, which becomes the cache key.

### Execution model

`TopLevelEntrypoint` is an async context manager (using `anyio.AsyncContextManagerMixin`) that owns a task group.

When `cache_slot.run()` is called:
1. Spawn a task in the entrypoint's task group
2. Return a `Pending` immediately
3. Inside the task:
   - Deep-walk args to await all nested `Pending` values
   - Compute cache key from resolved args
   - Acquire per-slot lock (for concurrent-run semantics)
   - Check slot state against cache key:
     - **Finished + matching key** → run cache-slot-arg replay check (see below). If pass → return cached result. If fail → abort workflow (stale cache).
     - **Finished + different key** → abort workflow (write-once violation)
     - **Partial + matching key** → re-execute (sub-slots provide caching)
     - **Partial + different key** → abort workflow (write-once violation)
     - **Empty** → transition to Partial, execute
     - **Split** → abort workflow (slot used for subslots, can't run)
   - Create `StepContext`, call step impl
   - On success → transition to Finished, persist state entity (result + slot_deps)
   - On failure → leave as Partial, propagate as `StepAbortError(BaseException)`

### Pending

Backed by an anyio `Event` + result/exception slot. `__await__` waits on the event and returns the result (or re-raises). When passed as an arg to another step, the runtime resolves it during arg deep-walk before dispatch.

### Error propagation

Uncaught exceptions in a step are wrapped in `StepAbortError(BaseException)` and propagated up. Since it's a BaseException, normal `except Exception` won't catch it. The slot stays Partial so it can be retried on next execution. Any finished sub-slots within the failed step are preserved.

To enforce poisoning: the `LiveStepContext` tracks an `_aborted` flag. When a sub-step raises `StepAbortError`, this flag is set on the containing step's context before the exception propagates. When a step returns a result, the runtime checks `_aborted` — if True, the result is discarded, the slot stays Partial, and the workflow aborts. This catches the case where someone explicitly catches the BaseException and tries to return normally.

### Cache-slot-arg replay check

**Goal**: When a finished run is found with a matching cache key, verify that reusing the cached result is safe — i.e., re-executing the step would not have produced observably different results due to changed cache slot contents.

**Why this is needed**: Cache slots passed as args are excluded from the cache key (they're replaced by placeholders). So a matching cache key doesn't tell us whether the cache slot contents are the same. If we skip re-execution and the slot contents differ, the cached result could be stale.

#### Indexing cache-slot args

Cache slots may appear nested inside collections in the Python args (e.g., `run(step, [slot_a, slot_b], {"x": slot_c})`). To consistently identify them across executions, we assign each cache-slot arg a stable index based on **lexicographic order by path** through the arg structure. For example, in `run(step, [slot_a, slot_b], key=slot_c)`, the paths would be `args[0][0]`, `args[0][1]`, `kwargs["key"]`, sorted lexicographically to produce indices 0, 1, 2. This ordering is deterministic given the same arg structure.

#### Tracing during execution

When a step runs with cache-slot args, those args are wrapped in **tracing proxies** that propagate through `.subslot()` calls. The proxy records the derivation path from the original arg (e.g., arg index 0 → subslot("a") → subslot("b")).

During execution, we record every run emitted inside this step that interacts with a foreign-derived slot in either of two ways:

- **(a) Dispatched to a foreign-derived slot**: e.g., `slot_X.subslot("a").run(step_C, ...)` — the run's result lives in a slot derived from our cache-slot arg.
- **(b) Received a foreign-derived slot as an arg**: e.g., `ctx.cache_slot("bar").run(step_D, slot_X.subslot("b"))` — the run received a slot derived from our cache-slot arg as one of its own args.

A single run can be both (a) and (b) simultaneously. Runs that only use internal cache slots (`ctx.cache_slot(...)`) without any foreign slot involvement are NOT tracked — their state is owned by our slot and immutable once finished.

For each tracked run, we store in `SlotFinished.slot_deps`:

```
TrackedForeignRun:
    # Where the run's slot lives, as a relative derivation path.
    # All references are relative — never absolute pin keys — so the
    # cache tree is portable across namespaces and prefixes.
    run_location: ForeignSlotRef | InternalSlotRef

    # ForeignSlotRef: resolved by walking current_cache_slot_args[arg_index]
    #   then following subslot_path
    # InternalSlotRef: resolved by walking step_slot._internal_cache_slot(key)
    #   then following subslot_path

    # The FxtrID of the SlotFinished entity observed at that slot when our
    # step completed. Content-addressed, so equal ID = identical state.
    observed_finished_id: FxtrID

    # For each foreign-derived cache-slot arg the run received (case b):
    # maps the run's cache-slot arg index → derivation from one of our args
    foreign_arg_mappings: list[ForeignArgMapping]
        # ForeignArgMapping:
        #   our_arg_index: int       — which of our cache-slot args
        #   subslot_path: list[str]  — derivation path from that arg
```

#### Replay check algorithm (recursive)

When checking whether a finished run's cached result can be reused:

```
def replay_check(slot_deps, current_cache_slot_args, step_slot) -> bool:
    for tracked_run in slot_deps:
        # 1. Resolve the run's current slot from relative path
        current_slot = resolve_slot_ref(
            tracked_run.run_location, current_cache_slot_args, step_slot
        )

        # 2. Load current state, get its SlotFinished entity ID
        current_state = await current_slot._load_state()
        current_finished_id = store(current_state).entity_id if finished else None

        # 3. Compare to recorded state
        if current_finished_id != tracked_run.observed_finished_id:
            return False  # → abort workflow

        # 4. Recursively check the run's own slot_deps,
        #    rebinding its cache-slot arg references through our mappings
        inner_args = []
        for mapping in tracked_run.foreign_arg_mappings:
            inner_args.append(
                current_cache_slot_args[mapping.our_arg_index]
                    .walk_subslot_path(mapping.subslot_path)
            )
        if not replay_check(current_state.slot_deps, inner_args, current_slot):
            return False

    return True
```

Key design choice: slot locations are resolved from the *current* cache-slot args at replay time, not from stored pin keys. This means swapping cache-slot arg B for C correctly resolves to C's subslots. It also makes the cache tree portable — you can move it to a different namespace or change the pin prefix without breaking the replay check.

**Why the ID check alone isn't enough**: Equal IDs mean the `SlotFinished` *structure* is identical, including its slot_deps references. But those slot_deps reference external slots by path, and the slots at those paths could have changed even though the reference structure hasn't. The recursive check catches this.

**Why this is complete**: Every access to a foreign-derived slot (dispatch-to or received-as-arg) is tracked at each level. The recursion through `slot_deps` ensures arbitrary nesting is covered. Runs that don't interact with foreign slots are safe by construction (owned state).

**If the check fails**: Abort the workflow. In phase 1 (no replay references), a failed check means the cache is inconsistent with the current state — the user needs to clear the cache or adjust their workflow. (In future best-effort replay mode, a failed check would trigger re-execution instead.)

### Concurrency within a slot

Each cache slot holds an anyio `Lock`. If two tasks call `run()` on the same slot concurrently:
- Same cache key: second waits for first to finish, then checks state (should be Finished → reuse)
- Different cache key: second gets an error after acquiring the lock

---

## File Structure

```
fxtr_workflow/
  __init__.py
  types.py          # ConcreteTypes, PendingOrConcreteTypes aliases, type checking/validation
  pending.py        # Pending class
  step_handle.py    # StepHandle ABC, ExternalStepHandle
  cache_slot.py     # CacheSlot ABC, LiveCacheSlot implementation, storage logic
  step_context.py   # StepContext ABC, LiveStepContext implementation
  entrypoint.py     # TopLevelEntrypoint
  resolve.py        # Deep-walk arg resolution (await pendings, serialize for cache keys)
  errors.py         # StepAbortError and related exceptions
```

## Implementation Steps

- [x] 1. **Types and errors**: `types.py` with type aliases and validation helpers (is this a valid ConcreteType? does this contain any Pending values?). `errors.py` with `StepAbortError(BaseException)` and `CacheSlotError(Exception)`.

- [x] 2. **Pending**: `pending.py`. Wraps an anyio `Event` + result slot. Supports `__await__`, setting a result, and setting an exception. Internal-only constructor (users don't create these).

- [x] 3. **Arg resolution**: `resolve.py`. Two operations:
  - `resolve_args(args, kwargs)` → deep-walk to await all `Pending` values, returning fully concrete args/kwargs
  - `compute_cache_key(scope, step_identity_id, args, kwargs)` → serialize resolved args to a CBOR entity (with cache slot placeholders), store it, return its FxtrID.

- [x] 4. **StepHandle**: `step_handle.py`. `StepHandle` ABC with an identity property. `ExternalStepHandle` holds a callable (the `StepImpl`) + a FxtrID (the key entity). Identity = the key entity's FxtrID.

- [x] 5. **CacheSlot**: `cache_slot.py`. `CacheSlot` ABC with `run()` and `subslot()`. `LiveCacheSlot` implementation that:
  - Takes a namespace, prefix, and reference to the entrypoint
  - Loads state from namespace on first access (lazy)
  - Implements the state machine (Empty → Partial → Finished, or Empty → Split)
  - Persists state transitions as a single entity per slot
  - Holds an anyio Lock for concurrent access
  - `run()`: resolve args, check cache, dispatch step, persist result
  - `subslot()`: return a new LiveCacheSlot with extended prefix, mark self as Split

- [x] 6. **StepContext**: `step_context.py`. `StepContext` ABC with `cache_slot()` and `log()` (no `run_uncached` yet). `LiveStepContext` implementation that:
  - Holds a reference to the entrypoint (for dispatching) and to its own slot (for internal cache slots)
  - Tracks all spawned Pendings; waits for all sub-steps on exit; checks for aborts (containment model)
  - `cache_slot(key)`: returns an internal sub-slot of this step's slot (at `<prefix>/c/<key>`)
  - `log()`: prints for now, can be wired up later

- [x] 7. **TopLevelEntrypoint**: `entrypoint.py`. Async context manager (via `AsyncContextManagerMixin`) that:
  - Opens a task group and its own FxtrScope on enter
  - Provides `bind_external_step(impl, key_id)` → creates ExternalStepHandle, lifts key_id into entrypoint scope
  - Provides `open_cache_slot(namespace, prefix)` → creates a root LiveCacheSlot
  - Internal method for dispatching a step into a slot: spawn task, resolve args, create StepContext, call impl, handle result/error with poisoning check
  - All entity stores go into the entrypoint scope; pins use this scope as the source

- [x] 8. **Update CODE_OVERVIEW.md** with the new file structure and concepts.

- [x] 9. **Tests**: Cover the key scenarios:
  - Unit tests for Pending (resolve, exception, forward as arg)
  - Unit tests for arg resolution (deep walk with nested pendings, cache key computation with slot placeholders)
  - Unit tests for cache slot state machine (empty→partial→finished, empty→split, write-once errors)
  - Integration test: simple two-step workflow (step A produces value, step B consumes it via cache slot)
  - Integration test: resume after partial completion (run workflow, one step "fails", re-run, finished steps reused)
  - Integration test: concurrent runs in same slot (same key → second reuses first's result)
  - Integration test: subslot tree (parent splits into subslots, each runs independently)
  - Integration test: poisoning (step catches StepAbortError and returns → result discarded, slot stays Partial)
  - Integration test: replay check — pass same cache slot args, finished step reused from cache
  - Integration test: replay check — pass cache slot arg with different finished state, workflow aborts
  - Integration test: replay check — nested case: step B dispatches to foreign subslot, inner run's result changed, replay check catches it
  - Integration test: replay check — step passes foreign-derived slot as arg to internal run, inner run's deps changed, recursive check catches it

---

## Open Questions / Things to Revisit

- **Result serialization**: Phase 1 only supports serializable primitives + lists + dicts in results. Returning CacheSlots or StepHandles from steps will need support later.
- **Task group lifecycle**: If a step fails with StepAbortError, we need the task group to cancel other in-flight steps cleanly. Need to think about whether we use `CancelScope` or let the abort propagate naturally. For the spike, propagating naturally (which will crash the task group) is probably fine.
