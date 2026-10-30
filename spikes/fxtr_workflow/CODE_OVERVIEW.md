# fxtr_workflow Code Overview

A spike prototype of a durable workflow management system for fxtr2. This package is intentionally separate from fxtr2 so that the core workflow ideas can be prototyped and tested independently before integration.

**NOTE:** Superseded by new workflow design, kept for historical reference only.

## Design

See `plans/phase0_setup/sketch.md` for the full design sketch and `plans/phase0_setup/step1_core_system.md` for the implementation plan.

### Key concepts

- **Steps**: Units of computation dispatched by the workflow. Currently only *external steps* (user-provided callables) are supported. Each step receives a `StepContext` and produces a serializable result.
- **Cache slots**: Persistent containers that store step run state. A slot is either Empty, Split (has subslots), Partial (run started but not finished), or Finished (run completed with a cached result). State is stored as fxtr2 entities in a `SQLiteFxtrNamespace`. Slots have two child-creation paths with different integrity rules: *subslots* (`/s/`) are managed by `LiveCacheSlot` and require the parent to be Split (enforced by `_ensure_split`), while *internal cache slots* (`/c/`, used by `StepContext.cache_slot`) are owned by `LiveStepContext` and live under a running step whose Partial state is established by the execution lifecycle.
- **Pending**: An awaitable future returned when a step is dispatched. Can be forwarded to other steps as args; the runtime resolves all Pendings before calling the step.
- **Cache keys**: Content-addressed FxtrIDs derived from step identity + serialized args. Stored as `CacheKeyEntity` (a real fxtr entity, inspectable). Cache slots are replaced by `CacheSlotMarker` entities and step handles by `StepHandleMarker` entities in the key (proper entity types, no collision risk with user data).
- **Replay check**: When reusing a cached result, the system verifies that all foreign cache-slot interactions (tracked via tracing proxies) still match the recorded state. Tracking data is stored in `TrackedForeignSlotDeps` (a separate entity, lazy-loadable via `EntityRef`). All slot references in `slot_deps` are relative derivation paths (`ForeignSlotRef` / `InternalSlotRef`), not absolute pin keys, making the cache tree portable. Each tracked run records `InnerSlotArgMapping` entries for *all* of the inner step's cache-slot args (not just foreign-origin ones), with both the inner arg index and the source ref, so the recursive replay check can reconstruct the complete inner args list.
- **Step results**: Wrapped in `StepOutput` entities and referenced via `EntityRef` from `SlotFinished`, so results can be lazy-loaded separately from slot metadata.
- **Same-slot concurrency**: Each cache slot enforces single-execution: if two concurrent `run()` calls target the same slot with the same cache key, the second blocks (via `_execution_finished` event) until the first completes, then reuses the cached result. If the cache keys differ, the second call raises `CacheSlotError` (write-once violation). This is coordinated between `LiveCacheSlot._execution_finished` and `_EntrypointBackend._execute_in_slot`.
- **Error propagation**: Uncaught exceptions become `StepAbortError(BaseException)`, leaving the slot Partial for retry. Steps *contain* all their sub-steps — the runtime waits for all spawned sub-steps before finishing, and any sub-step abort poisons the containing step's result.
- **Fallible runs**: `cache_slot.run_fallible()` caches errors as valid outcomes. Exceptions inside a fallible step are wrapped in `FallibleRunError` (a normal `Exception`) and persisted as `StepError` entities in `SlotFinished`. On replay, the same error is re-raised. Foreign cache-slot args are forbidden in fallible runs. Inside a fallible step, everything works normally (inner `run()` calls abort+poison as usual), but at the `run_fallible` boundary the exception is "handled" and re-raised without poisoning.
- **Checkpoints**: `StepContext.save_checkpoint(data)` / `load_checkpoint()` provide crash recovery for partial executions. Checkpoint data is persisted at `<slot>/ckpt` and survives across executions. Last-write-wins. Cleaned up automatically when the slot transitions to Finished.
- **Frozen internal slots**: When a step finishes, its internal cache slots (`/c/` pins) are frozen into a `FrozenInternalSlots` entity stored as part of `SlotFinished`. Each internal slot becomes a `FrozenSlotNode` tree (state + subslots, recursively). After freezing, the live `/c/` pins are deleted. The replay check reads from frozen state via `FrozenCacheSlot` (read-only wrapper with sync path accumulation and async deferred loading) and `FrozenStepContext` (short-key lookup into `FrozenInternalSlots`). This makes finished runs self-contained — all internal state is captured in the entity graph, not in mutable pins.
- **Replay references**: Cache slots can optionally have a **replay reference** — another cache slot (from a different namespace) whose cached results are tried first. Two modes: **strict** (error on mismatch) and **best-effort** (fall back to fresh execution). Set via `open_cache_slot(replay_reference=..., replay_mode=...)`. The slot mediates access through `probe_state()` (checks local then reference), `realize()` (copies reference state into local namespace via `scope.copy_from_scope`), and `reject()` (handles mismatch per mode). Replay references propagate through `subslot()` and through `LiveStepContext._internal_cache_slot()` (via `RunReplayReference` protocol). `FrozenStepContext` implements `RunReplayReference` for Finished references (frozen tree), `LiveRunReference` for Partial references (live `/c/` pins).
- **Network transport handles**: Opaque capability tokens (`NetworkTransportHandle`) that steps use to make network connections via `handle.connect() -> ByteStream`. Like `StepHandle`, they have an `identity` (FxtrID) included in cache keys via `NetworkTransportHandleMarker`. They are part of `ConcreteTypes` (can be step args) but not `SerializableTypes` (cannot be step results). Unlike cache slots, they don't need tracing or replay tracking — the identity in the cache key is sufficient. Bound at the entrypoint via `bind_network_transport(connector, key_id)`. A `TransportRouteConnector` adapter runs an in-process HTTP proxy (path filtering, header injection) for controlled upstream access. An httpx bridge utility wraps handles into `httpx.AsyncHTTPTransport` / `httpx.AsyncClient`.
- **FxtrID support**: `FxtrID` is a valid type for step arguments, results, cache keys, and checkpoint data. FxtrIDs are content-addressed, so identical entities produce identical cache keys. FxtrIDs with `_provenance_parent` are rejected in both args and results. Steps can also have a **config entity** (`config_id` on `ExternalStepHandle`) accessible at runtime via `StepContext.get_step_config_id()` — config entities are NOT part of the cache key.
- **Scope hierarchy and scope discipline**: The `TopLevelEntrypoint` captures the caller's scope as the **world scope** (stays active) and creates an isolated **system scope** (child of world scope) for step identities and config entities. Each dispatch creates a **working scope** (isolated, includes caller's active scope + system scope) for cache resolution, and the execute function creates a **step execution scope** (isolated, includes working scope) where the step function runs. Internal code follows **scope discipline** (`notes/scope_discipline.md`): all entity `store()`/`load()` calls use explicit scope parameters (no `FxtrScope.get_active()` in library code), and every scope reference includes a comment explaining why the entity is loadable there. `namespace.scope` is the canonical long-lived scope for all persistent slot state. The **pin-then-retrieve** pattern ensures all transitive deps (including FxtrID results) are accessible: `_persist_state` pins from the working/exec scope (transitive), then retrieves the root into `namespace.scope`, where children can be loaded by walking the ref chain. Result FxtrIDs are lifted into the caller's scope from `namespace.scope`.

