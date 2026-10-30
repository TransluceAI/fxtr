# Step 1 Post-4: Fix Inner Arg Index Mapping

## Problem

When the recursive replay check verifies an inner step's `slot_deps`, it builds an `inner_args` list and passes it as `current_cache_slot_args` to the recursive call. The inner step's `ForeignSlotRef.arg_index` values then index into this list. Two things are broken:

### 1. Missing inner arg index

`ForeignArgMapping` currently only stores `our_arg_index` (the outer step's index). The `inner_args` list is built by appending in `foreign_arg_mappings` iteration order, so positions are determined by `_collect_tracing_slots` traversal order. But the inner step's `arg_index` values are assigned by `collect_cache_slot_args` (which sorts by path). These orderings can diverge, especially for dict args with multiple cache slots.

### 2. Incomplete inner args

`foreign_arg_mappings` only contains entries for cache-slot args that were **foreign to the outer step**. But from the inner step's perspective, *all* cache-slot args are foreign — including ones that came from the outer step's internal cache slots (`ctx.cache_slot()`). The inner step's `collect_cache_slot_args` assigns indices over all of them. If the inner step's `slot_deps` references an arg that was outer-internal, the recursive replay check would index out of bounds or hit the wrong slot.

### Example

Outer step receives `foreign_slot` (arg_index=0) and creates `ctx.cache_slot("work")`. It dispatches an inner step with args `(ctx.cache_slot("work"), foreign_slot)`.

The inner step's `collect_cache_slot_args` returns `[("args[0]", work_slot), ("args[1]", foreign_slot)]` — inner indices 0 and 1.

But `foreign_arg_mappings` only records `foreign_slot` (outer-foreign). During recursive replay check, `inner_args = [foreign_slot_resolved]` — length 1. If the inner step's slot_deps has a `ForeignSlotRef(arg_index=0)` (pointing at `work_slot`), that's an index into a list that doesn't have an entry for it.

## Design

### New entity: `InnerSlotArgMapping`

Replace `ForeignArgMapping` with a mapping that tracks both indices and supports both source types:

```python
@dataclasses.dataclass(frozen=True)
class InnerSlotArgMapping(DataclassFxtrSubpart):
    """Maps one of the inner run's cache-slot args to its source in the containing step."""
    inner_arg_index: int       # position in the inner step's collect_cache_slot_args
    source: SlotRef            # ForeignSlotRef or InternalSlotRef, relative to the outer step
```

Rename `TrackedForeignRun.foreign_arg_mappings` → `inner_slot_arg_mappings: tuple[InnerSlotArgMapping, ...]`.

### Recording at dispatch time

In `dispatch_from_step`, compute the inner step's cache-slot arg ordering by calling `collect_cache_slot_args` on the **pre-unwrap** (traced) args. Since `TracingCacheSlot` is a `CacheSlot`, `collect_cache_slot_args` finds them in the same structural positions and sorted-path order that the inner step's `wrap_cache_slot_args` will use. The enumerate index directly gives the inner arg index — no id-map needed, and duplicate slots at different arg positions are handled correctly:

```python
# In dispatch_from_step, before unwrapping:
inner_slot_entries = collect_cache_slot_args(args, kwargs)
# inner_slot_entries[i] = (path, TracingCacheSlot) — i is the inner arg index
# Each TracingCacheSlot.origin gives the source ref relative to the outer step
```

Pass these entries to the tracker. The tracker builds `InnerSlotArgMapping` directly:

```python
for inner_idx, (_path, tracing_slot) in enumerate(inner_slot_entries):
    InnerSlotArgMapping(
        inner_arg_index=inner_idx,
        source=tracing_slot.origin,  # ForeignSlotRef or InternalSlotRef
    )
```

### Replay check

Build `inner_args` keyed by `inner_arg_index`, resolving each source via `_resolve_slot_ref`:

```python
inner_args: list[CacheSlot | None] = [None] * (max(m.inner_arg_index for m in mappings) + 1)
for mapping in tracked_run.inner_slot_arg_mappings:
    inner_args[mapping.inner_arg_index] = _resolve_slot_ref(
        mapping.source, current_cache_slot_args, step_slot
    )
```

### Tracking condition

Currently `record_run` skips purely-internal dispatches (no foreign target, no foreign args). This optimization is still valid — if neither the target nor any arg is foreign-derived (relative to the outer step), the run doesn't need tracking. The change is that when we *do* track, we record mappings for **all** cache-slot args, not just the foreign-origin ones.

### `_collect_tracing_slots` removal

`_collect_tracing_slots` is replaced entirely by `collect_cache_slot_args` called on the traced args. This gives us the complete, correctly-ordered set of TracingCacheSlots with their inner arg indices, making the separate collection function unnecessary.

## Implementation steps

- [x] Step 1: Replace `ForeignArgMapping` with `InnerSlotArgMapping` in `entities.py`, update `TrackedForeignRun` to use `inner_slot_arg_mappings`
- [x] Step 2: Update `dispatch_from_step` to use `collect_cache_slot_args` on traced args instead of `_collect_tracing_slots`, pass indexed entries to tracker
- [x] Step 3: Update `ForeignRunTracker.record_run` and `_PendingTrackedRun` to accept indexed slot entries
- [x] Step 4: Update `build_slot_deps` to build `InnerSlotArgMapping` entries for all cache-slot args
- [x] Step 5: Update `replay_check` to build `inner_args` keyed by `inner_arg_index`, resolving via `_resolve_slot_ref`
- [x] Step 6: Remove `_collect_tracing_slots`; keep `_unwrap_tracing` (still needed for unwrapping args before backend dispatch)
- [x] Step 7: Update existing tests (entity field renames) — no changes needed, existing tests don't reference ForeignArgMapping directly
- [x] Step 8: Add test: inner step receives both outer-foreign and outer-internal cache-slot args
- [x] Step 9: Add test: dict args with multiple cache slots (ordering correctness)
- [x] Step 10: Update CODE_OVERVIEW.md

## Future optimization: trusted inner args

When a containing step passes one of its own internal cache slots as a foreign arg to a sub-step, we already know that slot's state hasn't changed externally — its state is owned by the containing step's cache tree, and if the containing step's `SlotFinished` entity ID matches, all its internal slots are the same by definition.

We could mark `InnerSlotArgMapping` entries whose source is an `InternalSlotRef` as "trusted". Then during the recursive replay check, for a given tracked run, if the run's target resolves from a trusted arg **and** all of its `inner_slot_arg_mappings` sources are also trusted, we can skip the recursive descent into that run's `slot_deps` entirely — there's nothing externally-changeable to verify.

If a tracked run has a mix of trusted and truly-foreign args, we still need to check it (the truly-foreign ones could have changed). The trusted args still need to be present in `inner_args` so that `_resolve_slot_ref` can resolve them — we just avoid recursing when the check can't possibly fail.

This would avoid redundant recursive verification in deep call trees where internal state is threaded through multiple layers of sub-steps. Not needed now, but worth revisiting if replay checks become a performance bottleneck.
