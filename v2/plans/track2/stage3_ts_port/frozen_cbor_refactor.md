# Frozen CBOR Refactor: Unify FxtrCbor as readonly, remove FrozenFxtrCbor

## Context

During the entity codec refactor (Phase 5.5), `UnknownEntity.store()` had to use `structuredClone` to thaw frozen CBOR before passing it to `storeCbor`, which only accepts mutable `FxtrCbor`. This is unnecessary — the store path never mutates CBOR data, it only reads and encodes it.

The root cause is that TypeScript has two separate CBOR types: `FxtrCbor` (mutable) and `FrozenFxtrCbor` (readonly). This was ported from Python's `dict` vs `frozendict` distinction, but in TS the readonly annotation is purely a type-level constraint (no runtime enforcement). Rather than plumbing `FrozenFxtrCbor` acceptance through the store chain, we can simplify by making `FxtrCbor` itself readonly and removing `FrozenFxtrCbor` entirely.

**Intended outcome**: One CBOR type (`FxtrCbor`, always readonly), no `FrozenFxtrCbor`, no `UnknownMutableEntity`, no `structuredClone` thawing, merged `loadCbor`/`loadFrozenCbor` into a single method.

## Design

### Make `FxtrCbor` readonly

```typescript
export type FxtrCbor =
  | null | number | boolean | string | Uint8Array
  | readonly FxtrCbor[]
  | { readonly [key: string]: FxtrCbor }
  | RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID;
```

This is structurally identical to the current `FrozenFxtrCbor`. The `FrozenFxtrCbor` type alias is deleted.

**Why this works for construction**: When building CBOR in `toCbor` paths, local mutable variables (`const result: Record<string, FxtrCbor> = {}`) are mutable — `result[key] = val` compiles fine. Mutable arrays/records are assignable to their readonly supertypes, so returning them as `FxtrCbor` works without casts.

### Remove `FrozenFxtrCbor`

Find-replace all `FrozenFxtrCbor` → `FxtrCbor`. Delete the type alias.

### Remove `UnknownMutableEntity` and `unknownMutableLoadCodec`

With only one CBOR type, there's no need for a mutable variant. If callers need a mutable copy, they can `structuredClone` what they get from `UnknownEntity.raw_cbor`.

### Merge `loadCbor` / `loadFrozenCbor` → single `loadCbor`

Both methods return `FxtrCbor` (now always readonly). Keep `loadCbor`, remove `loadFrozenCbor`. The implementation uses `unknownLoadCodec` (cached, returns `UnknownEntity`).

### Remove `structuredClone` from `unknownStoreCodec`

Since `storeCbor` now accepts readonly `FxtrCbor`, no thawing needed.

### `deepFreeze` becomes optional runtime hardening

Keep the function and its tests — it's still useful for defense-in-depth (e.g., catching accidental mutation bugs). But it's no longer required by the type system. The `unknownLoadCodec.decode` and `bicodec.unknown().fromCbor` calls to `deepFreeze` stay for runtime safety.

### `bicodec.unknown()` field codec

Currently `FieldCodec<FrozenFxtrCbor>`. Becomes `FieldCodec<FxtrCbor>`. The `toCbor` method was `(value: FrozenFxtrCbor) => FxtrCbor` — now identity-typed.

## Files to change

| File | Changes |
|---|---|
| `src/core/entityCore.ts` | Make `FxtrCbor` readonly, delete `FrozenFxtrCbor`, update `deepFreeze` return type |
| `src/scope/entityRef.ts` | Remove `FrozenFxtrCbor` import, `UnknownEntity.raw_cbor` → `FxtrCbor`, delete `UnknownMutableEntity` + `unknownMutableStoreCodec` + `unknownMutableLoadCodec`, remove `structuredClone` from `unknownStoreCodec` |
| `src/scope/scope.ts` | Remove `FrozenFxtrCbor` import, delete `loadFrozenCbor`, update `loadCbor` to use `unknownLoadCodec` (cached), remove `UnknownMutableEntity` import |
| `src/common/bicodec.ts` | Remove `FrozenFxtrCbor` import, `unknown()` returns `FieldCodec<FxtrCbor>`, simplify its `toCbor` |
| `tests/scope/entityRef.test.ts` | Remove `UnknownMutableEntity`/`unknownMutableLoadCodec` imports+tests, update `loadCbor`/`loadFrozenCbor` tests → single `loadCbor` test |
| `tests/scope/scope.test.ts` | `loadFrozenCbor` calls → `loadCbor` |
| `tests/common/bicodec.test.ts` | Update `unknown()` codec test if it references `FrozenFxtrCbor` |
| `ts/CODE_OVERVIEW.md` | Update type descriptions |

No changes needed: `src/core/visitor.ts`, `src/arena/types.ts`, `src/arena/memoryArena.ts`, `src/scope/unionLoadCodec.ts`, `src/common/pyCompatSet.ts`, `src/common/systemEntities.ts`.

## Implementation order

### Step 1: Core type change
1. [x] In `entityCore.ts`: make `FxtrCbor` readonly (copy current `FrozenFxtrCbor` definition), delete `FrozenFxtrCbor` type alias
2. [x] Update `deepFreeze` signature: return type becomes `FxtrCbor` (identity, since FxtrCbor is now readonly)

### Step 2: entityRef.ts cleanup
1. [x] Remove `FrozenFxtrCbor` from imports
2. [x] `UnknownEntity.raw_cbor`: `FrozenFxtrCbor` → `FxtrCbor`; constructor param same
3. [x] Remove `structuredClone` from `unknownStoreCodec` — pass `value.raw_cbor` directly
4. [x] Delete `unknownMutableStoreCodec`, `UnknownMutableEntity`, `unknownMutableLoadCodec`

### Step 3: scope.ts cleanup
1. [x] Remove `FrozenFxtrCbor` and `UnknownMutableEntity` imports, remove `unknownMutableLoadCodec` import
2. [x] Delete `loadFrozenCbor` method
3. [x] Update `loadCbor` to use `unknownLoadCodec` (cached `UnknownEntity`) instead of `unknownMutableLoadCodec`, return `entity.raw_cbor`

### Step 4: bicodec.ts cleanup
1. [x] Remove `FrozenFxtrCbor` import (keep `deepFreeze` import since `unknown()` codec still calls it)
2. [x] `unknown()` field codec: `FieldCodec<FrozenFxtrCbor>` → `FieldCodec<FxtrCbor>`, simplify `toCbor`

### Step 5: Test updates
1. [x] `entityRef.test.ts`: remove `UnknownMutableEntity`/`unknownMutableLoadCodec` tests, update `loadCbor`/`loadFrozenCbor` tests → single `loadCbor` test
2. [x] `scope.test.ts`: `loadFrozenCbor` → `loadCbor`
3. [x] `bicodec.test.ts`: no changes needed
4. [x] `entityCore.test.ts`: no changes needed (`deepFreeze` tests pass as-is)
5. [x] Run full test suite — 287 tests pass

### Step 6: Docs
1. [x] Update `ts/CODE_OVERVIEW.md`
2. [x] Update `overview.md`: add Phase 5.6 entry
