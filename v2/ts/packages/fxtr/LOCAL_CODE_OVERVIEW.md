# @fxtr/core — Internal Code Overview

This covers implementation details and internal APIs of `@fxtr/core`. For the public API surface, see the root `CODE_OVERVIEW.md`. Everything here is either exported via `@fxtr/core/internal` or is internal implementation detail within public modules.

## Entrypoints

- **`@fxtr/core`** (`index.ts`): Public API — IDs, CBOR, scopes, entity refs, codecs, namespaces.
- **`@fxtr/core/internal`** (`internal.ts`): Arena types, memory arena implementation, unsafe symbols. Used by `@fxtr/http-client` and `@fxtr/http-server`.
- **`@fxtr/core/bicodec`** (`common/bicodec.ts`): Dedicated entrypoint for the bicodec module (field codec names are too generic for the top-level namespace).

## Internal Modules

### `arena/types.ts`

Abstract interfaces for arenas and storage backends. Ported from Python's `abstract_arena.py`. Exported via `@fxtr/core/internal`.

- **`DepKind`**: Enum for dependency relationship types (STRONG_REF, WEAK_REF, CTX_NODE, BACKSTEP, UNWRAPS_TO).
- **`FxtrEntityFlatData`**: Flat representation of entity data and dependencies (raw bytes + deps list).
- **`ReadableArena`**: Can load entities, check tracking, unwrap provenance nodes. Methods: `isTracked`, `load`, `loadFlat`, `unwrapProvenance`, `cNodeToContextData`, `pNodeToPathroot`, `stepBackPCtx`, `abandon`, `copyToNewArena`, `close`.
- **`CopyTargetArena`**: Extends ReadableArena with `copyFromArena`.
- **`ReadWriteArena`**: Extends CopyTargetArena with `store` (async — SHA-256), `trackBuiltin`, `createCNode`, `createPNode`, `createUNode`.
- **`DeepFetchable`**: Interface with `tryLookup(nodeId)` for deep fetch. Replaces Python's `ObservableStore` (no `backrefs`).

### `arena/memoryArena.ts`

In-memory implementation of arenas. Ported from Python's `memory_arena.py`. Exported via `@fxtr/core/internal`.

- **`InMemoryBytesBackingStore`**: Refcounted byte storage for entity data. All `MemoryStore` instances share a single `GLOBAL_BACKING_STORE` by default (mirroring Python's `GLOBAL_BACKING_STORE` context var), so copying entities between scopes shares the underlying bytes rather than duplicating data.
- **`MemoryStore`**: Backing store holding actual entity data. Manages node states (StoredNode, DerivedNode, InheritedNode), reference counting for GC, and arena lifecycle. Implements `DeepFetchable`. Accepts an optional `InMemoryBytesBackingStore` (defaults to `GLOBAL_BACKING_STORE`). Key methods: `newEmptyArena()`, `hasOpenArenas()`, `bareStore()` (async — SHA-256), `bareStoreFlat()` (sync), `bareLoad()`, `bareLoadFlat()`, `trackBuiltin()`, `createCNode()`, `createPNode()`, `createUNode()`, `collectOneGarbage()`, `tryLookup()`.
- **`MemoryArena`**: A "view" into the store with a tracked liveset. Implements `ReadWriteArena`. Key methods: `store()`, `load()`, `loadFlat()`, `trackBuiltin()`, `abandon()`, `copyToNewArena()`, `copyFromArena()`, `unsafeLazilyAttachFromArena()`, `copyFromSiblingMemoryArena()`, `clone()`.
- **Node states**: Discriminated union with `kind` field: `StoredNode` (d/n nodes with CBOR bytes), `DerivedNode` (contentless nodes computed on demand), `InheritedNode` (lazy placeholder from another arena).
- **GC**: Reference counting via `requiredBy`/`wrappedBy` sets. Dead nodes collected via `collectOneGarbage()`. No cycles possible (content-addressed, immutable).
- **SHA-256**: Uses Web Crypto API (`crypto.subtle.digest`) — async, cross-platform (browsers + Node.js 18+). This makes `store`, `createCNode`, `createPNode`, `createUNode` async, unlike Python where they are sync.
- **Inherited nodes**: Lazy attachment via `unsafeLazilyAttachFromArena()`. Nodes are loaded from parent arena on demand via `_ensureOwn()`.

Not ported from Python: `LMDBBytesBackingStore`, `StreamSink`/`MemoryStreamSink`, `PersistentPinningBackend`, `backrefs()`, debug logging, leak detection.

### `scope/unsafeSymbols.ts`

Internal symbols for `FxtrScope` that are not part of the public API. Exported via `@fxtr/core/internal`.

- **`UNSAFE_ATTACH_FROM_EXTERNAL_ARENA`**: Symbol-keyed method on `FxtrScope` that delegates to `mainArena.unsafeLazilyAttachFromArena()`. Used by `HttpFxtrReadNamespace` to attach HTTP-fetched entities into a scope's memory arena.

## Implementation Details of Public Modules

### `scope/scope.ts` — Internals

The public API (`FxtrScope`, `createFxtrScope`, `withFxtrScope`, `GLOBAL_SCOPE`) is documented in the root CODE_OVERVIEW. Internal details:

- **`FxtrScopeState`**: Manages all scope state — arena layer, hierarchy, caching, deep fetchability. One instance per logical scope. `FxtrScope` is a lightweight surface pointing at a state.
- **State/Surface split**: Multiple `FxtrScope` surfaces can share one `FxtrScopeState`. Each surface has an optional `_loadTrace` list for tracking side-effect loads during decode. This replaces Python's `_load_tracer` context var.
- **`KeepAlive`**: Allows child scopes to keep parent scopes alive. `hold()` returns a release function; `shutdown()` returns a promise that resolves when all holds are released. Simpler than Python's `_KeepAlive` (no `on_unexpected_close` callbacks — JS Promises don't have cancellation semantics).
- **`RefLoadCacheEntry<T>` / `RefLoadUncacheableMarker<T>`**: Cache entry types tracking loaded values and their side-effect loads.
- **Load implementation**: check cache → lift from parents → decode with tracing surface → cache if immutable.
- **Lifecycle**: Three-state (alive → closing → closed). `scheduleClose()` marks as closing and starts async background shutdown. In `closing` state, direct operations throw but child scopes continue working.

