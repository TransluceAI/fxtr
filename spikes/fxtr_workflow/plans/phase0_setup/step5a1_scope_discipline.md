# Step 5a1: Scope Discipline for Workflow State Management

## Context

Step 5a added first-class FxtrID support to the workflow system, including a scope hierarchy (world → system → working → exec) and a split between cache resolution and step execution. The core implementation works for primitive results (6 tests pass, 139 existing tests pass), but **7 tests fail** because FxtrID values in step results are inaccessible to the caller after the exec scope exits.

This sub-plan applies "scope discipline" (see `notes/scope_discipline.md`) to the workflow internals, fixing the FxtrID result issue and making the scope/lifetime management obviously correct via local reasoning.

## Root cause of failing tests

All 7 failures hit `ValueError: Node ... was not found in any parent scope!` in `_lift_result_fxtr_ids`. The chain:

1. Step creates a FxtrID (entity stored in exec scope)
2. `StepOutput(value=fid)` stored in exec scope, `SlotFinished` pinned
3. Exec scope exits → its arena is closed (line 912 of `entity_ref_scope.py`)
4. `_cache_resolution` loads `SlotFinished` (works — it was retrieved into `namespace.scope`)
5. `state.result_ref.load()` loads `StepOutput` — but StepOutput's data is gone with exec scope
6. Even if StepOutput loaded, the FxtrID inside it isn't tracked in any live scope

**Key insight**: `namespace.pin()` **is transitive** — it copies the entity and all transitive dependencies (strong AND weak link targets) into the namespace's backing store. The data IS persisted. The problem is that after pin, we never make those children *loadable in a scope* — we only retrieve the top-level `SlotFinished` into `namespace.scope`, not its children.

## Design: pin-then-retrieve-then-walk

After pinning a root entity (like `SlotFinished`), the namespace's backing store has all transitive deps. We can then **retrieve** the root into `namespace.scope` and **walk the ref chain** to load children — each child load is served by the namespace's `SQLiteNamespaceArena`, which can lazily serve any entity in the store.

### Persist flow (replacing current `_persist_state`)

```
1. Store state entity in whatever scope is convenient (exec scope, working scope)
2. Pin into namespace from that scope — all transitive deps go to the store
3. Retrieve into namespace.scope — root is now loadable there
4. Walk refs in namespace.scope if needed (e.g. load StepOutput to access result FxtrIDs)
5. Update slot lifecycle with the retrieved ref + namespace.scope
```

After step 3, the root and all children are loadable from `namespace.scope` because the backing store has them and the namespace's arena can serve them lazily. No explicit `copy_from_scope` needed — the pin already moved everything to the store.

### Result delivery flow

```
# After SlotFinished is in namespace.scope (from persist or from cache hit retrieve):
state = await ns_ref.load(scope=namespace_scope)       # SlotFinished
output = await state.result_ref.load(scope=namespace_scope)  # StepOutput
# output.value may contain FxtrIDs — they're loadable from namespace_scope
# because pin stored them and namespace's arena can serve them

for fid in collect_fxtr_ids(output.value):
    caller_scope.lift(fid)  # works: namespace.scope is ancestor of caller
```

## Scope discipline principles applied

Reference: `notes/scope_discipline.md`

### 1. No implicit active scope in library code

