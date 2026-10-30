# CBOR Encoding Spec

How the `cbor2` npm package maps to/from fxtr's custom CBOR tags, and how canonical encoding works in TypeScript.

## Overview

The CBOR layer is the foundation of the TS port. It provides:
- `FxtrID`: immutable entity identifier (no `_provenance_parent` — just raw data ID bytes)
- Tagged ID wrappers: `RawWeakFxtrID`, `RawStrongFxtrID`, `RawFxtrEntityMarkerID` (classes)
- `FxtrCbor` / `FrozenFxtrCbor` type definitions
- `fxtrCborDumps()` / `fxtrCborLoads()`: encode/decode with fxtr tags
- `fxtrCborDumpsWithMarkerCheck()`: encode with entity marker validation

## FxtrID

Direct port of Python's `FxtrID`, minus `_provenance_parent`.

```typescript
class FxtrID {
  /** The raw ID bytes: tag byte + payload. */
  readonly rawDataId: Uint8Array;

  /** Cached id string for use as Map key and general display. */
  private _idString: string | null = null;

  constructor(rawDataId: Uint8Array) { ... }

  /** The tag character (first byte). */
  getTag(): string { ... }

  /** Split into [tag, payload]. */
  splitTagAndPayload(): [string, Uint8Array] { ... }

  /** Canonical string representation. Also used as Map/Set key. */
  idString(): string { ... }

  /** Short summary for debugging. */
  summary(): string { ... }

  toString(): string { return this.idString(); }

  /** String key for use in Map<string, ...> and Set<string>. */
  key(): string { return this.idString(); }

  // --- Constructors ---

  static fromTagAndPayload(tag: string, payload: Uint8Array): FxtrID { ... }
  static fromIdString(idStr: string): FxtrID { ... }
  static builtin(name: string): FxtrID { ... }
  static here(): FxtrID { ... }  // the "." node
}
```

### FxtrID as Map key

JS objects don't have value equality, so `FxtrID` instances can't be used directly as `Map` or `Set` keys. Instead, use `id.key()` (which returns `idString()`) as the key:

```typescript
const entities = new Map<string, FxtrEntityFlatData>();
entities.set(id.key(), flatData);
const found = entities.get(id.key());
```

All internal data structures (MemoryStore, MemoryArena, scope caches, HttpReadableArena) use `Map<string, ...>` keyed by `id.key()`.

The `key()` result is cached on first call, so repeated lookups are cheap.

### ID string format

Same as Python — no `@` separator since we don't have provenance parents:

| Tag | Format | Example |
|-----|--------|---------|
| `.` | `.` | `.` |
| `d` | `d{base32}` | `dabcdef...` |
| `c` | `c{base32}` | `cabcdef...` |
| `n` | `n{base32}` | `nabcdef...` |
| `p` | `p{base32}[{varint32_path}]` | `pabcdef[0a]` |
| `!` | `!{name}` | `!ctx` |
| `u` | `u{inner_id_string}` | `ud...` |

