# Entity Codec System Spec

> **Note**: Parts of this spec are superseded by [entity_codec_refactor.md](entity_codec_refactor.md), which tightens the Entity/EntityLoadCodec/EntityRef type system. Specifically: `Entity` is now non-generic, `DeserializedEntity` is removed in favor of `FXTR_CACHEABLE`, unknown codecs return wrapper types, and `EntityDefinition` extends `EntityLoadCodec` directly. See that doc for the updated design.

The EntityStoreCodec / EntityLoadCodec / FXTR_CODEC design — the biggest divergence from Python.

## Overview

In Python, entities serialize via `Entity._fxtr_store_impl()` (a method on the entity class) and deserialize via `EntityLoadHandler` looked up from a global registry keyed by type annotation. In TypeScript:

- **Encoding** is via `[FXTR_CODEC]` symbol → `EntityStoreCodec<T>.store(value, scope)`
- **Decoding** is via `EntityLoadCodec<T>` passed explicitly on `EntityRef`
- **No global registry** — the load codec IS the equivalent of the type annotation

This keeps TypeScript's type system happy (load codecs carry `T` at the type level) while preserving the same flexibility as Python (codecs can do arbitrary transformations).

## FXTR_CODEC symbol

```typescript
/** Symbol used to attach a store codec to entity objects. */
export const FXTR_CODEC: unique symbol = Symbol("fxtrCodec");

/** Type helper for objects that carry a store codec. */
export interface Storable<T> {
  [FXTR_CODEC]: EntityStoreCodec<T>;
}
```

Any object with `[FXTR_CODEC]` can be stored via `scope.store()`. The symbol is exported so that bicodec and user code can attach codecs to entity objects.

## EntityStoreCodec

```typescript
interface EntityStoreCodec<T> {
  /**
   * Encode the value, store it into the scope, and return an EntityRef.
   *
   * The codec is responsible for:
   * 1. Encoding the value to FxtrCbor
   * 2. Calling scope.storeCbor(cbor) to store it
   * 3. Returning an EntityRef with an appropriate load codec attached
   *
   * Async because scope.storeCbor() is async (SHA-256).
   * Mirrors Python's Entity._fxtr_store_impl() -> EntityRef[Self].
   */
  store(value: T, scope: FxtrScope): Promise<EntityRef<T>>;
}
```

### How scope.store() works

```typescript
// Inside FxtrScope:
async store<T>(entity: Storable<T>): Promise<EntityRef<T>> {
  this.checkAlive();
  this.checkNotFrozen("store");
  const codec = entity[FXTR_CODEC];
  return codec.store(entity as T, this);
}
```

The scope reads `[FXTR_CODEC]` and delegates. The codec calls `scope.storeCbor()` internally. Note: `scope.store()` does NOT cache the result (same as Python) — the entity is cached on first `load()`.

### entity.store(scope) — not in core

The core system only provides `scope.store(entity)`. There is no `.store()` method on entity objects at the primitives level. Bicodec adds `entity.store(scope)` as a convenience method in Phase 5.

## EntityLoadCodec

```typescript
interface EntityLoadCodec<T> {
  /**
   * Check if this codec can handle a given $fxtr.type ID.
   *
   * Async because some codecs may need to load the type entity to
   * inspect it (e.g., dynamic type objects).
   *
   * The scope is a fresh child scope created by the caller specifically
   * for this check — any loads performed here are isolated and won't
   * pollute the main scope's tracking state. (Mirrors Python, which
   * creates `async with FxtrScope():` for this call.)
   *
   * The result should be deterministic given its inputs and may be cached.
   */
  supportsEntityType(typeId: FxtrID, scope: FxtrScope): Promise<boolean>;

  /**
   * Decode raw CBOR to a rich object.
   *
   * Async to support loading child entities during decode.
   * The scope is a tracing surface — loads through it are tracked as
   * side effects. Stores are disallowed (runtime error).
   *
   * The returned object should have [FXTR_CODEC] attached for round-trip
   * support (so it can be re-stored).
   */
  decode(cbor: FxtrCbor, scope: FxtrScope): Promise<DeserializedEntity<T>>;
}
```