## Package Structure

```
projects/fxtr_workflow/
  pyproject.toml              # Package config, depends on fxtr2
  CLAUDE.md                   # Instructions for working in this package
  CODE_OVERVIEW.md            # This file
  plans/phase0_setup/         # Design and implementation plans
  fxtr_workflow/
    __init__.py               # Public API exports
    types.py                  # Type aliases (ConcreteTypes, PendingOrConcreteTypes, SerializableTypes)
    errors.py                 # Step / cache-slot / replay / fallible error types
    entities.py               # fxtr2 entity types + serializable data types — see "Module details"
    pending.py                # Pending[R]: awaitable step result future
    step_handle.py            # StepHandle ABC + ExternalStepHandle (callable + FxtrID key)
    network_transport.py      # NetworkTransportHandle ABC + ExternalNetworkTransportHandle
    cache_slot.py             # CacheSlot machinery (live, frozen, tracing variants) — see "Module details"
    step_context.py           # StepContext ABC + LiveStepContext (tracker, args wrapping, dispatch, checkpoints)
    resolve.py                # Arg resolution, cache key computation, cache-slot traversal, FxtrID collection
    entrypoint.py             # TopLevelEntrypoint + _EntrypointBackend (dispatch logic)
    proxy.py                  # TransportRoute config + proxy_connection (h11-based HTTP proxy over anyio streams)
    transport_utils.py        # TransportRouteConnector (ByteStreamConnectable adapter) + httpx bridge helpers
  tests/                      # See test filenames for coverage scope
```