Base32 encoding: lowercase, no padding (same as Python's `_b32enc`/`_b32dec`).
Varint32 encoding: same custom base-32 alphabet as Python.

### What we skip

- `_provenance_parent` field
- `id_string(include_provenance=...)` parameter
- `@` separator parsing in `fromIdString()`
- `fxtr_cbor_lift_to_provenance()`
- `_fxtr_id_lift()`

## Tagged ID wrappers

Classes wrapping a `FxtrID` with a CBOR tag number. Using classes (rather than discriminated unions with a `tag` field) so they don't interfere with user objects that might have their own `tag` field.

Each class implements `toCBOR()` for encoding (the `cbor2` npm package recognizes this method automatically):

```typescript
class RawWeakFxtrID {
  static readonly CBOR_TAG = 33001;
  constructor(public readonly innerId: FxtrID) {}
  toCBOR() { return [33001, this.innerId.rawDataId]; }
}

class RawStrongFxtrID {
  static readonly CBOR_TAG = 33002;
  constructor(public readonly innerId: FxtrID) {}
  toCBOR() { return [33002, this.innerId.rawDataId]; }
}

class RawFxtrEntityMarkerID {
  static readonly CBOR_TAG = 33000;
  constructor(public readonly innerId: FxtrID) {}
  toCBOR() { return [33000, this.innerId.rawDataId]; }
}

/** Union of all tagged ID types. */
type RawFxtrRef = RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID;
```

## FxtrCbor type

```typescript
/** Mutable CBOR value with fxtr tags. */
type FxtrCbor =
  | null
  | number
  | boolean
  | string
  | Uint8Array
  | FxtrCbor[]
  | { [key: string]: FxtrCbor }
  | RawWeakFxtrID
  | RawStrongFxtrID
  | RawFxtrEntityMarkerID;

/** Deeply-readonly CBOR value. Type-level enforcement via Readonly;
 *  runtime enforcement via Object.freeze() when needed (e.g. caching). */
type FrozenFxtrCbor =
  | null
  | number
  | boolean
  | string
  | Uint8Array
  | readonly FrozenFxtrCbor[]
  | { readonly [key: string]: FrozenFxtrCbor }
  | RawWeakFxtrID
  | RawStrongFxtrID
  | RawFxtrEntityMarkerID;
```

No separate `freeze_fxtr_cbor()` / `thaw_fxtr_cbor()` conversion functions. Instead:
- `FrozenFxtrCbor` is a type-level constraint (readonly arrays and objects)
- `deepFreeze(cbor: FxtrCbor): FrozenFxtrCbor` utility for runtime enforcement when storing into caches
- No thaw needed — if you want a mutable copy, spread/structuredClone

### Python differences

| Python | TypeScript | Notes |
|--------|-----------|-------|
| `frozendict` | `Readonly<Record<string, ...>>` | Type-level only; `Object.freeze()` at runtime |
| `tuple` (frozen list) | `readonly T[]` | Type-level only; `Object.freeze()` at runtime |
| `bytes` | `Uint8Array` | |
| `int` / `float` | `number` | JS has no int/float distinction; CBOR encodes integers as integers |
| `None` | `null` | |

## cbor2 (npm) integration

We use the `cbor2` npm package (https://github.com/hildjj/cbor2), not to be confused with the Python `cbor2` library.

### Encoding (fxtrCborDumps)

The `cbor2` package automatically calls `toCBOR()` on any object that has it, so our tagged ID classes encode themselves. We use `sortLengthFirstDeterministic` for RFC 7049 canonical key ordering (length-first, then bytewise), matching Python's `cbor2.dumps(canonical=True)`.

```typescript
import { encode } from 'cbor2';
import { sortLengthFirstDeterministic } from 'cbor2/sorts';

const fxtrEncodeOptions = {
  sortKeys: sortLengthFirstDeterministic,
};

function fxtrCborDumps(value: FxtrCbor): Uint8Array {
  return encode(value, fxtrEncodeOptions);
}
```

This produces byte-identical output to Python for the same logical data, ensuring FxtrIDs match cross-language.

### Decoding (fxtrCborLoads)

Register tag decoders via the `tags` option. The decoder receives a `Tag` object with `.tag` (number) and `.contents` (decoded inner value):

```typescript
import { decode, Tag } from 'cbor2';

const fxtrTagDecoders = new Map([
  [33000, ({ contents }: { contents: Uint8Array }) =>
    new RawFxtrEntityMarkerID(new FxtrID(contents))],
  [33001, ({ contents }: { contents: Uint8Array }) =>
    new RawWeakFxtrID(new FxtrID(contents))],
  [33002, ({ contents }: { contents: Uint8Array }) =>
    new RawStrongFxtrID(new FxtrID(contents))],
]);

const fxtrDecodeOptions = {
  tags: fxtrTagDecoders,
};

function fxtrCborLoads(data: Uint8Array): FxtrCbor {
  return decode(data, fxtrDecodeOptions) as FxtrCbor;
}
```

### Marker check (fxtrCborDumpsWithMarkerCheck)

Port of Python's `fxtr_cbor_dumps_with_marker_check`. Validates:
1. Value is a plain object (not array/primitive/Uint8Array)
2. Has `$fxtr.ctx` field
3. `$fxtr.ctx` is a `RawFxtrEntityMarkerID`
4. Exactly one `RawFxtrEntityMarkerID` in the entire tree

Implementation: validate the structure and walk the tree to count markers before encoding.

```typescript
function fxtrCborDumpsWithMarkerCheck(value: FxtrCbor): Uint8Array {
  if (typeof value !== 'object' || value === null ||
      Array.isArray(value) || value instanceof Uint8Array ||
      value instanceof RawWeakFxtrID || value instanceof RawStrongFxtrID ||
      value instanceof RawFxtrEntityMarkerID) {
    throw new InvalidEntityError('Entity must be a plain object');
  }
  if (!('$fxtr.ctx' in value)) {
    throw new InvalidEntityError('Entity must have a "$fxtr.ctx" field');
  }
  if (!(value['$fxtr.ctx'] instanceof RawFxtrEntityMarkerID)) {
    throw new InvalidEntityError('"$fxtr.ctx" field must be a RawFxtrEntityMarkerID');
  }
  let markerCount = 0;
  visitIds(value, (ref) => {
    if (ref instanceof RawFxtrEntityMarkerID) markerCount++;
  });
  if (markerCount !== 1) {
    throw new DuplicateEntityMarkerError(
      'Found more than one RawFxtrEntityMarkerID in this entity'
    );
  }
  return fxtrCborDumps(value);
}
```

## Visitor functions (visitor.ts)

Direct port of `arena_util.py`. Uses `instanceof` for dispatch on tagged ID classes:

```typescript
function visitIds(
  root: FxtrCbor,
  callback: (ref: RawFxtrRef) => void,
): void {
  if (root instanceof RawWeakFxtrID ||
      root instanceof RawStrongFxtrID ||
      root instanceof RawFxtrEntityMarkerID) {
    callback(root);
  } else if (Array.isArray(root)) {
    for (const item of root) visitIds(item, callback);
  } else if (root !== null && typeof root === 'object' &&
             !(root instanceof Uint8Array)) {
    for (const value of Object.values(root)) visitIds(value, callback);
  }
  // primitives (null, number, boolean, string, Uint8Array): no-op
}

function visitAndProjectIds(
  root: FxtrCbor,
  callback: (ref: RawFxtrRef) => FxtrID,
): FxtrCbor {
  if (root instanceof RawWeakFxtrID) return new RawWeakFxtrID(callback(root));
  if (root instanceof RawStrongFxtrID) return new RawStrongFxtrID(callback(root));
  if (root instanceof RawFxtrEntityMarkerID) return new RawFxtrEntityMarkerID(callback(root));
  if (Array.isArray(root)) return root.map(item => visitAndProjectIds(item, callback));
  if (root !== null && typeof root === 'object' && !(root instanceof Uint8Array)) {
    const result: { [key: string]: FxtrCbor } = {};
    for (const [k, v] of Object.entries(root)) {
      result[k] = visitAndProjectIds(v, callback);
    }
    return result;
  }
  return root;
}
```

### Canonical-order visitor

`visitAndProjectIdsInCanonicalCborOrder` is needed for p-nodes (path indices refer to serialization order). Uses the same encode-then-decode-with-projection trick as Python:

```typescript
function visitAndProjectIdsInCanonicalCborOrder(
  root: FxtrCbor,
  callback: (ref: RawFxtrRef) => FxtrID,
): FxtrCbor {
  // Encode to get canonical byte order
  const asBytes = fxtrCborDumps(root);
  // Decode with tag decoders that apply the projection callback
  const projectionTagDecoders = new Map([
    [33000, ({ contents }: { contents: Uint8Array }) => {
      const id = new RawFxtrEntityMarkerID(new FxtrID(contents));
      return new RawFxtrEntityMarkerID(callback(id));
    }],
    [33001, ({ contents }: { contents: Uint8Array }) => {
      const id = new RawWeakFxtrID(new FxtrID(contents));
      return new RawWeakFxtrID(callback(id));
    }],
    [33002, ({ contents }: { contents: Uint8Array }) => {
      const id = new RawStrongFxtrID(new FxtrID(contents));
      return new RawStrongFxtrID(callback(id));
    }],
  ]);
  return decode(asBytes, { tags: projectionTagDecoders }) as FxtrCbor;
}
```

### Async canonical-order visitor

Port of Python's `async_visit_and_project_ids_in_canonical_cbor_order`. Three-pass approach:

1. Encode to canonical bytes
2. Decode once to collect all IDs in canonical order
3. Await all async callbacks sequentially
4. Decode again, substituting projected IDs

```typescript
async function asyncVisitAndProjectIdsInCanonicalCborOrder(
  root: FxtrCbor,
  callback: (ref: RawFxtrRef) => Promise<FxtrID>,
): Promise<FxtrCbor> {
  const asBytes = fxtrCborDumps(root);

  // Pass 1: collect IDs in canonical order
  const idsInOrder: RawFxtrRef[] = [];
  const collectTagDecoders = new Map([
    [33000, ({ contents }: { contents: Uint8Array }) => {
      const id = new RawFxtrEntityMarkerID(new FxtrID(contents));
      idsInOrder.push(id);
      return id;
    }],
    [33001, ({ contents }: { contents: Uint8Array }) => {
      const id = new RawWeakFxtrID(new FxtrID(contents));
      idsInOrder.push(id);
      return id;
    }],
    [33002, ({ contents }: { contents: Uint8Array }) => {
      const id = new RawStrongFxtrID(new FxtrID(contents));
      idsInOrder.push(id);
      return id;
    }],
  ]);
  decode(asBytes, { tags: collectTagDecoders });

  // Pass 2: call async callback for each ID (sequentially, in order)
  const projectedIds: FxtrID[] = [];
  for (const typedId of idsInOrder) {
    projectedIds.push(await callback(typedId));
  }

  // Pass 3: reconstruct with projected IDs
  let idx = 0;
  const projTagDecoders = new Map([
    [33000, () => new RawFxtrEntityMarkerID(projectedIds[idx++])],
    [33001, () => new RawWeakFxtrID(projectedIds[idx++])],
    [33002, () => new RawStrongFxtrID(projectedIds[idx++])],
  ]);
  return decode(asBytes, { tags: projTagDecoders }) as FxtrCbor;
}
```

## Deep freeze utility

```typescript
function deepFreeze<T extends FxtrCbor>(obj: T): FrozenFxtrCbor {
  if (obj === null || typeof obj !== 'object') return obj as FrozenFxtrCbor;
  if (obj instanceof Uint8Array ||
      obj instanceof RawWeakFxtrID ||
      obj instanceof RawStrongFxtrID ||
      obj instanceof RawFxtrEntityMarkerID) {
    return Object.freeze(obj) as FrozenFxtrCbor;
  }
  if (Array.isArray(obj)) {
    for (const item of obj) deepFreeze(item);
    return Object.freeze(obj) as unknown as FrozenFxtrCbor;
  }
  for (const value of Object.values(obj)) deepFreeze(value as FxtrCbor);
  return Object.freeze(obj) as FrozenFxtrCbor;
}
```

## Error types

```typescript
class InvalidEntityError extends Error {}
class DuplicateEntityMarkerError extends InvalidEntityError {}
```

## File mapping

| Python | TypeScript | Contents |
|--------|-----------|----------|
| `entity_core.py` | `src/core/entityCore.ts` | FxtrID class, base32/varint32 helpers, tagged ID classes, FxtrCbor types, fxtrCborDumps/Loads, marker check, deepFreeze, error types |
| `arena_util.py` | `src/core/visitor.ts` | visitIds, visitAndProjectIds, canonical-order variants |

Single file mirrors Python's `entity_core.py` and avoids a circular dependency between FxtrID (needs CBOR for p-node payloads) and the tagged ID classes (need FxtrID).