### Scope passed to decode() is a tracing surface

When `FxtrScope.load(ref)` calls `ref.loadCodec.decode(cbor, scope)`, the `scope` argument is a tracing surface — a `FxtrScope` object pointing at the same scope state but with load tracing enabled. This lets the scope track which child entities were loaded as side effects.

The tracing surface:
- **Allows**: `load`, `loadCbor`, `loadFrozenCbor`, `isTracked`, `getCachedValue`, `isAlive`, `includedInScope`
- **Disallows** (runtime error): `store`, `storeCbor`, `abandon`, `clearCache`, `lift`, `copyFromScope`, `trackBuiltin`, `freeze`

The codec doesn't know it's being traced — it just uses normal scope methods.

## DeserializedEntity

```typescript
interface DeserializedEntity<T> {
  /** The deserialized entity value. */
  readonly value: T;

  /**
   * Whether the value is immutable and safe to cache.
   *
   * If true, the scope will cache the value and reuse it for repeated
   * loads of the same entity ref without re-decoding. The cached value
   * will be returned by reference for all subsequent loads — it MUST NOT
   * be mutated after returning. The codec is solely responsible for
   * ensuring this invariant; the scope does not validate immutability.
   * (Python enforces hashability as a proxy for immutability, but TS has
   * no equivalent, so we rely on the codec author's correctness here.)
   *
   * If false, each load() call will re-decode from CBOR.
   */
  readonly cacheable: boolean;
}
```

Mirrors Python's `DeserializedEntity`. The `cacheable` flag controls whether the scope's load cache stores this result.

## EntityRef

```typescript
class EntityRef<T> {
  readonly entityId: FxtrID;
  readonly loadCodec: EntityLoadCodec<T>;

  constructor(entityId: FxtrID, loadCodec: EntityLoadCodec<T>) { ... }

  /** Load the entity from the given scope. Convenience for scope.load(ref). */
  async load(scope: FxtrScope): Promise<T> {
    return scope.load(this);
  }

  /** Create a new ref with the same ID but a different load codec. */
  withLoadCodec<U>(newCodec: EntityLoadCodec<U>): EntityRef<U> {
    return new EntityRef(this.entityId, newCodec);
  }

  /** Copy this entity between scopes. */
  async copy(opts: { from: FxtrScope; to: FxtrScope }): Promise<EntityRef<T>> {
    await opts.to.copyFromScope(this, opts.from);
    return this;
  }

  /** Lift this entity into the given scope (must already be in a parent). */
  lift(scope: FxtrScope): EntityRef<T> {
    scope.lift(this);
    return this;
  }

  /** Value equality: same entity ID and same load codec reference. */
  equals(other: EntityRef<unknown>): boolean { ... }
}
```

### EntityRef equality

Python's `EntityRef.__eq__` compares `entity_id` and `target_type`. In TS, equality checks compare `entityId` (via `.key()`) and `loadCodec` (by object identity / reference equality).

There is no `ref.key()` method — the scope cache uses a two-level structure (`Map<string, WeakMap<EntityLoadCodec, ...>>`) keyed by entity ID string and then codec instance, avoiding the need for a composite string key.

