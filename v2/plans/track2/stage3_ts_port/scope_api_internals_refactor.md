# FxtrScope Internal API Refactor

## Goal

Separate FxtrScope's public API from its internal implementation details using a symbol-keyed internals pattern. Internal consumers (like `@fxtr/http-server` and `@fxtr/http-client`) opt into internal APIs by importing `FXTR_SCOPE_INTERNALS` from `@fxtr/core/internal`. Public consumers only see stable, documented methods.

## Current Problems

- `_state` is `@internal`-annotated but accessible on every `FxtrScope` instance — tests and internal packages reach through it to access `mainArena`, `store`, `deepFetchSources`, etc.
- `deepFetch()` and `deepFetchSources` are marked `@internal` but are regular public methods/getters on `FxtrScope`.
- `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` is a symbol-keyed method that hides one specific internal operation, but the other internals are exposed without the same protection.
- The inconsistency: one internal op is symbol-gated, others are just `@internal`-tagged public members.

## Design

### Symbol-keyed internals object

A single `FXTR_SCOPE_INTERNALS` symbol, exported from `@fxtr/core/internal`, gates access to all internal scope operations. The symbol indexes a separate `FxtrScopeInternals` object (not `this`) so that internal methods don't appear as regular members on `FxtrScope`.

```ts
// @fxtr/core/internal
export const FXTR_SCOPE_INTERNALS: unique symbol = Symbol("fxtrScopeInternals");

export interface FxtrScopeInternals {
  deepFetch(nodeId: FxtrID): PromiseOrEager<ReadableArena | null>;
  readonly deepFetchSources: Set<DeepFetchable>;
  unsafeAttachFromExternalArena(nodeId: FxtrID, source: ReadableArena): void;
  readonly mainArena: MemoryArena;
  readonly store: MemoryStore;
}
```

```ts
// On FxtrScope
class FxtrScope {
  readonly [FXTR_SCOPE_INTERNALS]: FxtrScopeInternals;
  // ... only public methods ...
}
```

### What moves where

**Stays on FxtrScope (public API):**
- `store()`, `storeCbor()`
- `load()`, `loadCbor()`
- `lift()`, `trackBuiltin()`
- `copyFromScope()`
- `abandon()`
- `isTracked()`, `getCachedValue()`, `isAlive()`, `includedInScope()`, `getContextMarkerId()`
- `clearCache()`
- `freeze()`, `frozen`
- `scheduleClose()`, `dispose()`

**Moves to FxtrScopeInternals:**
- `deepFetch()` — used by `@fxtr/http-server` (3 sites)
- `deepFetchSources` — used by `@fxtr/http-client` (1 site: `.add()`)
- `unsafeAttachFromExternalArena()` — replaces the `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` symbol method, used by `@fxtr/http-client` (2 sites)
- `mainArena` — used by tests for provenance node creation (`createUNode`, `createCNode`, etc.)
- `store` (MemoryStore) — used by one test (`deepFetchSources.add(sourceScope._state.store)`)

**Becomes truly private (currently `@internal`-tagged but publicly accessible):**
- `_state` → `private _state` (or `#state`) — all external access migrated to `[FXTR_SCOPE_INTERNALS]`
- `_loadTrace` → `private _loadTrace` (or `#loadTrace`) — no external access exists today, just not enforced
- `FxtrScopeState` — stays as the internal state class, but no longer reachable from outside scope.ts

### Implementation: FxtrScopeInternalsImpl

A small class that holds a reference to `FxtrScopeState` and delegates to it:

```ts
class FxtrScopeInternalsImpl implements FxtrScopeInternals {
  constructor(private readonly _state: FxtrScopeState) {}

  deepFetch(nodeId: FxtrID) { return this._state.deepFetch(nodeId); }
  get deepFetchSources() { return this._state.deepFetchSources; }
  unsafeAttachFromExternalArena(nodeId: FxtrID, source: ReadableArena) {
    this._state.mainArena.unsafeLazilyAttachFromArena(nodeId, source);
  }
  get mainArena() { return this._state.mainArena; }
  get store() { return this._state.store; }
}
```

