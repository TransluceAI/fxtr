# Scope API Spec

FxtrScope and withFxtrScope API surface — how Python's scope system maps to TS without context variables.

## Overview

`FxtrScope` is the central coordination point for entity lifetime management. It provides store/load/lift/copy/abandon operations on entities, manages caching of loaded values, and enforces lifecycle rules (freezing, deep fetchability).

In Python, scopes use context vars for implicit threading. In TS, scopes are always passed explicitly — there is no "active scope."

## Architecture: State/Surface split

Instead of Python's single `FxtrScope` class with a `_load_tracer` context var, we split into two layers:

### FxtrScopeState (internal)

Manages all scope state. One per scope. Not directly exposed to users.

```typescript
class FxtrScopeState {
  // Identity & hierarchy
  readonly name: string | null;
  readonly parents: readonly FxtrScopeState[];
  readonly allAncestorIds: ReadonlySet<number>;  // for includedInScope checks
  readonly isRootScope: boolean;

  // Arena layer
  readonly store: MemoryStore;
  readonly mainArena: MemoryArena;
  readonly contextMarker: FxtrID;  // the "." builtin

  // Lifecycle
  private started: boolean;
  private shutdownComplete: boolean;
  private frozen: boolean;
  private keepAlive: KeepAlive;

  // Caching
  private refLoadedValueCache: Map<string, Map<EntityLoadCodec<any>, RefLoadCacheEntry<any>>>;
  // Outer key: entityId.key(), inner key: codec instance (by reference identity)
  private refsLoadedSinceAbandon: Map<string, Set<EntityLoadCodec<any>>>;
  // Outer key: entityId.key(), inner: codec instances loaded since last abandon

  // Deep fetchability
  private deeplyFetchableFlag: boolean | null;
  readonly deepFetchSources: Set<DeepFetchable>;
}
```

### FxtrScope (user-facing surface)

A lightweight object pointing to a `FxtrScopeState`. Multiple surfaces can share one state. Each surface has an optional load tracing list.

```typescript
class FxtrScope {
  /** @internal */
  readonly _state: FxtrScopeState;
  /** @internal — if set, loads are traced into this list. */
  readonly _loadTrace: Array<RefLoadCacheEntry<any> | RefLoadUncacheableMarker<any>> | null;

  // All user-facing methods delegate to _state, passing _loadTrace where relevant.
}
```

### Why the split?

When `scope.load(ref)` needs to call `ref.loadCodec.decode(cbor, scope)`, it needs to track which child entities the codec loads as side effects. In Python, this is done via a `_load_tracer` context var scoped to the current async context. In TS (no context vars), we create a new `FxtrScope` surface pointing at the same state but with `_loadTrace` set to a fresh list. The codec calls `scope.load(childRef)` on this surface, which records the load into the trace list. After `decode()` returns, the original surface retrieves the trace list from the tracing surface.

### Tracing surface restrictions

A tracing surface (one with `_loadTrace` set) disallows operations that the scope can't replay from cache:

- **Allows**: `load`, `loadCbor`, `loadFrozenCbor`, `isTracked`, `getCachedValue`, `isAlive`, `includedInScope`
- **Disallows** (runtime error): `store`, `storeCbor`, `lift`, `abandon`, `clearCache`, `copyFromScope`, `trackBuiltin`, `freeze`

`lift` is disallowed because it has side effects (attaching nodes from parent arenas) that can't be replayed from cache. User code inside a decode codec must not call `lift` directly.

### Internal unchecked lift

The `load()` implementation needs to call `lift()` internally (to check parent caches and ensure the ID is tracked). This works via an internal method on `FxtrScopeState` that bypasses surface-level checks. The tracing surface's `load()` uses this internal path, not the user-facing `lift()` method.

## withFxtrScope

The only way to create a scope (other than the global root scope).

```typescript
interface FxtrScopeOptions {
  /** Human-readable name for debugging. */
  name?: string;
  /** Parent scopes to include. Entities in parent scopes are accessible. */
  include?: FxtrScope[];
}

async function withFxtrScope<T>(
  opts: FxtrScopeOptions,
  callback: (scope: FxtrScope) => Promise<T>,
): Promise<T>
```

### Lifecycle within withFxtrScope

1. Create `FxtrScopeState` with the given options
2. Validate and connect to parent scopes (wait for parents to be started, register keep-alive)
3. Compute deep fetchability from parents
4. Create the primary `FxtrScope` surface (no load tracing)
5. Call `callback(scope)`
6. On callback completion (or error):
   - Signal shutdown requested
   - Wait for keep-alive holders (child scopes) to close
   - Close the main arena
   - Close the store (if no remaining arenas)
