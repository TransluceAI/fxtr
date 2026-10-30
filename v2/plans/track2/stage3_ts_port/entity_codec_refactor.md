# Entity Codec Refactor: Entity constraint + FXTR_CACHEABLE

## Goal

Tighten the Entity / EntityLoadCodec / EntityRef type system so that:

1. `Entity` is a non-generic interface with `[FXTR_CODEC]: EntityStoreCodec<this>` and optional `[FXTR_CACHEABLE]`
2. `EntityLoadCodec<T>`, `EntityStoreCodec<T>`, and `EntityRef<T>` all require `T extends Entity`
3. `unknownLoadCodec` / `unknownMutableLoadCodec` return proper `UnknownEntity` / `UnknownMutableEntity` wrapper types (matching Python)
4. `DeserializedEntity<T>` is removed; cacheability is read from `[FXTR_CACHEABLE]` on the returned entity
5. `EntityDefinition<T>` extends `EntityLoadCodec<EntityInstance<T>>` directly (subtyping replaces composition)

This fixes a design gap where load codecs could return arbitrary types that weren't re-storable, and consolidates cacheability signaling into a single mechanism.

## Current state

- `Entity<T>` is a generic interface: `{ [FXTR_CODEC]: EntityStoreCodec<T> }`
- `EntityLoadCodec<T>` has no constraint on `T`; `decode()` returns `Promise<DeserializedEntity<T>>`
- `DeserializedEntity<T>` is a `{ value: T, cacheable: boolean }` wrapper
- `unknownLoadCodec` returns raw `FrozenFxtrCbor`, `unknownMutableLoadCodec` returns raw `FxtrCbor`
- `EntityDefinition<T>` has a `.loadCodec` property; `resolveLoadCodec()` duck-types to extract it
- `FXTR_CACHEABLE` exists but is bicodec-internal (not part of the Entity interface)
- Internal scope types use `EntityRef<unknown>` as a wildcard

## Design

### New `Entity` interface

```typescript
export const FXTR_CACHEABLE: unique symbol = Symbol("fxtrCacheable");

export interface Entity {
    [FXTR_CODEC]: EntityStoreCodec<this>;
    [FXTR_CACHEABLE]?: boolean;
}
```

No type parameter. `EntityStoreCodec<this>` uses TS's polymorphic `this` type, so when you access `entity[FXTR_CODEC]` on a value typed as `T extends Entity`, you get `EntityStoreCodec<T>`.

`[FXTR_CACHEABLE]` is optional. The scope treats absent/undefined as `false` (not cacheable). This is the safe default.

### Bounded generics

```typescript
interface EntityStoreCodec<T extends Entity> { ... }
interface EntityLoadCodec<T extends Entity> { ... }
class EntityRef<T extends Entity> { ... }
```

All internal scope types change from `EntityRef<unknown>` / `EntityLoadCodec<unknown>` to `EntityRef<Entity>` / `EntityLoadCodec<Entity>`.

### `decode()` returns `Promise<T>` directly

```typescript
interface EntityLoadCodec<T extends Entity> {
    supportsEntityType(typeId: FxtrID, scope: FxtrScope): Promise<boolean>;
    decode(cbor: FxtrCbor, scope: FxtrScope): Promise<T>;
}
```

No more `DeserializedEntity<T>` wrapper. After `decode()`, the scope reads `result[FXTR_CACHEABLE]` to decide caching. The `DeserializedEntity` interface is deleted.

### `UnknownEntity` / `UnknownMutableEntity`

New wrapper types, matching Python's `UnknownEntity` / `UnknownMutableEntity`:

```typescript
class UnknownEntity implements Entity {
    readonly raw_cbor: FrozenFxtrCbor;
    readonly [FXTR_CODEC]: EntityStoreCodec<UnknownEntity>;  // re-stores raw_cbor
    readonly [FXTR_CACHEABLE] = true;
}

class UnknownMutableEntity implements Entity {
    readonly raw_cbor: FxtrCbor;
    readonly [FXTR_CODEC]: EntityStoreCodec<UnknownMutableEntity>;  // re-stores raw_cbor
    // FXTR_CACHEABLE absent → defaults to false
}
```

The store codec for each calls `scope.storeCbor(thaw(this.raw_cbor))` or `scope.storeCbor(this.raw_cbor)` respectively, returning an `EntityRef` with the matching unknown load codec. `UnknownEntity` is frozen (Object.freeze). `UnknownMutableEntity` is not.

`unknownLoadCodec` becomes `EntityLoadCodec<UnknownEntity>`, `unknownMutableLoadCodec` becomes `EntityLoadCodec<UnknownMutableEntity>`.

### `loadCbor()` / `loadFrozenCbor()` unwrap

These methods continue to return `FxtrCbor` / `FrozenFxtrCbor` by unwrapping the wrapper:

```typescript
async loadCbor(nodeId: FxtrID): Promise<FxtrCbor> {
    const entity = await this.load(new EntityRef(nodeId, unknownMutableLoadCodec));
    return entity.raw_cbor;
}

async loadFrozenCbor(nodeId: FxtrID): Promise<FrozenFxtrCbor> {
    const entity = await this.load(new EntityRef(nodeId, unknownLoadCodec));
    return entity.raw_cbor;
}
```