Not ported from Python: `ACTIVE_SCOPE` context var, `get_active()`, `as_active_scope()`, `_resolve_scope()`, `deep_load()`, `EntityRef.val`, `outer` property, `_ENTITY_LOAD_REGISTRY`, `EntityTypeAnnotation`.

### `common/bicodec.ts` — Implementation Notes

- **Store codec**: Awaits type ID, lifts it into scope, encodes fields, adds `$fxtr.type` and `$fxtr.ctx`, calls `scope.storeCbor()`.
- **Load codec** (`decode()`): Decodes fields, attaches `[FXTR_CODEC]`, `[FXTR_CTX]`, and `[FXTR_CACHEABLE]`, freezes if all fields frozen, returns the entity directly.
- **`FXTR_CTX`**: Symbol for preserving context marker on round-trip. Set on loaded entities, absent on created ones.
- **Promise type IDs**: `typeId` can be `Promise<FxtrID>` for `defineType()`-based types. Awaited lazily on first store/supportsEntityType.

### `core/entityCore.ts` — Low-level Encoding

These are exported publicly but are low-level utilities mostly used internally:

- **`b32enc()` / `b32dec()`**: Base32 encoding/decoding (lowercase, no padding).
- **`encodeVarint32()` / `decodeVarint32()`**: Custom base-32 varint encoding for p-node paths.
- **`fxtrCborDumpsWithMarkerCheck()`**: Encode with validation that exactly one entity marker exists at `$fxtr.ctx`.

## File Structure

```
src/
├── index.ts              # Public API exports
├── internal.ts           # @fxtr/core/internal exports (arenas, unsafe symbols)
├── util/
│   └── eager.ts          # Eager<T>, PromiseOrEager<T>
├── core/
│   ├── entityCore.ts     # FxtrID, CBOR encoding, tagged ID classes, types
│   └── visitor.ts        # CBOR tree traversal and ID projection
├── arena/
│   ├── types.ts          # Abstract interfaces: ReadableArena, ReadWriteArena, DeepFetchable
│   └── memoryArena.ts    # In-memory implementation: MemoryStore, MemoryArena
├── scope/
│   ├── entityRef.ts      # Entity, EntityRef, codecs, FXTR_CODEC/FXTR_CACHEABLE, UnknownEntity
│   ├── unionLoadCodec.ts # unionLoadCodec (separate file to avoid circular deps)
│   ├── unsafeSymbols.ts  # UNSAFE_ATTACH_FROM_EXTERNAL_ARENA symbol
│   └── scope.ts          # FxtrScope, withFxtrScope, GLOBAL_SCOPE, caching, keep-alive
├── common/
│   ├── bicodec.ts        # Declarative codec builder: FieldCodec, entity(), field factories
│   ├── pyCompatSet.ts    # PyCompatSet class + pyCompatSet() field codec
│   └── systemEntities.ts # FxtrBasicType, defineType(), trackBuiltinTypeId()
└── namespace/
    └── readNamespace.ts  # FxtrReadNamespace interface

tests/
├── core/
│   ├── smoke.test.ts
│   ├── entityCore.test.ts
│   └── visitor.test.ts
├── arena/
│   └── memoryArena.test.ts
├── scope/
│   ├── entityRef.test.ts
│   └── scope.test.ts
├── common/
│   ├── bicodec.test.ts
│   └── crossLanguageConsistency.test.ts
└── util/
    └── eager.test.ts
```