7. Return/throw the callback's result/error

### No isolated flag

All scopes are effectively isolated (no context vars). The `include` option serves the same purpose as Python's `include` parameter — it specifies parent scopes. There is no implicit inheritance from an "active scope."

## FxtrScope method surface

### Store operations (async, mutating)

```typescript
class FxtrScope {
  /** Store an entity that carries [FXTR_CODEC]. */
  async store<T>(entity: Storable<T>): Promise<EntityRef<T>>;

  /** Store raw CBOR data. Lifts all child IDs first. Returns content-addressed ID. */
  async storeCbor(cbor: FxtrCbor): Promise<FxtrID>;
}
```

Both check alive, check not frozen, and check not a tracing surface.

`storeCbor` calls `visitIds(cbor, ...)` to lift all referenced children into this scope before calling `arena.store(cbor)`.

`store` reads `entity[FXTR_CODEC]` and delegates to `codec.store(entity, this)`.

### Load operations (async, allowed on tracing surfaces)

```typescript
class FxtrScope {
  /** Load an EntityRef, using the ref's load codec. Returns the decoded entity. */
  async load<T>(ref: EntityRef<T>): Promise<T>;

  /** Load raw mutable CBOR for an entity. */
  async loadCbor(nodeId: FxtrID): Promise<FxtrCbor>;

  /** Load raw frozen CBOR for an entity. Cached. */
  async loadFrozenCbor(nodeId: FxtrID): Promise<FrozenFxtrCbor>;
}
```

`loadCbor` creates `EntityRef(nodeId, unknownMutableLoadCodec)` and calls `this.load()`.
`loadFrozenCbor` creates `EntityRef(nodeId, unknownLoadCodec)` and calls `this.load()`.

### load() implementation detail

Mirrors Python's `FxtrScope.load()`:

1. Check cache for this ref (by entity ID + load codec)
2. If not cached, `lift(ref)` to check parent caches and track the ID
3. If cached: replay side effects via `_loadForSideEffects()`, log to trace if tracing, return cached value
4. If not cached:
   a. Load CBOR from arena
   b. Extract `$fxtr.type` field
   c. Call `cachedSupportsEntityType(ref.loadCodec, typeId, this)` — this handles caching and creates a fresh child scope internally to isolate any loads the type check triggers. For `unionLoadCodec`, this dispatches to branches.
   d. Create a tracing surface, call `ref.loadCodec.decode(cbor, tracingSurface)`
   e. Retrieve trace list from tracing surface → `sideEffectLoads`
   f. If `result.cacheable`: create `RefLoadCacheEntry(ref, value, sideEffectLoads)`, store in cache
   g. If tracing: log the cache entry (or uncacheable marker) to trace
   h. Return `result.value`

**Note — intentional deviation from Python (union-concrete caching):** Python's `load()` caches results under both the union type annotation and the resolved concrete subtype, so that loading `EntityRef(id, A | B)` also populates the cache for `EntityRef(id, A)`. In TS, since union dispatch lives inside `unionLoadCodec.decode()` rather than in the scope, the scope only sees the union codec and caches under it alone. This means loading with a union codec and then loading the same entity with the specific branch codec will miss the cache. This is an intentional simplification; union-aware cache sharing could be added in the future if profiling shows it matters.

### Tracking operations (sync where possible, mutating)

```typescript
class FxtrScope {
  /**
   * Ensure a node is tracked in this scope (must be in a parent).
   * Copies cache entries from parent scopes. Sync.
   */
  lift(node: FxtrID | EntityRef<any>): typeof node;

  /** Track a builtin ("." or "!") node. Sync. */
  trackBuiltin(builtinId: FxtrID): FxtrID;
}
```

`lift` walks parent scopes (BFS) to find the node, then calls `mainArena.unsafeLazilyAttachFromArena()`. Also copies cache entries from the parent where the node was found.

### Copy operations (async, mutating)

```typescript
class FxtrScope {
  /**
   * Copy an entity (and transitive deps) from another scope into this one.
   * Also copies cache entries.
   */
  async copyFromScope(node: FxtrID | EntityRef<any>, sourceScope: FxtrScope): Promise<typeof node>;
}
```

Mirrors Python: lifts the node in the source scope first, then calls `mainArena.copyFromArena()`. Copies cache entries.

### Abandon (sync, mutating)

```typescript
class FxtrScope {
  /** Release an entity from this scope. Clears its cache entries. */
  abandon(nodeId: FxtrID | EntityRef<any>): void;
}
```

Mirrors Python: abandons from arena, removes cache entries, clears `_refsLoadedSinceAbandon`.