Callers see no API change.

### `EntityDefinition extends EntityLoadCodec`

```typescript
export interface EntityDefinition<T> extends EntityLoadCodec<EntityInstance<T>> {
    create(fields: T): EntityInstance<T>;
    readonly typeId: FxtrID | Promise<FxtrID>;
    // supportsEntityType and decode are inherited from EntityLoadCodec
}
```

The `loadCodec` property is removed. The definition *is* the load codec. This means:

- `entityRef(MyDef)` just works via subtyping — `MyDef` is an `EntityLoadCodec<EntityInstance<T>>`
- `resolveLoadCodec()` is deleted
- `entityRef()`, `childRef()`, `child()`, `linked()` lose their `EntityDefinition` overloads; they just accept `EntityLoadCodec<T>`
- Test code changes from `TestEntity.loadCodec` to just `TestEntity`

### `scope.store()` signature

```typescript
async store<T extends Entity>(entity: T): Promise<EntityRef<T>> {
    this._state.checkAlive();
    this._state.checkNotFrozenAndNotTracing("store", this._loadTrace);
    const codec = entity[FXTR_CODEC];
    return codec.store(entity, this);
}
```

No more `Entity<T>` wrapping — just `T extends Entity`.

### `scope.load()` caching logic

Currently reads `deserialized.cacheable`. Changes to:

```typescript
const decoded = await ref.loadCodec.decode(loadedCbor, tracingSurface);

const cacheable = decoded[FXTR_CACHEABLE] === true;  // absent/undefined → false

if (cacheable) {
    // ... cache entry ...
}
return decoded;
```

### `FXTR_CACHEABLE` moves from bicodec to entityRef.ts

Currently `FXTR_CACHEABLE` is defined in `bicodec.ts` as a bicodec-internal symbol. It moves to `entityRef.ts` since it's now part of the core `Entity` interface. Bicodec imports it from there.

### `EntityInstance` type update

```typescript
export type EntityInstance<T> = T & Entity & {
    readonly [FXTR_CTX]?: FxtrID;
    store(scope: FxtrScope): Promise<EntityRef<EntityInstance<T>>>;
};
```

`EntityInstance<T>` intersects with `Entity` (which provides `[FXTR_CODEC]` and `[FXTR_CACHEABLE]`). This is cleaner than listing the symbol properties again.

### Cache/internal type changes

All scope internals that use `EntityRef<unknown>` change to `EntityRef<Entity>`:

- `RefLoadCacheEntry<T>` → `T extends Entity`
- `RefLoadUncacheableMarker<T>` → `T extends Entity`
- Cache maps: `EntityLoadCodec<unknown>` → `EntityLoadCodec<Entity>`
- `EntityRef<unknown>` → `EntityRef<Entity>` throughout scope.ts

Similarly in pyCompatSet.ts: `EntityRef<unknown>` → `EntityRef<Entity>`.

## Files to change

| File | Changes |
|---|---|
| `src/scope/entityRef.ts` | New `Entity` interface (non-generic), `FXTR_CACHEABLE` moved here, `T extends Entity` bounds on all generics, `UnknownEntity`/`UnknownMutableEntity` classes, delete `DeserializedEntity`, update `unknownLoadCodec`/`unknownMutableLoadCodec` |
| `src/scope/scope.ts` | `scope.store()` signature, `scope.load()` caching reads `FXTR_CACHEABLE`, `loadCbor`/`loadFrozenCbor` unwrap, all internal `<unknown>` → `<Entity>` |
| `src/scope/unionLoadCodec.ts` | Remove `DeserializedEntity` import, update `decode()` return type, `<unknown>` → `<Entity>` |
| `src/common/bicodec.ts` | Remove `FXTR_CACHEABLE` definition (import from entityRef), `EntityDefinition extends EntityLoadCodec`, delete `resolveLoadCodec`, collapse overloads on `entityRef`/`childRef`/`child`/`linked`, `entity()` decode returns `T` not `DeserializedEntity<T>`, remove `.loadCodec` from definition |
| `src/common/pyCompatSet.ts` | `EntityRef<unknown>` → `EntityRef<Entity>` |
| `src/common/systemEntities.ts` | `.loadCodec` → direct definition usage |
| `tests/scope/entityRef.test.ts` | Update hand-rolled codecs to return `Entity`-conforming objects, update unknown codec tests for wrapper types |
| `tests/scope/scope.test.ts` | Same: hand-rolled codecs return Entity objects, `DeserializedEntity` → direct return |
| `tests/common/bicodec.test.ts` | `.loadCodec` → direct definition, adjust assertions |
| `tests/common/crossLanguageConsistency.test.ts` | Likely no changes (uses bicodec.entity, not raw codecs) |

## Implementation order

### Step 1: Core type changes in `entityRef.ts`