## Module details

**`entities.py`** holds every fxtr2 entity and serializable subpart used by the workflow:
- Entity types (`DataclassEntity`): `SlotSplit`, `SlotPartial`, `SlotFinished`, `CacheKeyEntity`, `CacheSlotMarker`, `StepHandleMarker`, `NetworkTransportHandleMarker`, `StepOutput`, `StepError`, `StepCheckpoint`, `TrackedForeignSlotDeps`, `FrozenSlotNode`, `FrozenInternalSlots`.
- Plain serializable types (`DataclassFxtrSubpart`): `ForeignSlotRef`, `InternalSlotRef`, `InnerSlotArgMapping`, `TrackedForeignRun`.
- Type alias: `FrozenCacheKeyData`.

**`cache_slot.py`** contains the cache-slot machinery in three variants:
- `CacheSlot` ABC — `run`, `run_fallible`, `subslot`. `ReplayMode` enum + `RunReplayReference` ABC + `LiveRunReference`.
- `LiveCacheSlot` — state machine + persistence + checkpoints + `probe_state` / `realize` / `reject` for replay references.
- `FrozenCacheSlot` — read-only frozen slot wrapper.
- `FrozenStepContext` — frozen replay check (`_resolve_slot_ref`, `replay_check`, internal slot lookup; implements `RunReplayReference`).
- `TracingCacheSlot` — proxy for replay tracking.
- `ForeignRunTracker`.

## Architecture

The `TopLevelEntrypoint` is the main orchestrator. It captures the caller's scope as the **world scope**, creates an isolated **system scope**, and provides methods to bind external steps and open cache slots.

**Scope hierarchy per dispatch**:
```
World scope (captured at entrypoint creation; stays active)
  ├─ System scope (isolated; step identities, config entities)
  └─ Working scope (isolated; includes caller + system; per-dispatch)
       └─ Step execution scope (isolated; includes working; step runs here)
```

**Two dispatch contexts**: At the top level, `cache_slot.run()` dispatches directly via the backend (no tracing). Inside a step, all cache slots are `TracingCacheSlot` instances — both foreign args and internal cache slots — and dispatches route through the step context.

**Inside a step**, when `traced_slot.run(step, *args)` is called:
1. `LiveStepContext.dispatch_from_step()` inspects the target and args for tracing info
2. Records a `_PendingTrackedRun` (target origin + foreign arg origins)
3. Unwraps tracing and delegates to the backend

**Cache resolution** (`_EntrypointBackend._cache_resolution`, runs in working scope):
1. Resolves all `Pending` values in args (deep walk)
2. Validates FxtrIDs in args (no provenance parents)
3. Computes cache key in working scope
4. Checks the slot's persisted state (Finished → `_handle_finished_slot` for replay check → return cached; Partial → re-execute; Empty → new execution)
5. On cache miss: calls `_execute_external_step`
6. On success (hit or miss): lifts result FxtrIDs into caller's scope

**Step execution** (`_EntrypointBackend._execute_external_step`, creates exec scope):
1. Creates step execution scope (isolated, includes working scope)
2. Creates `LiveStepContext` (with tracker, config_id)
3. Wraps cache-slot args with tracing, runs the step with exec scope active
4. On success: waits for sub-steps, checks aborts, validates result, builds slot_deps, persists `SlotFinished` from exec scope
5. On failure: leaves slot as `SlotPartial`, propagates `StepAbortError`

**Slot state persistence** (scope discipline): `LiveCacheSlot._persist_state(state, store_scope=...)` stores in the provided scope, pins to the namespace (transitively persisting all deps including EntityRef targets like StepOutput), then retrieves the root into `namespace.scope`. All loads use `namespace.scope` explicitly via `_SlotLocalPresent.load_scope`. `_save_checkpoint(data, store_scope=...)` similarly takes an explicit scope. No `FxtrScope.get_active()` in library code — only at user boundaries (`dispatch_in_slot` captures caller scope, `save_checkpoint` captures exec scope from user code).

`LiveStepContext` owns internal cache slot state (`_internal_cache_slots` dict, short-keyed) for live execution. `FrozenStepContext` owns slot-ref resolution and replay checking for finished runs — it reads from frozen entity state, not live pins. This separation means replay checks on cached results never need a live execution context.

## Development

```sh
cd projects/fxtr_workflow
uv sync                   # Install dependencies
uv run pytest tests/ -v   # Run tests
```
