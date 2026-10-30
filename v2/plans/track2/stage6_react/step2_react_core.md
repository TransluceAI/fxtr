# Step 2: `@fxtr/react` — React Bindings

## Goal

Create a new `@fxtr/react` package providing React hooks and components for loading and displaying fxtr entities. The package should make the common case trivial (load an entity, render it) while allowing advanced use cases (programmatic stores, cache isolation, bridging scopes).

## Design overview

Three layers, from simple to advanced:

1. **`<FxtrProvider>` + `useFxtrLoad(ref)`** — the default. A shared internal scope managed by the provider; hooks load entities via Suspense. Users never touch the scope directly.

2. **`<FxtrScope>`** — optional cache isolation boundary. Creates a child scope of the current internal scope for its subtree. When it unmounts, tracked entities and cache entries are cleaned up. Purely an optimization knob.

3. **`useFxtrScope()`** + **`<FxtrScope include={[...]}>` ** — escape hatch. Get a real mutable child scope for programmatic work (stores, complex async loads). Bridge the results back into the component tree by including the scope in a `<FxtrScope>`.

## Package setup

New workspace package at `packages/fxtr-react/`:

```
packages/fxtr-react/
├── package.json        # @fxtr/react, peerDeps: react, @fxtr/core
├── tsconfig.json
└── src/
    ├── index.ts        # Public exports
    ├── context.ts      # React context + FxtrProvider
    ├── hooks.ts        # useFxtrLoad, useFxtrScope
    └── FxtrScope.ts    # <FxtrScope> component
```

Dependencies:
- **peerDependencies**: `react` (>=18), `@fxtr/core`
- **devDependencies**: `@types/react`, `react`, `react-dom`, `@testing-library/react`, `@fxtr/core`

No dependency on `@fxtr/http-client` — the provider accepts any `FxtrScope`, the HTTP wiring happens in user code.

## API design

### `<FxtrProvider scope={parentScope}>`

