# Stage 3: TypeScript Port

## Goal

Create an idiomatic TypeScript package (`v2/ts/`) that ports the core fxtr2 system — entity IDs, CBOR encoding, arenas, scopes, entity refs, and the HTTP client/server — enabling interoperability between Python and TypeScript over HTTP.

## What we're porting

| Python module | TS equivalent | Notes |
|---|---|---|
| `entity_core.py` | `src/core/entityCore.ts` | FxtrID, CBOR encoding/decoding, node types, tags |
| `abstract_arena.py` | `src/arena/types.ts` | ReadableArena, CopyTargetArena, FxtrEntityFlatData, DepKind |
| `memory_arena.py` | `src/arena/memoryArena.ts` | MemoryStore, MemoryArena |
| `arena_util.py` | `src/core/visitor.ts` | visit_ids, visit_and_project_ids |
| `entity_ref_scope.py` | `src/scope/scope.ts`, `src/scope/entityRef.ts` | FxtrScope, EntityRef, Entity protocol, codecs |
| `conversion/converters/mashumaro_entities.py` | `src/common/bicodec.ts` | Declarative codec builder (replaces mashumaro + DataclassEntity) |
| `system_entities.py` | `src/common/systemEntities.ts` | ContextEntity, PathRootEntity, FxtrBasicType, define_type |
| `experimental/http/asgi_app.py` | `src/http/server.ts` | HTTP route handlers |
| `experimental/http/client.py` | `src/http/client.ts` | HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace |
| `experimental/read_namespace.py` | `src/http/readNamespace.ts` | FxtrReadNamespace ABC |

## What we're NOT porting

- Filesystem stores (git, sqlite, segment files)
- Read/write namespaces (FxtrNamespace, FxtrQueryableNamespace)
- Store indexing and SQL querying
- Mashumaro-based serialization (replaced by store codec / load codec system)
- Type annotation handler registry (base_registry.py)
- RPC system
- Legacy compatibility layer
- `_provenance_parent` automatic tracking on FxtrID (provenance node *types* are ported; the transparent tracking via private property is not)
- `isolated` scope flag (no contextvars in TS, so all scopes are effectively isolated)
- `ACTIVE_SCOPE` / `get_active()` context variable
- `as_active_scope()` — no active scope concept
- `deep_load` / `EntityRef.val` — sync access sugar that doesn't translate well without context vars
- Global entity load registry (`_ENTITY_LOAD_REGISTRY`) — codecs are passed explicitly instead

## What we're adapting

### Entity store codec / load codec system (replaces mashumaro + type registry)

In Python, entities serialize via `Entity._fxtr_store_impl()` and deserialize via `EntityLoadHandler` looked up from a global registry keyed by type annotation. In TypeScript, we split this into two explicit pieces:

#### Encoding (store-side): `Symbol.fxtrCodec`

Every storable entity object carries a `[FXTR_CODEC]` symbol property pointing to an `EntityStoreCodec<T>`:

```typescript
const FXTR_CODEC: unique symbol = Symbol('fxtrCodec');

interface EntityStoreCodec<T> {
  /** Encode the value to CBOR-ready data, store it into the scope, and return
   *  an EntityRef with the appropriate load codec attached.
   *  This mirrors Python's Entity._fxtr_store_impl() -> EntityRef[Self].
   *  Async because content-hash computation uses the Web Crypto API. */
  store(value: T, scope: FxtrScope): Promise<EntityRef<T>>;
}
```

When `scope.store(entity)` is called, it reads `entity[FXTR_CODEC]` and delegates to `codec.store(entity, scope)`. The codec is responsible for encoding to CBOR, calling `scope.storeCbor()`, and returning an `EntityRef<T>` with whatever load codec it chooses. This means the store codec doesn't need to pre-declare its load codec — it's determined at store time, just like in Python.