Two refs with the same entity ID but different load codec instances are different refs (they'll decode to different types). This matches Python where `EntityRef(id, Foo)` != `EntityRef(id, Bar)`.

## unknownLoadCodec

Two variants, matching Python's `UnknownEntity` and `UnknownMutableEntity`:

### unknownLoadCodec (frozen)

```typescript
const unknownLoadCodec: EntityLoadCodec<FrozenFxtrCbor> = {
  async supportsEntityType(typeId: FxtrID, scope: FxtrScope): Promise<boolean> {
    return true;  // accepts any type
  },

  async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<DeserializedEntity<FrozenFxtrCbor>> {
    return {
      value: deepFreeze(cbor),
      cacheable: true,  // frozen objects are safe to cache
    };
  },
};
```

Used by `scope.loadFrozenCbor(id)`. Returns deeply frozen CBOR. Cacheable.

### unknownMutableLoadCodec (mutable)

```typescript
const unknownMutableLoadCodec: EntityLoadCodec<FxtrCbor> = {
  async supportsEntityType(typeId: FxtrID, scope: FxtrScope): Promise<boolean> {
    return true;
  },

  async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<DeserializedEntity<FxtrCbor>> {
    return {
      value: cbor,
      cacheable: false,  // mutable, not safe to cache
    };
  },
};
```

Used by `scope.loadCbor(id)`. Returns mutable CBOR. Not cacheable (each call re-decodes).

## cachedSupportsEntityType

See [scope_api.md](scope_api.md) for the `cachedSupportsEntityType` utility. It lives in `scope.ts` (not `entityRef.ts`) because it needs runtime access to `withFxtrScope`. It is used by both `FxtrScope.load()` and `unionLoadCodec`.

## unionLoadCodec

```typescript
function unionLoadCodec<Codecs extends EntityLoadCodec<any>[]>(
  ...codecs: Codecs
): EntityLoadCodec<InferUnion<Codecs>> {
  return {
    async supportsEntityType(typeId: FxtrID, scope: FxtrScope): Promise<boolean> {
      for (const codec of codecs) {
        if (await cachedSupportsEntityType(codec, typeId, scope)) return true;
      }
      return false;
    },

    async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<DeserializedEntity<InferUnion<Codecs>>> {
      // Extract $fxtr.type from the CBOR
      const typeField = (cbor as Record<string, FxtrCbor>)["$fxtr.type"];
      const typeId = /* extract FxtrID from typeField */;

      // Find the matching codec
      const matching: EntityLoadCodec<any>[] = [];
      for (const codec of codecs) {
        if (await cachedSupportsEntityType(codec, typeId, scope)) {
          matching.push(codec);
        }
      }

      if (matching.length === 0) {
        throw new Error(`No codec supports entity type ${typeId}`);
      }
      if (matching.length > 1) {
        throw new Error(`Multiple codecs match entity type ${typeId}`);
      }

      return matching[0].decode(cbor, scope);
    },
  };
}
```

`InferUnion<Codecs>` is a TS type-level helper that extracts the union of all codec value types. The exact typing may need refinement during implementation.

Mirrors Python's union dispatch in `FxtrScope.load()`, where `_ENTITY_LOAD_REGISTRY.find_handler_or_union_handlers` finds handlers for each union branch and then dispatches based on `$fxtr.type`.

## Round-trip binding

The store codec and load codec form a pair that reference each other:

```
Store path:
  entity[FXTR_CODEC].store(entity, scope)
    → codec encodes entity to FxtrCbor
    → codec calls scope.storeCbor(cbor) → FxtrID
    → codec returns EntityRef(id, loadCodec)

Load path:
  scope.load(ref)
    → scope loads CBOR from arena
    → scope calls ref.loadCodec.decode(cbor, tracingScope)
    → codec decodes CBOR to entity object
    → codec attaches [FXTR_CODEC] to the decoded object (for re-storing)
    → scope caches the result if cacheable
    → returns the decoded entity
```

The load codec is responsible for attaching `[FXTR_CODEC]` to decoded objects. This ensures that after loading, the entity can be immediately re-stored without the user needing to know about codecs.

## Comparison with Python

| Aspect | Python | TypeScript |
|--------|--------|-----------|
| Store mechanism | `Entity._fxtr_store_impl()` method on class | `entity[FXTR_CODEC].store(value, scope)` |
| Load mechanism | `EntityLoadHandler` from global registry | `EntityLoadCodec` on `EntityRef` |
| Type dispatch | Registry keyed by type annotation | Explicit codec on ref, `unionLoadCodec` combinator |
| Union resolution | `_ENTITY_LOAD_REGISTRY.find_handler_or_union_handlers` | `unionLoadCodec(codecA, codecB, ...)` |
| Unknown fallback | `UnknownEntity` / `UnknownMutableEntity` classes | `unknownLoadCodec` / `unknownMutableLoadCodec` |
| Type annotation | `EntityTypeAnnotation[T]` (runtime) | `EntityLoadCodec<T>` (carries T at type level) |
| Store return | `EntityRef[Self]` | `Promise<EntityRef<T>>` (async) |
| Scope threading | Implicit via `ACTIVE_SCOPE` context var | Explicit scope parameter |

## File location and circular dependency

All codec/ref types go in `src/scope/entityRef.ts`:
- `FXTR_CODEC` symbol
- `Storable<T>` interface
- `EntityStoreCodec<T>` interface
- `EntityLoadCodec<T>` interface
- `DeserializedEntity<T>` interface
- `EntityRef<T>` class
- `unknownLoadCodec` / `unknownMutableLoadCodec`
- `unionLoadCodec` function

`entityRef.ts` and `scope.ts` have a circular type dependency (codecs reference `FxtrScope`, scope references `EntityRef`). This is resolved via `import type`:

- `entityRef.ts`: `import type { FxtrScope } from "./scope.js"` — type-only, erased at runtime
- `scope.ts`: `import { EntityRef, FXTR_CODEC, ... } from "./entityRef.js"` — real runtime import

This works because `entityRef.ts` only needs `FxtrScope` for type annotations in method signatures. At runtime, `EntityRef.load(scope)` just calls `scope.load(this)` on the passed-in object — no need to import the class itself.

## Future refactor: enforce Entity constraint on EntityLoadCodec<T> + FXTR_CACHEABLE

Currently `EntityLoadCodec<T>` places no constraint on `T`, and `unknownLoadCodec` / `unknownMutableLoadCodec` return raw `FrozenFxtrCbor` / `FxtrCbor` without `[FXTR_CODEC]`. This was an oversight — the original intent was for unknown codecs to also brand their results with `[FXTR_CODEC]`, mirroring Python's `UnknownEntity`.

### Planned changes

1. **`unknownLoadCodec` / `unknownMutableLoadCodec` return branded wrapper objects** — similar to Python's `UnknownEntity`, the result would be an object with `[FXTR_CODEC]` and a `raw_cbor` field holding the raw CBOR value. The store codec would re-store the `raw_cbor` as-is. `scope.loadCbor()` / `scope.loadFrozenCbor()` would unwrap the `raw_cbor` for callers that want the raw value.

2. **Constrain `EntityLoadCodec<T>` so `T` must carry `[FXTR_CODEC]`** — once all load codecs return branded objects, we can enforce this at the type level. This makes the "Entity" in `EntityLoadCodec` meaningful.

3. **Replace `DeserializedEntity<T>` with `FXTR_CACHEABLE` symbol** — since `T` is now always an object, we can attach a `[FXTR_CACHEABLE]: boolean` symbol property to the decoded value instead of wrapping it in `DeserializedEntity`. `scope.load()` reads `[FXTR_CACHEABLE]` from the value to decide whether to cache. This eliminates the separate metadata wrapper and gives a single source of truth for cacheability that works across `scope.load()`, `child()`, `linked()`, and any future consumer.

TODO: Work out the details on how to adjust this to fix up the system so that load codecs properly type themselves as returning Entity, and possibly so that Entity doesn't need to be `T extends Entity<T>` but can just be `T extends Entity`? perhaps via
```
export interface Entity {
    [FXTR_CODEC]: EntityStoreCodec<this>;
    [FXTR_CACHEABLE]?: boolean;
}
```
