# Step 3: Replay Reference Modes (Strict + Best-Effort)

## Overview

Cache slots can optionally have a **replay reference** — another cache slot (from a different namespace) whose cached results we try to reuse. Two modes:

- **Strict**: Error if the reference has a result that doesn't match (wrong cache key, failed replay check).
- **Best-effort**: If the reference matches, copy it. If it diverges, drop the reference and execute fresh.

No replay reference = current behavior, completely unchanged.

Three parts: **Part A0** refactors the code to cleanly separate cache slot navigation from run context concerns, **Part A** freezes internal cache slot state into finished runs, **Part B** adds the replay reference mechanism.

---

## Part A0: Refactor — Separate CacheSlot from RunContext

### Motivation

`LiveCacheSlot` currently does double duty: it's both "a place to launch runs into" (with subslots, user-facing) and "a container for a running step's internal state" (with internal cache slots, checkpoints). These have different navigation patterns, and Part B will need different replay reference types for each. Separating them now makes the frozen navigation design clean.

The key insight: `LiveStepContext` already exists and is close to what a "run context" should be. This refactor moves internal-cache-slot ownership and replay-check logic onto `LiveStepContext`.

### Changes

**Move `_internal_cache_slots` off `LiveCacheSlot` onto `LiveStepContext`**: Currently `_subslots` on `LiveCacheSlot` mixes subslots (short keys like `"foo"`) and internal cache slots (full pin keys like `"root/c/foo"`). Internal cache slots only exist for the lifetime of a step execution, so they belong on the step context:

```python
class LiveCacheSlot:
    _subslots: dict[str, LiveCacheSlot]  # subslots only, short keys (unchanged)
    # _internal_cache_slots removed — now lives on LiveStepContext

class LiveStepContext:
    _internal_cache_slots: dict[str, LiveCacheSlot]  # short key → child

    def _internal_cache_slot(self, key: str) -> LiveCacheSlot:
        key = str(key)
        if key not in self._internal_cache_slots:
            pin_key = f"{self._slot.pin_key}/c/{key}"
            child = LiveCacheSlot(
                self._slot._namespace, pin_key, self._backend, is_internal=True
            )
            self._internal_cache_slots[key] = child
        return self._internal_cache_slots[key]
```

`LiveCacheSlot._internal_cache_slot()` is removed. `LiveStepContext.cache_slot()` calls `self._internal_cache_slot()` directly.

**Move `_resolve_slot_ref` and `replay_check` onto `FrozenStepContext`**: Replay checking only happens on finished runs, so it belongs on `FrozenStepContext` (introduced in Part A), not `LiveStepContext`. `LiveStepContext` is purely an execution-time context — it owns tracing, internal cache slots, and sub-step containment, but has no replay check logic. When `_handle_finished_slot` finds a cache hit, it builds a `FrozenStepContext` with the cache-slot args and delegates the replay check to it directly. Recursive replay checks build inner `FrozenStepContext` instances.

```python
class LiveStepContext:
    _slot: LiveCacheSlot
    _backend: _Backend
    _tracker: ForeignRunTracker
    _internal_cache_slots: dict[str, LiveCacheSlot]  # moved from LiveCacheSlot
    _spawned_pendings: list[Pending]
    # No _cache_slot_args, _resolve_slot_ref, or replay_check —
    # those live on FrozenStepContext.
```

**`_execute_in_slot` changes**: Step context is created early (cheap — just stores references). On cache hit, a `FrozenStepContext` is built for the replay check; the `LiveStepContext` is discarded.

```python
async def _execute_in_slot(self, slot, step, raw_args, raw_kwargs, fallible=False):
    args, kwargs = await resolve_args(raw_args, raw_kwargs)
    cache_key_ref = compute_cache_key(...)
    step_ctx = LiveStepContext(slot, self, ForeignRunTracker())

    async with slot._lock:
        state = await slot._load_state()
        if isinstance(state, SlotFinished):
            # Replay check via FrozenStepContext — no LiveStepContext involved
            return await self._handle_finished_slot(
                slot, state, cache_key_ref, args, kwargs, fallible
            )
        ...

    # Execution phase uses the LiveStepContext
    traced_args, traced_kwargs = step_ctx.wrap_cache_slot_args(args, kwargs)
    ...
```

