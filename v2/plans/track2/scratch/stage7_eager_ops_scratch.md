# Eager Ops — Scratch Notes

## Current async points in the load path

### scope.load(ref)
1. Cache check — **sync** (getCacheEntryForRef, liftUnchecked)
2. Side-effect replay — **async** (arena.load + recursive replay)
3. Arena load — **async** (bareLoad → _ensureOwn)
4. cachedSupportsEntityType — **async** (but cached after first call)
5. codec.decode — **async** (field codecs, which may call scope.load recursively)
6. Cache write — **sync**

### _ensureOwn(nodeId) in MemoryStore
- Non-inherited node → **sync** (immediate return)
- Inherited node → **async** (parentArena.loadFlat)

### bareLoad(nodeId) in MemoryStore
- Stored, non-inherited d/n node → sync after _ensureOwn
- Derived p-node → async (recursive bareLoad of unwrapsTo)
- Derived u-node → async (recursive bareLoad of unwrapsTo)

### FieldCodec.fromCbor
- Scalars (string, number, boolean, bytes, unknown) → sync value, wrapped in Promise
- fxtrId, childFxtrId → sync
- entityRef, childRef → sync (just construct EntityRef)
- object → awaits all field codecs
- array → awaits all element codecs
- record → awaits all value codecs
- nullable → awaits inner codec
- mapped → awaits inner codec
- lazy → awaits inner codec
- child/linked → calls scope.load (genuinely async currently)
- pyCompatSet → sync-ish (just wraps array items)

### FieldCodec.toCbor
- Scalars → sync
- child/linked → calls codec.store (SHA-256, genuinely async)
- Composites → awaits children

## Where Eager wins

The biggest win: **cache hit + all side effects already replayed**.
- getCacheEntryForRef is sync
- isRefLoadedSinceAbandon is sync
- If all side effects are marked loaded → entire load is sync → Eager<T>

Second win: **cache miss but all sync path** (stored non-inherited d/n node, all field codecs return Eager).
- bareLoad for stored non-inherited node is sync
- If all field codecs (scalar + entityRef) return Eager, decode is sync
- This means loading a "leaf" entity (no child/linked fields) from a memory arena is fully sync

Third win (biggest for React): **useFxtrLoad with cached entity**.
- Currently: even cache hits go through async load() → Promise → Suspense throw → re-render
- With Eager: cache hit → Eager → unwrap() → return immediately, no Suspense

## Eager.all implementation sketch

```typescript
static all(values: PromiseOrEager<T>[]): PromiseOrEager<T[]> {
  const results: T[] = [];
  for (const v of values) {
    if (v instanceof Eager) {
      if (!v._ok) return Eager.reject(v._error); // fast-fail
      results.push(v._value);
    } else {
      // Hit a Promise. Fall back to Promise.all for remaining.
      // Convert already-resolved Eagers to plain values.
      return Promise.all(
        values.map(v => v instanceof Eager ? v.unwrap() : v)
      );
    }
  }
  return Eager.resolve(results);
}
```

Wait — this has a subtlety. If we call `v.unwrap()` on an error Eager, it throws synchronously inside `Promise.all(values.map(...))`, which means the map itself throws. We'd need to handle that:

```typescript
return Promise.all(
  values.map(v => {
    if (v instanceof Eager) {
      // Convert Eager to value or rejected promise
      return v._ok ? v._value : Promise.reject(v._error);
    }
    return v;
  })
);
```

Actually even simpler: since Eager is a thenable, Promise.all should handle it natively. `Promise.all` calls `.then()` on thenables. So we can just:
```typescript
return Promise.all(values);
```
This works because Promise.all already handles thenables (and Eager.then resolves synchronously, which Promise.all is fine with — it just wraps the result in a microtask).

But wait, that means we lose the sync benefit for the all-Eager case. We still need the fast path:

```typescript
static all(values: PromiseOrEager<T>[]): PromiseOrEager<T[]> {
  const results: T[] = [];
  for (const v of values) {
    if (!(v instanceof Eager)) {
      // Hit a Promise; delegate to Promise.all for all values.
      return Promise.all(values) as Promise<T[]>;
    }
    if (!v._ok) return Eager.reject(v._error);
    results.push(v._value);
  }
  return Eager.resolve(results);
}
```

