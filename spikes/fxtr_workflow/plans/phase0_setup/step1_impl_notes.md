# Step 1 Implementation Notes

## Things discovered during implementation

### Entity type registration: `define_type` not `FxtrID.builtin`

Initial attempt used `FxtrID.builtin("fxtr_workflow.slot_split")` for entity type definitions. This doesn't work because:
1. `store_cbor` requires `$fxtr.ctx` in the entity dict — raw CBOR doesn't have it
2. Builtin IDs aren't automatically tracked in any scope, so `lift` fails

The fix: use `system_entities.define_type(namespace_uuid, name)` which stores a `FxtrBasicType` entity and returns its FxtrID. This handles scope tracking automatically. All fxtr_workflow entity types use a shared namespace UUID `1a39d997-3113-43b8-a18d-0a98db1879e0`.

Similarly, `compute_cache_key` was initially trying to build raw CBOR dicts and call `store_cbor` directly. Fixed to use a proper `CacheKeyEntity` DataclassEntity type, which handles `$fxtr.ctx` and FxtrID reference tracking automatically.

### Cache key: proper entity, not raw CBOR

The cache key is now a `CacheKeyEntity(step_identity: FxtrID, args_data: Any)` — a real fxtr entity that can be loaded and inspected. The step identity is stored as a FxtrID reference (navigable), not raw bytes. Cache slot placeholders in `args_data` are represented as `{"$placeholder": "cache_slot"}` dicts.

### Nested DataclassEntity types need `Child[T]`

Nesting one DataclassEntity inside another (e.g., `TrackedForeignRun` inside `SlotFinished`) causes `DuplicateEntityMarkerError` because each entity has its own `$fxtr.ctx` marker. The fix is to use `Child[T]` annotations, which tells mashumaro to serialize the child as a separate entity with a strong reference. Applied to `SlotFinished.slot_deps: list[Child[TrackedForeignRun]]` and `TrackedForeignRun.foreign_arg_mappings: list[Child[ForeignArgMapping]]`.

### Entrypoint owns a scope

Initial implementation relied on `get_active_scope()` and stored entities directly into namespace scopes. This broke when entities referenced FxtrIDs from different scopes (e.g., cache key entity referencing a step identity stored in a different scope).

Fix: `TopLevelEntrypoint` creates its own `FxtrScope` on enter (inherits from the current active scope). All internal stores (cache keys, slot state) go into this scope. When pinning to a namespace, we pass the entrypoint's scope as the source. `bind_external_step` lifts the user's key_id into this scope.

### `bind_external_step` simplified

No longer takes a `scope` parameter — it always lifts the key_id into the entrypoint's own scope, which can see entities from its parent scopes (including namespace scopes).

### Abort/poisoning: sub-step containment model

Initial plan had poisoning detection via a `_aborted` flag on step context, set when a StepAbortError was observed during Pending.__await__. This required special-casing in Pending and a context variable.

Refined to a cleaner model where the step *contains* all its sub-steps:
- `LiveStepContext` tracks all Pendings spawned during the step (registered via `CURRENT_STEP_CONTEXT` context variable in the dispatch code)
- After the step impl returns (or raises), we `wait_for_all_substeps()` — no dangling tasks
- Then `check_for_aborts()` scans all spawned Pendings for StepAbortError
- If any sub-step aborted, the containing step's result is discarded even if it returned normally

This is simpler, doesn't require Pending to know about StepAbortError, and gives consistent behavior: a step is the container for everything it launches.

### Subslot split-state: lazy persistence

`subslot()` is synchronous, so it can't persist `SlotSplit` state immediately. Instead, split state is persisted lazily: `_save_state` calls `_ensure_parent_split()` which walks up the parent chain and persists `SlotSplit` for any ancestor that hasn't been persisted yet. This means we never see persisted subslot data without the parent being marked as Split. Each `LiveCacheSlot` tracks its `_parent` for this purpose.

### Replay check: relative paths, not absolute pin keys

Initial implementation stored absolute pin keys in `TrackedForeignRun.run_pin_key`. This broke when the same logical slot was accessed via a different cache-slot arg (e.g., swapping slot B for slot C). The replay check would look up the old pin key instead of resolving through the new arg.

Fix: all slot references in `slot_deps` are now relative derivation paths:
- `ForeignSlotRef(arg_index, subslot_path)` — resolved by walking the current cache-slot arg
- `InternalSlotRef(cache_slot_key, subslot_path)` — resolved by walking the step's own slot

The replay check resolves each tracked run's location from the *current* cache-slot args at check time, so swapping a different cache slot with different data is correctly detected. This also makes the cache tree portable — you can move it to a different namespace or prefix without breaking anything.

## Refactoring: tracing/dispatch separation (step1post1)

### Case (b) fixed: unified tracing in step context

The tracing layer was refactored so that all tracking lives in `LiveStepContext`:

- `TracingCacheSlot` now carries `_origin: ForeignSlotRef | InternalSlotRef` (unified for both foreign args and internal cache slots) and holds a `_step_ctx` reference instead of a tracker.
- `TracingCacheSlot.run()` routes through `step_ctx.dispatch_from_step()`, which inspects both the target and args for tracing info, records a single `_PendingTrackedRun`, unwraps tracing, and delegates to the backend.
- `LiveStepContext.cache_slot(key)` returns a `TracingCacheSlot` with `InternalSlotRef` origin, so internal slots are also tracked.
- `_EntrypointBackend.dispatch_in_slot` is simplified — only deals with plain `LiveCacheSlot` instances, no tracing awareness.

This gives a clean separation: **tracing (write side)** lives entirely in the step context, **replay check (read side)** lives in the dispatch layer and only needs `LiveCacheSlot` + persisted `slot_deps`.

### Boundaries improved

The refactoring partially addressed the leaky-boundary concern. The backend no longer inspects `CURRENT_STEP_CONTEXT` for pending tracking (the step context handles that in `dispatch_from_step`). Tracing proxy setup moved from `_execute_in_slot` to `LiveStepContext`. Some internal accesses remain (`slot._lock`, `slot._loaded_state`) but these are less concerning now that the main cross-cutting concern (tracing) is cleanly separated.

### Remaining maintainability items

`_execute_in_slot` is still a long sequential method. Further refactoring into a `SlotExecution` lifecycle object would be a good next step but isn't blocking.

## Minor things to revisit

- **`LiveCacheSlot._load_state` type dispatch**: The `SlotSplit | SlotPartial | SlotFinished` union load works in tests but relies on mashumaro's discriminated union support. May need refinement for edge cases.
- **Concurrent access edge cases**: The per-slot Lock covers same-slot races, but cross-slot coordination (e.g., two tasks writing to sibling subslots simultaneously) hasn't been stress-tested.
- **`observed_finished_id` computation**: Currently computed by re-storing the `SlotFinished` entity (idempotent for content-addressed data) in `build_slot_deps`. This works but is a bit indirect — could instead pass around the `EntityRef` from when the state was originally stored.