Note: `store()` is async in TS (unlike Python where it is sync) because SHA-256 hashing uses the Web Crypto API (`crypto.subtle.digest`), which is async. See "Async store operations" below for details.

#### Decoding (load-side): `EntityLoadCodec<T>`

Load codecs are the TS equivalent of Python's `EntityLoadHandler`. They are passed explicitly (no global registry):

```typescript
interface EntityLoadCodec<T> {
  /** Check if this load codec can handle a given $fxtr.type ID.
   *  Async because some codecs may need to load the type entity to inspect it
   *  (e.g. dynamic type objects). */
  supportsEntityType(typeId: FxtrID, scope: FxtrScope): Promise<boolean>;

  /** Decode raw CBOR to a rich object. Async to support future auto-loading.
   *  The load codec should attach [FXTR_CODEC] to the returned object for round-trip support. */
  decode(cbor: FxtrCbor, scope: FxtrScope): Promise<T>;
}
```

`EntityRef<T>` carries the load codec:

```typescript
class EntityRef<T> {
  readonly entityId: FxtrID;
  readonly loadCodec: EntityLoadCodec<T>;
}
```

`scope.load(ref)` calls `ref.loadCodec.decode(cbor, scope)`.

#### Union dispatch

`unionLoadCodec(codecA, codecB, ...)` creates a combined load codec that inspects `$fxtr.type` and dispatches to the first codec where `supportsEntityType()` returns true. Return type is the union of all codec types.

#### Default "unknown" load codec