Every `FxtrScope.get_active()` call in the workflow internals must be replaced with an explicit scope parameter. The active scope is only used at user boundaries:
- `dispatch_in_slot` captures `caller_scope = FxtrScope.get_active()` (the user's scope)
- `_execute_external_step` sets exec scope as active via `with exec_scope.as_active_scope()` for the user's step function

### 2. Pass and store scopes alongside references

`_SlotLocalPresent` currently stores `state_ref: EntityRef[SlotState]` without recording where it's loadable. After the refactor, it should also carry a `load_scope: FxtrScope` (= `namespace.scope`) so that any code holding a lifecycle state knows where to load from.

### 3. Document membership rationales

Every `store()`, `load()`, `pin()`, and `lift()` call should have a brief comment explaining why the entity is in the given scope. For example:
```python
# StepOutput is loadable in namespace_scope because we pinned SlotFinished
# (which references it) and then retrieved SlotFinished into namespace_scope.
# The namespace's backing store has all transitive deps from the pin.
output = await state.result_ref.load(scope=namespace_scope)
```

## Code changes

### `_persist_state` (cache_slot.py)

**Current**: uses `FxtrScope.get_active()`, stores entity in active scope, pins, retrieves into `namespace.scope`, but keeps the original ref in lifecycle.

**New signature**: `_persist_state(self, state: SlotState, *, store_scope: FxtrScope) -> EntityRef[SlotState]`

**New flow**:
1. `state.store(scope=store_scope)` — store in the provided scope
2. `namespace.pin(key, ref, scope=store_scope)` — all transitive deps go to store
3. `ns_ref = namespace.retrieve(key, type=SlotState, into_scope=namespace.scope)` — retrieve into namespace scope
4. `self._lifecycle = _SlotLocalPresent(state_ref=ns_ref, load_scope=namespace.scope, ref=...)` — store the *retrieved* ref
5. Return `ns_ref`

Note: store_scope can be any scope that has the entity and its deps tracked. We don't need to move entities to namespace.scope before pinning — pin reads from the source scope's arena.

### `_save_state` (cache_slot.py)

**New signature**: `_save_state(self, state: SlotState, *, store_scope: FxtrScope) -> EntityRef[SlotState]`

Passes `store_scope` through to `_persist_state` and `_ensure_split`.

### `_ensure_split` (cache_slot.py)

Needs a `store_scope` parameter for the `_persist_state(SlotSplit())` call. SlotSplit has no children, so any scope works — use the provided one.

### `_SlotLocalPresent` (cache_slot.py)

Add `load_scope: FxtrScope` field. This is always `namespace.scope` — the scope where `state_ref` was retrieved into.

### `_load_state` (cache_slot.py)

Currently retrieves from namespace into the active scope. Under scope discipline:
- Uses `namespace.try_retrieve(key, type=SlotState, into_scope=namespace.scope)` — always into namespace scope
- Loads from `namespace.scope`, not the active scope
- Stores `load_scope=namespace.scope` in `_SlotLocalPresent`

### `_realize_inner` (cache_slot.py)

**New signature**: `_realize_inner(self, expected_ref, *, scope: FxtrScope)`

Uses the explicit scope for `copy_from_scope` and pinning. After pinning and retrieve, stores `load_scope=namespace.scope` in lifecycle.

### `_save_checkpoint` / `_load_checkpoint` (cache_slot.py)

`_save_checkpoint(self, data, *, scope: FxtrScope)` — stores and pins with explicit scope.
`_load_checkpoint` — retrieves into namespace.scope, loads from there.

### `_execute_in_slot` / `_cache_resolution` (entrypoint.py)

**Working scope**: Still created per dispatch. Used for arg resolution, cache key computation. The working scope is where `CacheKeyEntity` and `SlotPartial` are initially stored before pinning.

**Cache resolution changes**:
- `compute_cache_key(scope=working_scope)` — unchanged
- `slot._save_state(SlotPartial(...), store_scope=working_scope)` — explicit scope
- `slot._load_state()` — now always retrieves into namespace.scope internally
- `_handle_finished_slot` — loads all entities from namespace.scope (via lifecycle's `load_scope`)

**Result delivery** (after hit or miss):
- Load `SlotFinished` from namespace.scope
- Load `StepOutput` from namespace.scope (walking the ref chain)
- Lift result FxtrIDs from namespace.scope into caller_scope

### `_execute_external_step` (entrypoint.py)

**Exec scope**: Still created per execution. Step function runs with it as active. All entity creation by the step happens here.

**After step success**:
1. Store `StepOutput`, `TrackedForeignSlotDeps`, `FrozenInternalSlots` in exec_scope
2. Store `SlotFinished` in exec_scope (references all the above)
3. `slot._save_state(SlotFinished(...), store_scope=exec_scope)` — pins transitively, retrieves into namespace.scope
4. Exec scope can now exit — all data is in the namespace store

**After step failure (fallible)**:
Same pattern — `StepError` stored in exec_scope, `SlotFinished` pinned from exec_scope.

### `_handle_finished_slot` (entrypoint.py)

Loads everything from namespace.scope:
```python
ns_scope = slot.load_scope  # from _SlotLocalPresent
state = await state_ref.load(scope=ns_scope)  # SlotFinished
result_entity = await state.result_ref.load(scope=ns_scope)  # StepOutput or StepError
```

For replay check, loads `slot_deps` and `frozen_internals` from `ns_scope` too.

### `_lift_result_fxtr_ids` (entrypoint.py)

Unchanged in purpose, but now works because FxtrIDs are loadable from namespace.scope (which is an ancestor of caller_scope).

### `_freeze_slot_tree` / `_freeze_internal_slots` (entrypoint.py)

Currently take a `scope` parameter (exec_scope) — this is fine, they store into exec_scope and the entities are picked up transitively when SlotFinished is pinned.

### `LiveStepContext` checkpoint delegation (step_context.py)

`save_checkpoint` needs to pass the exec scope explicitly. The step context needs access to the exec scope (or namespace scope) to pass to `_save_checkpoint`. Currently the exec scope is the active scope when the step function calls `save_checkpoint`, but under scope discipline we should capture it explicitly.

Options:
- (a) `LiveStepContext.__init__` receives `exec_scope` and stores it
- (b) `save_checkpoint` captures `FxtrScope.get_active()` at call time — this is a user boundary (the step function is user code), so active scope usage is acceptable under scope discipline

Option (b) is simpler and valid since `save_checkpoint` is called by user code from within the step, and the exec scope IS the active scope at that point. Add a comment documenting this as a user-boundary exception.

### `FrozenCacheSlot._ensure_loaded` and `load_state` (cache_slot.py)

These use `EntityRef.load()` without specifying a scope. Under scope discipline, they should load from an explicit scope. The frozen cache slot is loaded in the context of a replay check, which runs inside the working scope. The entities are in namespace.scope (ancestor of working scope), so they're loadable.

Options:
- Pass namespace_scope through `FrozenCacheSlot`
- Use the slot's associated namespace.scope
- Since FrozenCacheSlot is only used during replay checks (within working scope), and namespace.scope is always an ancestor, loading from any descendant works

For now: add a `load_scope` to `FrozenCacheSlot.__init__` and use it.

### `FrozenStepContext.replay_check` (cache_slot.py)

Loads entities during replay check. Under scope discipline, should use explicit namespace.scope. Receives it as parameter or via the FrozenCacheSlot.

## Implementation steps

- [x] 1. Add `load_scope: FxtrScope` to `_SlotLocalPresent`
- [x] 2. Refactor `_persist_state`: take explicit `store_scope`, pin-then-retrieve into `namespace.scope`, store retrieved ref + `load_scope` in lifecycle
- [x] 3. Refactor `_save_state` and `_ensure_split`: thread `store_scope` parameter
- [x] 4. Refactor `_load_state`: always retrieve into `namespace.scope`, store `load_scope`
- [x] 5. Refactor `_realize_inner`: take explicit `scope` parameter
- [x] 6. Refactor `_save_checkpoint` / `_load_checkpoint`: explicit scope for save; load from `namespace.scope`
- [x] 7. Update `_cache_resolution` in entrypoint: pass `working_scope` to save_state, load results from `namespace.scope`
- [x] 8. Update `_execute_external_step`: pass `exec_scope` to `_save_state`
- [x] 9. Update `_handle_finished_slot`: load all entities from `namespace.scope` (via lifecycle `load_scope`)
- [x] 10. Update result delivery: load StepOutput in namespace.scope, then lift FxtrIDs into caller_scope
- [x] 11. Update `LiveStepContext.save_checkpoint`: user-boundary active-scope usage (option b)
- [x] 12. Update `FrozenCacheSlot` and `FrozenStepContext`: add `load_scope`, use for all loads
- [x] 13. Add membership rationale comments throughout
- [x] 14. Verify all 152 tests pass (139 existing + 13 FxtrID)
- [x] 15. All 13 FxtrID tests pass (the 7 previously failing ones now work)
- [x] 16. Update CODE_OVERVIEW.md

## Risks and open questions

### 1. Namespace arena lazy loading for children

The plan assumes that after pinning SlotFinished (transitive) and retrieving it into namespace.scope, loading its EntityRef children (StepOutput, etc.) in namespace.scope will work because the namespace's `SQLiteNamespaceArena` can lazily serve any entity in the store. If this doesn't work out of the box (e.g., if the scope needs the child entity_id to be explicitly tracked), we may need an extra step to register child IDs — but the pin's transitivity should handle this.

### 2. `_SlotLocalPresent.state_ref` vs the original exec-scope ref

Currently `state_ref` is set from `state.store(scope)` — the exec-scope ref. After the refactor, it's set from `namespace.retrieve(...)` — same entity_id, but loadable from namespace.scope. Code that currently uses `state_ref.entity_id` for comparison (like `build_slot_deps`) should be unaffected since the entity_id is the same.

### 3. Concurrent working scopes

Multiple dispatches can be in flight simultaneously, each with their own working scope. Under scope discipline, all slot state is in namespace.scope (shared, long-lived), so there's no issue with cross-scope access. The working scopes are only used for temporary computation (arg resolution, cache key).

### 4. Replay reference `copy_from_scope`

`_realize_inner` currently uses `copy_from_scope` to copy from the replay reference's namespace scope. Under scope discipline, this still needs an explicit scope parameter. The target scope should be... the working scope? Then we pin from there and retrieve into namespace.scope. Or we can pin the entity directly from the replay ref namespace into our namespace. Need to check the mechanics here.