One allocation per scope (negligible — scopes aren't created in hot loops).

### Removal of UNSAFE_ATTACH_FROM_EXTERNAL_ARENA symbol

The old `unsafeSymbols.ts` file and its `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` export are deleted. The functionality is subsumed by `FXTR_SCOPE_INTERNALS` — the method becomes `scope[FXTR_SCOPE_INTERNALS].unsafeAttachFromExternalArena(...)`.

### createFxtrScope stays unchanged

`createFxtrScope()` continues to return `FxtrScope`. No separate `createFxtrScopeInternal` needed — the internals are always present on the object, just gated by the symbol import.

## Affected call sites

### `@fxtr/http-server/src/server.ts` (3 sites)
```
// Before:
scope.deepFetch(fxtrId)
// After:
scope[FXTR_SCOPE_INTERNALS].deepFetch(fxtrId)
```
Needs to add `import { FXTR_SCOPE_INTERNALS } from "@fxtr/core/internal"`.

### `@fxtr/http-client/src/client.ts` (3 sites)
```
// Before:
scope[UNSAFE_ATTACH_FROM_EXTERNAL_ARENA](entityId, arena)
scope.deepFetchSources.add(deepFetchable)
// After:
scope[FXTR_SCOPE_INTERNALS].unsafeAttachFromExternalArena(entityId, arena)
scope[FXTR_SCOPE_INTERNALS].deepFetchSources.add(deepFetchable)
```
Changes import from `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` to `FXTR_SCOPE_INTERNALS`.

### Tests accessing `_state`

**`packages/fxtr/tests/scope/scope.test.ts`** (1 site):
- `targetScope.deepFetchSources.add(sourceScope._state.store)` → `targetScope[FXTR_SCOPE_INTERNALS].deepFetchSources.add(sourceScope[FXTR_SCOPE_INTERNALS].store)`

**`packages/fxtr-http-server/tests/http/server.test.ts`** (2 sites):
- `scope._state.mainArena.createUNode(...)` → `scope[FXTR_SCOPE_INTERNALS].mainArena.createUNode(...)`
- `scope._state.mainArena.createCNode(...)` → `scope[FXTR_SCOPE_INTERNALS].mainArena.createCNode(...)`

**`packages/fxtr-http-client/tests/http/client.test.ts`** (~10 sites):
- All `scope._state.mainArena.{unwrapProvenance,cNodeToContextData,pNodeToPathroot,createUNode,...}` → `scope[FXTR_SCOPE_INTERNALS].mainArena.{...}`

### Internal scope.ts usage

Within scope.ts itself, `FxtrScope` methods continue to access `this._state` directly (private field). The `_state` property just stops being externally visible. `createFxtrScope` (a free function) accesses parent states via `FxtrScope._getState(s)` — a static method that can access `private` fields from within the class.

## Implementation Steps

- [x] 1. **Add `FXTR_SCOPE_INTERNALS` symbol and `FxtrScopeInternals` interface** directly in `scope/scope.ts`. Export from `internal.ts`.
- [x] 2. **Create `FxtrScopeInternalsImpl`** in scope.ts. Wire it into the `FxtrScope` constructor.
- [x] 3. **Remove old internal members from FxtrScope's public surface**: delete the `deepFetch()` method, `deepFetchSources` getter, and `[UNSAFE_ATTACH_FROM_EXTERNAL_ARENA]` method from `FxtrScope`. Make `_state` and `_loadTrace` truly private (TypeScript `private`).
- [x] 4. **Update `@fxtr/core/internal` exports**: add `FXTR_SCOPE_INTERNALS` and `FxtrScopeInternals`, remove `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA`.
- [x] 5. **Update `@fxtr/http-server`**: import `FXTR_SCOPE_INTERNALS`, update 3 `deepFetch` call sites.
- [x] 6. **Update `@fxtr/http-client`**: switch from `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` to `FXTR_SCOPE_INTERNALS`, update 3 call sites.
- [x] 7. **Update tests**: migrate all `_state` access and `deepFetchSources` access to use `FXTR_SCOPE_INTERNALS`.
- [x] 8. **Delete `scope/unsafeSymbols.ts`** — the symbol now lives in `scope/scope.ts` and is re-exported via `internal.ts`.
- [x] 9. **Run tests**, typecheck, lint. Fix any issues.
- [x] 10. **Update CODE_OVERVIEW.md** — document the `FXTR_SCOPE_INTERNALS` pattern, update the public API section for FxtrScope, update the internal exports section.
