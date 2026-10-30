# Step 1 Post-1: Fix Case (b) Replay Tracking

## Problem

The replay check only tracks case (a): runs dispatched TO a foreign-derived slot. Case (b) — passing a foreign-derived slot as an ARG to a run in an internal slot — is not tracked. This means the replay check can incorrectly reuse cached results when the foreign slot's contents have changed.

Example:
```python
async def my_step(ctx, foreign_slot):
    # Case (a) — tracked correctly:
    await foreign_slot.subslot("x").run(step_a, ...)

    # Case (b) — NOT tracked, replay check misses this:
    await ctx.cache_slot("my_run").run(step_b, foreign_slot.subslot("y"))
```

In case (b), step_b's result depends on the foreign slot, but `my_step`'s `slot_deps` don't record this dependency. On replay with different foreign slot contents, we'd incorrectly reuse the cached result.

## Design: Separate tracing (write) from replay check (read)

The fix also addresses a broader design issue: tracing and dispatch are currently interleaved across `TracingCacheSlot.run()`, `dispatch_in_slot`, and `_execute_in_slot`. We clean this up by separating two concerns:

**Tracing layer (step context)**: Lives entirely in the step context. During a step's execution, all slots the step can access are `TracingCacheSlot` instances — both foreign args and internal cache slots. When any run is dispatched from within the step, the step context inspects the target and args, records tracking info, then delegates to the backend. When the step finishes, the step context builds `slot_deps` from the recorded info.

**Dispatch layer (backend)**: Handles the actual execution: resolve args, check cache (including replay check against persisted `slot_deps`), execute step, persist results. Only deals with `LiveCacheSlot` — never sees `TracingCacheSlot`. The replay check loads persisted `slot_deps` and walks relative paths against the current plain cache slot args.

This means:
- `TracingCacheSlot.run()` routes through the step context, NOT directly through the backend
- The step context unwraps tracing before passing to the backend
- The backend's `dispatch_in_slot` receives only plain `LiveCacheSlot` instances
- All tracing logic lives in one place (the step context)

### Two dispatch contexts

There are exactly two contexts where `cache_slot.run()` can be called:

1. **Top level** (orchestrator code): No tracing. `CacheSlot.run()` dispatches directly via the backend. Cache slots are plain `LiveCacheSlot` instances.

2. **Inside a step**: All cache slots are `TracingCacheSlot` instances. `TracingCacheSlot.run()` calls back into the step context, which records the dispatch, unwraps tracing, and delegates to the backend.

### Unified `TracingCacheSlot`

`TracingCacheSlot` carries an `_origin: ForeignSlotRef | InternalSlotRef` that says how this slot was derived:

- Foreign args: `_origin = ForeignSlotRef(arg_index=0, subslot_path=["y"])`
- Internal cache slots: `_origin = InternalSlotRef(cache_slot_key="my_run", subslot_path=[])`

`subslot()` extends `_origin.subslot_path`. Both kinds are registered with the same step-level tracker.

### Step context dispatch flow

When `traced_slot.run(step, *args)` is called inside a step:

```
traced_slot.run(step, arg1, traced_foreign_arg)
  → step_context.dispatch_from_step(traced_slot, step, args, kwargs)
    → record _PendingTrackedRun:
        location = traced_slot.origin  (ForeignSlotRef or InternalSlotRef)
        foreign_args = [traced_foreign_arg.origin, ...]  (any TracingCacheSlots in args)
    → unwrap: target = traced_slot.inner, args = unwrap_tracing(args)
    → pending = backend.dispatch_in_slot(target, step, unwrapped_args)
    → track pending for containment (wait_for_all_substeps)
    → return pending
```

The step context handles both case (a) and case (b) in the same code path:
- If `traced_slot.origin` is a `ForeignSlotRef` → that's case (a)
- If any arg has a `ForeignSlotRef` origin → that's case (b)
- Both can apply to the same run — it's just one `_PendingTrackedRun` with both pieces of info

### What changes where

**`TracingCacheSlot`**: `run()` calls `self._step_ctx.dispatch_from_step()` instead of calling the backend directly or going through the tracker. No longer has a direct reference to the tracker. `_arg_index` + `_subslot_path` replaced by `_origin: ForeignSlotRef | InternalSlotRef`.

**`LiveStepContext`**:
- Holds a reference to the step's `ForeignRunTracker`
- `cache_slot(key)` returns a `TracingCacheSlot` with `InternalSlotRef` origin
- New `dispatch_from_step(target, step, args, kwargs)` method: records tracking, unwraps tracing, delegates to backend, tracks pending
- Wraps incoming cache-slot args with `TracingCacheSlot` (moved from `_execute_in_slot`)

**`_EntrypointBackend.dispatch_in_slot`**: Simplified. No longer inspects args for tracing. Just spawns task and dispatches. Receives only plain `LiveCacheSlot` instances.

**`_execute_in_slot`**: No longer sets up tracing proxies (moved to step context). Still handles arg resolution, cache key computation, cache checking (including replay check), step execution, and persistence. The tracing proxy setup that was here moves to `LiveStepContext`.

**`ForeignRunTracker`**: `dispatch_foreign_run` removed. Recording happens in `LiveStepContext.dispatch_from_step()`. `build_slot_deps()` unchanged.

## Implementation Steps

- [x] 1. **Write failing tests.** All of these should fail before the fix:
   - Case (b) basic: step receives foreign slot, passes it as arg to `ctx.cache_slot("x").run(...)`. Swap foreign slot contents → should fail replay check but currently doesn't.
   - Combined case (a) + (b): run dispatched TO foreign slot AND receives another foreign slot as arg
   - Multiple foreign args: step receives two foreign slots, passes both to an internal run
   - Nested: step A passes foreign slot to step B (internal), step B dispatches into the foreign slot

- [x] 2. **Generalize `TracingCacheSlot`.** Replace `_arg_index` + `_subslot_path` with `_origin: ForeignSlotRef | InternalSlotRef`. Replace `_tracker` with `_step_ctx: LiveStepContext`. `run()` calls `self._step_ctx.dispatch_from_step()`. `subslot()` extends `_origin.subslot_path`.

- [x] 3. **Add `dispatch_from_step` to `LiveStepContext`.** Records `_PendingTrackedRun` (inspecting target and args for tracing info), unwraps tracing, delegates to backend, tracks pending.

- [x] 4. **Update `LiveStepContext.cache_slot()`.** Returns `TracingCacheSlot` with `InternalSlotRef` origin.

- [x] 5. **Move cache-slot arg wrapping from `_execute_in_slot` to `LiveStepContext`.** The step context wraps incoming cache-slot args with `TracingCacheSlot` (foreign origin) when the step is created.

- [x] 6. **Simplify `_execute_in_slot` and `dispatch_in_slot`.** Remove tracing proxy setup. `dispatch_in_slot` no longer needs to inspect `CURRENT_STEP_CONTEXT` for pending tracking — the step context handles that before calling the backend.

- [x] 7. **Verify all failing tests from step 1 pass.**

- [x] 8. **Run all existing tests** to verify nothing regressed.

- [x] 9. **Update impl notes and CODE_OVERVIEW.**
