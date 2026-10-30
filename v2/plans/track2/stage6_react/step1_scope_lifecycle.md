# Step 1: Scope Lifecycle API (`createFxtrScope`, `scheduleClose`, `dispose`)

## Goal

Add a non-callback-based scope lifecycle API to `@fxtr/core` so that scopes can be created and cleaned up in contexts where a callback pattern doesn't work — specifically React hooks (`useEffect` cleanup is sync) and JavaScript's `using` declarations.

The existing `withFxtrScope` continues to work and is reimplemented in terms of these new primitives.

## Motivation

`withFxtrScope(opts, callback)` ties scope lifetime to a callback. This works for scripts and request handlers, but not for:

- **React `useEffect`**: cleanup function must be synchronous, can't `await withFxtrScope`'s finally block.
- **`await using`**: needs `Symbol.asyncDispose` on the scope, not a callback wrapper.
- **Long-lived scopes** managed by external systems (e.g., a React component tree that creates/destroys scopes as components mount/unmount).

## Design

### Three-state lifecycle

```
alive → closing → closed
```

| State | Direct operations (store/load/freeze) | Child operations | Arena | Parent holds |
|-------|--------------------------------------|-----------------|-------|-------------|
| alive | allowed | allowed | open | held |
| closing | throw | allowed | open | held |
| closed | throw | throw (children are dead too) | closed | released |

**`alive → closing`** is synchronous (`scheduleClose()`). It immediately blocks new direct operations on the scope but doesn't disturb children that are still using the parent's arena. An async background process waits for all child keep-alive holds to release, then transitions to `closed`.

**`closing → closed`** is async. The `dispose()` method returns a promise that resolves when this transition completes.

### New `FxtrScopeState` field

```typescript
// In FxtrScopeState, alongside existing `started` and `shutdownComplete`:
closeScheduled = false;
```

`checkAlive()` becomes:
```typescript
checkAlive(): void {
    if (!this.started) throw new Error("Scope has not been started!");
    if (this.closeScheduled) throw new Error("Scope is closing!");
    if (this.shutdownComplete) throw new Error("Scope has already been shut down!");
}
```

Children never call `checkAlive()` on their parent — they only check their own state. They interact with the parent arena via `liftUnchecked` and `unsafeLazilyAttachFromArena`, which don't go through `checkAlive()`. So a parent in `closing` state doesn't break children.

### `FxtrScopeState` changes

Add a `_disposePromise` field and a `_parentHoldReleases` field that capture the shutdown machinery currently embedded in `withFxtrScope`:

```typescript
class FxtrScopeState {
    // ... existing fields ...
    closeScheduled = false;
    private _disposePromise: Promise<void> | null = null;
    readonly parentHoldReleases: Array<() => void> = [];

    scheduleClose(): void {
        if (this.closeScheduled || this.shutdownComplete) return; // idempotent
        this.closeScheduled = true;
        // Start async shutdown in background
        this._disposePromise = this._doShutdown();
    }

    async dispose(): Promise<void> {
        if (!this._disposePromise) this.scheduleClose();
        await this._disposePromise;
    }

    private async _doShutdown(): Promise<void> {
        await this.keepAlive.shutdown();
        this.mainArena.close();
        this.shutdownComplete = true;
        if (!this.store.hasOpenArenas()) {
            this.store.close();
        }
        for (const release of this.parentHoldReleases) {
            release();
        }
    }
}
```

### New public API on `FxtrScope`

```typescript
class FxtrScope {
    // ... existing methods ...

    /**
     * Schedule this scope for closure. Sync, safe for React useEffect cleanup.
     *
     * Immediately prevents new direct operations (store/load/freeze) on this
     * scope. Existing child scopes continue to work — they hold keep-alive
     * references and will access the arena through their own state.
     *
     * An async background process waits for all children to close, then
     * performs the actual arena cleanup and releases parent holds.
     *
     * Idempotent — calling multiple times is safe.
     */
    scheduleClose(): void {
        this._state.scheduleClose();
    }

    /**
     * Fully close this scope. Async — waits for all children to close first.
     *
     * Calls scheduleClose() if not already scheduled, then awaits the
     * completion of the shutdown process.
     */
    async dispose(): Promise<void> {
        await this._state.dispose();
    }
}

// Conditionally define Symbol.asyncDispose if the runtime supports it.
// This enables `await using scope = createFxtrScope(...)` in environments
// that support the Explicit Resource Management proposal (Node 22+,
// Chrome 134+). In older environments, use dispose() directly.
if (typeof Symbol.asyncDispose === "symbol") {
    FxtrScope.prototype[Symbol.asyncDispose] = FxtrScope.prototype.dispose;
}
```

### `Symbol.asyncDispose` compatibility

`Symbol.asyncDispose` is part of the TC39 Explicit Resource Management proposal. It's available in Node 22+ and Chrome 134+, but not in Node 18 or older browsers.

**Runtime**: If `Symbol.asyncDispose` doesn't exist, the conditional assignment is skipped. The scope works fine — just can't be used with `await using`. No runtime errors.

