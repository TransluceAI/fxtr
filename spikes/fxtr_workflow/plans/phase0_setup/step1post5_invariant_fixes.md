# Step 1 Post-5: Fix Flagged Invariant Issues

Four issues were flagged during the correctness audit. They range from dead code to latent bugs.

## Issue 1: Remove dead `CURRENT_STEP_CONTEXT`

`CURRENT_STEP_CONTEXT` is set in `_execute_in_slot` and reset in the finally block, but `.get()` is never called anywhere. It was originally used for pending tracking via the context variable, but that was moved to `dispatch_from_step` which directly appends to `_spawned_pendings`.

### Fix

- Delete `CURRENT_STEP_CONTEXT` from `step_context.py`
- Remove the `token = CURRENT_STEP_CONTEXT.set(step_ctx)` / `CURRENT_STEP_CONTEXT.reset(token)` from `_execute_in_slot` in `entrypoint.py`
- Remove the import in `entrypoint.py`
- Remove `import contextvars` from `step_context.py` if no longer needed

## Issue 2: Normalize subslot keys to `str`

`TracingCacheSlot.subslot(key)` coerces the key to `str` for the serialized `subslot_path`, but `LiveCacheSlot.subslot(key)` stores the child under the original key type in `_subslots`. So `subslot(42)` and `subslot("42")` create separate `LiveCacheSlot` objects with separate locks for the same `pin_key`. During replay check, `_resolve_slot_ref` always passes `str` keys (from the deserialized path), so it may create a different object than the one used during execution.

This doesn't corrupt data (same pin_key → same namespace entry) but means:
- Two objects, two locks → concurrent access isn't properly serialized
- State loaded by one object isn't visible to the other

### Fix

Normalize keys to `str` in `LiveCacheSlot.subslot()` and `_internal_cache_slot()`:

```python
def subslot(self, key: str | int) -> LiveCacheSlot:
    key = str(key)
    if key in self._subslots:
        ...
```

This way the `_subslots` dict always uses `str` keys, matching the serialized `subslot_path` format. The `pin_key` format (`f"{pin_key}/s/{key}"`) already produces the same string for both `42` and `"42"`.

For `_internal_cache_slot`, the key is already str-coerced in `LiveStepContext.cache_slot()` via `InternalSlotRef(cache_slot_key=str(key))`, and it uses the full `internal_key` as the `_subslots` dict key, so it's less of an issue there — but normalizing for consistency is still good practice.

Also document in the `CacheSlot` ABC that int keys are equivalent to their stringified form.

## Issue 3: Assert on `state_ref is None` in `build_slot_deps`

`build_slot_deps` silently skips tracked runs where `inner_slot.state_ref is None`. This should be impossible after `wait_for_all_substeps()` + `check_for_aborts()` — if we reach `build_slot_deps`, all sub-steps completed successfully and their slots should be in Finished state.

Silently dropping a tracked run makes the replay check less strict (a missing entry means a slot interaction goes unchecked).

### Fix

Replace the `if state_ref is None: continue` guard with an assertion:

```python
state_ref = entry.inner_slot.state_ref
assert state_ref is not None, (
    f"Tracked run at {entry.location} has no persisted state — "
    f"this indicates a bug in the step execution lifecycle"
)
```

## Issue 4: `wrap_cache_slot_args` duplicate slot handling

If the same `LiveCacheSlot` instance appears at multiple arg positions, the `id()`-based `proxy_map` only keeps the last entry. All occurrences get the same `TracingCacheSlot` proxy with the last `arg_index`.

This is functionally correct today because:
- Cache key: all CacheSlots become `CacheSlotMarker`, so duplicates don't affect the key
- Replay check: both occurrences point to the same data, so checking either one is equivalent
- `collect_cache_slot_args` on traced args finds the same proxy at multiple paths, and `InnerSlotArgMapping` records each with its correct `inner_arg_index` + the (slightly wrong) source

But the source `ForeignSlotRef.arg_index` for earlier occurrences is wrong — it points to the last occurrence's position instead of their own.

### Fix

Extract the arg-tree traversal into two generic functions in `resolve.py`, defined next to each other and documented as sharing a canonical traversal order:

```python
def collect_cache_slots(args, kwargs) -> list[CacheSlot]:
    """Collect all CacheSlot instances from args/kwargs in canonical order.

    Canonical traversal: positional args left-to-right, kwargs sorted by key,
    lists by index, dicts sorted by key. Primitives/StepHandles/Pendings are
    skipped.
    """

def replace_cache_slots(args, kwargs, fn: Callable[[int, CacheSlot], Any]) -> tuple[tuple, dict]:
    """Replace each CacheSlot in args/kwargs by calling fn(index, slot).

    Uses the same canonical traversal order as collect_cache_slots. The index
    passed to fn matches the position in collect_cache_slots output. Each
    occurrence gets its own call, even if the same slot object appears multiple
    times.
    """
```

Both use the same internal `_walk_value` helper, which guarantees the traversal order matches by construction — the order is defined in exactly one place.

Then:
- `collect_cache_slot_args` wraps `collect_cache_slots`, adding debug paths
- `wrap_cache_slot_args` calls `replace_cache_slots` with a function that creates TracingCacheSlots
- `_replace_slots` (the old id-based replacement) is removed
- `_unwrap_tracing` stays separate (it replaces TracingCacheSlot specifically, not all CacheSlots, and doesn't need to match the canonical order)

## Implementation steps

- [x] Step 1: Remove `CURRENT_STEP_CONTEXT` dead code
- [x] Step 2: Normalize subslot keys to `str` in `LiveCacheSlot.subslot()` and `_internal_cache_slot()`, document in ABC
- [x] Step 3: Assert on `state_ref is None` in `build_slot_deps`
- [x] Step 4: Extract `collect_cache_slots` and `replace_cache_slots` in `resolve.py`
- [x] Step 5: Rewrite `collect_cache_slot_args` with same traversal pattern
- [x] Step 6: Rewrite `wrap_cache_slot_args` using `replace_cache_slots`, remove `_replace_slots`
- [x] Step 7: Add test for duplicate slot args
- [x] Step 8: Verify all tests pass
- [x] Step 9: Update CODE_OVERVIEW.md, remove TODOs from code