1. [x] Move `FXTR_CACHEABLE` symbol definition here (remove from bicodec.ts)
2. [x] Redefine `Entity` as non-generic with `[FXTR_CODEC]: EntityStoreCodec<this>` and `[FXTR_CACHEABLE]?: boolean`
3. [x] Add `T extends Entity` bounds to `EntityStoreCodec<T>`, `EntityLoadCodec<T>`, `EntityRef<T>`
4. [x] Change `EntityLoadCodec.decode()` return type from `Promise<DeserializedEntity<T>>` to `Promise<T>`
5. [x] Delete `DeserializedEntity<T>` interface
6. [x] Create `UnknownEntity` class (frozen, `raw_cbor: FrozenFxtrCbor`, `[FXTR_CACHEABLE] = true`, store codec re-stores raw_cbor)
7. [x] Create `UnknownMutableEntity` class (mutable, `raw_cbor: FxtrCbor`, no FXTR_CACHEABLE, store codec re-stores raw_cbor)
8. [x] Update `unknownLoadCodec` to return `UnknownEntity`, `unknownMutableLoadCodec` to return `UnknownMutableEntity`
9. [x] Update `EntityRef.equals()` parameter from `EntityRef<unknown>` to `EntityRef<Entity>`

### Step 2: Scope changes in `scope.ts`

1. [x] Update `scope.store()` signature: `<T extends Entity>(entity: T)` instead of `<T>(entity: Entity<T>)`
2. [x] Update `scope.load()` caching: read `decoded[FXTR_CACHEABLE] === true` instead of `deserialized.cacheable`
3. [x] Update `loadCbor()` / `loadFrozenCbor()` to unwrap `.raw_cbor`
4. [x] Change all `EntityRef<unknown>` to `EntityRef<Entity>`, `EntityLoadCodec<unknown>` to `EntityLoadCodec<Entity>`, `RefLoadCacheEntry<unknown>` / `RefLoadUncacheableMarker<unknown>` similarly
5. [x] Update `cachedSupportsEntityType` parameter type

### Step 3: Union load codec

1. [x] Remove `DeserializedEntity` import
2. [x] Update `decode()` return type
3. [x] Update `<unknown>` bounds to `<Entity>`

### Step 4: Bicodec changes

1. [x] Remove `FXTR_CACHEABLE` definition; import from `entityRef.ts`
2. [x] Make `EntityDefinition<T>` extend `EntityLoadCodec<EntityInstance<T>>`
3. [x] Delete `resolveLoadCodec()`
4. [x] Collapse `entityRef()`, `childRef()`, `child()`, `linked()` to single signatures accepting `EntityLoadCodec<T>`
5. [x] In `entity()` factory: the returned object directly implements `supportsEntityType` and `decode` (no separate `loadCodecImpl`); `decode()` returns `Promise<EntityInstance<T>>` (not wrapped in DeserializedEntity)
6. [x] Remove `.loadCodec` property from definition object
7. [x] Update `EntityInstance<T>` to intersect with `Entity`

### Step 5: Other source files

1. [x] `pyCompatSet.ts`: `EntityRef<unknown>` → `EntityRef<Entity>`
2. [x] `systemEntities.ts`: any `.loadCodec` references → direct definition usage (likely minimal)

### Step 6: Test updates

1. [x] `entityRef.test.ts`: Update hand-rolled codecs to return Entity-conforming objects (add `[FXTR_CODEC]`, optionally `[FXTR_CACHEABLE]`); update unknown codec assertions for wrapper types; remove `DeserializedEntity` imports; `.loadCodec` → definition
2. [x] `scope.test.ts`: Same pattern for hand-rolled codecs; update `loadCbor`/`loadFrozenCbor` assertions if any check raw return type
3. [x] `bicodec.test.ts`: `.loadCodec` → direct definition; update assertions
4. [x] Run full test suite, fix any remaining type errors or test failures

### Step 7: Update docs

1. [x] Update `entity_codec_system.md`: add note at top that this spec is partially superseded by this refactor doc
2. [x] Update `bicodec.md`: add similar note
3. [x] Update `overview.md`: add Phase 5.5 for this refactor
4. [x] Update `ts/CODE_OVERVIEW.md`

## Notes on hand-rolled test codecs

The test files in `entityRef.test.ts` and `scope.test.ts` define ad-hoc load codecs with inline `decode()` that return `DeserializedEntity<T>` where `T` is a plain object like `{ count: number }`. After this refactor, these codecs need to return objects conforming to `Entity`:

```typescript
// Before:
async decode(cbor, scope) {
    return { value: { count: (cbor as any).count }, cacheable: true };
}

// After:
async decode(cbor, scope) {
    const val = { count: (cbor as any).count } as CounterEntity;
    Object.defineProperty(val, FXTR_CODEC, { value: counterStoreCodec });
    (val as any)[FXTR_CACHEABLE] = true;
    return val;
}
```

This is more verbose for tests, but it's the correct behavior — every loaded entity should be re-storable. We could create a small test helper like `makeTestEntity(fields, storeCodec, cacheable)` to reduce boilerplate.