---

## Part A: Freeze Internal Cache Slot State Into Finished Runs

### Motivation

When a run finishes, its internal cache slots (`/c/` pins) remain as live mutable pins. This is problematic:
- A finished run should be immutable, but its internal sub-results are technically mutable pins.
- Copying a finished run for realization would require walking all `/c/` pins separately.
- No clean boundary between "belongs to this finished run" vs. "stale leftover from a partial run."

Freezing internal state into an entity stored as part of `SlotFinished` makes finished runs truly self-contained.

### New entities

```
FrozenSlotNode(DataclassEntity):
    state_ref: EntityRef[SlotState] | None   # None = empty
    subslots: frozendict[str, EntityRef[FrozenSlotNode]]

FrozenInternalSlots(DataclassEntity):
    slots: frozendict[str, EntityRef[FrozenSlotNode]]
```

`FrozenSlotNode` captures a single slot's state + its subslot children. Internal cache slots of a node are NOT stored here — if the node's state is `SlotFinished`, its own `frozen_internal_slots_ref` already captures them (natural recursion through the entity structure).

### SlotFinished changes

```
SlotFinished:
    cache_key_ref: ...
    result_ref: ...
    slot_deps_ref: ...
    frozen_internal_slots_ref: EntityRef[FrozenInternalSlots]  # NEW
```

### Building the frozen tree

When a step finishes (in `_execute_in_slot`, before persisting `SlotFinished`):

1. Walk the step context's `_internal_cache_slots` dict.
2. For each internal cache slot, recursively build a `FrozenSlotNode`:
   - Use the slot's `state_ref` (already loaded during execution)
   - Recursively build `FrozenSlotNode`s for its subslots (from its `_subslots`)
   - Store the entity
3. Build `FrozenInternalSlots` from the mapping.
4. Include in `SlotFinished`.

