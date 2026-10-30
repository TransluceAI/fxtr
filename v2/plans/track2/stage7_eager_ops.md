# Stage 7: Eager Operations

## Goal

Introduce an `Eager<T>` type that enables synchronous resolution of load/store operations when the needed data is already cached or locally available, while remaining compatible with async operations via the thenable protocol. The primary win is avoiding React Suspense flicker and unnecessary re-renders when entities are already in the scope cache.

## Design

### `Eager<T>`

A synchronous thenable. Always deterministically contains either a resolved value or a rejected error. Key properties:

- **Thenable**: has `.then(onFulfilled, onRejected)` so `await eager` works (though `await` still yields a microtask — the sync benefit requires `.unwrap()` or `.then()` chaining)
- **Synchronous `.then()`**: calls callbacks immediately, not on the microtask queue
- **Return type propagation**: `.then(f)` returns `Eager<U>` if `f` returns `Eager<U>`, `Promise<U>` if `f` returns `Promise<U>`. Static return type is `PromiseOrEager<U>`.
- **Error propagation**: mirrors Promise semantics exactly, just synchronously. Error Eagers skip `onFulfilled` and call `onRejected` (or propagate if no handler). If a callback throws, result is an error `Eager`.
- **`.unwrap()`**: returns value synchronously or throws the error synchronously.
- **`.catch(onRejected)`** and **`.finally(onFinally)`**: convenience methods, expressed in terms of `.then()`.
- **`Eager.resolve(value)`**: mirrors `Promise.resolve` — if given an `Eager`, passes it through; if given a `Promise` or other thenable, returns it as-is (can't adopt synchronously); if given a plain value, wraps in a resolved `Eager`. Return type: `PromiseOrEager<T>`. This means `Eager.resolve` is the universal "wrap in the most appropriate container" function.
- **`Eager.resolveSync(value)`**: always returns `Eager<T>`. Passes through Eagers, wraps plain values. Throws if given a Promise or other thenable — use when you *know* the value must be synchronous.
- **`Eager.reject(error)`**: always returns `Eager<never>`.
- **`Eager.all(values)`**: if all inputs are `Eager`, returns `Eager<T[]>`. If any input is `Promise`, returns `Promise<T[]>` (delegates to `Promise.all`). Fast-fails on first error `Eager`. `Eager.all([])` returns `Eager<[]>`.

### `PromiseOrEager<T>`

Type alias: `Promise<T> | Eager<T>`. Used throughout the codebase as the return type for operations that may or may not resolve synchronously.

### Pattern: replacing `async/await` loops

Since we can't use `async/await` with Eager (it defeats the sync benefit), we need a replacement for the common pattern of "await each item in a list, then combine." `Eager.all()` combined with regular `.map()` covers this:

```pseudo
// Old (always async):
async fromCbor(cbor, scope) {
  for (key in schema) { results[key] = await schema[key].fromCbor(...) }
  return results;
}

// New (sync when possible):
fromCbor(cbor, scope) {
  return Eager.all(keys.map(k => schema[k].fromCbor(...))).then(results => ...)
}
```

## Async Audit

The guiding principle: every function returns `PromiseOrEager<T>` unless it **directly** awaits something inherently external (network I/O, Web Crypto SHA-256). Even those are typed as `PromiseOrEager` for interface consistency — only their current implementations return `Promise`.

### Functions that become `PromiseOrEager` (currently only async because they call other async)

**Arena layer** (`arena/types.ts`, `arena/memoryArena.ts`):
- `ReadableArena.load`, `.loadFlat` — MemoryArena impl can be sync for non-inherited nodes
- `CopyTargetArena.copyFromArena` — MemoryArena impl is sync for same-store copies (`_trackFromStore`), async for cross-store
- `ReadWriteArena.store`, `.createCNode`, `.createPNode`, `.createUNode` — always Promise in practice (SHA-256), but typed PromiseOrEager
- `DeepFetchable.tryLookup` — MemoryStore impl is purely sync (just a Map check + tracking)
- `MemoryStore.bareLoad`, `._ensureOwn`, `.bareStore`, `.tryLookup`
- `MemoryStore._buildTmpArenaCopyingFromExternalArena` — typed PromiseOrEager at interface, stays `async` internally. (Exception to the guiding principle, due to a complicated implementation.)

**Scope** (`scope/scope.ts`):
- `FxtrScope.load`, `.loadCbor`, `.store`, `.storeCbor`
- `FxtrScope._loadForSideEffects`
- `FxtrScope.copyFromScope` — sync for same-store arena copies (the common case when copying between parent/child scopes sharing a MemoryStore)
- `FxtrScope.deepFetch` — `_deepFetchCached` is sync when MemoryStore.tryLookup is sync; `_deepFetchScanSources` is async for HTTP sources but typed PromiseOrEager
- `FxtrScopeState.deepFetch`, `._deepFetchCached`, `._deepFetchScanSources`
- `cachedSupportsEntityType` — sync on cache hit
- `withFxtrScope` — chains callback + dispose

**Entity codecs** (`scope/entityRef.ts`):
- `EntityStoreCodec.store`, `EntityLoadCodec.decode`, `EntityLoadCodec.supportsEntityType`
- `EntityRef.load`, `EntityRef.copy`
- `unknownLoadCodec.decode` — always `Eager` (just wraps frozen CBOR)
- `unknownLoadCodec.supportsEntityType` — always `Eager` (returns true)
- `unknownStoreCodec.store` — delegates to storeCbor

**Union load codec** (`scope/unionLoadCodec.ts`):
- `unionLoadCodec.supportsEntityType`, `.decode`

**Field codecs** (`common/bicodec.ts`, `common/pyCompatSet.ts`):
- `FieldCodec.toCbor`, `FieldCodec.fromCbor` (interface)
- All scalar codecs — always `Eager`
- All composite codecs — `Eager` when all children are `Eager`
- `child`/`linked` codecs — passthrough from `scope.load()`/`codec.store()`
- `entity()` definition: `.decode()`, `.store()`, `.supportsEntityType()`

**Namespace** (`namespace/readNamespace.ts`):
- `FxtrReadNamespace.retrieve`, `.tryRetrieve`, `.listPins` — interface typed PromiseOrEager; HTTP impl returns Promise

**Visitor** (`core/visitor.ts`):
- `asyncVisitAndProjectIdsInCanonicalCborOrder` — callback type becomes `PromiseOrEager`, function returns `PromiseOrEager`. Sync when all callbacks return Eager (used by p-node bareLoad).

**React hooks** (`packages/fxtr-react/src/hooks.ts`):
- `startRefLoad`, `startCallbackLoad` — internal; detect Eager from `scope.load()` result
- `useFxtrLoadQuery` callback form — `load` function returns `PromiseOrEager<T>`

**HTTP client** (`packages/fxtr-http-client/src/client.ts`):
- All methods typed `PromiseOrEager` at the interface level, but always return `Promise` (network I/O)
- `HttpFxtrReadNamespace.retrieve`, `.tryRetrieve`, `.listPins`, `._retrieveImpl`
- `HttpReadableArena.load`, `.loadFlat`, `._fetchData`
- `HttpDeepFetchable.tryLookup`
- `withHttpReadNamespace`

### Functions that become `PromiseOrEager` (lifecycle)

- `KeepAlive.shutdown` — returns immediately (sync) when `_holdCount === 0`, only waits when children are still holding
- `FxtrScopeState._doShutdown` — chains off `keepAlive.shutdown()`; sync when no active holds
- `FxtrScopeState.dispose` — chains off `_doShutdown`
- `FxtrScope.dispose` — chains off state dispose

### Functions that stay `Promise`

- HTTP server route handlers — Hono middleware pattern requires Promise

## Changes by Layer

### 1. New file: `packages/fxtr/src/util/eager.ts`

The `Eager<T>` class and `PromiseOrEager<T>` type alias. Exported from `@fxtr/core`.

Implementation details:
- Two internal states: `{ ok: true, value: T }` or `{ ok: false, error: unknown }`
- `.then()` tries calling the callback synchronously, catches thrown exceptions into error Eagers. If callback returns Eager → return that Eager. If callback returns Promise → return that Promise. If callback returns plain value → wrap in `Eager.resolve()`.
- `.catch(onRejected)` and `.finally(onFinally)` expressed in terms of `.then()`
- `Eager.resolve(value)`: pass through Eager, pass through Promise/thenable, wrap plain values in Eager. Return type `PromiseOrEager<T>`.
- `Eager.resolveSync(value)`: pass through Eager, wrap plain values, throw on Promise/thenable. Return type `Eager<T>`.
- `Eager.all()` iterates values; on first `Promise` instance, falls back to `Promise.all(values)` (works because Eager is thenable and Promise.all handles thenables). On first error Eager, fast-fails. Empty array → `Eager.resolve([])`.
- Tuple-preserving overload on `Eager.all` so that `Eager.all([eagerA, eagerB])` infers `[A, B]`, not `(A|B)[]`.
- Optional: overload where all-`Eager` inputs → `Eager` return type (nice for static analysis but not essential).
- Note: chaining `.then()` on a `PromiseOrEager<T>` works at runtime (both types have `.then()`). TypeScript's inference for `Promise.then` with thenable-returning callbacks may need a small helper for correct typing — we'll address this during implementation if needed.

### 2. Arena interfaces (`arena/types.ts`)

All methods become `PromiseOrEager`:
```
ReadableArena.load:          Promise<FxtrCbor>            →  PromiseOrEager<FxtrCbor>
ReadableArena.loadFlat:      Promise<FxtrEntityFlatData>  →  PromiseOrEager<FxtrEntityFlatData>
CopyTargetArena.copyFromArena: Promise<void>              →  PromiseOrEager<void>
ReadWriteArena.store:        Promise<FxtrID>              →  PromiseOrEager<FxtrID>
ReadWriteArena.createCNode:  Promise<FxtrID>              →  PromiseOrEager<FxtrID>
ReadWriteArena.createPNode:  Promise<FxtrID>              →  PromiseOrEager<FxtrID>
ReadWriteArena.createUNode:  Promise<FxtrID>              →  PromiseOrEager<FxtrID>
DeepFetchable.tryLookup:     Promise<ReadableArena|null>  →  PromiseOrEager<ReadableArena|null>
```

### 3. MemoryArena / MemoryStore (`arena/memoryArena.ts`)

**`_ensureOwn(nodeId)`**: `PromiseOrEager<StoredNode | DerivedNode>`
- Non-inherited node → `Eager.resolve(node)` (the common case)
- Inherited node → remains a `Promise` (needs `parentArena.loadFlat`)

**`bareLoad(nodeId)`**: `PromiseOrEager<FxtrCbor>`
- Chains off `_ensureOwn` via `.then()`.
- For stored d/n nodes: CBOR decode is sync → Eager (when _ensureOwn is Eager).
- For p/u derived nodes: recursively calls `bareLoad` (which itself returns `PromiseOrEager`), chains via `.then()`. Uses the updated `visitAndProjectIdsInCanonicalCborOrder` (see Visitor section) for p-nodes. Fully sync for non-inherited chains.

**`MemoryArena.load(nodeId)`**: `PromiseOrEager<FxtrCbor>` — chains off `bareLoad`, runs `visitIds` tracking in `.then()`.

**`MemoryArena.loadFlat(nodeId)`**: `PromiseOrEager<FxtrEntityFlatData>` — chains off `bareLoadFlat`, which chains off `_ensureOwn`. Sync for non-inherited stored nodes (same pattern as `load`).

**`MemoryStore.bareLoadFlat(nodeId)`**: `PromiseOrEager<FxtrEntityFlatData>` — chains off `_ensureOwn` via `.then()`. After `_ensureOwn`, all work is sync (reading bytes from backing store, collecting deps from node state).

**`MemoryArena.copyFromArena(entityId, source)`**: `PromiseOrEager<void>`
- Same-store fast path (source is MemoryArena with same store): `Eager.resolve(undefined)` after sync `_trackFromStore`.
- Cross-store: chains off `_buildTmpArenaCopyingFromExternalArena` (Promise).

**`MemoryArena.store(entity)`**: `PromiseOrEager<FxtrID>` — chains off `bareStore` (always Promise due to SHA-256).

**`MemoryStore.tryLookup(nodeId)`**: `PromiseOrEager<ReadableArena | null>` — purely sync, returns `Eager`.

**`MemoryStore._buildTmpArenaCopyingFromExternalArena`**: stays `async` internally (worklist loop is not worth converting). Returns `Promise<MemoryArena>`.

No changes needed for `HttpReadableArena` — it always returns `Promise` (network I/O), which satisfies `PromiseOrEager`.

### 4. Entity codec interfaces (`scope/entityRef.ts`)

```
EntityStoreCodec.store:             Promise<EntityRef<T>>  →  PromiseOrEager<EntityRef<T>>
EntityLoadCodec.decode:             Promise<T>             →  PromiseOrEager<T>
EntityLoadCodec.supportsEntityType: Promise<boolean>       →  PromiseOrEager<boolean>
EntityRef.load:                     Promise<T>             →  PromiseOrEager<T>
EntityRef.copy:                     Promise<EntityRef<T>>  →  PromiseOrEager<EntityRef<T>>
```

`UnknownEntity`'s codecs:
- `unknownLoadCodec.decode` → `Eager` (just wraps frozen CBOR)
- `unknownLoadCodec.supportsEntityType` → `Eager` (always returns true)
- `unknownStoreCodec.store` → PromiseOrEager (delegates to storeCbor)

### 5. Field codecs (`common/bicodec.ts`)

```
FieldCodec.toCbor:   Promise<FxtrCbor>        →  PromiseOrEager<FxtrCbor>
FieldCodec.fromCbor: Promise<DecodedField<T>> →  PromiseOrEager<DecodedField<T>>
```

**Scalar codecs** (`string`, `number`, `boolean`, `bytes`, `unknown`, `fxtrId`, `childFxtrId`, `entityRef`, `childRef`): return `Eager` (currently fake-async, just drop the `async`).

**Composite codecs** (`object`, `array`, `record`, `nullable`, `mapped`, `lazy`):
- Rewrite from `async/await` to `.then()` chaining with `Eager.all()`.
- Return `Eager` when all children return `Eager`, `Promise` when any child returns `Promise`.

**Inline child codecs** (`child`, `linked`):
- `fromCbor`: calls `scope.load()` → returns whatever `scope.load()` returns (PromiseOrEager).
- `toCbor`: calls `codec.store()` → PromiseOrEager.

**`pyCompatSet`**: same treatment as composites.

**Entity definition** (`entity()`):
- `decode()`: rewrite field iteration to use `Eager.all()` + `.then()`. Returns Eager when all field decodes are Eager.
- `store()`: rewrite similarly. Will usually return Promise (SHA-256 in storeCbor), but typed as PromiseOrEager.
- `supportsEntityType()`: returns `Eager` when typeId is already resolved. Otherwise chains off the typeId promise.

### 6. Scope (`scope/scope.ts`)

**`load(ref)`**: `PromiseOrEager<T>`
- Cache hit path: replay side effects via `_loadForSideEffects()` (now returns `PromiseOrEager<void>`), then return value. If replay is Eager → return Eager.
- Cache miss path: chain `arena.load()` → `cachedSupportsEntityType()` → `codec.decode()` → cache write. Each step via `.then()`. Returns Eager if entire chain is Eager (e.g., stored non-inherited entity with all-scalar fields).

**`_loadForSideEffects(cached)`**: `PromiseOrEager<void>`
- Fast path (already loaded since abandon): `Eager.resolve(undefined)`
- Slow path: `arena.load()` → recursively replay children. Chain via `.then()` and `Eager.all()` for child side effects.

**`loadCbor(nodeId)`**: `PromiseOrEager<FxtrCbor>` — delegates to `load()`.

**`store(entity)`**: `PromiseOrEager<EntityRef<T>>` — delegates to codec.store.

**`storeCbor(cbor)`**: `PromiseOrEager<FxtrID>` — delegates to arena.store.

**`cachedSupportsEntityType()`**: `PromiseOrEager<boolean>`
- Cache hit → `Eager.resolve(cachedResult)`
- Cache miss → `codec.supportsEntityType()` → `.then(result => { cache; return result; })`

**`copyFromScope(node, sourceScope)`**: `PromiseOrEager<N>`
- Delegates to `arena.copyFromArena()`. Sync for same-store copies (common case: parent/child scopes share a MemoryStore).

**`deepFetch(nodeId)`**: `PromiseOrEager<ReadableArena | null>`
- `_deepFetchCached`: chains `store.tryLookup()` (now PromiseOrEager) + parent recursion. Sync when MemoryStore is the store (tryLookup returns Eager).
- `_deepFetchScanSources`: chains external sources (HTTP → Promise) + parent recursion.

**`dispose()`**: `PromiseOrEager<void>`
- `KeepAlive.shutdown()` becomes `PromiseOrEager<void>`: when `_holdCount === 0`, returns `Eager.resolve(undefined)`.
- `_doShutdown()` chains off `keepAlive.shutdown()` via `.then()`. If shutdown is Eager (no active child holds), the rest (arena close, release parent holds) is sync → entire dispose is Eager.
- `FxtrScopeState._disposePromise` field type becomes `PromiseOrEager<void> | null`.
- `scheduleClose()` stores the result of `_doShutdown()` (may be Eager or Promise).

**`withFxtrScope(opts, callback)`**: `PromiseOrEager<T>` — chains callback + dispose.

### 7. Union load codec (`scope/unionLoadCodec.ts`)

`unionLoadCodec.supportsEntityType`: iterates codecs, calls `cachedSupportsEntityType`. Uses sequential `.then()` chaining (short-circuits on first true). Returns Eager when all cached.

`unionLoadCodec.decode`: similar iteration to find matching codec, then delegates to its `.decode()`. Returns whatever it returns.

### 8. Visitor (`core/visitor.ts`)

**`asyncVisitAndProjectIdsInCanonicalCborOrder`**: rename to `visitAndProjectIdsInCanonicalCborOrderAsync` or keep name but change signature:
- Callback type: `(ref: RawFxtrRef) => PromiseOrEager<FxtrID>`
- Return: `PromiseOrEager<FxtrCbor>`
- Pass 2 (call callbacks) collects `PromiseOrEager<FxtrID>[]`, uses `Eager.all()`. If all callbacks return Eager, the whole function returns Eager.

### 9. Namespace interface (`namespace/readNamespace.ts`)

```
FxtrReadNamespace.retrieve:    Promise<FxtrID | EntityRef<T>>       →  PromiseOrEager<...>
FxtrReadNamespace.tryRetrieve: Promise<FxtrID | EntityRef<T> | null> →  PromiseOrEager<...>
FxtrReadNamespace.listPins:    Promise<ReadonlyMap<string, FxtrID>>  →  PromiseOrEager<...>
```

HTTP implementation always returns Promise (no change to behavior, just to interface types).

### 10. React hooks (`packages/fxtr-react/src/hooks.ts`)

**`useFxtrLoad(ref)`**: After existing cache checks, call `scope.load(ref)`. If result is `instanceof Eager`:
- `.unwrap()` and return immediately (no Suspense throw).
- Store in load cache as resolved entry.

If result is `Promise`: existing Suspense-throw path.

**`useFxtrLoadQuery(ref)`**: Same Eager detection. Eager → return `{ data, loading: false, error: undefined }` immediately. Promise → subscribe and return loading state.

**`useFxtrLoadQuery(callback)`**: The `load` function passed to the callback now returns `PromiseOrEager<T>`. The callback itself can return `PromiseOrEager<T>`. If the callback returns Eager, the hook returns data immediately without going through the loading state.

**`resolveRefFromCache`**: Unchanged (already sync). But less critical since `scope.load()` returning Eager covers more cases.

### 11. HTTP client (`packages/fxtr-http-client/src/client.ts`)

All methods typed `PromiseOrEager` at the interface level. Implementations unchanged — always return Promise (network I/O). Affected methods:
- `HttpReadableArena.load`, `.loadFlat`, `._fetchData`
- `HttpDeepFetchable.tryLookup`
- `HttpFxtrReadNamespace.retrieve`, `.tryRetrieve`, `.listPins`, `._retrieveImpl`
- `withHttpReadNamespace`

### 12. HTTP server (`packages/fxtr-http-server/src/server.ts`)

No changes needed. Hono route handlers stay `async`. They call scope methods that now return `PromiseOrEager`, but `await` works on both.

## Implementation Steps

- [x] **Step 1**: Create `util/eager.ts` with `Eager<T>`, `PromiseOrEager<T>`, `Eager.all`, `.catch`, `.finally`, `.unwrap`. Add thorough unit tests (`util/eager.test.ts`). Export from `@fxtr/core`.
- [x] **Step 2**: Update arena interfaces (`arena/types.ts`) — all methods become `PromiseOrEager`. Update `MemoryArena` and `MemoryStore` implementations (`arena/memoryArena.ts`): `_ensureOwn`, `bareLoad`, `load`, `loadFlat`, `copyFromArena`, `store`, `createCNode/PNode/UNode`, `tryLookup`, `_buildTmpArenaCopyingFromExternalArena`. Update arena tests.
- [x] **Step 3**: Update `core/visitor.ts` — `asyncVisitAndProjectIdsInCanonicalCborOrder` callback and return type become `PromiseOrEager`. Uses `Eager.all` for pass 2. Update visitor tests.
- [x] **Step 4**: Update `EntityStoreCodec`, `EntityLoadCodec`, `EntityRef` interfaces (`scope/entityRef.ts`). Update `UnknownEntity` codecs to return Eager. Update entityRef tests.
- [x] **Step 5**: Update `FieldCodec` interface and all field codec implementations in `common/bicodec.ts`. Scalars → Eager, composites → Eager.all chaining, child/linked → PromiseOrEager passthrough. Update `pyCompatSet.ts`. Update bicodec tests.
- [x] **Step 6**: Update `entity()` definition's `store`, `decode`, and `supportsEntityType` in `bicodec.ts`. Update bicodec entity tests.
- [x] **Step 7**: Update `FxtrScope` methods (`scope/scope.ts`): `load`, `loadCbor`, `store`, `storeCbor`, `_loadForSideEffects`, `copyFromScope`, `deepFetch`/`_deepFetchCached`/`_deepFetchScanSources`, `cachedSupportsEntityType`, `withFxtrScope`. Update `unionLoadCodec.ts`. Update scope tests.
- [x] **Step 8**: Update namespace interface (`namespace/readNamespace.ts`) — `retrieve`, `tryRetrieve`, `listPins` become `PromiseOrEager`.
- [x] **Step 9**: Update `@fxtr/http-client` (`client.ts`) — type annotations on all methods. `withHttpReadNamespace` return type. No behavior changes. Update client tests if needed.
- [x] **Step 10**: Update React hooks (`hooks.ts`): `useFxtrLoad`, `useFxtrLoadQuery` (both ref and callback forms), `startRefLoad`, `startCallbackLoad` to detect Eager and bypass Suspense/loading state. Update react tests.
- [x] **Step 11**: Update `@fxtr/http-server` if any type annotations need updating (likely minimal). Verify server tests still pass.
- [x] **Step 12**: Update CODE_OVERVIEW.md.

## Testing Sync Resolution

Beyond correctness tests (right values come out), we need tests that assert operations resolve synchronously via `instanceof Eager` checks:

- **Eager unit tests**: `.then()` chaining preserves Eager when callbacks return Eager; becomes Promise when callbacks return Promise; error propagation; `Eager.all` with all-Eager, mixed, empty inputs; `Eager.resolve` with plain/Eager/Promise inputs; `Eager.resolveSync` throws on Promise.
- **Arena**: `MemoryArena.load()` returns Eager for non-inherited stored nodes; returns Promise for inherited nodes.
- **Scope**: `scope.load()` returns Eager on cache hit with all side effects replayed; returns Eager on cache miss for in-memory entity with scalar-only fields; returns Promise for inherited nodes or HTTP-sourced entities.
- **Bicodec**: scalar codecs return Eager; object codec with all-scalar fields returns Eager; entity with child fields returns Eager when children are cached.
- **React**: `useFxtrLoad` with cached entity does not throw (no Suspense); `useFxtrLoadQuery` with cached entity returns data immediately.
- **Dispose**: `scope.dispose()` returns Eager when no child holds active.

## Risks / Open Questions

- **`async/await` contagion**: The biggest implementation risk is that `async/await` in consumer code defeats the Eager benefit. This is by design (documented tradeoff), but we should make sure our own internal code uses `.then()` chaining where it matters. Callers who `await` a `PromiseOrEager` still get correct results, just without the sync benefit.
- **Error stack traces**: Synchronous `.then()` chaining produces different stack traces than `async/await`. Errors in Eager chains will have the full synchronous call stack (arguably better for debugging).
- **`Promise.all` on Eager thenables**: When `Eager.all` falls back to `Promise.all`, each Eager's `.then()` fires synchronously inside `Promise.all`'s resolution loop, which is correct behavior — `Promise.all` handles thenables by calling `.then()` on them.
- **Arena.load returning Eager for non-inherited nodes**: This is a significant change to MemoryStore.bareLoad. The recursive p-node/u-node case needs careful testing to make sure the .then() chaining handles all the node state mutation correctly (node states are mutated during bareLoad for derived nodes).
- **Internal vs public priority**: Public APIs should all be typed `PromiseOrEager` even if the internal implementation still uses `async/await` and returns `Promise`. This is especially true for store-side operations (SHA-256) and complex internal methods like `_buildTmpArenaCopyingFromExternalArena`. The priority is making the public surface Eager-aware so callers can benefit; internal conversion can happen incrementally.
- **`withFxtrScope` and dispose**: `withFxtrScope` chains callback + dispose. Since dispose is now `PromiseOrEager`, `withFxtrScope` can return `Eager` when the callback returns Eager and the scope has no child holds (dispose is sync).