Top-level provider. Accepts any `FxtrScope` (frozen or not — doesn't matter). Creates an internal child scope using `createFxtrScope({ include: [parentScope] })`, stores it in context, manages its lifecycle.

```tsx
import { FxtrProvider } from "@fxtr/react";

// In an app using HTTP:
const scope = createFxtrScope({ ... }); // user's own setup
root.render(
  <FxtrProvider scope={scope}>
    <App />
  </FxtrProvider>
);
```

Internally:

```typescript
const InternalScopeContext = createContext<FxtrScope | null>(null);

function FxtrProvider({ scope, children }: { scope: FxtrScope; children: ReactNode }) {
    const internalScopeRef = useRef<FxtrScope | null>(null);
    if (!internalScopeRef.current) {
        internalScopeRef.current = createFxtrScope({ include: [scope] });
    }

    // scheduleClose on unmount.
    useEffect(() => {
        return () => {
            internalScopeRef.current?.scheduleClose();
            internalScopeRef.current = null;
        };
    }, []);

    return (
        <InternalScopeContext.Provider value={internalScopeRef.current}>
            {children}
        </InternalScopeContext.Provider>
    );
}
```

The `scope` prop changing is an edge case — probably best to treat it as a remount (key on scope identity).

### `useFxtrLoad<T>(ref: EntityRef<T>): T`

The primary hook. Loads an entity using the current internal scope, suspending via React Suspense if the data isn't cached yet.

```tsx
function ArticleView({ articleRef }: { articleRef: EntityRef<Article> }) {
    const article = useFxtrLoad(articleRef);
    return <h1>{article.title}</h1>;
}

// Wrap in Suspense at some level above:
<Suspense fallback={<Spinner />}>
    <ArticleView articleRef={ref} />
</Suspense>
```

#### Memoization strategy

Each `useFxtrLoad` hook instance memoizes its loaded value in a `useRef`. This means:

- **Same hook, same object**: the resolved value is stable across re-renders. The hook always returns the same object reference until the ref prop changes.
- **Different hooks, independent objects**: two components loading the same non-cacheable entity each get their own `load()` call and their own decoded object. No mutable state is shared between hooks.
- **Cacheable entities still fast**: `getCachedValue` is checked before starting any load, so cacheable entities that were loaded elsewhere (e.g., as a side effect of loading a parent entity) are returned immediately without a new `load()` call.
- **No shared suspense cache**: there is no provider-level or context-level promise/value cache. Each hook manages its own state via `useRef`, so `FxtrProvider` and `<FxtrScope>` only need to provide the internal scope in context.

#### Implementation sketch

```typescript
interface HookCacheEntry<T> {
    key: string;            // entityId.key() + codec identity
    promise: Promise<void>; // the in-flight load (no value stored on promise)
    value?: T;              // set when load resolves
    error?: unknown;        // set when load rejects
}

function useFxtrLoad<T extends Entity>(ref: EntityRef<T>): T {
    const scope = useContext(InternalScopeContext);
    if (!scope) {
        throw new Error("useFxtrLoad must be used within a <FxtrProvider>");
    }

    const cacheRef = useRef<HookCacheEntry<T> | null>(null);
    const key = makeCacheKey(ref);

    // Invalidate if ref changed (parent passed a different entity)
    if (cacheRef.current && cacheRef.current.key !== key) {
        cacheRef.current = null;
    }

    // Return memoized value or re-throw pending/error state
    if (cacheRef.current) {
        if (cacheRef.current.error) throw cacheRef.current.error;
        if (cacheRef.current.value !== undefined) return cacheRef.current.value;
        throw cacheRef.current.promise; // still pending — suspend
    }

    // Fast path: scope already has it cached (cacheable entity, loaded elsewhere)
    try {
        const cached = scope.getCachedValue(ref);
        cacheRef.current = { key, promise: Promise.resolve(), value: cached };
        return cached;
    } catch (e) {
        if (!(e instanceof EntityNotLoadedError)) throw e;
    }

    // Start load — value memoized in this hook instance's ref
    const entry: HookCacheEntry<T> = {
        key,
        promise: scope.load(ref).then(
            (value) => { entry.value = value; },
            (error) => { entry.error = error; },
        ),
    };
    cacheRef.current = entry;
    throw entry.promise;
}
```

#### Cache key

Needs to uniquely identify a ref: `ref.entityId.key()` for the entity ID, plus codec identity. Since codecs are compared by reference, we use a `WeakMap<EntityLoadCodec, number>` to assign stable numeric IDs, then key as `${entityId.key()}:${codecId}`.

### `<FxtrScope>`

Optional component that creates a child scope for its subtree. Two purposes:

1. **Cache isolation** — entities loaded within this subtree are tracked in a child scope. When the component unmounts, the child scope closes and its entities are released.
2. **Scope bridging** — via the `include` prop, pull in entities from external scopes (obtained via `useFxtrScope`).

```tsx
// Simple: cache isolation
<FxtrScope>
    <HeavyDataView />  {/* loads cleaned up on unmount */}
</FxtrScope>

// Advanced: bridge a programmatic scope
const myScope = useFxtrScope();
// ... store entities into myScope ...
<FxtrScope include={[myScope]}>
    <ChildThatLoadsFromMyScope />
</FxtrScope>
```

Implementation: creates a child scope via `createFxtrScope`, provides it via context. When `include` changes, the old scope is closed and a new one is created. Cleanup via `scheduleClose` in `useEffect` return.

```typescript
function FxtrScope({ include, children }: {
    include?: FxtrScope[];
    children: ReactNode;
}) {
    const parentScope = useContext(InternalScopeContext);
    if (!parentScope) {
        throw new Error("<FxtrScope> must be used within a <FxtrProvider>");
    }

    // Build the full parent list: always include the context scope,
    // plus any explicitly provided scopes.
    const allParents = [parentScope, ...(include ?? [])];

    // Recreate the child scope whenever the parent set changes.
    // useMemo compares the include array contents (by reference identity
    // of each scope), so a new scope is only created when the actual
    // set of parents changes.
    const childScope = useMemo(() => {
        return createFxtrScope({ include: allParents });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [parentScope, ...(include ?? [])]);

    useEffect(() => {
        return () => childScope.scheduleClose();
    }, [childScope]);

    return (
        <InternalScopeContext.Provider value={childScope}>
            {children}
        </InternalScopeContext.Provider>
    );
}
```

When `include` changes (e.g., because a new scope was created via `useFxtrScope`), the `useMemo` dependency list changes, triggering a new child scope. The previous child scope is cleaned up via the `useEffect` cleanup. Since scope parents are fixed at creation time, recreating is the only way to change the parent set.

Note: spreading `include` into the `useMemo` deps means each scope in the array is compared by reference identity. Adding/removing a scope or passing a different scope instance triggers recreation. Passing the same scope instances in the same order does not.

### `useFxtrScope(): FxtrScope`

Returns a fresh mutable child scope of the current internal scope. The scope is created on mount and `scheduleClose`d on unmount.

```tsx
function MyComponent() {
    const scope = useFxtrScope();

    useEffect(() => {
        (async () => {
            const ref = await scope.store(Article.create({ ... }));
            setArticleRef(ref);
        })();
    }, [scope]);

    // ...
}
```

Implementation:

```typescript
function useFxtrScope(): FxtrScope {
    const parentScope = useContext(InternalScopeContext);
    if (!parentScope) {
        throw new Error("useFxtrScope must be used within a <FxtrProvider>");
    }

    const scopeRef = useRef<FxtrScope | null>(null);
    if (!scopeRef.current) {
        scopeRef.current = createFxtrScope({ include: [parentScope] });
    }

    useEffect(() => {
        return () => {
            scopeRef.current?.scheduleClose();
            scopeRef.current = null;
        };
    }, []);

    return scopeRef.current;
}
```

Note: React strict mode will call the effect cleanup and re-run the effect. The `useRef` + lazy init pattern handles this — the scope is created synchronously in the ref initializer (not in the effect), so it's available immediately. The cleanup schedules close, but in strict mode React re-mounts immediately, and the ref initializer creates a fresh scope. The old scope's `scheduleClose` runs its async shutdown in the background.

## Suspense integration details

### Promise stability

React Suspense requires that the same Promise object is thrown on each re-render until it resolves. The `useRef`-based hook cache ensures this — once a load is initiated, the same `HookCacheEntry` (and its `promise`) is returned on subsequent renders until resolved or the ref changes.

### Concurrent rendering

React 18 concurrent features may render a component, discard the render, and re-render. Loads initiated during a discarded render will complete in the background and populate the scope cache, which is fine — the next render attempt will find the value cached (for cacheable entities) or the hook's ref will have the resolved value (for non-cacheable entities, if the same component instance re-renders).

### Error handling

If `scope.load(ref)` rejects, the error is stored in the hook's `HookCacheEntry`. On next render, it's re-thrown, which React catches at the nearest Error Boundary. This matches the standard Suspense error handling pattern.

### Multiple `useFxtrLoad` calls in one component

Sequential hooks work naturally:

```tsx
function ArticleFull({ articleRef }) {
    const article = useFxtrLoad(articleRef);  // suspends on first render
    const author = useFxtrLoad(article.author); // reached on second render, may suspend
    // Both cached by third render — full render completes
}
```

Each suspension throws, React retries from the top of the component, earlier hooks hit their memoized value and return synchronously, the next uncached hook suspends. This converges after N renders for N sequential loads.

## Implementation steps

- [x] Create `packages/fxtr-react/` package scaffold (package.json, tsconfig.json)
- [x] Add to pnpm workspace
- [x] Implement `context.tsx`: `InternalScopeContext`, `FxtrProvider`
- [x] Implement `hooks.ts`: `useFxtrLoad` (with `useRef`-based memoization), `useFxtrScope`
- [x] Implement `FxtrSubscope.tsx`: `<FxtrSubscope>` component (renamed from `<FxtrScope>` to avoid name collision with `@fxtr/core`'s `FxtrScope` class)
- [x] Implement `index.ts`: exports
- [x] Write tests:
  - `FxtrProvider` renders children
  - `useFxtrLoad` returns cached value synchronously
  - `useFxtrLoad` suspends and resolves for uncached entity
  - `useFxtrLoad` sequential loads in one component
  - `useFxtrLoad` parent→child cascading loads via Suspense
  - `<FxtrSubscope>` children load through boundary
  - `<FxtrSubscope include={[...]}>` bridges external scope
  - `useFxtrScope` returns mutable child scope
  - (Deferred: stable object ref, ref invalidation, non-cacheable entity, error boundary — tricky to test with React testing library's Suspense/act interaction)
- [x] Update root `tsconfig.json` project references
- [x] Update CODE_OVERVIEW.md

---

## Step 2b: Callback form + `useFxtrLoadQuery`

### Motivation

`useFxtrLoad` currently only accepts a single `EntityRef` and requires React Suspense. This is limiting in two ways:

1. **No way to compose loads** — loading an article and then its author requires two sequential suspensions (two render cycles). Users can't do parallel loads or derive a value from multiple entities in a single logical operation.
2. **Suspense is rigid** — some components need fine-grained control over loading/error states without an `<ErrorBoundary>` + `<Suspense>` wrapper. The `{ data, loading, error }` pattern is standard in hooks like React Query/SWR and gives more flexibility.

### API design

#### Types

```typescript
/** The load function passed into callbacks. Equivalent to scope.load(). */
type LoadFn = <T extends Entity>(ref: EntityRef<T>) => Promise<T>;

/** Return type of useFxtrLoadQuery. */
type LoadStatus<T> = {
    data: T | undefined;
    loading: boolean;
    error: unknown;
};
```

#### `useFxtrLoad` — ref only (no callback form)

```typescript
function useFxtrLoad<T extends Entity>(ref: EntityRef<T>): T;
```

The callback form was originally planned as an overload of `useFxtrLoad`, but React Suspense requires the promise cache to be external to component state. Since the callback form relies on `useMemo` to produce a stable function identity (which is component-local state), the memoized key doesn't survive Suspense re-renders — each retry creates a new function, causing an infinite suspend loop. The callback form is therefore only available via `useFxtrLoadQuery`, which doesn't throw promises.

#### `useFxtrLoadQuery` — overloaded

```typescript
// Ref form: load a single ref, return status object
function useFxtrLoadQuery<T extends Entity>(
    ref: EntityRef<T>,
    opts?: { enabled?: boolean },
): LoadStatus<T>;

// Callback form: run an async callback, return status object
function useFxtrLoadQuery<R>(
    fn: (load: LoadFn) => Promise<R>,
    deps: DependencyList,
    opts?: { enabled?: boolean },
): LoadStatus<R>;
```

Usage:

```tsx
// Conditional loading
const { data: article, loading, error } = useFxtrLoadQuery(articleRef, {
    enabled: !!articleRef,
});

if (loading) return <Spinner />;
if (error) return <ErrorDisplay error={error} />;
return <ArticleView article={article!} />;

// Callback form with status
const { data, loading } = useFxtrLoadQuery(async (load) => {
    const article = await load(articleRef);
    const author = await load(article.authorRef);
    return { article, author };
}, [articleRef], { enabled: articleRef != null });
```

### Caching strategy

#### Ref form (both hooks)

No change from current implementation. Uses the existing provider-level `LoadCache` (keyed by `entityId.key()` + codec identity). Enforces `FXTR_CACHEABLE` — throws if the loaded entity isn't cacheable, same as today.

#### Callback form (`useFxtrLoadQuery` only)

New provider-level `WeakMap<Function, CallbackCacheEntry>`, stored alongside the existing `LoadCache` in context.

The key insight: `useMemo(() => fn, deps)` produces a stable function reference when deps haven't changed. This memoized function identity becomes the WeakMap key. This works for `useFxtrLoadQuery` because it never throws promises — `useMemo` state survives normal re-renders. (It would NOT work for Suspense, where thrown promises cause React to discard uncommitted hook state.)

```typescript
interface CallbackCacheEntry {
    promise: Promise<void>;
    value?: unknown;
    error?: unknown;
}
```

Flow:
1. `useMemo(() => fn, deps)` → stable `memoizedFn`
2. Check `callbackCache.get(memoizedFn)` → hit? Return/throw/suspend as appropriate
3. Miss? Execute `memoizedFn(scope.load.bind(scope))`, store the entry in the WeakMap
4. When deps change → new `memoizedFn` identity → cache miss → new load
5. Old entries are GC'd by the WeakMap when the old function reference is collected

No cacheability enforcement for the callback form. The user manages mutability of whatever they return. Since each component instance gets its own memoized function identity, there's no accidental cross-component sharing.

#### Context consolidation

Replace the separate `InternalScopeContext` and `LoadCacheContext` with a single context holding everything:

```typescript
interface FxtrContextValue {
    scope: FxtrScope;
    loadCache: LoadCache;
    callbackCache: CallbackCache;
}

export type CallbackCache = WeakMap<Function, CallbackCacheEntry>;

export const FxtrContext = createContext<FxtrContextValue | null>(null);
```

`FxtrProvider` and `FxtrSubscope` both create a fresh `FxtrContextValue` (with new `LoadCache` and `CallbackCache` instances). The existing `ScopeAndCacheProvider` becomes a single `<FxtrContext.Provider value={...}>` wrapper.

### `useFxtrLoadQuery` implementation sketch

Unlike the Suspense hooks, `useFxtrLoadQuery` never throws. It uses `useReducer` to trigger re-renders when the promise resolves, and a ref to track which promise it has already subscribed to (avoiding duplicate `.then()` attachments across re-renders).

```typescript
function useFxtrLoadQuery<T extends Entity>(
    ref: EntityRef<T>,
    opts?: { enabled?: boolean },
): LoadStatus<T> {
    const { scope, loadCache } = useFxtrContext();
    const [, rerender] = useReducer((x: number) => x + 1, 0);
    const subscribedRef = useRef<Promise<void> | null>(null);
    const enabled = opts?.enabled ?? true;

    if (!enabled) {
        return { data: undefined, loading: false, error: undefined };
    }

    const entityKey = ref.entityId.key();
    const codec = ref.loadCodec as EntityLoadCodec<Entity>;

    // Check cache (same provider-level cache as useFxtrLoad)
    const existing = getLoadCacheEntry(loadCache, entityKey, codec);
    if (existing) {
        if (existing.error) return { data: undefined, loading: false, error: existing.error };
        if (existing.value !== undefined) return { data: existing.value as T, loading: false, error: undefined };
        // Still pending — subscribe once per promise identity
        if (subscribedRef.current !== existing.promise) {
            subscribedRef.current = existing.promise;
            existing.promise.then(rerender, rerender);
        }
        return { data: undefined, loading: true, error: undefined };
    }

    // Try scope cache fast path (same as useFxtrLoad)...

    // Start load, store in cache, subscribe
    const entry = { promise: scope.load(ref).then(...) };
    setLoadCacheEntry(loadCache, entityKey, codec, entry);
    subscribedRef.current = entry.promise;
    entry.promise.then(rerender, rerender);
    return { data: undefined, loading: true, error: undefined };
}
```

The `subscribedRef` ensures we only attach one `.then(rerender, rerender)` per promise. When deps change (callback form) or the ref changes, the new promise identity differs from the ref, triggering a fresh subscription.

### Implementation steps

- [x] Consolidate context: replace `InternalScopeContext` + `LoadCacheContext` with single `FxtrContext` holding `{ scope, loadCache, callbackCache }` in `context.tsx`
- [x] Add `CallbackCacheEntry` and `CallbackCache` types to `context.tsx`
- [x] Update `FxtrProvider` to create `FxtrContextValue` with all three fields
- [x] Update `FxtrSubscope` to create fresh `FxtrContextValue`
- [x] Update `hooks.ts` to read from unified `FxtrContext` via a `useFxtrContext()` helper
- [x] Add `LoadFn`, `LoadStatus`, `LoadQueryOptions` types to `hooks.ts`
- [x] ~~Implement callback-form overload of `useFxtrLoad`~~ — removed; Suspense can't use component-local memoized keys (see note above)
- [x] Implement `useFxtrLoadQuery` (both ref and callback forms, with `subscribedRef` pattern)
- [x] Export new types and `useFxtrLoadQuery` from `index.ts`
- [x] Write tests:
  - `useFxtrLoadQuery` ref form: returns `{ loading: true }` then `{ data }`
  - `useFxtrLoadQuery` ref form: returns cached value synchronously
  - `useFxtrLoadQuery` ref form: `enabled: false` returns idle state
  - `useFxtrLoadQuery` ref form: `enabled` flip triggers load
  - `useFxtrLoadQuery` callback form: returns status through lifecycle (parallel loads)
  - `useFxtrLoadQuery` callback form: `enabled: false` returns idle
  - `useFxtrLoadQuery` callback form: deps change triggers reload
  - `useFxtrLoadQuery` callback form: stable deps return memoized value
  - `useFxtrLoadQuery` callback form: non-cacheable loads inside callback are allowed
- [x] Update CODE_OVERVIEW.md
