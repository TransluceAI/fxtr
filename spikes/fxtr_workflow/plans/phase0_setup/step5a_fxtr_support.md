# Step 5a: First-class fxtr support inside step functions

## Goal

Allow step functions to receive and return `FxtrID` values. Add config entity support to `bind_external_step`. Restructure the scope and execution model to cleanly separate cache resolution from step execution, with a decoupled execute function that can later be swapped for sandbox execution.

## Scope hierarchy

### Entrypoint scopes

When `TopLevelEntrypoint` is created, it captures two scopes:

- **World scope**: The scope that was active at entrypoint creation time. Remains active inside the entrypoint context manager (the entrypoint does NOT change the active scope). The world scope is the "user's world" — it has their entities, their namespaces, etc.

- **System scope**: A new scope created by the entrypoint. `FxtrScope(isolated=True, include=[world_scope])`. NOT active. Serves three purposes:
  1. "I can see all the namespaces" — when the user binds a cache slot, we verify its namespace scope is in the ancestry of the world scope (and therefore accessible from the system scope).
  2. Place to store internal workflow data (currently CacheKeyEntity, slot state, etc. are stored in what's currently `self._scope`).
  3. Keeps the world scope alive for the duration of the entrypoint context, even if the user started it in another async task.

### Per-dispatch scopes

When `cache_slot.run()` is called (whether from top-level or from within a step):

- **Working scope**: `FxtrScope(isolated=True, include=[active_scope])`. This is where cache resolution happens — cache key computation, replay checks, loading cached results. Since we validate that the caller's active scope descends from the world scope (which includes all namespace scopes), the working scope transitively has access to everything: the user's entities, all namespace data, and entities from the dispatching step. (Note: if it turns out the working scope needs direct access to the system scope — e.g. for storing internal entities — we can add `system_scope` to the include list. Add a code comment at the creation site flagging this.)

On cache miss, the working scope calls a decoupled execute function:

- **Step execution scope**: `FxtrScope(isolated=True, include=[working_scope])`. Created inside the execute function. The step function runs with this scope set as active (via `scope.as_active_scope()`). For external steps, this directly inherits from the working scope, so the step can load arg entities and namespace data. (For future sandbox steps, the execute function would create a more restrictive scope with explicit entity mounting instead.)

### Scope diagram

```
World scope (captured at entrypoint creation; stays active throughout)
  │
  └─ System scope (isolated; depends on world scope; NOT active)
       │   - all namespace scopes verified as ancestors of world scope
       │   - stores workflow internal entities
       │
       └─ Working scope W₁ (isolated; depends on active_scope = world scope)
            │   - created per top-level cache_slot.run() call
            │   - cache key computation, replay check
            │
            └─ Step execution scope S₁ (isolated; depends on W₁; set as ACTIVE)
                 │   - step function runs here
                 │   - step creates entities here
                 │
                 └─ Working scope W₂ (isolated; depends on active_scope = S₁)
                      │   - created per sub-step cache_slot.run() call
                      │   - inner cache key computation
                      │
                      └─ Step execution scope S₂ (isolated; depends on W₂; ACTIVE)
                           │   - sub-step runs here
                           ...
```

## Cache resolution flow

The cache resolution flow is the same at every level (top-level and sub-step). It runs in the working scope.

```
cache_slot.run(step, *args) is called:

1. Validate: caller's active scope must descend from the world scope
   (or the step execution scope if inside a step). Error if not.

2. Create working scope W:
   FxtrScope(isolated=True, include=[active_scope])

3. In W: resolve args (await Pendings)

4. In W: validate FxtrIDs in args (no _provenance_parent)

5. In W: compute cache key → CacheKeyEntity stored in W
   - FxtrIDs in args are entity links; they're liftable in W because W
     includes the active scope (which has them)

6. In W: acquire slot lock, check slot state (probe/realize/reject)

7a. CACHE HIT (SlotFinished with matching key, replay check passes):
    - Load result from namespace into the caller's active scope
      (namespace.retrieve into active_scope)
    - Return the result value

7b. CACHE MISS (or replay check fails in best-effort mode):
    - Pin SlotPartial into namespace from W
      (this transitively persists arg entities into namespace)
    - Call execute function:
        execute_external_step(slot, step, args, kwargs, W, cache_key_ref, ...)
    - Execute function pins result into namespace on success
    - Load result from namespace into the caller's active scope
    - Return the result value

7c. CONCURRENT SAME-KEY: wait for first execution, then treat as cache hit
```

### Key insight: results always flow through the namespace

Whether cache hit or miss, the caller always receives results by loading from the namespace into their active scope. This means:
- The execute function's only "output" is what it pins into the namespace
- The caller doesn't need access to any execute-internal scopes
- Entity lifetime is managed by the namespace, not by scope relationships

## Execute function (external steps)

The execute function is decoupled from cache resolution. It receives everything it needs and is responsible for running the step and persisting the result.

```python
async def execute_external_step(
    slot: LiveCacheSlot,
    step: ExternalStepHandle,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    working_scope: FxtrScope,
    cache_key_ref: EntityRef[CacheKeyEntity],
    tracker: ForeignRunTracker,
    run_replay_ref: RunReplayReference | None,
) -> None:
    """Execute an external step and persist its result into the namespace.

    On success, SlotFinished is pinned. On failure, slot remains Partial.
    The caller reads the result from the namespace after this returns.
    """
```

### Execute function flow

```
1. Create step execution scope S:
   FxtrScope(isolated=True, include=[working_scope])

2. Create LiveStepContext (owns tracker, internal cache slots, etc.)

3. Wrap cache-slot args with tracing (existing mechanism)

4. Run the step function with S as active:
     with S.as_active_scope():
         result = await step.impl(step_ctx, *traced_args, **traced_kwargs)

5. Validate result (including FxtrID provenance check)

6. Wait for all sub-steps to complete (existing containment model)

7. Check for poisoning (existing abort propagation)

8. On success:
   - Store StepOutput, TrackedForeignSlotDeps, FrozenInternalSlots,
     SlotFinished all in S
     (result FxtrIDs are in S; CacheKeyEntity is liftable from
      working scope via parent chain)
   - Pin SlotFinished into namespace from S
   - Clean up frozen pins, delete checkpoint

9. On failure:
   - If fallible: cache error, pin SlotFinished from S
   - If not fallible: leave slot as Partial, raise StepAbortError
```

Note: post-execution entities are stored in the step execution scope (not the working scope), because result FxtrIDs created by the step live in S. Since S is a child of the working scope, S can't lift result entities "upward" into the working scope. But S can lift the CacheKeyEntity "downward" from the working scope, so S has everything it needs.

### Sub-step dispatch from within a step

When the step function calls `cache_slot.run(sub_step, ...)` from inside the step execution scope:

1. The active scope is the step execution scope S₁
2. Cache resolution creates a new working scope using the same uniform formula:
   `W₂ = FxtrScope(isolated=True, include=[active_scope])` where
   `active_scope = S₁`. The system scope is transitively accessible
   via S₁ → W₁ → world_scope → ... → system_scope ancestry.
3. Cache resolution proceeds identically to top-level
4. On cache miss, a new step execution scope S₂ is created from W₂
5. On completion, result is pinned to namespace
6. Result is loaded from namespace into S₁ (the caller's active scope)

Entity `y` created by step A in S₁ and passed to sub-step B:
- `y` is in S₁ (step A stored it there)
- W₂ includes S₁, so `y` is liftable in W₂ for cache key computation
- Pinning SlotPartial from W₂ transitively persists `y` into namespace
- S₂ includes W₂ which includes S₁, so `y` is accessible in S₂

Entity `z` returned by sub-step B to step A:
- `z` is pinned into namespace as part of B's SlotFinished
- After B's execute function returns, the cache resolution loads `z`
  from namespace into S₁ (the caller's active scope)
- Step A can now use `z`

## Changes to existing code

### Type system

**`types.py`**: Add `FxtrID` to `ConcreteTypes`, `PendingOrConcreteTypes`, and `SerializableTypes`.

**`entities.py`**: Add `FxtrID` to `FrozenCacheKeyData`.

### Validation

**`_validate_result`**: Accept `FxtrID`, reject if `_provenance_parent is not None`.

**New `_validate_fxtr_ids_in_args`**: Walk resolved args, reject FxtrIDs with `_provenance_parent`.

**New `collect_fxtr_ids(value) -> list[FxtrID]`**: Deep-walk utility in `resolve.py`. Used by validation, and potentially by mounting logic in the future.

### Cache key computation

**`_serialize_for_cache_key`**: Add `FxtrID` case — store directly as an entity link (FxtrID is content-addressed, so identical entities produce identical cache keys).

**`FrozenCacheKeyData`**: Add `FxtrID` to the union.

### Mashumaro serialization

`StepOutput.value` and `StepCheckpoint.value` are typed as `SerializableTypes`. Adding `FxtrID` means mashumaro must serialize/deserialize FxtrID within this recursive union. The existing `FxtrIDSerializationStrategy` handles serialization (FxtrID → CBOR weak link tag 33001). Deserialization needs verification — mashumaro must map tag 33001 back to `FxtrID` within the union. **Spike this early** — if it doesn't work natively, we may need a custom discriminator or manual handling.

### Step handle and config entity

**`step_handle.py`**: `ExternalStepHandle` gains `config_id: FxtrID | None`.

**`entrypoint.py`**: `bind_external_step` gains optional `config_id` parameter.

**`step_context.py`**: `StepContext` ABC gains `get_step_config_id() -> FxtrID | None`. `LiveStepContext` stores and returns it. Config entity is NOT part of the cache key.

Config entity lifetime: the config FxtrID is lifted into the system scope at `bind_external_step` time (just like the step's key_id). It's accessible in the working scope and step execution scope via the parent chain.

### Entrypoint restructure

**`TopLevelEntrypoint`**:
- Capture world scope: `FxtrScope.get_active()` at creation time
- Create system scope: `FxtrScope(isolated=True, include=[world_scope])`
- Do NOT create a new active scope (currently creates `FxtrScope()` which becomes active)
- The system scope replaces the current `self._scope` as the place for internal entities
- Namespace scope validation in `open_cache_slot`: check that `namespace.scope` is in the ancestry of the world scope

**`_EntrypointBackend`**:
- Holds both `world_scope` and `system_scope`
- `get_scope()` → returns system scope (for internal entity storage)

### Cache resolution refactor

Currently `_execute_in_slot` handles both cache resolution and step execution in one method. Split into:

1. **Cache resolution** (in `_execute_in_slot` or a new method): Creates the working scope, computes cache key, checks cache, handles hit/miss/concurrent. On miss, calls the execute function. On return (hit or miss), loads result from namespace into the caller's active scope.

2. **Execute function** (`_execute_external_step`): Receives working scope + all needed context. Creates step execution scope, runs step, persists result. Returns nothing — result is in the namespace.

### Loading results from namespace into caller's scope

After cache hit or successful execution, the result is in the namespace. The cache resolution code loads it into the caller's active scope (captured at dispatch time):

```python
# In cache resolution, after result is available in namespace:
result_entity = await slot_finished.result_ref.load()  # loads in working scope
result_value = result_entity.value  # may contain FxtrIDs

# Lift result FxtrIDs into caller's active scope
for fid in collect_fxtr_ids(result_value):
    caller_scope.lift(fid)

return result_value
```

The caller's active scope always has the namespace scope in its ancestry:
- Top-level: world scope → namespace scope (verified at bind time)
- Sub-step: step execution scope → working scope → system scope → world scope → namespace scope

So `lift` works — it walks the parent chain, finds the entity in the namespace scope, and bridges it into the caller's scope.

### Checkpoint support

Checkpoints contain `SerializableTypes`, which now includes `FxtrID`. When `save_checkpoint` is called:
- The step is running in the step execution scope (active)
- Checkpoint data may contain FxtrIDs stored in that scope
- The `StepCheckpoint` entity must be stored in the step execution scope (not the system scope as currently done), so it can lift those FxtrIDs
- Then pinned into the namespace from the step execution scope

Currently `LiveCacheSlot._save_checkpoint` takes a scope parameter (`self._backend.get_scope()`, i.e. the system scope). This needs to change to accept and use the step execution scope instead.

When `load_checkpoint` is called:
- The checkpoint entity is loaded from the namespace
- FxtrIDs in the data are in the namespace
- The step execution scope has the namespace in its ancestry, so lift works

### Slot state persistence scope

`LiveCacheSlot._persist_state` and `_save_state` currently always use `self._backend.get_scope()` (the system scope) for storing state entities and pinning them. In the new model, different slot state transitions happen in different scopes:

- **SlotSplit, SlotPartial** (during cache resolution): stored and pinned from the working scope. SlotPartial's CacheKeyEntity may reference FxtrID args, which are in the working scope.
- **SlotFinished** (after step execution): stored and pinned from the step execution scope. References StepOutput which contains result FxtrIDs.

So `_persist_state` / `_save_state` need to accept the scope as a parameter rather than always pulling from the backend. Every call site that persists slot state must pass the appropriate scope.

### Pending resolution and FxtrID access

When a step awaits a Pending whose result contains FxtrIDs:
- The result FxtrIDs were pinned into the namespace by the producing step
- After the cache resolution loads them into the caller's active scope (the step execution scope), they're available
- The step can then load them normally

The cache resolution code (in its spawned task) is responsible for loading results into the caller's scope. The Pending resolution itself just returns the value — the FxtrIDs are already accessible because the cache resolution handled it.

**No context-variable hook needed on Pending.** The dispatch machinery captures the caller's active scope at dispatch time. After the execute function returns (or on cache hit), it loads result FxtrIDs from the namespace into that captured scope. Since `pending.set_result()` is called after loading completes, the entities are in the right scope before the caller can observe the result.

## Lifetime walkthrough

### Step A (top-level) calls step B with locally-created entity

```
Workflow script (active scope = world scope W):
  x = store some entity in W
  slot_a.run(step_a, x)

Cache resolution for A:
  Working scope W_A = FxtrScope(isolated=True, include=[W])
  Cache key computed in W_A (x liftable from W)
  Cache miss → pin SlotPartial (persists x into namespace)
  → execute_external_step(...)

Execute function for A:
  Step execution scope S_A = FxtrScope(isolated=True, include=[W_A])
  with S_A.as_active_scope():
    # x is accessible: S_A → W_A → W → (x is there)
    step_a runs:
      y = create new entity (stored in S_A)
      slot_b.run(step_b, y)

Cache resolution for B:
  active scope = S_A
  Working scope W_B = FxtrScope(isolated=True, include=[S_A])
  Cache key in W_B: y is liftable from S_A
  Cache miss → pin SlotPartial (persists y into namespace)
  → execute_external_step(...)

Execute function for B:
  Step execution scope S_B = FxtrScope(isolated=True, include=[W_B])
  with S_B.as_active_scope():
    # y is accessible: S_B → W_B → S_A → (y is there)
    step_b runs:
      z = create new entity (stored in S_B)
      return z

  Store StepOutput(value=z), SlotFinished etc. in S_B
  Pin SlotFinished into namespace from S_B (persists z)
  S_B exits

Cache resolution for B (continued):
  Load result from namespace into caller's scope (S_A)
  S_A.lift(z)  ← z is in namespace scope, ancestor of S_A
  Pending resolves with z

Back in step_a:
  z = await pending_b  ← z is already in S_A
  return [y, z]

Execute function for A (continued):
  Store StepOutput(value=[y, z]), SlotFinished etc. in S_A
    y: created by step, already in S_A
    z: lifted from namespace into S_A during cache resolution for B
    CacheKeyEntity: liftable from W_A via parent chain
  Pin SlotFinished into namespace from S_A
  S_A exits

Cache resolution for A (continued):
  Load result from namespace into caller's scope (W, the world scope)
  W.lift(y), W.lift(z)  ← both in namespace scope, ancestor of W
  Pending resolves with [y, z]
```

## Implementation steps

- [x] 1. Add `FxtrID` to type aliases (`ConcreteTypes`, `PendingOrConcreteTypes`, `SerializableTypes`, `FrozenCacheKeyData`)
- [x] 2. Add `collect_fxtr_ids(value)` utility in `resolve.py`
- [x] 3. Add FxtrID validation utilities (provenance parent check for args and results)
- [x] 4. Update `_serialize_for_cache_key` to handle `FxtrID`
- [x] 5. Update `_validate_result` to accept `FxtrID` (with provenance check)
- [x] 6. Verify mashumaro serialization of `FxtrID` in `StepOutput.value` / `StepCheckpoint.value` — spike and add test
- [ ] 7. Update CODE_OVERVIEW.md with new type changes
- [x] 8. Add config entity support:
  - [x] `ExternalStepHandle` gains `config_id: FxtrID | None`
  - [x] `bind_external_step` gains optional `config_id`
  - [x] `StepContext` ABC gains `get_step_config_id() -> FxtrID | None`
  - [x] `LiveStepContext` stores and returns config_id
- [x] 9. Restructure entrypoint scopes:
  - [x] Capture world scope at creation
  - [x] Create system scope (isolated, depends on world scope)
  - [x] Remove current `FxtrScope()` creation that changes active scope
  - [ ] Namespace scope ancestry validation in `open_cache_slot` (deferred)
  - [ ] Active scope validation when `cache_slot.run()` is called (deferred)
- [x] 10. Refactor cache resolution (split from step execution):
  - [x] Create working scope per dispatch (isolated, includes active scope + system scope)
  - [x] Cache key computation in working scope
  - [x] Cache hit path: load result from namespace, lift FxtrIDs into caller's scope
  - [x] Cache miss path: pin SlotPartial, call execute function, lift FxtrIDs
  - [x] Concurrent same-key handling (existing mechanism, adapted)
- [x] 11. Implement decoupled execute function for external steps:
  - [x] Create step execution scope (isolated, includes working scope)
  - [x] Create LiveStepContext with execution scope
  - [x] Run step with execution scope as active
  - [x] Store all result entities in execution scope
  - [x] Pin SlotFinished from execution scope
- [x] 12. Update sub-step dispatch:
  - [x] Working scope for sub-steps includes [active_scope (= step execution scope)] — same formula as top-level
  - [x] Results loaded from namespace into parent's step execution scope
  - [x] Pending resolution delivers FxtrIDs that are already in the right scope
- [x] 13. Update checkpoint save/load for FxtrID support (uses active scope automatically)
- [x] 14. Write tests (in test_fxtr_support.py — all 13 pass after scope discipline refactor):
  - [x] Basic: step receives FxtrID arg, loads entity, returns primitive ✓
  - [x] Basic: step creates entity, returns FxtrID ✓ (fixed by scope discipline)
  - [x] Cache reuse with FxtrID args (same/different entity → same/different cache key) ✓
  - [x] Step A → step B with FxtrID passing (locally-created entity) ✓ (fixed by scope discipline)
  - [x] Step creates entity, passes to sub-step, sub-step returns derived entity ✓ (fixed by scope discipline)
  - [x] Config entity: step accesses config, loads entity ✓
  - [x] Config entity: None when not set ✓
  - [x] FxtrID in checkpoint save/load ✓ (fixed by scope discipline)
  - [x] Provenance parent rejection (args and results) ✓
  - [x] Cache hit with FxtrID results (loaded into caller's scope) ✓ (fixed by scope discipline)
  - [ ] Scope ancestry validation (run outside expected scope → error) (deferred)
  - [x] FxtrID in list/dict results ✓ (fixed by scope discipline)
- [x] 15. Update CODE_OVERVIEW.md with 5a documentation

## Open questions and risks

### 1. Mashumaro serialization of FxtrID in the SerializableTypes union

Adding `FxtrID` to the recursive `SerializableTypes` union means mashumaro must handle it during both serialization and deserialization of `StepOutput.value` and `StepCheckpoint.value`. Serialization should work via `FxtrIDSerializationStrategy` (FxtrID → CBOR weak link tag 33001). Deserialization is the concern: mashumaro must map tag 33001 back to `FxtrID` within the union. This may need a custom discriminator or manual handling. **Spike early** (step 6).

### 2. Scope creation cost on cache hits

Every `cache_slot.run()` creates a working scope, even for cache hits. If scope creation is expensive (async context manager, arena allocation), this adds overhead to the hot path. If it's a problem, we could defer working scope creation until after the initial cache check (but this complicates the flow since cache key computation needs the working scope).

### 3. Multiple namespace scopes

Cache-slot args can come from different namespaces. The working scope needs access to all of them for the replay check. Since the working scope includes the active scope, which descends from the world scope, which must be an ancestor of all namespace scopes (verified at bind time), this works transitively. If namespaces are opened dynamically during the workflow, we'd need to re-validate. For now, assume all namespaces are pre-opened.

### 4. Loading results into caller's scope after Pending resolution

The dispatch code (in a spawned task) loads the result from the namespace into the caller's active scope. This requires capturing the caller's scope at dispatch time. The scope must remain alive until the result is loaded — guaranteed because the caller is alive while awaiting the Pending. The spawned task finishes loading before calling `pending.set_result()`, so entities are in the right scope before the caller observes the result.

### 5. Future sandbox execute functions

The current design has the step execution scope inherit from the working scope (full access). For sandboxed steps, the execute function would instead create a restricted scope with explicit entity mounting (via `unsafe_lazily_attach_from_arena`). The execute function interface should be designed so that this swap is straightforward — it receives the working scope but is free to create whatever internal scope structure it needs.

## Implementation status

### What's done

- **Type system**: `FxtrID` added to `ConcreteTypes`, `PendingOrConcreteTypes`, `SerializableTypes`, `FrozenCacheKeyData`. Mashumaro round-trip verified (spike test passed).
- **Validation**: `_validate_result` accepts FxtrID (rejects provenance parent). `_validate_fxtr_ids_in_args` checks args after resolution. `collect_fxtr_ids` utility in resolve.py. `_serialize_for_cache_key` handles FxtrID (stores directly as entity link).
- **Config entity**: `ExternalStepHandle.config_id`, `bind_external_step(config_id=)`, `StepContext.get_step_config_id()`, `LiveStepContext` stores and returns it.
- **Scope restructure**: World scope captured at entrypoint creation. System scope created (isolated, includes world scope). Entrypoint does NOT change active scope. Working scope created per dispatch (isolated, includes caller + system scope). Step execution scope created per execution (isolated, includes working scope).
- **Cache resolution / execute split**: `_execute_in_slot` → `_cache_resolution` (working scope) + `_execute_external_step` (exec scope). After execution, result is loaded from namespace via `_handle_finished_slot`. `_Backend.get_scope()` removed; `_persist_state`, `_realize_inner`, `_save_checkpoint` all use `FxtrScope.get_active()`.
- **Cross-scope state access**: `_persist_state` retrieves pinned entity into `namespace.scope` after pinning, so `_load_state`'s cached `state_ref` is loadable from any future working scope.
- **All 139 existing tests pass.**
- **6 of 13 new FxtrID tests pass** (arg passing, cache reuse, config entity, provenance rejection).

### What's blocked: FxtrID result scope plumbing

7 new tests fail because step result FxtrIDs aren't accessible in the caller's scope after the working scope exits. The specific issue:

1. Step creates a FxtrID (entity stored in exec scope)
2. `StepOutput(value=fid)` is stored in exec scope, pinned as part of `SlotFinished`
3. After execution, `_cache_resolution` loads `SlotFinished` from namespace → loads `StepOutput` → gets the FxtrID value
4. `_lift_result_fxtr_ids` tries to `lift()` the FxtrID into the caller scope
5. **Fails**: the FxtrID is tracked in the working scope (where `StepOutput` was loaded), but `lift()` walks UP the parent chain. The caller scope is a parent of the working scope, so it can't see into it.

**Root cause**: `StepOutput` (and its child entities like the result FxtrID) are stored in the exec scope via `.store()` but never individually pinned to the namespace. The namespace pin for `SlotFinished` stores the `SlotFinished` CBOR (which contains `result_ref` as a weak link), but the actual `StepOutput` entity data needs to be accessible in a scope that the caller can reach via `lift`.

**What was tried**:
- Loading `StepOutput` in `namespace.scope` — failed because `namespace.scope` is an ancestor of the world scope, and loading entities there requires their transitive references to also be accessible in that scope's parent chain (they aren't — they're in descendant scopes).
- Loading in the working scope (default) — works for the load itself, but the result FxtrIDs end up in the working scope which the caller can't reach via `lift`.

**Resolution**: See `step5a1_scope_discipline.md` — apply scope discipline to the workflow internals. The key insight is that `namespace.pin` IS transitive (copies all deps to the store), so after pinning SlotFinished and retrieving it into `namespace.scope`, we can walk the ref chain to load children (StepOutput, result FxtrIDs) from `namespace.scope`. The fix involves threading explicit scopes through all internal methods (replacing `FxtrScope.get_active()`) and storing the retrieved ref + `namespace.scope` in the slot lifecycle.