**TypeScript**: Add `"ESNext.Disposable"` to the `lib` array in `@fxtr/core`'s tsconfig to get the type definitions for `Symbol.asyncDispose` and `AsyncDisposable`. This is a type-level-only change; it doesn't affect emitted code.

**`dispose()` is always the canonical API**. The symbol method is a convenience for `await using`. The React package uses `scheduleClose()` and `dispose()` directly, so it's unaffected regardless of runtime support.

### `createFxtrScope`

A new exported function that does the setup currently in `withFxtrScope` but returns the scope instead of running a callback:

```typescript
export function createFxtrScope(opts: FxtrScopeOptions): FxtrScope {
    const parentStates = (opts.include ?? []).map((s) => s._state);
    const state = new FxtrScopeState({
        name: opts.name,
        parents: parentStates,
    });

    // Validate parents and register keep-alive holds.
    for (const parent of parentStates) {
        if (!parent.started) {
            throw new Error(`Parent scope ${parent.debugName()} has not been started!`);
        }
        if (parent.shutdownComplete) {
            throw new Error(`Parent scope ${parent.debugName()} has already been shut down!`);
        }
        state.parentHoldReleases.push(parent.keepAlive.hold());
    }

    state.inferDeepFetchability();
    state.started = true;
    return new FxtrScope(state);
}
```

### Reimplement `withFxtrScope`

```typescript
export async function withFxtrScope<T>(
    opts: FxtrScopeOptions,
    callback: (scope: FxtrScope) => Promise<T>,
): Promise<T> {
    const scope = createFxtrScope(opts);
    try {
        return await callback(scope);
    } finally {
        await scope.dispose();
    }
}
```

Uses `dispose()` directly in a try/finally rather than `await using`, so this works in all environments without depending on `Symbol.asyncDispose`. In environments that support it, callers can also use `await using scope = createFxtrScope(opts)` directly.

### `createHttpReadNamespace` (`@fxtr/http-client`)

Same pattern applied to `withHttpReadNamespace`. Returns the namespace directly instead of wrapping a callback:

```typescript
export function createHttpReadNamespace(fetchFn: FetchFn): FxtrReadNamespace {
    const scope = createFxtrScope({ name: "http_read_ns" });
    const deepFetchable = new HttpDeepFetchable(fetchFn);
    scope.deepFetchSources.add(deepFetchable);
    return new HttpFxtrReadNamespace(scope, fetchFn);
}
```

Cleanup is via the namespace's scope: `ns.scope.scheduleClose()` or `await ns.scope.dispose()`.

Reimplement `withHttpReadNamespace` in terms of it:

```typescript
export async function withHttpReadNamespace<R>(
    fetchFn: FetchFn,
    callback: (ns: FxtrReadNamespace) => Promise<R>,
): Promise<R> {
    const ns = createHttpReadNamespace(fetchFn);
    try {
        return await callback(ns);
    } finally {
        await ns.scope.dispose();
    }
}
```

### Edge case: `scheduleClose` on parent while child creates a new grandchild

`KeepAlive.hold()` throws if shutdown has been requested. So after `scheduleClose()`, no *new* children can be created with this scope as a parent. Existing children are unaffected.

This is the desired behavior: once a scope is closing, you shouldn't be creating new children from it.

### Edge case: `scheduleClose` called before any operations

The scope transitions directly to closing. The background `_doShutdown` calls `keepAlive.shutdown()` which resolves immediately (no holds), closes the arena, and releases parent holds. Clean and fast.

## Implementation steps

- [x] Add `closeScheduled`, `_disposePromise`, `parentHoldReleases` fields to `FxtrScopeState`
- [x] Update `checkAlive()` to check `closeScheduled`
- [x] Implement `scheduleClose()` and `_doShutdown()` on `FxtrScopeState`
- [x] Implement `dispose()` on `FxtrScopeState`
- [x] Add `scheduleClose()`, `dispose()` to `FxtrScope`, conditionally define `Symbol.asyncDispose`
- [x] Add `"ESNext.Disposable"` to `@fxtr/core` tsconfig lib array
- [x] Implement `createFxtrScope()` as a new exported function
- [x] Reimplement `withFxtrScope` in terms of `createFxtrScope` + `dispose()`
- [x] Export `createFxtrScope` from `@fxtr/core` public API
- [x] Implement `createHttpReadNamespace()` in `@fxtr/http-client`
- [x] Reimplement `withHttpReadNamespace` in terms of `createHttpReadNamespace`
- [x] Export `createHttpReadNamespace` from `@fxtr/http-client`
- [x] Write tests:
  - `createFxtrScope` basic lifecycle (create, use, dispose)
  - `scheduleClose` blocks direct ops but children continue working
  - `dispose` waits for children
  - `withFxtrScope` still works (regression)
  - Edge: `scheduleClose` idempotent
  - Edge: can't create child of a closing scope
  - (Skipped `await using` test — requires Node 22+ runtime, tested via `dispose()` directly)
- [x] Update CODE_OVERVIEW.md