### Read-only operations (sync)

```typescript
class FxtrScope {
  /** Check if a node is tracked. */
  isTracked(node: FxtrID | EntityRef<any>): boolean;

  /** Get a cached value for a ref, or throw EntityNotLoadedError. */
  getCachedValue<T>(ref: EntityRef<T>): T;

  /** Check if this scope is alive (started and not shut down). */
  isAlive(): boolean;

  /** Check if this scope is included as an ancestor of another scope. */
  includedInScope(scope: FxtrScope): boolean;

  /** Get the context marker ID (always "."). */
  getContextMarkerId(): FxtrID;
}
```

### Cache management (mutating)

```typescript
class FxtrScope {
  /** Clear cached loaded values. Not allowed on frozen scopes. */
  clearCache(includeParents?: boolean): void;
}
```

### Freezing

```typescript
class FxtrScope {
  /** Freeze this scope — no further mutations allowed. */
  freeze(): void;

  /** Whether this scope is frozen. */
  get frozen(): boolean;
}
```

Rules (same as Python):
- Root scopes cannot be frozen
- All parent scopes must already be frozen
- After freezing: `store`, `storeCbor`, `load`, `loadCbor`, `loadFrozenCbor`, `lift`, `copyFromScope`, `abandon`, `clearCache`, `trackBuiltin` all raise
- After freezing: `isTracked`, `getCachedValue`, `isAlive`, `includedInScope`, `frozen` still work
- Idempotent (freezing a frozen scope is a no-op)

### Deep fetch

```typescript
class FxtrScope {
  /**
   * Search all backing stores for an entity by ID.
   * Only callable if scope is deeply fetchable.
   * Returns a temporary arena containing the entity, or null.
   */
  async deepFetch(nodeId: FxtrID): Promise<ReadableArena | null>;

  /** Set of external DeepFetchable sources (e.g., HTTP stores). */
  readonly deepFetchSources: Set<DeepFetchable>;
}
```

Two-phase search (same as Python):
1. `_deepFetchCached`: check this scope's store, then parent stores recursively
2. `_deepFetchScanSources`: check all `deepFetchSources`, adopting found entities into this scope's store; then check parent sources recursively

A scope is deeply fetchable if all its parents are deeply fetchable (or if explicitly marked).

## Load caching system

### Cache entry types

```typescript
interface RefLoadCacheEntry<T> {
  readonly ref: EntityRef<T>;
  readonly value: T;
  readonly sideEffectLoads: ReadonlyArray<RefLoadCacheEntry<any> | RefLoadUncacheableMarker<any>>;
}

interface RefLoadUncacheableMarker<T> {
  readonly ref: EntityRef<T>;
  readonly sideEffectLoads: ReadonlyArray<RefLoadCacheEntry<any> | RefLoadUncacheableMarker<any>>;
}
```

### Cache structure

Two-level map keyed by entity ID string, then by codec object identity:

```
FxtrScopeState._refLoadedValueCache:
  Map<string, Map<EntityLoadCodec<any>, RefLoadCacheEntry<any>>>
       ^              ^
  entityId.key()   codec instance (Map uses === for object keys)
```

Using a regular `Map` (not `WeakMap`) for the inner level because we need to iterate entries during `lift()`, `copyFromScope()`, and `clearCache()`. Codec identity is by object reference (`===`). Codec objects are typically module-level singletons so GC isn't a concern, and caches are cleaned up when scopes close.

### Side effect replay

When returning a cached value, the scope must ensure that all side effects of the original load are still valid. `_loadForSideEffects(cachedLoad)`:

1. **Fast path**: if `cachedLoad.ref` is in `_refsLoadedSinceAbandon`, all side effects are idempotent → return immediately
2. **Slow path**: load the entity from the arena (tracking deps), then recursively replay each `sideEffectLoad`
3. Mark the ref in `_refsLoadedSinceAbandon`

`_refsLoadedSinceAbandon` is cleared whenever `abandon()` is called, forcing the slow path to re-validate.

### getCachedValue (sync)

For sync access to cached values (used by frozen scopes and snapshot patterns):

1. Look up cache entry for the ref
2. If not found: throw `EntityNotLoadedError`
3. **Fast path**: ref in `_refsLoadedSinceAbandon` → return value
4. **Slow path**: recursively check `getCachedValue` for all side effect loads. If any throws, propagate
5. Mark ref in `_refsLoadedSinceAbandon`, return value

## Keep-alive mechanism

Ported from Python. Allows child scopes to keep parent scopes alive.