Only slots accessed during this execution (present in the step context's `_internal_cache_slots`) are frozen. Stale slots from a previous partial run that weren't accessed are left as orphan pins.

The fallible error path also freezes internal slots and cleans up pins using the same rules — a fallible step can use `ctx.cache_slot()` internally even though it can't take cache-slot args.

### Pin cleanup

After freezing and persisting `SlotFinished`, delete pins for exactly the slots that were frozen:

```
def collect_pins_to_delete(slot: LiveCacheSlot) -> list[str]:
    pins = [slot.pin_key, f"{slot.pin_key}/ckpt"]
    # Walk subslots only — inner steps' internal cache slots are owned
    # by their own step contexts and should already be cleaned up when
    # those inner steps finished. (Guaranteed because _execute_in_slot
    # calls wait_for_all_substeps() before reaching this point, and each
    # inner step's _execute_in_slot does its own freeze+cleanup.)
    for child in slot._subslots.values():
        pins.extend(collect_pins_to_delete(child))
    return pins

for child in step_ctx._internal_cache_slots.values():
    for pin in collect_pins_to_delete(child):
        namespace.unpin(pin)
```

Deletes exactly the frozen pins. Orphans remain but are inaccessible through normal API usage.

Note: `namespace.unpin()` may raise if the pin doesn't exist. We should check the `FxtrNamespace` API and potentially add an `allow_missing=True` flag to make this pattern idiomatic (e.g., for the `/ckpt` pin which may or may not exist).

### FrozenCacheSlot (read-only wrapper, subslot-only)

Wraps a `FrozenSlotNode` (or a deferred `EntityRef[FrozenSlotNode]`) and supports subslot navigation via path accumulation:

```
FrozenCacheSlot:
    _root_ref: EntityRef[FrozenSlotNode]
    _subslot_path: tuple[str, ...]    # deferred, walked on first async access
    _node: FrozenSlotNode | None      # cached after load

    def child_subslot(key) -> FrozenCacheSlot
        # Sync: returns FrozenCacheSlot with extended path

    async _ensure_loaded() -> FrozenSlotNode | None
        # Loads root_ref, walks subslot_path, caches result

    async load_state() -> SlotState | None
        # Load from node.state_ref after _ensure_loaded
```

Does NOT implement `CacheSlot` ABC — no `run`/`run_fallible`. Does NOT handle internal cache slot navigation — that responsibility belongs to `FrozenStepContext` (see Part B).

Path accumulation means chained `subslot()` calls are sync (just extend the path tuple). The actual entity loading happens on the first async method call. This avoids the need for eagerly loading the entire frozen subtree.

### `_resolve_slot_ref` on `FrozenStepContext`

`_resolve_slot_ref` lives on `FrozenStepContext` and handles both `LiveCacheSlot` and `FrozenCacheSlot` in `_cache_slot_args` (since recursive replay checks may produce either type). For `ForeignSlotRef`, it walks subslot paths using `child_subslot()` (frozen) or `subslot()` (live). For `InternalSlotRef`, it always resolves from the frozen internal slots.

Note: `LiveCacheSlot._internal_cache_slot()` was moved to `LiveStepContext` with short keys (just the user-provided `key` string), matching what `FrozenInternalSlots.slots` uses.

---

## Part B: Replay Reference Modes

### New types

```python
class ReplayMode(enum.Enum):
    STRICT = "strict"
    BEST_EFFORT = "best_effort"
```

### FrozenStepContext (frozen counterpart of LiveStepContext)

Wraps the `FrozenInternalSlots` from a finished reference run, providing sync lookup of internal cache slot replay references. This is the frozen counterpart of `LiveStepContext` — where `LiveStepContext` owns live `_internal_cache_slots`, `FrozenStepContext` owns the frozen equivalent:

```python
class FrozenStepContext:
    _frozen_internals: FrozenInternalSlots  # eagerly loaded

    def get_internal_replay_ref(self, key: str) -> FrozenCacheSlot | None:
        """Sync: dict lookup in the already-loaded FrozenInternalSlots."""
        ref = self._frozen_internals.slots.get(key)
        if ref is None:
            return None
        return FrozenCacheSlot(root_ref=ref, subslot_path=())
```

When the reference state is `SlotPartial` (not `SlotFinished`), there's no `FrozenInternalSlots`. Internal cache slots use the reference's live `/c/` pins instead. For this case, we create a `LiveStepContext`-based reference that navigates live pins:

```python
class LiveRunReference:
    _reference_slot: LiveCacheSlot
    _namespace: FxtrNamespace
    _backend: _Backend

    def get_internal_replay_ref(self, key: str) -> LiveCacheSlot:
        pin_key = f"{self._reference_slot.pin_key}/c/{key}"
        return LiveCacheSlot(self._namespace, pin_key, self._backend, is_internal=True)
```

Both `FrozenStepContext` and `LiveRunReference` implement a `RunReplayReference` protocol (or ABC) so `LiveStepContext` can use either uniformly.

### Two kinds of replay reference

The separation between CacheSlot and RunContext means two different replay reference types:

- **CacheSlot replay reference** (`_replay_ref` on `LiveCacheSlot`): A `FrozenCacheSlot` or `LiveCacheSlot` from the reference namespace. Propagated through `subslot()`. Used for `probe_state()` / `realize()` / `reject()`.

- **Run replay reference** (`_run_replay_ref` on `LiveStepContext`): A `RunReplayReference` (`FrozenStepContext` or `LiveRunReference`). Provides internal cache slot replay references. Created eagerly when the step context is set up (for execution) or when recursing in `replay_check`.

### Probe/realize/reject API

The slot mediates access to its own state (including replay reference) through three operations. The slot internally tracks whether the probed state came from local storage or from a replay reference — callers don't need to know.

**`probe_state() -> EntityRef[SlotState] | None`**: Checks local state first. If empty and has replay reference, checks the reference. No mutations. Caches the result internally (including whether it came from local or reference). Returns an `EntityRef` — callers use `.entity_id` for ID comparison (e.g., in replay check) and `.load()` when they need the actual state object. This is the same type as `_state_ref`.

**`realize(expected_ref) -> None`**: Commits to the probed state. Takes the `EntityRef` returned by `probe_state()` as a parameter and verifies it still matches the slot's current probed state (guards against concurrent modification). Acquires the **target slot's** lock before writing. If the probed state came from the reference, copies the state entity (full subgraph) from the reference namespace into this slot's namespace by passing the reference namespace's scope to `ns.pin(..., scope=ref_ns.scope)` — `pin` reads the entity from the provided scope. If local, no-op.

**`reject(reason) -> None`**: The probed state doesn't match what we need. The slot handles all error logic internally based on what it knows:
- **Local state** (no replay reference involved): always raises `CacheSlotError` — this is a write-once violation or structural mismatch. The `reason` parameter provides context for the error message.
- **Reference state + strict mode**: raises `CacheSlotError`.
- **Reference state + best-effort mode**: the reference is no longer used for this slot's own state, but its internal structure is preserved for child slot propagation. Concretely:
  - The slot marks itself as "reference rejected" — future `probe_state()` calls will not check the reference for this slot's state.
  - The reference (and its internal structure) is retained in a separate field so that internal cache slot replay references can still be derived from it (via `RunReplayReference`):
    - Reference was Finished → its `frozen_internal_slots_ref` provides `FrozenStepContext`
    - Reference was Partial → its live `/c/` pins provide `LiveRunReference`
  - Subslot children's replay references are NOT affected by reject — they were already propagated when `subslot()` was called.

### LiveStepContext changes for replay references

`LiveStepContext` gets `_run_replay_ref` for execution-time replay reference propagation. It does NOT have `_resolve_slot_ref` or `replay_check` — those live on `FrozenStepContext` (from Part A).

```python
class LiveStepContext:
    _slot: LiveCacheSlot
    _backend: _Backend
    _tracker: ForeignRunTracker
    _internal_cache_slots: dict[str, LiveCacheSlot]  # from Part A0
    _run_replay_ref: RunReplayReference | None        # NEW
    _spawned_pendings: list[Pending]

    def _internal_cache_slot(self, key: str) -> LiveCacheSlot:
        """Get or create internal cache slot, with replay reference propagation."""
        key = str(key)
        if key not in self._internal_cache_slots:
            pin_key = f"{self._slot.pin_key}/c/{key}"
            child = LiveCacheSlot(
                self._slot._namespace, pin_key, self._backend, is_internal=True
            )
            if self._run_replay_ref:
                replay = self._run_replay_ref.get_internal_replay_ref(key)
                if replay is not None:
                    child._replay_ref = replay  # FrozenCacheSlot or LiveCacheSlot
            self._internal_cache_slots[key] = child
        return self._internal_cache_slots[key]
```

Replay reference propagation for internal cache slots is centralized in `_internal_cache_slot()`, which is used by `cache_slot()` (user-facing).

### Usage in replay_check

```
ref = await slot.probe_state()
if ref is None or ref.entity_id != expected_entity_id:
    return False
await slot.realize(ref)
# continue recursive check (load state via ref.load() if needed)...
```

Replay check never calls `reject()`. It just returns False on mismatch.

### Usage in _execute_in_slot

Same-slot concurrency is handled straight-line (no loop): if this slot already has an in-flight execution (`slot._execution_finished` is set), we grab the event, release the lock, wait, then re-acquire the lock once to check the result. If the first execution succeeded (slot is `SlotFinished`), we use the cached result. If it aborted (slot is still `SlotPartial`), we raise `StepAbortError` — re-executing is pointless because the abort already poisoned the containing step. See the `_execution_finished` event mechanism in `entrypoint.py` and `cache_slot.py`.

```
wait_event = None
async with slot._lock:
    ref = await slot.probe_state()
    if ref is None:
        # fresh execution — claim the slot
        slot._execution_finished = anyio.Event()
        # fall through to execute
    else:
        state = await ref.load()

        if isinstance(state, SlotFinished):
            if state.cache_key_ref.entity_id == cache_key_ref.entity_id:
                if replay_check_passes:
                    await slot.realize(ref)
                    return cached_result
                else:
                    await slot.reject(reason="replay_check_failed")
                    # strict/local: raised. best-effort: fall through
            else:
                await slot.reject(reason="cache_key_mismatch")
                # strict/local: raised. best-effort: fall through

        elif isinstance(state, SlotPartial):
            if state.cache_key_ref.entity_id == cache_key_ref.entity_id:
                if slot._execution_finished is not None:
                    # Same key, in-flight — grab event to wait on
                    wait_event = slot._execution_finished
                else:
                    # Same key, no in-flight (e.g. resumed after crash)
                    await slot.realize(ref)
                    slot._execution_finished = anyio.Event()
                    # fall through to re-execute
            else:
                await slot.reject(reason="cache_key_mismatch")
                # strict/local: raised. best-effort: fall through

        elif isinstance(state, SlotSplit):
            await slot.reject(reason="structural_mismatch")
            # strict/local: raised. best-effort: fall through

        if wait_event is None and slot._execution_finished is None:
            # Best-effort reject fell through — claim the slot
            slot._execution_finished = anyio.Event()

# If waiting on a concurrent execution, block then check result
if wait_event is not None:
    await wait_event.wait()
    async with slot._lock:
        state = await slot._load_state()
        if isinstance(state, SlotFinished):
            return handle_finished(...)
        # First execution aborted — containing step is poisoned anyway
        raise StepAbortError(...)

# Execute step...
# After execution (success or failure), signal waiters:
_signal_execution_finished(slot)
```

### Building RunReplayReference

**During execution** (in `_execute_in_slot`): After `probe_state()` finds a reference state:
- If `SlotFinished`: eagerly load `frozen_internal_slots_ref` → create `FrozenStepContext`
- If `SlotPartial`: create `LiveRunReference` from the reference slot
- Set on the `LiveStepContext` as `_run_replay_ref`

**During replay_check**: Handled entirely by `FrozenStepContext` (from Part A). When recursing into an inner run, `FrozenStepContext.replay_check()` loads the inner `SlotFinished`'s `frozen_internal_slots_ref` and builds an inner `FrozenStepContext` — no `LiveStepContext` involved.

### Concurrency

**Same-slot run concurrency** is handled by `LiveCacheSlot._execution_finished` (an `anyio.Event`). When `_execute_in_slot` claims a slot for execution, it sets the event. Other callers that find the slot in `SlotPartial` with the same cache key and an active event wait on it (outside the lock), then loop back to re-check state. After execution completes (success or failure), `_signal_execution_finished()` signals waiters and clears the event. Different cache keys always raise `CacheSlotError`. This mechanism is already implemented and tested. The pseudocode above integrates it with probe/realize/reject.

**`realize()` concurrency**: `realize()` acquires the target slot's lock before writing, and verifies that the probed state hasn't been concurrently modified (via the `expected_ref` parameter). This is important because `replay_check` may call `realize()` on foreign slots that could be concurrently accessed by other steps.

The `expected_ref` guard means: if two concurrent steps both probe the same foreign slot and try to realize it, one succeeds and the other detects the change and handles it (returns False from the replay check, since the state was modified). In practice, concurrent realizations of the same slot from the same reference would produce identical results (same content-addressed entity), so the guard is mainly about correctness rather than preventing corruption.

### Subslot propagation

`slot.subslot(key)` → child gets `replay_reference.subslot(key)` (or `child_subslot(key)` for `FrozenCacheSlot`) as its replay reference (same mode).

For `FrozenCacheSlot`, `child_subslot(key)` is sync — it returns a new `FrozenCacheSlot` with the subslot path extended by `key`. No entity loading happens. This supports chained calls like `slot.subslot("a").subslot("b")` where each propagation is sync.

For `LiveCacheSlot` references, `subslot(key)` returns a child `LiveCacheSlot` pointing at the derived pin key in the reference namespace.

### `_ensure_split` interaction

When a slot is empty and needs to become Split (because `subslot()` was called):
1. Probe state
2. If Split → realize (copy Split state in if from reference), proceed normally
3. If anything else → reject (reason="structural_mismatch"):
   - Local/strict: error
   - Best-effort: drop replay reference, proceed without it

### open_cache_slot changes

```python
def open_cache_slot(
    self,
    namespace: FxtrNamespace,
    prefix: str,
    replay_reference: CacheSlot | None = None,
    replay_mode: ReplayMode | None = None,
) -> LiveCacheSlot:
```

Typed as `CacheSlot`, runtime isinstance check for `LiveCacheSlot`. Document that the reference's namespace must remain open for the duration of the workflow.

---

## Implementation Steps

### Part A0: Refactor CacheSlot / RunContext separation
- [x] 1. Move `_internal_cache_slots` dict and `_internal_cache_slot()` method from `LiveCacheSlot` to `LiveStepContext`. Remove from `LiveCacheSlot._subslots` (which now only contains subslots). The step context constructs child `LiveCacheSlot`s using the slot's `pin_key` and `_namespace`.
- [x] 2. (Originally: add `_cache_slot_args` to `LiveStepContext`. Superseded by Part A refactor — `_cache_slot_args` now lives on `FrozenStepContext` instead.)
- [x] 3. (Originally: move `_resolve_slot_ref` and `replay_check` onto `LiveStepContext`. Superseded by Part A refactor — these now live on `FrozenStepContext` instead. `LiveStepContext` has no replay check logic.)
- [x] 4. Create `LiveStepContext` earlier in `_execute_in_slot` (after arg resolution, before cache check). On cache hit, build a `FrozenStepContext` for the replay check; the `LiveStepContext` is discarded.
- [x] 5. Update tests — `test_replay_check.py` tests that call `replay_check()` directly will need adjustment to go through a step context (or a test helper that creates one).
- [x] 6. Update CODE_OVERVIEW.md with the refactored structure.

### Part A: Freeze internal state

Revised ordering: pin cleanup requires `replay_check` to read from frozen state (since `/c/` pins will be deleted). So `FrozenCacheSlot`, `FrozenStepContext`, and the `replay_check` update come before pin cleanup.

`FrozenStepContext` is pulled forward from Part B. It's the frozen counterpart of `LiveStepContext` — it owns `_cache_slot_args`, `_resolve_slot_ref()`, and `replay_check()` for finished runs. When a slot is already `SlotFinished`, `_handle_finished_slot` builds a `FrozenStepContext` and delegates the replay check to it directly — no `LiveStepContext` is involved. Recursive replay checks also build inner `FrozenStepContext` instances. In Part B, `FrozenStepContext` will additionally implement the `RunReplayReference` protocol.

- [x] 7. Define `FrozenSlotNode` and `FrozenInternalSlots` entities in `entities.py`.
- [x] 8. Add `frozen_internal_slots_ref` field to `SlotFinished`. Fix all existing construction sites (tests, entrypoint) — may want a helper to keep this manageable.
- [x] 9. Implement frozen tree building: walk `_internal_cache_slots` after step completion, build and store `FrozenInternalSlots`, include in `SlotFinished` (in `_execute_in_slot`). Both the success and fallible-error paths.
- [x] 10. Implement `FrozenCacheSlot` read-only wrapper (path accumulation for subslots, async `_ensure_loaded`, `load_state`).
- [x] 11. Implement `FrozenStepContext`: wraps `FrozenInternalSlots` + `cache_slot_args`. Provides `get_internal_slot(key)`, `_resolve_slot_ref()`, and `replay_check()`. Lives in `cache_slot.py` alongside `FrozenCacheSlot`.
- [x] 12. Move `replay_check` and `_resolve_slot_ref` from `LiveStepContext` onto `FrozenStepContext`. `_handle_finished_slot` builds a `FrozenStepContext` with cache-slot args and calls `replay_check` on it directly. Recursive checks build inner `FrozenStepContext` instances (not `LiveStepContext`). `LiveStepContext` no longer has replay check logic.
- [x] 13. Implement pin cleanup: delete exactly the pins that were frozen. `FxtrNamespace.unpin()` raises `KeyError` for missing pins — this is correct for frozen slot pins (we know they exist; a `KeyError` would indicate an integrity violation). For `/ckpt` pins, continue using the existing `try/except KeyError` pattern in `_delete_checkpoint` since checkpoints may or may not exist.
- [x] 14. Tests: freezing produces correct tree structure (simple, nested, with subslots of internal cache slots).
- [x] 15. Tests: `FrozenCacheSlot` navigation (child_subslot, chained subslots, state loading, path accumulation with deferred loading).
- [x] 16. Tests: pin cleanup (frozen pins deleted, orphan pins from stale partial run preserved). Also: replay check works after pin cleanup by reading from frozen state.
- [x] 17. Update CODE_OVERVIEW.md with frozen internal slots concept.

### Part B: Replay references
- [x] 18. Add `ReplayMode` enum.
- [x] 19. Implement `RunReplayReference` protocol/ABC (have `FrozenStepContext` implement it), and `LiveRunReference` (wraps reference `LiveCacheSlot` for Partial state).
- [x] 20. Add `_replay_ref` and `_replay_mode` to `LiveCacheSlot`. Add `_run_replay_ref` to `LiveStepContext`.
- [x] 21. Add `probe_state()` / `realize()` / `reject()` methods to `LiveCacheSlot`.
- [x] 22. Update `open_cache_slot` signature with `replay_reference` and `replay_mode` params, runtime check.
- [x] 23. Implement subslot propagation of replay references (sync, using `FrozenCacheSlot.child_subslot` path accumulation for frozen refs).
- [x] 24. Implement internal cache slot replay reference propagation in `LiveStepContext._internal_cache_slot()` (sync lookup via `RunReplayReference.get_internal_replay_ref`). This covers `cache_slot()` (user-facing). Replay check uses `FrozenStepContext` directly (already handles frozen internal slots from Part A).
- [x] 25. Integrate replay reference into `_ensure_split` (probe → realize Split, or reject on structural mismatch).
- [x] 26. Update `_execute_in_slot` to use probe/realize/reject. Build `RunReplayReference` from probed state (FrozenStepContext for Finished, LiveRunReference for Partial) and pass to `LiveStepContext`.
- [x] 27. Update `FrozenStepContext.replay_check` to use `probe_state()` / `realize()` when encountering `LiveCacheSlot` entries with replay references (foreign slots). For `FrozenCacheSlot` entries (internal slots from recursive checks), continue reading state directly.
- [x] 28. Update `__init__.py` exports.
- [x] 29. Tests: strict mode — reference matches → result reused from reference.
- [x] 30. Tests: best-effort mode — reference matches → result reused from reference.
- [x] 31. Tests: strict mode — cache key mismatch → error.
- [x] 32. Tests: best-effort mode — cache key mismatch → executes fresh.
- [x] 33. Tests: strict mode — replay check fails → error.
- [x] 34. Tests: best-effort mode — replay check fails → re-executes.
- [x] 35. Tests: subslot propagation (reference subslots used for child slots).
- [x] 36. Tests: realization during replay check (foreign slot empty + reference has matching state → realized).
- [x] 37. Tests: partial realization during replay check (some foreign slots realize, others don't, overall fails, best-effort re-executes).
- [x] 38. Tests: no replay reference → current behavior unchanged (regression).
- [x] 39. Tests: reference has Partial state with matching cache key → realize partial + resume.
- [x] 40. Tests: fallible run with replay reference (cached error replayed from reference).
- [x] 41. Tests: nested replay (step A realized from reference, inner steps also realized).
- [x] 42. Tests: `_ensure_split` with replay reference (reference is Split → realize; reference is Finished → strict error / best-effort drops reference).
- [x] 43. Tests: internal cache slot reference resolution (Finished → frozen tree).
- [x] 44. Tests: best-effort reject of Partial reference (cache key mismatch) → re-executes fresh.
- [x] 45. Update CODE_OVERVIEW.md with replay reference concepts.

---

## Post-implementation TODOs

These were identified during Part B implementation and should be revisited:

### DONE: `realize()` lock semantics

Resolved: `_realize_inner()` is the unlocked core logic, used by `_execute_in_slot` (already holds `slot._lock`). Public `realize()` acquires the lock and delegates to `_realize_inner()`, used by `FrozenStepContext.replay_check` on foreign slots and by `_ensure_split` on self.

### DONE: `LiveCacheSlot` lifecycle enum refactor

Replace the scattered mutable fields (`_state_ref`, `_state_loaded`, `_probed_from_ref`, `_probed_ref`, `_ref_rejected`) with a single lifecycle enum that encodes the full state of the slot's reference interaction and local state.

**Reference config** (bundled, immutable after setup):

```python
@dataclasses.dataclass(frozen=True)
class ReplayRefConfig:
    replay_ref: FrozenCacheSlot | LiveCacheSlot
    replay_mode: ReplayMode
    replay_ref_namespace: FxtrNamespace
```

**Lifecycle states** (single `_lifecycle` field on `LiveCacheSlot`):

```python
@dataclasses.dataclass
class SlotFresh:
    """Not loaded yet."""
    ref: ReplayRefConfig | None

@dataclasses.dataclass
class SlotLocalEmpty:
    """Loaded from namespace, nothing there. Reference not yet consulted."""
    ref: ReplayRefConfig | None

@dataclasses.dataclass
class SlotProbedFromRef:
    """Local empty, reference had state, awaiting realize/reject."""
    ref: ReplayRefConfig
    probed: EntityRef[SlotState]

@dataclasses.dataclass
class SlotRefRejected:
    """Reference rejected (best-effort). Local empty. Ref retained for child propagation."""
    ref: ReplayRefConfig

@dataclasses.dataclass
class SlotLocalPresent:
    """Has local state (loaded, realized, or written)."""
    state_ref: EntityRef[SlotState]
    ref: ReplayRefConfig | None  # retained for child propagation
```

**Transitions:**

```
SlotFresh(ref)
  → _load_state() → SlotLocalEmpty(ref) | SlotLocalPresent(state_ref, ref)

SlotLocalEmpty(ref=None)
  → probe_state() → returns None, stays SlotLocalEmpty
  → _persist_state() → SlotLocalPresent(state_ref, ref=None)

SlotLocalEmpty(ref=some)
  → probe_state(), ref has state → SlotProbedFromRef(ref, probed)
  → probe_state(), ref empty → stays SlotLocalEmpty(ref)
  → _persist_state() → SlotLocalPresent(state_ref, ref)

SlotProbedFromRef(ref, probed)
  → _realize_inner() → SlotLocalPresent(state_ref=probed, ref)
  → reject() strict → raises
  → reject() best-effort → SlotRefRejected(ref)

SlotRefRejected(ref)
  → probe_state() → returns None, stays SlotRefRejected
  → _persist_state() → SlotLocalPresent(state_ref, ref)

SlotLocalPresent(state_ref, ref)
  → _persist_state() (overwrite) → SlotLocalPresent(new_state_ref, ref)
  → probe_state() → returns state_ref, stays SlotLocalPresent
```

**What stays outside the enum:**

- `_execution_finished: anyio.Event | None` — task coordination for same-slot concurrency. Separate concern from the reference/state lifecycle.
- `_replay_ref_namespace` moves into `ReplayRefConfig`.
- `_replay_ref` and `_replay_mode` move into `ReplayRefConfig`.
- `_subslots` dict stays — it's about navigation, not state lifecycle.

**Accessing state_ref:** Add a `state_ref` property on `LiveCacheSlot` that extracts it from `SlotLocalPresent` (or returns None for other states). Used by `ForeignRunTracker.build_slot_deps()` and the freeze logic.

**Accessing ref config for child propagation:** `subslot()` and `_internal_cache_slot()` read the `ref` from whatever lifecycle state the slot is in. All states carry `ref` (or `None`), so this is always available.

**Impact on `_execute_in_slot`:** The lifecycle enum makes state checks explicit — instead of `if slot._probed_from_ref:`, it's `if isinstance(slot._lifecycle, SlotProbedFromRef):`. This should also help with the `_execute_in_slot` complexity issue since each branch's valid transitions are clear from the types.

### TODO: `_execute_in_slot` complexity audit

The `_execute_in_slot` flow handles many cases (local/reference × Finished/Partial/Split/Empty × same-key/different-key × in-flight/not × strict/best-effort). The lifecycle enum refactor should make this clearer, but a focused review pass afterward is still worthwhile to verify all paths correctly build (or skip) the `run_replay_ref`.