This is clean. If all Eager → sync. If any Promise → Promise.all handles everything (Eager thenables + Promises).

## Arena interface changes

ReadableArena.load currently returns Promise<FxtrCbor>. We'd change to PromiseOrEager<FxtrCbor>.
MemoryArena.load can return Eager when _ensureOwn is sync (non-inherited node) AND bareLoad is sync (stored node, not p/u derived).

But bareLoad for p/u nodes recursively calls bareLoad... which could itself be sync. So actually the whole chain could be Eager for non-inherited nodes. The only async case is inherited nodes (_ensureOwn needs loadFlat from parent).

_ensureOwn currently returns Promise<StoredNode | DerivedNode>. We'd change to PromiseOrEager. For non-inherited: Eager.resolve(node). For inherited: Promise (from loadFlat).

Then bareLoad chains off _ensureOwn via .then(), and recursively calls bareLoad for p/u nodes, chaining those too. If all sync → Eager. If any inherited → Promise.

## Bicodec rewrite pattern

Can't use async/await (that always yields). Need to use .then() chaining.

Object codec example:
```typescript
fromCbor(cbor, scope): PromiseOrEager<DecodedField<T>> {
  const fieldResults = Object.entries(schema).map(
    ([key, codec]) => codec.fromCbor(cborMap[key], scope)
  );
  return Eager.all(fieldResults).then(decodeds => {
    // build result object from decodeds
    return { value: result, frozen: allFrozen };
  });
}
```

Entity decode:
```typescript
decode(cbor, scope): PromiseOrEager<Instance> {
  // similar to object codec
  const fieldResults = Object.keys(schema).map(
    key => schema[key].fromCbor(cborMap[key], scope)
  );
  return Eager.all(fieldResults).then(decodeds => {
    // build instance
  });
}
```

## cachedSupportsEntityType

Currently creates a child scope and calls codec.supportsEntityType. The result is cached in a Map. On cache hit → Eager.resolve(result). On miss → codec.supportsEntityType(...) which returns PromiseOrEager.

## React hooks changes

useFxtrLoad:
```typescript
function useFxtrLoad<T>(ref: EntityRef<T>): T {
  // existing cache checks...

  const result = scope.load(ref);
  if (result instanceof Eager) {
    // Sync path: store in cache, return immediately
    const value = result.unwrap();
    setLoadCacheEntry(loadCache, entityKey, codec, {
      promise: Promise.resolve(), value, error: undefined
    });
    return value;
  }
  // Async path: throw for Suspense
  const entry = { promise: result.then(...), value: undefined, error: undefined };
  setLoadCacheEntry(loadCache, entityKey, codec, entry);
  throw entry.promise;
}
```

Actually the existing resolveRefFromCache already checks scope.getCachedValue(). The difference is that with Eager, even the *first* load can be sync if the data is in the arena. Currently the first load is always async (scope.load returns Promise).

## toCbor side

Store path: entity() store codec does:
1. getTypeId() → PromiseOrEager<FxtrID> (Eager if already resolved)
2. scope.lift(tid) → sync
3. Each field's toCbor → PromiseOrEager
4. scope.storeCbor → PromiseOrEager (in practice always Promise due to SHA-256)

So toCbor will almost always end up as Promise due to step 4. But the interface should still be PromiseOrEager for consistency and future-proofing.

## Typing Eager.all for tuples

We want:
```typescript
Eager.all([Eager<A>, Eager<B>, Promise<C>]) → Promise<[A, B, C]>
Eager.all([Eager<A>, Eager<B>]) → Eager<[A, B]> (at runtime, but type is PromiseOrEager<[A, B]>)
```

The static type can't distinguish these. Best we can do:
```typescript
static all<T extends readonly PromiseOrEager<any>[]>(
  values: T
): PromiseOrEager<{ -readonly [K in keyof T]: T[K] extends PromiseOrEager<infer U> ? U : never }>
```

This gives tuple-preserving types. The return is always PromiseOrEager.

We could also add overloads for all-Eager case:
```typescript
static all<T extends readonly Eager<any>[]>(values: T): Eager<...>
static all<T extends readonly PromiseOrEager<any>[]>(values: T): PromiseOrEager<...>
```

This would give Eager return type when all inputs are statically known to be Eager. Nice to have but not essential.