```typescript
class KeepAlive {
  private shutdownRequested: boolean;
  private holdCount: number;

  /** Register a keep-alive hold. Returns a release function. */
  hold(): () => void;

  /** Request shutdown and wait for all holds to be released. */
  async shutdown(): Promise<void>;
}
```

When `withFxtrScope` enters, it registers a keep-alive hold on each parent scope. When the callback completes, the hold is released. The parent's `withFxtrScope` won't return until all holds are released (i.e., all child scopes have closed).

This handles the case where a scope is smuggled outside its callback (e.g., via setTimeout) — the parent blocks until the smuggled child scope closes.

**Deviation from Python:** Python's `_KeepAlive` also supports an `on_unexpected_close` callback, which is invoked when `anyio` delivers a cancellation exception during `shutdown()`. JS Promises don't have cancellation semantics, so there's no equivalent mechanism for an exception to interrupt `shutdown()`. The `on_unexpected_close` system is omitted. If we later add `AbortSignal` support to `withFxtrScope`, we'd revisit this.

## Global scope

```typescript
/** The global root scope, where type builtins are stored. */
export const GLOBAL_SCOPE: FxtrScope = createRootScope();
```

`createRootScope()` creates a permanently-open scope (never closed, never frozen). It:
1. Creates a `FxtrScopeState` with no parents
2. Marks it as a root scope and deeply fetchable
3. Creates and returns a `FxtrScope` surface

This is the only way to create a scope without `withFxtrScope`. Only used for `GLOBAL_SCOPE`.

## cachedSupportsEntityType

Mirrors Python's `handler_supports_entity_type()` which caches `(handler, type_id) -> bool` results. Lives in `scope.ts` because it needs runtime access to `withFxtrScope`.

```typescript
/**
 * Check if a codec supports a given entity type, with caching.
 *
 * Creates a fresh child scope to isolate any loads the type check triggers.
 * Results are cached per (codec, typeId) pair. The cache uses a WeakMap
 * keyed by codec so that if a codec is GC'd, its cache entries are too.
 */
const _supportsCache = new WeakMap<EntityLoadCodec<any>, Map<string, boolean>>();

export async function cachedSupportsEntityType(
  codec: EntityLoadCodec<any>,
  typeId: FxtrID,
  scope: FxtrScope,
): Promise<boolean> {
  let codecCache = _supportsCache.get(codec);
  if (!codecCache) {
    codecCache = new Map();
    _supportsCache.set(codec, codecCache);
  }
  const key = typeId.key();
  if (codecCache.has(key)) {
    return codecCache.get(key)!;
  }
  // Run the check in a fresh child scope to isolate side effects.
  const result = await withFxtrScope({ include: [scope] }, async (freshScope) => {
    return codec.supportsEntityType(typeId, freshScope);
  });
  codecCache.set(key, result);
  return result;
}
```

Used by:
- `FxtrScope.load()` when checking if the ref's load codec supports the entity type (step 4c)
- `unionLoadCodec` when dispatching to branch codecs

Exported from `scope.ts` so that `unionLoadCodec` (in `entityRef.ts`) can import it. This means `entityRef.ts` has one runtime import from `scope.ts` (`cachedSupportsEntityType`), while `scope.ts` has runtime imports from `entityRef.ts` (`EntityRef`, `FXTR_CODEC`, etc.). This is a circular runtime import, but it's safe because:
- `entityRef.ts` only calls `cachedSupportsEntityType` inside async functions (not at module load time)
- `scope.ts` only uses `entityRef.ts` exports inside class methods (not at module load time, except for `GLOBAL_SCOPE` construction which doesn't involve `cachedSupportsEntityType`)

## What we're NOT porting

- `ACTIVE_SCOPE` context var
- `get_active()` / `get_active_scope()`
- `as_active_scope()` context manager
- `_resolve_scope()` (no implicit scope resolution)
- `deep_load()` — sync access sugar
- `EntityRef.val` — sync cached access
- `outer` property (no implicit outer scope)
- `_ENTITY_LOAD_REGISTRY` global registry
- `EntityTypeAnnotation` type (replaced by `EntityLoadCodec<T>`)

## File mapping

| Contents | File |
|----------|------|
| FxtrScopeState, FxtrScope, withFxtrScope, createRootScope, GLOBAL_SCOPE, cachedSupportsEntityType | `src/scope/scope.ts` |
| KeepAlive | `src/scope/scope.ts` (internal) |
| RefLoadCacheEntry, RefLoadUncacheableMarker, EntityNotLoadedError | `src/scope/scope.ts` |
| EntityRef, EntityStoreCodec, EntityLoadCodec, FXTR_CODEC, etc. | `src/scope/entityRef.ts` |