An `unknownLoadCodec` accepts any `$fxtr.type` and returns the raw frozen CBOR object (analogous to Python's `UnknownEntity`). This is what you use when you just want the data without transformation.

#### Round-trip binding

The store codec produces a ref with a load codec; the load codec attaches a store codec to decoded entities:
- **Store path**: `entity[FXTR_CODEC].store(entity, scope)` → stores CBOR → returns `EntityRef<T>` with load codec
- **Load path**: `ref.loadCodec.decode(cbor, scope)` → entity with `[FXTR_CODEC]` attached → ready to re-store

### Context managers → callback pattern

Python's `async with FxtrScope() as scope:` becomes:

```typescript
await withFxtrScope(options, async (scope) => {
  // scope is valid within this callback
  // cleanup happens after callback completes
});
```

No manual `new FxtrScope()` / `scope.close()` API — the closure callback is the only way to create and use scopes, preventing scope leaks. For long-lived scopes (e.g., HTTP server), the callback simply doesn't return until the server shuts down.

### No context variables

All APIs that implicitly use the active scope in Python take an explicit `scope` parameter in TypeScript:

| Python | TypeScript |
|---|---|
| `entity.store()` | `await scope.store(entity)` or `await entity.store(scope)` |
| `ref.load()` | `await scope.load(ref)` or `await ref.load(scope)` |
| `FxtrScope.get_active()` | (pass scope explicitly) |
| `ref.copy_to(other_scope)` | `await targetScope.copyFromScope(ref, sourceScope)` or `await ref.copy({from: sourceScope, to: targetScope})` |

Both `scope.store(entity)` / `entity.store(scope)` and `scope.load(ref)` / `ref.load(scope)` are equivalent — just a matter of which reads better at the call site.

### Async store operations

In Python, `store()` is sync because `hashlib.sha256()` is sync. In TypeScript, SHA-256 hashing uses the Web Crypto API (`crypto.subtle.digest`), which is async. This propagates outward: `scope.store()`, `scope.storeCbor()`, `EntityStoreCodec.store()`, and `entity.store(scope)` are all async (return `Promise`).

Morally, store operations are still "fast" — they only compute a hash and store data in memory. The async boundary is a platform constraint, not a fundamental one. Loading and cross-scope copying can be genuinely "slow" (network I/O, transitive walks). In Python this speed distinction is encoded via sync/async; in TS it is a documentation-level convention since both use `Promise`.

### DeepFetchable (replaces ObservableStore)

Python's `ObservableStore` has `try_lookup()` and `backrefs()`. In TS, we simplify to a `DeepFetchable` interface with just `tryLookup()`:

```typescript
interface DeepFetchable {
  tryLookup(nodeId: FxtrID): Promise<ReadableArena | null>;
}
```

`backrefs()` is not ported — it's an implementation detail on the Python side used mainly for visualization, and not needed for core functionality or HTTP interop.

### HTTP client: browser + Node.js

The HTTP client accepts a `fetch`-compatible function (available in both modern Node.js 18+ and browsers). No environment-specific wrappers needed — the caller provides their own `fetch` (or a wrapper with a base URL baked in).

## Package structure

```
v2/ts/
├── package.json          # pnpm, typescript, vitest, biome
├── tsconfig.json
├── biome.json            # Linting + formatting config
├── src/
│   ├── index.ts          # Public API re-exports
│   ├── core/
│   │   ├── entityCore.ts # FxtrID, CBOR encode/decode, tagged ID classes, FxtrCbor types
│   │   └── visitor.ts    # visit_ids, visit_and_project_ids for walking CBOR trees
│   ├── arena/
│   │   ├── types.ts      # ReadableArena interface, DepKind, FxtrEntityFlatData
│   │   └── memoryArena.ts # MemoryStore, MemoryArena (in-memory implementation)
│   ├── scope/
│   │   ├── scope.ts      # FxtrScope (lifecycle, hierarchy, freezing, deep fetch, caching)
│   │   └── entityRef.ts  # EntityRef<T>, EntityLoadCodec<T>, EntityStoreCodec<T>, FXTR_CODEC symbol
│   ├── common/
│   │   ├── bicodec.ts    # Declarative codec builder (entity definitions, field types, constructors)
│   │   ├── pyCompatSet.ts # PyCompatSet class + pyCompatSet() field codec
│   │   └── systemEntities.ts # ContextEntity, PathRootEntity, FxtrBasicType, defineType()
│   └── http/
│       ├── readNamespace.ts  # FxtrReadNamespace interface
│       ├── client.ts      # HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace
│       └── server.ts      # HTTP route handlers (mountable)
└── tests/
    ├── core/
    │   ├── entityCore.test.ts
    │   └── visitor.test.ts
    ├── arena/
    │   └── memoryArena.test.ts
    ├── scope/
    │   ├── scope.test.ts
    │   ├── entityRef.test.ts
    │   └── frozenScope.test.ts
    └── http/
        ├── client.test.ts
        └── server.test.ts
```

## Dependencies

Runtime:
- **cbor2** — CBOR encoding/decoding with custom tag support and RFC 7049 canonical key sorting
- **hono** — Minimal HTTP framework for the server (works in Node.js, edge runtimes, etc.)

Dev:
- **vitest** — Testing
- **biome** — Linting + formatting (replaces eslint + prettier with a single fast tool)
- **typescript** — Type checking

## Scope design details

### Hierarchy

Scopes form a tree. Each scope has an optional set of "included" parent scopes. A child scope can access anything tracked in its parents. When a scope closes, it cleans up entities not referenced by any surviving scope.

Since there are no context variables, there is no "active scope" — scopes are always passed explicitly. The `isolated` flag from Python is unnecessary (all scopes are effectively isolated).

### Freezing

`scope.freeze()` makes a scope immutable — no further store/load/lift/copy/abandon operations allowed. Read-only operations (getCachedValue, isTracked) remain allowed. Freezing requires all parent scopes to already be frozen. Useful for HTTP server backing scopes and component rendering.

### Deep fetchability

`scope.deepFetch(nodeId)` searches the scope's memory store, parent stores, and any attached `DeepFetchable` instances (in `deepFetchSources`). This is how the HTTP server resolves arbitrary IDs. When a source finds an entity, it's adopted into the scope's memory store for caching.

### Caching

Each scope has a `refLoadedValueCache` for immutable entities. Cache entries propagate to child scopes on lift/copy.

## Detailed spec docs

These specs are filled out before or during implementation of each phase:

| Spec | Covers | Status |
|---|---|---|
| [cbor_encoding.md](cbor_encoding.md) | cbor2 tag mapping, canonical encoding, in-memory representation | Done |
| [memory_arena.md](memory_arena.md) | TS-specific considerations for MemoryStore/MemoryArena 1:1 port | Done |
| [entity_codec_system.md](entity_codec_system.md) | EntityStoreCodec, EntityLoadCodec, FXTR_CODEC, round-trip binding | Done |
| [scope_api.md](scope_api.md) | FxtrScope, withFxtrScope, hierarchy, freezing, deep fetch, caching | Done |
| [bicodec.md](bicodec.md) | Declarative codec builder for entity type definitions | Done (partially superseded by entity_codec_refactor) |
| [entity_codec_refactor.md](entity_codec_refactor.md) | Entity constraint tightening, FXTR_CACHEABLE, DeserializedEntity removal | Done |
| [frozen_cbor_refactor.md](frozen_cbor_refactor.md) | Unify FxtrCbor as readonly, remove FrozenFxtrCbor/UnknownMutableEntity | Done |
| [http_protocol.md](http_protocol.md) | Wire format, Hono server, fetch-based client | TODO |

## Implementation order

The dependency graph dictates the order. The arena layer is independent of codecs/scopes, so it can be fully implemented and tested before we touch the entity system. The codec and scope layers are tightly coupled and should be implemented together.

### Phase 1: Project setup
1. [x] pnpm init, tsconfig, biome, vitest, pre-commit hooks

### Phase 2: Core layer (spec: [cbor_encoding.md](cbor_encoding.md))
2. [x] **`core/entityCore.ts`**: FxtrID, CBOR encode/decode with fxtr tags, tagged ID classes, FxtrCbor types
3. [x] **`core/visitor.ts`**: visitIds, visitAndProjectIds, canonical-order variants
4. [x] Tests for core layer (54 entityCore + 17 visitor = 71 tests)
5. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Phase 3: Arena layer (spec: [memory_arena.md](memory_arena.md))
7. [x] **`arena/types.ts`**: ReadableArena, CopyTargetArena, ReadWriteArena, DeepFetchable, DepKind, FxtrEntityFlatData
8. [x] **`arena/memoryArena.ts`**: MemoryStore, MemoryArena (1:1 port with refcounting/GC)
9. [x] Tests for arena layer (35 tests)
10. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Phase 4: Scope + codec primitives (specs: [entity_codec_system.md](entity_codec_system.md), [scope_api.md](scope_api.md))
11. [x] **`scope/entityRef.ts`**: EntityRef, EntityLoadCodec, EntityStoreCodec, FXTR_CODEC symbol, unknownLoadCodec, unionLoadCodec
12. [x] **`scope/scope.ts`**: FxtrScope with full lifecycle, hierarchy, freezing, deep fetch, caching, store/load/lift/copy/abandon
13. [x] Unit tests for scope + codec primitives (using hand-rolled codecs) — 75 tests (18 entityRef + 57 scope)
14. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Phase 5: Codec definition tooling (spec: [bicodec.md](bicodec.md))
15. [x] **`common/bicodec.ts`**: Declarative codec builder (entity definitions, field types, constructors)
16. [x] **`common/pyCompatSet.ts`**: PyCompatSet class + pyCompatSet() field codec
17. [x] **`common/systemEntities.ts`**: FxtrBasicType, defineType (using bicodec). ContextEntity and PathRootEntity deferred (depend on `unknown()`/Any field design).
18. [x] Integration tests: store/load round-trips with bicodec-defined entities, FxtrBasicType, scope interactions (54 tests)
19. [x] Cross-language consistency tests (hardcoded IDs in both Python and TS — 7 tests each)
20. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Phase 5.5: Entity codec refactor (spec: [entity_codec_refactor.md](entity_codec_refactor.md))
21. [x] Tighten Entity/EntityLoadCodec/EntityRef type system: non-generic Entity interface, `T extends Entity` bounds, FXTR_CACHEABLE on Entity, remove DeserializedEntity
22. [x] UnknownEntity/UnknownMutableEntity wrapper types (matching Python), update unknownLoadCodec/unknownMutableLoadCodec
23. [x] EntityDefinition extends EntityLoadCodec directly; remove resolveLoadCodec and .loadCodec
24. [x] Update all tests for new type contracts
25. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Phase 5.6: Frozen CBOR refactor (spec: [frozen_cbor_refactor.md](frozen_cbor_refactor.md))
26. [x] Unify `FxtrCbor` as always-readonly, remove `FrozenFxtrCbor` type alias
27. [x] Remove `UnknownMutableEntity`/`unknownMutableLoadCodec`, remove `structuredClone` thawing
28. [x] Merge `loadCbor`/`loadFrozenCbor` into single `loadCbor` method
29. [x] Update tests and `v2/ts/CODE_OVERVIEW.md`

### Phase 6: HTTP layer (spec: [http_protocol.md](http_protocol.md))
19. [x] **`http/readNamespace.ts`**: FxtrReadNamespace interface
20. [x] **`http/client.ts`**: HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace
21. [x] **`http/server.ts`**: Hono route handlers
22. [x] Tests for HTTP layer (39 tests: 14 server + 25 client)
23. [x] Update `v2/ts/CODE_OVERVIEW.md`

### Phase 7: Wrap up
24. [x] Update `v2/CODE_OVERVIEW.md` to reference the TS package

## Process notes

- **Tests as you go**: Tests are written alongside each component, not deferred to the end. Each phase includes a testing step, but in practice tests should be written as each file is implemented.
- **TS CODE_OVERVIEW.md**: The TypeScript package has its own `v2/ts/CODE_OVERVIEW.md` describing the current state of the TS codebase. This is updated at the end of each phase to reflect what was built. Like the Python CODE_OVERVIEW, it should describe the current state for someone coming to it fresh — not a chronological log.
- **Spec before implementation**: Each phase's spec doc should be filled out before starting implementation of that phase.

## MemoryArena design

See [memory_arena.md](memory_arena.md) for full details. Key points:

- **MemoryStore**: 1:1 port with node states (StoredNode, DerivedNode, InheritedNode), refcounting/GC, arena lifecycle. Implements `DeepFetchable` (replaces `ObservableStore`). Store operations are async due to SHA-256.
- **MemoryArena**: View into the store with a tracked liveset. Supports `unsafeLazilyAttachFromArena()`, `copyFromArena()`, `copyToNewArena()`, `loadFlat()`.
- **InMemory only**: No LMDB backing store. No StreamSink/MemoryStreamSink. No debug logging or leak detection (can add later).
- Same refcounting/GC semantics as Python: when an arena closes, its tracked entities are decremented; when refcount hits zero, entities are cleaned up.

## Open questions / future work

- **Auto-loading inline entities**: The async load codec interface supports this, but we don't need to build the `Child<T>` / `LinkedEntity<T>` pattern in this iteration.
- **React integration**: Sugar for using scopes in React components (e.g., a `useFxtrScope()` hook) is a future extension.
- **LocalStorage/IndexedDB store**: A browser-native persistent store could be added later.
- **RPC port**: The RPC system will be ported separately once it stabilizes in Python.
- **CBOR byte-level compatibility**: We should verify that `cbor2` (npm) with `sortLengthFirstDeterministic` produces byte-identical output to Python's `cbor2.dumps(canonical=True)` for representative test cases during Phase 2 implementation.
