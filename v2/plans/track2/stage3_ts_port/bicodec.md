# Bicodec Spec

> **Note**: Parts of this spec are superseded by [entity_codec_refactor.md](entity_codec_refactor.md). Specifically: `EntityDefinition` now extends `EntityLoadCodec` directly (no `.loadCodec` property), `resolveLoadCodec` is removed, `FXTR_CACHEABLE` moves to the core `Entity` interface, and `decode()` returns the entity directly instead of `DeserializedEntity`. See that doc for details.

Declarative codec builder for defining fxtr entity types in TypeScript — analogous to what mashumaro / DataclassEntity does for Python.

**Design inspiration**: [ts-jsonbinding](https://github.com/adl-lang/ts-jsonbinding) — a bidirectional JSON binding library with a similar `toJson`/`fromJson` symmetry. Bicodec adapts this pattern for FxtrCbor instead of JSON, and adds entity-level concerns (type IDs, scopes, entity refs, inline child entities).

## Overview

Bicodec provides two layers:

1. **Field codecs** (`FieldCodec<T>`) — bidirectional converters between TypeScript values and FxtrCbor fragments. Composable primitives (`string()`, `number()`, `array()`, `entityRef()`, etc.) that can be nested arbitrarily.

2. **Entity definitions** (`EntityDefinition<T>`) — built on top of field codecs. Wraps a schema of field codecs into a full `EntityStoreCodec` + `EntityLoadCodec` pair, adding `$fxtr.type` / `$fxtr.ctx` metadata, `[FXTR_CODEC]` attachment, and a `.create()` factory.

Plus `defineType()` for creating content-addressed type IDs (mirrors Python's `define_type`), and `FxtrBasicType` (the one system entity needed for `defineType` to work). `ContextEntity` and `PathRootEntity` are not ported yet — they depend on `unknown()` / `Any` fields whose design hasn't been validated, and they aren't heavily used in Python currently.

## Design goals

- **Top-level definitions**: Entity definitions and type IDs are defined at module top level, synchronously. Async work (SHA-256, scope ops) is deferred to store/load time.
- **Type inference**: `Infer<typeof MyEntity>` gives you the TypeScript type without writing it separately.
- **Frozen on load**: Loaded entities are deeply frozen and cacheable. Created entities (via `.create()`) are mutable.
- **Round-trip**: Loaded entities carry `[FXTR_CODEC]` and `.store()`, so they can be immediately re-stored.
- **Cross-language consistency**: Entities defined with bicodec in TS must produce byte-identical CBOR to equivalent entities defined with mashumaro/DataclassEntity in Python.
- **Explicit codecs**: No global registry. Entity refs carry their load codec. Union dispatch is via `unionLoadCodec(...)`.

## FieldCodec interface

```typescript
/**
 * Bidirectional converter between a TypeScript value and an FxtrCbor fragment.
 *
 * Inspired by ts-jsonbinding's JsonBinding<T> interface, but targeting
 * FxtrCbor instead of JSON, and async to support entity ref resolution.
 */
interface FieldCodec<T> {
  /** Identifies what kind of codec this is (e.g., "string", "number", "fxtrId",
   *  "entityRef", "array", etc.). Used by pyCompatSet to determine sort order
   *  and key extraction, and useful for debugging/introspection. */
  readonly kind: string;

  /** Encode a TS value to a CBOR-ready value. Async for entity ref stores. */
  toCbor(value: T, scope: FxtrScope): Promise<FxtrCbor>;

  /** Decode a CBOR value to a TS value. Async for entity ref loads. */
  fromCbor(cbor: FxtrCbor, scope: FxtrScope): Promise<DecodedField<T>>;
}

/** Result of decoding a single field. */
interface DecodedField<T> {
  readonly value: T;

  /** Whether this value is *deeply* frozen — the value itself and everything
   *  reachable from it is immutable. The entity codec ANDs all field flags
   *  to determine whether the loaded entity is cacheable. */
  readonly frozen: boolean;
}
```

The scope parameter threads through to child/entityRef codecs that need to do scope operations during encoding/decoding. Primitive codecs ignore it.

### Why async?

All `FieldCodec` methods are async even though most primitives don't need it. This is because:
- Store-side: `child()` / `linked()` codecs call `scope.store()` during `toCbor`, which is async (SHA-256).
- Load-side: `child()` / `linked()` codecs call `scope.load()` during `fromCbor`, which is async (arena I/O, deep fetch).

Making the interface uniformly async avoids a sync/async split across codec types. The overhead for primitives is negligible since they return immediately.

### Immutability propagation

`fromCbor` returns `DecodedField<T>` with a `frozen` flag indicating whether the value is **deeply frozen** — the value itself and everything reachable from it is immutable.

Each codec is responsible for:
1. Locally freezing its own result (e.g., `Object.freeze` on the array/record/object it builds).
2. Setting `frozen: true` only if the local freeze PLUS all nested values' `frozen` flags guarantee deep immutability.

The entity codec ANDs all field `frozen` flags. If all are true, it freezes the top-level entity object and marks it `cacheable: true`. If any are false, it returns `cacheable: false` and does not freeze the entity.

**No deep-freeze at the entity level.** The entity codec trusts that each field codec did its local freezing and reported accurately. This avoids needing `deepFreeze` to handle arbitrary runtime types (Sets, Maps, custom classes, etc.) — each codec knows its own type best.

Frozen propagation rules per codec:

| Codec | Local freeze | `frozen` flag |
|---|---|---|
| `string()`, `number()`, `boolean()` | n/a (primitives) | always `true` |
| `bytes()` | `Object.freeze` on Uint8Array | always `true` |
| `fxtrId()`, `childFxtrId()` | n/a (FxtrID is immutable) | always `true` |
| `entityRef()`, `childRef()` | n/a (EntityRef is immutable) | always `true` |
| `unknown()` | `deepFreeze` (raw CBOR) | always `true` |
| `array(item)` | `Object.freeze` on array | `true` iff all items `frozen` |
| `record(value)` | `Object.freeze` on record | `true` iff all values `frozen` |
| `object(schema)` | `Object.freeze` on object | `true` iff all fields `frozen` |
| `pyCompatSet(item)` | freezes `PyCompatSet` | always `true` (contents are primitives/IDs/refs) |
| `nullable(inner)` | delegates | `true` if null; inner's flag otherwise |
| `child(codec)` / `linked(codec)` | delegates | passes through loaded entity's `cacheable` flag |
| `mapped(inner, ...)` | user's responsibility | `opts.frozen ?? false` |
| `lazy(fn)` | delegates | delegates to resolved codec |

## Field codec primitives

### Scalar types

```typescript
function string(): FieldCodec<string>;
function number(): FieldCodec<number>;
function boolean(): FieldCodec<boolean>;
function bytes(): FieldCodec<Uint8Array>;
```

Scalars pass through directly — these types are already valid FxtrCbor. `bytes()` handles `Uint8Array` ↔ CBOR byte strings.

### Any / unknown passthrough

```typescript
/** Passthrough: any FxtrCbor value, no transformation. */
function unknown(): FieldCodec<FxtrCbor>;
```

Used for fields typed as `Any` in Python (e.g., `ctx_key` and `metadata` on system entities). Passes FxtrCbor through without transformation. On the TS side, users work with raw FxtrCbor values for these fields.

### FxtrID references

```typescript
/** Weak ID reference (CBOR tag 33001). */
function fxtrId(): FieldCodec<FxtrID>;

/** Strong/owning ID reference (CBOR tag 33002). */
function childFxtrId(): FieldCodec<FxtrID>;
```

- `toCbor`: wraps the ID in `RawWeakFxtrID` or `RawStrongFxtrID`
- `fromCbor`: extracts the `FxtrID` from the tagged wrapper

These are raw IDs without type information — just node references in the CBOR graph.

### Entity refs

```typescript
/** Weak entity reference. Serialized as RawWeakFxtrID (tag 33001). */
function entityRef<T>(codec: EntityLoadCodec<T> | EntityDefinition<T>): FieldCodec<EntityRef<T>>;

/** Strong/owning entity reference. Serialized as RawStrongFxtrID (tag 33002). */
function childRef<T>(codec: EntityLoadCodec<T> | EntityDefinition<T>): FieldCodec<EntityRef<T>>;
```

- `toCbor`: extracts `ref.entityId`, wraps in appropriate tag. Does NOT store the referenced entity — it should already be stored.
- `fromCbor`: reads the tagged ID, creates `new EntityRef(id, loadCodec)`. Does NOT load the entity — the caller decides when to load.

Both accept either an `EntityLoadCodec` directly or an `EntityDefinition` (extracts `.loadCodec`). This makes the common case ergonomic: `bicodec.entityRef(MyEntity)`.

### Inline child entities

```typescript
/** Inline entity — strong link, auto-stored on store, auto-loaded on load. */
function child<T>(codec: EntityLoadCodec<T> | EntityDefinition<T>): FieldCodec<T>;

/** Inline entity — weak link, auto-stored on store, auto-loaded on load. */
function linked<T>(codec: EntityLoadCodec<T> | EntityDefinition<T>): FieldCodec<T>;
```

These are the TS equivalent of Python's `Child[T]` and `LinkedEntity[T]`:

- `toCbor`: the value must carry `[FXTR_CODEC]` (i.e., be a storable entity). Calls `scope.store(value)` to store it, then encodes its ID as `RawStrongFxtrID` (child) or `RawWeakFxtrID` (linked).
- `fromCbor`: reads the tagged ID, creates an `EntityRef` with the provided load codec, calls `scope.load(ref)` to load inline. Returns the loaded entity object, not an `EntityRef`.

Accepting a load codec (rather than only an EntityDefinition) allows `child(unionLoadCodec(A.loadCodec, B.loadCodec))` for polymorphic inline children.

At store time, the child entity is stored using its own `[FXTR_CODEC]` (whatever it happens to be). At load time, the provided load codec is used for decoding. The user is responsible for ensuring the provided load codec can decode what the child's store codec produces.

Unlike Python's two-phase collect-then-rebuild pattern, TS codecs are already async, so we simply `await scope.load(ref)` inline during decode. (Future optimization: parallel loading of multiple child fields.)

### Composites

```typescript
/** Fixed-schema nested object (not an entity — no $fxtr.type). */
function object<S extends Record<string, FieldCodec<any>>>(
  schema: S
): FieldCodec<{ [K in keyof S]: InferField<S[K]> }>;

/** Ordered collection. */
function array<T>(itemCodec: FieldCodec<T>): FieldCodec<T[]>;

/** String-keyed map. Serialized as a CBOR map. */
function record<V>(valueCodec: FieldCodec<V>): FieldCodec<Record<string, V>>;

/** Unordered homogeneous collection. Serialized as a sorted array.
 *  Takes an item codec — only string(), number(), bytes(), fxtrId(),
 *  childFxtrId(), entityRef(), or childRef() are allowed. Sort order
 *  matches Python's _canonical_set_sort_key. See pyCompatSet section below. */
function pyCompatSet<T>(itemCodec: FieldCodec<T>): FieldCodec<PyCompatSet<T>>;

/** Nullable field (CBOR null). */
function nullable<T>(codec: FieldCodec<T>): FieldCodec<T | null>;
```

#### `object(schema)`

For nested structured data within an entity (e.g., a metadata block). Not an entity itself — no `$fxtr.type`, no `[FXTR_CODEC]`.

- `toCbor`: encodes each field via its codec, assembles a CBOR map. (Canonical key ordering is handled by `fxtrCborDumps` at the top level, not here.)
- `fromCbor`: decodes each field from the CBOR map via its codec.

#### `pyCompatSet(itemCodec)`

Serialized as a **sorted array** matching Python's `_canonical_set_sort_key` exactly. Takes a homogeneous item codec — sets are always single-typed. The codec uses the item codec's `kind` tag to determine sort order and key extraction:

| Item codec kind | CBOR representation | Sort order |
|---|---|---|
| `"fxtrId"` / `"childFxtrId"` | `RawWeakFxtrID` / `RawStrongFxtrID` | raw bytes |
| `"entityRef"` / `"childRef"` | `RawWeakFxtrID` / `RawStrongFxtrID` | entity ID raw bytes |
| `"string"` | string | lexicographic |
| `"bytes"` | byte string | lexicographic |
| `"number"` | number | numeric |

Only these codec kinds are allowed. Passing any other kind throws an error at codec construction time.

**Runtime representation**: `PyCompatSet<T>` — a custom set-like class backed by a `Map<string, T>`. This is needed because JS `Set` uses reference equality, which doesn't work for `FxtrID` or `EntityRef` objects. See the `PyCompatSet` section below.

- `toCbor`: iterates the set, encodes each item via the item codec's `toCbor`, sorts by the kind-specific comparator, assembles a CBOR array.
- `fromCbor`: decodes array items via the item codec's `fromCbor`, builds a frozen `PyCompatSet`. (Items are assumed already sorted in canonical order from CBOR.)

The `frozen` flag is always `true` — set contents are primitives, FxtrIDs, or EntityRefs, all of which are immutable, and the `PyCompatSet` itself is frozen on load.

### PyCompatSet

`PyCompatSet<T>` is a set-like class backed by `Map<string, T>`. It lives in `src/common/pyCompatSet.ts`.

```typescript
class PyCompatSet<T> {
  private _map: Map<string, T>;
  private _keyFn: (item: T) => string;
  private _frozen: boolean;

  constructor(keyFn: (item: T) => string, items?: Iterable<T>);

  has(item: T): boolean;
  add(item: T): this;         // throws if frozen
  delete(item: T): boolean;   // throws if frozen
  values(): IterableIterator<T>;
  get size(): number;
  [Symbol.iterator](): IterableIterator<T>;

  /** Freeze the set — add/delete throw after this. */
  freeze(): this;
  get frozen(): boolean;
}
```

Key extraction per codec kind:
- `"string"` / `"number"` → `String(value)`
- `"bytes"` → hex encoding of Uint8Array
- `"fxtrId"` / `"childFxtrId"` → `id.key()`
- `"entityRef"` / `"childRef"` → `ref.entityId.key()`

The `pyCompatSet()` codec factory creates the `PyCompatSet` with the appropriate key function based on the item codec's `kind`. For ergonomic construction at call sites, `PyCompatSet` also has a static factory:

```typescript
// The pyCompatSet codec exposes a helper for creating empty/populated sets:
const tagsCodec = bicodec.pyCompatSet(bicodec.string());

// Users construct sets directly:
const tags = new PyCompatSet<string>(stringKeyFn, ["ranked", "season3"]);
// Or more ergonomically, bicodec could expose a createSet helper on the codec.
```

**Note on Python compatibility**: Python is being updated in parallel to also require homogeneous sets with an explicit item type, matching this design.

#### `nullable(codec)`

The field is present in CBOR but its value may be CBOR null.

- `toCbor`: if `null`, returns CBOR `null`. Otherwise delegates to inner codec.
- `fromCbor`: if CBOR `null`, returns `null`. Otherwise delegates.

### Combinators

#### `mapped(inner, toTs, fromTs, opts?)`

```typescript
function mapped<A, B>(
  inner: FieldCodec<A>,
  toTs: (a: A) => B,           // CBOR-side → TS-side (decode direction)
  fromTs: (b: B) => A,         // TS-side → CBOR-side (encode direction)
  opts?: { frozen?: boolean },  // default: false
): FieldCodec<B>;
```

Transform between a CBOR representation type `A` and a TS-side type `B`. Inspired by ts-jsonbinding's `mapped()`.

- `toCbor`: applies `fromTs(value)` to get the CBOR-side value, then delegates to `inner.toCbor`.
- `fromCbor`: delegates to `inner.fromCbor`, then applies `toTs(innerValue)`.
- `frozen`: `opts.frozen ?? false`. If `true`, the user promises that `toTs` always produces a deeply frozen value. If `false`, the entity codec will treat the entity as not cacheable (unless all other fields are also frozen).

This is how you'd define custom runtime types. For example, a UUID class backed by a string:

```typescript
const uuidCodec = bicodec.mapped(
  bicodec.string(),
  (s) => new UUID(s),        // string → UUID on decode
  (u) => u.toString(),       // UUID → string on encode
  { frozen: true },          // UUID instances are immutable
);
```

#### `lazy(fn)`

```typescript
function lazy<T>(fn: () => FieldCodec<T>): FieldCodec<T>;
```

Deferred codec evaluation for recursive types. The factory function is called once on first use and cached.

- `toCbor` / `fromCbor`: delegates to the resolved codec.
- `frozen`: delegates to the resolved codec.

```typescript
// Recursive tree type
const TreeNode: EntityDefinition<...> = bicodec.entity(TREE_TYPE, {
  label: bicodec.string(),
  children: bicodec.array(bicodec.lazy(() => bicodec.child(TreeNode))),
});
```

#### No `optional()` for now

Python's DataclassEntity doesn't have a concept of optional/absent fields — all fields are always present in the CBOR. We skip `optional()` to keep cross-language consistency simple. Can be added later if needed.

## EntityDefinition

```typescript
interface EntityDefinition<T> {
  /** Create an entity instance. Synchronous — type ID is not needed yet. */
  create(fields: T): EntityInstance<T>;

  /** The load codec for deserializing entities of this type. */
  readonly loadCodec: EntityLoadCodec<EntityInstance<T>>;

  /** The type ID. For builtin types this is a plain FxtrID;
   *  for defineType-based types this starts as a Promise and resolves on first use. */
  readonly typeId: FxtrID | Promise<FxtrID>;
}

/** Symbol for preserving the context marker from load → re-store. */
const FXTR_CTX: unique symbol = Symbol("fxtrCtx");

/** An entity instance: user fields + [FXTR_CODEC] + store convenience + optional ctx. */
type EntityInstance<T> = T & {
  readonly [FXTR_CODEC]: EntityStoreCodec<EntityInstance<T>>;
  readonly [FXTR_CTX]?: FxtrID;  // set on loaded entities, absent on created ones
  store(scope: FxtrScope): Promise<EntityRef<EntityInstance<T>>>;
};

/** Type-level helper: extract the instance type from a definition. */
type Infer<D> = D extends EntityDefinition<infer T> ? EntityInstance<T> : never;

/** Infer the field type from a FieldCodec. */
type InferField<C> = C extends FieldCodec<infer T> ? T : never;
```

### `bicodec.entity(typeId, schema)`

```typescript
function entity<S extends Record<string, FieldCodec<any>>>(
  typeId: FxtrID | Promise<FxtrID>,
  schema: S,
): EntityDefinition<{ [K in keyof S]: InferField<S[K]> }>;
```

Returns an `EntityDefinition` that:

1. **Has a `.create(fields)` method** — synchronous, returns a mutable object with `[FXTR_CODEC]` and `.store()` attached. Does not need the type ID.

2. **Has a `.loadCodec`** — an `EntityLoadCodec` that:
   - `supportsEntityType(typeId)`: awaits the definition's type ID (if promise), compares by `.key()`.
   - `decode(cbor, scope)`: reads user fields from CBOR (skipping `$fxtr.type` and `$fxtr.ctx`), decodes each via the schema's field codecs. Saves the `$fxtr.ctx` marker ID on the result as `[FXTR_CTX]`. Attaches `[FXTR_CODEC]` and `.store()`. Deep-freezes the result. Returns `{ value, cacheable: true }`.

3. **Has an internal store codec** that:
   - Awaits the type ID (if promise; caches the resolved value).
   - Lifts the type ID into the scope (it must already be tracked in the scope or a parent — typically GLOBAL_SCOPE). Does NOT call `trackBuiltin`.
   - Encodes each field via the schema's field codecs (`toCbor`).
   - Adds `$fxtr.type` as `RawWeakFxtrID(typeId)`.
   - Adds `$fxtr.ctx`: if the entity has `[FXTR_CTX]` (i.e., it was loaded from a scope), uses that. Otherwise, gets a fresh marker from `scope.getContextMarkerId()`.
   - Calls `scope.storeCbor(cborMap)`.
   - Returns `new EntityRef(storedId, definition.loadCodec)`.

### create() details

```typescript
create(fields: T): EntityInstance<T>
```

- Takes a plain object with just the user-defined fields.
- Returns a new object with those fields plus `[FXTR_CODEC]` and `store(scope)`.
- The returned object is **mutable** (not frozen). Users can build up objects incrementally before storing.
- `.store(scope)` is a convenience that calls `scope.store(this)`.

### Frozen on load

When `loadCodec.decode()` produces an entity:
- All user fields are decoded from CBOR.
- The result object (including `[FXTR_CODEC]` and `store`) is **deeply frozen** via `Object.freeze`.
- `cacheable: true` — the scope caches it.

This matches Python's `frozen=True` dataclass semantics. The trade-off: loaded entities cannot be mutated, but they can be efficiently shared and cached.

### $fxtr.ctx handling

In Python, `$fxtr.ctx` is set at entity instantiation time via `get_active_context_marker()` (a context variable). In TypeScript, there is no active scope / context variable mechanism.

**On store**: `$fxtr.ctx` is added at store time by the bicodec store codec. If the entity has `[FXTR_CTX]` (meaning it was previously loaded), that value is reused — this preserves the original context marker on re-store. If `[FXTR_CTX]` is absent (the entity was freshly created via `.create()`), the store codec gets a fresh marker from `scope.getContextMarkerId()`.

**On load**: `$fxtr.ctx` is read from CBOR, and its marker ID (the `FxtrID` inside the `RawFxtrEntityMarkerID`) is saved on the entity instance as `[FXTR_CTX]`. This is not exposed as a user field — it's internal metadata. It enables distinguishing loaded entities from freshly created ones, and preserves context identity on round-trip.

**On create**: `.create()` does NOT set `$fxtr.ctx` or `[FXTR_CTX]`. The entity gets its context marker when stored.

### $fxtr.type as Promise<FxtrID>

The type ID can be a `Promise<FxtrID>`. This enables ergonomic top-level definitions:

```typescript
// Module top level — all synchronous:
const MyEntity = bicodec.entity(
  defineType(MY_NAMESPACE, "MyThing"),
  { name: bicodec.string(), score: bicodec.number() },
);
type MyEntity = Infer<typeof MyEntity>;
```

The promise is awaited lazily:
- `create()`: never needs the type ID — fully synchronous.
- `store()`: awaits the type ID to add `$fxtr.type` to CBOR. Caches the resolved ID.
- `supportsEntityType()`: awaits the type ID for comparison.
- `decode()`: does not need the type ID (it's already in the CBOR; the load codec was selected by `supportsEntityType`).

The resolved type ID is accessible after first store/load via `definition.typeId` (which at that point is a resolved `FxtrID`, not a `Promise`).

## defineType

```typescript
/**
 * Create a content-addressed type ID by storing a FxtrBasicType entity.
 * Mirrors Python's define_type().
 *
 * Defaults to GLOBAL_SCOPE. Returns a Promise<FxtrID> that resolves once
 * the type entity is stored.
 */
function defineType(
  namespaceUuid: string,
  typeName: string,
  scope?: FxtrScope,  // defaults to GLOBAL_SCOPE
): Promise<FxtrID>;
```

Implementation:
1. Creates a `FxtrBasicType` entity: `FxtrBasicType.create({ namespace_uuid: namespaceUuid, type_name: typeName })`
2. Stores it into the scope: `await scope.store(entity)`
3. Returns the entity ref's ID: `ref.entityId`

Since type entities are content-addressed, calling `defineType` with the same namespace+name always produces the same ID (same CBOR → same SHA-256 hash).

### UUID representation

Python's `uuid.UUID` is serialized as a string by mashumaro (e.g., `"550e8400-e29b-41d4-a716-446655440000"`). In TS, we represent UUIDs as plain strings — no custom type needed. The bicodec field is just `bicodec.string()`.

## System entities

System entity type IDs are **builtin IDs** (`!name`), not content-addressed hashes. Builtin IDs are well-known constants: `FxtrID.builtin("basic_type")` produces `!basic_type`, etc. These are tracked in the scope as contentless `DerivedNode`s with no dependencies.

This is simple and avoids any bootstrapping issues — `FxtrBasicType`'s type ID is `!basic_type`, a constant that doesn't depend on hashing anything.

### Definitions

Defined in `common/systemEntities.ts` using bicodec. `FxtrBasicType` must produce byte-identical CBOR to the Python equivalent in `fxtr2/data/system_entities.py`.

```typescript
import * as bicodec from "./bicodec.js";
import { FxtrID } from "../core/entityCore.js";
import { GLOBAL_SCOPE } from "../scope/scope.js";

/** Track a builtin ID in GLOBAL_SCOPE so bicodec can lift it into child scopes.
 *  Mirrors Python's track_builtin_id(). */
function trackBuiltinTypeId(name: string): FxtrID {
  const id = FxtrID.builtin(name);
  GLOBAL_SCOPE.trackBuiltin(id);
  return id;
}

// --- FxtrBasicType ---
// Type ID: !basic_type (builtin, tracked in GLOBAL_SCOPE)
const FxtrBasicType = bicodec.entity(trackBuiltinTypeId("basic_type"), {
  namespace_uuid: bicodec.string(),  // UUID as string
  type_name: bicodec.string(),
});
export type FxtrBasicType = bicodec.Infer<typeof FxtrBasicType>;

/**
 * Create a content-addressed type ID by storing a FxtrBasicType.
 * Mirrors Python's define_type().
 */
export async function defineType(
  namespaceUuid: string,
  typeName: string,
  scope: FxtrScope = GLOBAL_SCOPE,
): Promise<FxtrID> {
  const entity = FxtrBasicType.create({
    namespace_uuid: namespaceUuid,
    type_name: typeName,
  });
  const ref = await scope.store(entity);
  return ref.entityId;
}
```

Note: `FxtrBasicType` uses synchronous `FxtrID.builtin("basic_type")` — no Promise needed. Only user-defined types created via `defineType()` use promises.

### Not ported yet

`ContextEntity` and `PathRootEntity` are deferred. They use `Any`-typed fields (`ctx_key`, `metadata`) which would require a `bicodec.unknown()` passthrough codec. Since these entities aren't heavily used in Python yet, we'll port them later once the `unknown()` field type has proven its design. The `unknown()` codec is still described above for reference but is not required for Phase 5.

### Type ID tracking

The bicodec store codec assumes the type ID is already tracked in the scope or a parent scope, and lifts it in. It does NOT call `trackBuiltin` itself.

- **Builtin type IDs** (`!name`): must be explicitly tracked in GLOBAL_SCOPE before being passed to `bicodec.entity()`. The `trackBuiltinTypeId()` helper in `systemEntities.ts` does this.
- **Content-addressed type IDs** (from `defineType`): already stored in GLOBAL_SCOPE by `defineType()`, so they're automatically available for lifting.

This mirrors Python where `track_builtin_id("ctx")` both creates the builtin ID and tracks it in the active scope.

## Comparison with Python's DataclassEntity

| Aspect | Python (mashumaro) | TypeScript (bicodec) |
|---|---|---|
| Entity definition | `@dataclass class Foo(DataclassEntity):` | `const Foo = bicodec.entity(typeId, schema)` |
| Field declaration | Dataclass fields with type annotations | Schema record of field codecs |
| Type ID | `fxtr_type=track_builtin_id("ctx")` or `define_type(ns, name)` | `FxtrID.builtin("ctx")` or `defineType(ns, name)` |
| Context marker | `fxtr_ctx` field, set at instantiation via context var | `$fxtr.ctx` added at store time from scope |
| Construction | `Foo(field1=x, field2=y)` | `Foo.create({ field1: x, field2: y })` |
| Serialization | `entity.to_dict()` via mashumaro | Field codec `toCbor()` calls |
| Deserialization | `Foo.from_dict(data)` via mashumaro | Field codec `fromCbor()` calls |
| Inline children | Two-phase collect/rebuild | Direct `await scope.load()` inline |
| Immutability | `frozen=True` dataclass | `Object.freeze` on load; mutable on create |
| Any-typed fields | Python `Any` | `bicodec.unknown()` → raw FxtrCbor |
| UUID fields | `uuid.UUID` → string via mashumaro | `bicodec.string()` (plain string) |
| Type registry | Global `_ENTITY_LOAD_REGISTRY` | Explicit `loadCodec` on `EntityRef` |
| Type inference | Runtime type annotations | Compile-time `Infer<typeof Def>` |

## Usage example

```typescript
import * as bicodec from "./bicodec.js";
import { defineType } from "./systemEntities.js";

const MY_NS = "550e8400-e29b-41d4-a716-446655440000";

const Score = bicodec.entity(defineType(MY_NS, "score"), {
  player: bicodec.string(),
  value: bicodec.number(),
  tags: bicodec.pyCompatSet(bicodec.string()),
});
type Score = bicodec.Infer<typeof Score>;

// Create (sync)
const score = Score.create({
  player: "alice",
  value: 42,
  tags: new PyCompatSet(stringKeyFn, ["ranked", "season3"]),
});

// Store (async)
const ref = await score.store(scope);
// or: const ref = await scope.store(score);

// Load (async) — returns a frozen Score with [FXTR_CODEC] + .store()
const loaded = await ref.load(scope);
console.log(loaded.player); // "alice"
console.log(loaded.tags);   // PyCompatSet(["ranked", "season3"]) — frozen

// Inline children
const Game = bicodec.entity(defineType(MY_NS, "game"), {
  name: bicodec.string(),
  winner: bicodec.child(Score),       // strong, auto-stored/loaded
  loser: bicodec.child(Score),        // strong, auto-stored/loaded
  referee: bicodec.entityRef(Score),  // weak ref, not auto-loaded
});
type Game = bicodec.Infer<typeof Game>;

const game = Game.create({
  name: "finals",
  winner: score,       // Score instance (will be stored inline)
  loser: Score.create({ player: "bob", value: 10, tags: new Set() }),
  referee: ref,        // EntityRef<Score> (already stored)
});

const gameRef = await scope.store(game);
const loadedGame = await gameRef.load(scope);
// loadedGame.winner is a loaded Score object (not an EntityRef)
// loadedGame.referee is an EntityRef<Score> (call .load() to resolve)
```

## Cross-language consistency tests

A dedicated test file in both TS and Python verifies that equivalent entity definitions produce byte-identical content-addressed IDs. This serves as both a cross-language compatibility test and a regression test to catch accidental changes to canonical encoding.

### Approach

1. Define a set of test entity types that cover all field codec types (string, number, boolean, bytes, fxtrId, childFxtrId, entityRef, childRef, child, linked, object, array, record, set, nullable, unknown).
2. In one language (say Python), create instances with known field values, store them, and record the resulting entity IDs.
3. Hardcode those same IDs into test files for both Python and TS.
4. Both test suites verify: create equivalent instances → store → assert entity ID matches the hardcoded value.

If either language changes its canonical encoding in a way that breaks compatibility, the hardcoded ID assertion will fail.

### File locations

```
v2/tests/test_cross_language/
└── test_bicodec_consistency.py     # Python side

v2/ts/tests/common/
└── crossLanguageConsistency.test.ts  # TS side
```

Both files should be clearly documented in their respective CODE_OVERVIEWs as cross-language consistency tests.

### What to cover

- `defineType()` produces same ID for same namespace+name
- Each scalar field type: string, number, boolean, bytes
- FxtrID fields (weak and strong)
- EntityRef / ChildRef fields
- Inline child / linked entities
- Nested objects
- Arrays, records, sets (including canonical set sort order)
- Nullable fields
- FxtrBasicType with identical field values (verifies `defineType` produces same IDs cross-language)

## Implementation plan

### File structure

```
src/common/
├── bicodec.ts          # FieldCodec, field codec factories, EntityDefinition, entity()
├── pyCompatSet.ts      # PyCompatSet class + pyCompatSet() field codec
└── systemEntities.ts   # defineType, FxtrBasicType, trackBuiltinTypeId
```

Bicodec core is a single file unless it grows unwieldy (> ~500 lines). The `pyCompatSet` codec and its backing `PyCompatSet` class live in a separate file to keep the set-specific logic (key extraction, sorting, custom collection class) from cluttering the core codec definitions.

### Implementation order

1. [x] **FieldCodec interface + DecodedField + scalar codecs**: `string()`, `number()`, `boolean()`, `bytes()`, `unknown()`. Trivial pass-throughs. All return `frozen: true`. Each codec has a `kind` tag.

2. [x] **FxtrID codecs**: `fxtrId()`, `childFxtrId()`. Wrap/unwrap `RawWeakFxtrID` / `RawStrongFxtrID`.

3. [x] **Composite codecs** (excluding pyCompatSet): `object()`, `array()`, `record()`, `nullable()`. Each does local `Object.freeze` and propagates `frozen` flag from children.

4. [x] **Combinators**: `mapped()`, `lazy()`.

5. [x] **Entity ref codecs**: `entityRef()`, `childRef()`. These create/read `EntityRef` instances without loading.

6. [x] **PyCompatSet** (`src/common/pyCompatSet.ts`): `PyCompatSet<T>` class (Map-backed, freezable) + `pyCompatSet(itemCodec)` field codec. Key extraction and sort order based on item codec `kind`. Validates allowed codec kinds at construction time.

7. [x] **`entity()` + EntityDefinition**: The core — `create()`, store codec, load codec. Wire up `$fxtr.type` (lifted at store time), `$fxtr.ctx` (added at store time from scope, or preserved from `[FXTR_CTX]`), `[FXTR_CODEC]`, `[FXTR_CTX]`, `.store()`. Cacheability determined by ANDing all field `frozen` flags.

8. [x] **Inline child codecs**: `child()`, `linked()`. These call `scope.store()` / `scope.load()`. Depend on EntityDefinition working.

9. [x] **`defineType()` + FxtrBasicType**: `FxtrBasicType` (with builtin `!basic_type` type ID tracked in GLOBAL_SCOPE), `defineType()`, `trackBuiltinTypeId()`.

10. [x] **Unit tests**: Round-trips for each field codec type. Entity create/store/load. Inline children. Frozen/cacheable propagation. PyCompatSet with all supported item codec kinds. FxtrBasicType. (54 tests)

11. [x] **Cross-language consistency tests**: Hardcoded ID assertions in both Python and TS test suites. (7 tests each)

12. [x] **Type inference validation**: `Infer<>` and `InferField<>` work correctly (validated via compile-time type checking in tests).

13. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Testing strategy

- **Unit tests for each field codec**: round-trip `toCbor` → `fromCbor` for each type, including edge cases (empty arrays, empty sets, nested objects, etc.). Verify `frozen` flag propagation.
- **Entity round-trip tests**: `create` → `store` → `load` → verify fields match. Verify `[FXTR_CODEC]`, `.store()`, and `[FXTR_CTX]` on loaded entities.
- **Cacheability tests**: entity with all-frozen fields → `cacheable: true`. Entity with a `mapped(..., { frozen: false })` field → `cacheable: false`.
- **Inline child tests**: entity with `child()` / `linked()` fields — verify children are stored/loaded correctly, strong/weak refs produce correct CBOR tags.
- **Cross-language consistency tests**: hardcoded entity IDs verified in both Python and TS (see above).
- **System entity tests**: `defineType` round-trips, `FxtrBasicType` store/load correctly.
- **PyCompatSet class tests**: add/has/delete, iteration, freeze behavior (throws on mutation after freeze), key extraction for each codec kind.
- **Set canonical encoding tests**: verify `pyCompatSet` serialization order matches Python's `_canonical_set_sort_key` for each supported item codec kind (string, number, bytes, fxtrId, entityRef). Verify invalid codec kinds are rejected at construction time.
- **mapped() tests**: custom codec with `frozen: true` and `frozen: false`, verify propagation to entity cacheability.
- **lazy() tests**: recursive entity type, verify round-trip works.

## Implementation notes

### Type inference for `child()` / `linked()`

The overload signatures must distinguish between accepting an `EntityDefinition<T>` and a raw `EntityLoadCodec<T>`:

```typescript
// EntityDefinition overload — result type includes EntityInstance wrapper
function child<T>(def: EntityDefinition<T>): FieldCodec<EntityInstance<T>>;
// Raw load codec overload — result type is whatever the codec produces
function child<T>(codec: EntityLoadCodec<T>): FieldCodec<T>;
```

This matters for `Infer<>` — a parent entity with `winner: bicodec.child(Score)` should infer `winner` as `EntityInstance<ScoreFields>` (with `[FXTR_CODEC]`, `.store()`, etc.), not just `ScoreFields`.

Same applies to `linked()`.

### Schema keys are CBOR keys

The keys in the schema record passed to `entity()` are used verbatim as CBOR map keys. There is no aliasing or renaming (unlike Python's mashumaro which aliases `fxtr_type` → `$fxtr.type`). The `$fxtr.type` and `$fxtr.ctx` keys are added automatically by the store codec and are not part of the user schema.

This means if a Python entity has a field named `ctx_key`, the TS bicodec schema must also use `ctx_key` as its key. Field names must match exactly across languages for cross-language compatibility.

### Unknown CBOR keys on decode

The entity load codec silently ignores CBOR keys that are not in the schema (besides `$fxtr.type` and `$fxtr.ctx`). This matches Python's mashumaro behavior (which ignores extra keys by default) and provides forward compatibility — a newer entity definition can add fields without breaking older decoders.

**TODO (revisit):** This means load-then-store is not a perfect round-trip: if an entity has CBOR keys that the bicodec schema doesn't know about, those keys are silently dropped on re-store. This could cause data loss when an older decoder re-stores an entity created by a newer encoder. We should revisit whether we want to preserve unknown keys (e.g., by stashing them on a symbol property of the decoded entity and re-emitting them on store). Leave a TODO in the code as well.
