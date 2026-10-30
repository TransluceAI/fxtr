# Pattern: Promise-Only Cache Entries in React

## Problem

When using `PromiseOrEager<T>` in React, operations that resolve synchronously via `Eager` should not produce values that get thrown at React Suspense. React expects thrown values to be `Promise`-like thenables that settle asynchronously. Throwing an already-settled `Eager` (whose `.then()` fires synchronously) can cause re-render loops or other undefined behavior.

## The Pattern

Cache entries that track async operations always use `Promise<void>` for their notification signal — never `Eager`. For operations that resolve synchronously via Eager, use `Promise.resolve()` as the promise. The promise serves as an async notification signal: you only look at it if `value` and `error` are both unset and you need to wait for the result.

```ts
interface CacheEntry {
  /** Async notification signal. Always a real Promise, never Eager. */
  promise: Promise<void>;
  value?: SomeType;
  error?: unknown;
}
```

### Creating entries

```ts
const result = scope.load(ref); // returns PromiseOrEager<T>

if (result instanceof Eager) {
  // Synchronous — settled immediately. Promise.resolve() as signal.
  try {
    entry = { promise: Promise.resolve(), value: result.unwrap() };
  } catch (error) {
    entry = { promise: Promise.resolve(), error };
  }
} else {
  // Asynchronous — .then() callbacks fill in value/error when settled.
  const entry: CacheEntry = {
    promise: result.then(
      (value) => { entry.value = value; },
      (error) => { entry.error = error; },
    ),
  };
}
```

### Consuming entries

Natural check order — no narrowing tricks or casts needed:

```ts
if (entry.error) throw entry.error;           // error boundary
if (entry.value !== undefined) return entry.value;  // settled — use directly
throw entry.promise;                           // pending — React Suspense
```

## Why This Works

- `promise` is always `Promise<void>`, never `Eager` — impossible to throw an Eager at Suspense by construction.
- `Promise.resolve()` is essentially free in modern engines (they cache the void resolved promise).
- A settled `Promise.resolve()` is safe to throw at Suspense — React schedules a microtick re-render. But we never actually throw it because for eagerly-resolved entries, `value` or `error` is always set, so we return/throw before reaching the promise.
- No discriminated unions, no narrowing tricks, no special check order, no casts.

## Where This Applies

- **`@fxtr/react` (`context.tsx`)**: `LoadCacheEntry`, `CallbackCacheEntry`
- **`@fxtr/react` (`hooks.ts`)**: `startRefLoad`, `startCallbackLoad`, `resolveRefFromCache`
- **`@fxtr/react-dispatch` (`context.tsx`)**: `DispatchCacheEntry`
- **`@fxtr/react-dispatch` (`Render.tsx`)**: `EntityLoadState`

The same pattern should be followed anywhere new cache entry types are introduced that interact with React Suspense and `PromiseOrEager`.
