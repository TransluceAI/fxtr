# Fxtr2 TypeScript Core — Code Overview

`v2/ts/packages/` holds the core fxtr2 TypeScript packages: an
idiomatic TS port of the fundamental fxtr data layer (entity IDs, CBOR
encoding, arenas, scopes, entity refs) plus the experimental HTTP
client/server and the MDX→FxtrCbor transform. These enable
interoperability with the Python implementation over HTTP.

These packages are somewhat out of date relative to the latest fxtr2
design point, but are still usable and worth keeping. The newer
frontend substrate (`@fxtr-ui/*`) is built from scratch and lives in
[`projects/fxtr_workstation/frontend_lib/`](../../projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md);
it does **not** depend on these packages.

The older React bindings, reports framework, example apps, and the
`@fxtr-workstation/*` panel-viewer prototype that used to live
alongside these packages have been archived under
[`spikes/old_typescript/`](../../spikes/old_typescript/CODE_OVERVIEW.md)
(they were subsumed by `@fxtr-ui/*`).

## Workspace

These packages are part of the **repo-root pnpm workspace** (see
[`../../pnpm-workspace.yaml`](../../pnpm-workspace.yaml); `v2/ts/` is
**not** its own workspace). The same workspace also contains the
`@fxtr-ui/*` `frontend_lib` packages, the report-builder CLI, the
workstation-demo frontend, and the archived `spikes/old_typescript/*`
packages. One `pnpm install` from the repo root links everything; one
`pnpm check` / `pnpm test` / `pnpm lint` from there covers all of it.
Shared tooling config (`tsconfig.base.json`, `tsconfig.json`,
`vitest.config.ts`, `biome.json`, `package.json` scripts) lives at the
repo root.

Packages (all under `v2/ts/packages/`):

- **`@fxtr/core`** (`fxtr/`) — Core entity system: IDs, CBOR encoding, arenas, scopes, entity refs, codecs, system entities, namespaces. Minimal dependencies (`cbor2`), environment agnostic. See `LOCAL_CODE_OVERVIEW.md` for arena internals and implementation details.
- **`@fxtr/http-server`** (`fxtr-http-server/`) — Hono-based HTTP server for serving fxtr entities. Dependencies: `@fxtr/core`, `cbor2`, `hono`.
- **`@fxtr/http-client`** (`fxtr-http-client/`) — HTTP client for consuming fxtr HTTP APIs. Dependencies: `@fxtr/core`, `@fxtr/http-server` (types), `cbor2`.
- **`@fxtr/mdx-transform`** (`fxtr-mdx-transform/`) — MDX-to-FxtrCbor transform library. Custom JSX runtime that compiles MDX into FxtrCbor tuple trees. Dependency: `@fxtr/core`. See `LOCAL_CODE_OVERVIEW.md`.

**Tooling:**
- **Package manager**: pnpm (repo-root workspace)
- **Build**: TypeScript project references (`tsc --build`). Shared config in repo-root `tsconfig.base.json`, per-package `tsconfig.json`.
- **Tests**: vitest, single repo-root config discovering `packages/*/tests/**/*.test.{ts,tsx}` across the workspace roots.
- **Lint/format**: biome (shared `biome.json` at repo root)
- **Pre-commit hooks**: pnpm lockfile check, biome lint, tsc typecheck (in repo root `.pre-commit-config.yaml`)
- **tsconfig lib**: `["ES2022", "DOM", "ESNext.Disposable"]` — DOM is included for `TextEncoder`/`TextDecoder` types (available in browsers and Node.js 18+), ESNext.Disposable enables `Symbol.asyncDispose` types for scope lifecycle management
- **Package exports**: Currently point to `.ts` source files (e.g. `./src/index.ts`), which works for workspace development. To publish, switch exports to built output (`./dist/src/index.js` + `./dist/src/index.d.ts`).

## File Structure

```
v2/ts/
├── .gitignore
├── CODE_OVERVIEW.md
└── packages/
    ├── fxtr/                  # @fxtr/core              (see LOCAL_CODE_OVERVIEW.md for internals)
    ├── fxtr-http-server/      # @fxtr/http-server
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── src/
    │   │   ├── index.ts
    │   │   └── server.ts          # createFxtrApp(), Hono routes
    │   └── tests/
    │       └── http/
    │           └── server.test.ts
    ├── fxtr-http-client/      # @fxtr/http-client
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── src/
    │   │   ├── index.ts
    │   │   └── client.ts         # HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace
    │   └── tests/
    │       └── http/
    │           └── client.test.ts
    └── fxtr-mdx-transform/    # @fxtr/mdx-transform     (see LOCAL_CODE_OVERVIEW.md)
```

## Package Entrypoints

### `@fxtr/core` (public API)

```typescript
import { FxtrID, FxtrScope, withFxtrScope, EntityRef, ... } from "@fxtr/core";
```

Exports from core, scope, common, and namespace modules. Everything an end user needs to define entities, create scopes, store/load data.

### `@fxtr/core/internal`

```typescript
import { MemoryStore, MemoryArena, ReadableArena, FXTR_SCOPE_INTERNALS, ... } from "@fxtr/core/internal";
```

Arena types and scope internals. Used by `@fxtr/http-client` and `@fxtr/http-server` internally. End users don't normally need these. Importing `FXTR_SCOPE_INTERNALS` gives access to internal scope operations like `deepFetch`, `deepFetchSources`, `unsafeAttachFromExternalArena`, `mainArena`, `backingStore`, and `state` via `scope[FXTR_SCOPE_INTERNALS]`.

### `@fxtr/core/bicodec`

```typescript
import * as bicodec from "@fxtr/core/bicodec";
const MyEntity = bicodec.entity(typeId, { name: bicodec.string() });
```

Dedicated entrypoint for the bicodec module. Field codec names (`string`, `number`, etc.) are too generic for the top-level namespace, so bicodec is imported as a namespace.

## `@fxtr/core` — Public API

For internal implementation details (arenas, memory store, unsafe symbols), see `packages/fxtr/LOCAL_CODE_OVERVIEW.md`.

### `util/eager.ts`

Synchronous thenable for avoiding unnecessary async context switches. Used throughout the codebase as the return type for operations that may or may not resolve synchronously.

- **`Eager<T>`**: A synchronous thenable. Always deterministically contains either a resolved value or a rejected error. `.then()` calls callbacks immediately (not on the microtask queue). `await eager` works but yields a microtask — use `.unwrap()` or `.then()` chaining for the sync benefit. Error propagation mirrors Promise semantics exactly.
- **`PromiseOrEager<T>`**: Type alias `Promise<T> | Eager<T>`. The standard return type for all load/store/codec operations.
- **`Eager.resolve(value)`**: Mirrors `Promise.resolve` — passes through Eager/Promise, wraps plain values in Eager. Return type: `PromiseOrEager<T>`.
- **`Eager.resolveSync(value)`**: Always returns `Eager<T>`. Throws on Promise/thenable input.
- **`Eager.reject(error)`**: Always returns `Eager<never>`.
- **`Eager.all(values)`**: Like `Promise.all` but returns `Eager` when all inputs are `Eager`. Falls back to `Promise.all` on first `Promise` input.
- **`.unwrap()`**: Returns value synchronously or throws error synchronously.

All arena, scope, codec, and namespace interfaces use `PromiseOrEager<T>` as their return type. Scalar field codecs return `Eager` (purely synchronous). Composite codecs use `Eager.all()` to stay sync when all children are sync.

### `core/entityCore.ts`

The foundation of the system. Ported from Python's `entity_core.py` as a single file (avoids circular dependency between FxtrID and CBOR tag classes). Contains:

- **`FxtrID`**: Immutable entity identifier. Holds raw ID bytes (tag byte + payload). Uses `idString()` / `key()` as canonical string representation for Map/Set keys. Supports all node types: `.` (here), `!` (builtin), `d` (data), `c` (context), `n` (normal), `p` (path), `u` (untracked).
- **`RawWeakFxtrID`** (tag 33001), **`RawStrongFxtrID`** (tag 33002), **`RawFxtrEntityMarkerID`** (tag 33000): Classes wrapping a FxtrID with a CBOR tag number. Each implements `toCBOR()` for automatic encoding by the `cbor2` library.
- **`RawFxtrRef`**: Union type of all three tagged ID classes.
- **`FxtrCbor`**: Type definition for fxtr-tagged CBOR data. Always readonly at the type level (containers use `readonly` modifiers). Runtime immutability can be enforced via `deepFreeze()` where needed. Mutable locals (e.g. `Record<string, FxtrCbor>`) are assignable to `FxtrCbor`, so construction works naturally.
- **`fxtrCborDumps()` / `fxtrCborLoads()`**: Encode/decode with fxtr tags using `cbor2` npm package with RFC 7049 canonical key sorting (`sortLengthFirstDeterministic`).
- **`deepFreeze()`**: Recursively freeze a CBOR value for immutability.
- **`InvalidEntityError` / `DuplicateEntityMarkerError`**: Error types for entity validation.

### `core/visitor.ts`

CBOR tree traversal utilities. Ported from Python's `arena_util.py`. Uses `instanceof` checks on the tagged ID classes for dispatch.

- **`visitIds(root, callback)`**: Walk a CBOR tree, calling a callback for each tagged ID (weak, strong, or marker).
- **`visitAndProjectIds(root, callback)`**: Walk and transform all tagged IDs, returning a new tree with transformed IDs. Preserves ref type (weak/strong/marker).
- **`visitAndProjectIdsInCanonicalCborOrder(root, callback)`**: Same but processes IDs in canonical CBOR serialization order (encode then decode with projection). Needed for p-nodes where path indices refer to byte-order positions.
- **`asyncVisitAndProjectIdsInCanonicalCborOrder(root, callback)`**: Async version using three-pass approach: collect IDs in canonical order → await all callbacks sequentially → reconstruct.

### `scope/entityRef.ts`

Entity codec system — the TS equivalent of Python's `Entity` base class, `EntityLoadHandler`, and `EntityRef`. The biggest divergence from Python: no global registry; codecs are carried explicitly on `EntityRef` instances.

- **`FXTR_CODEC`**: Unique symbol used to attach a store codec to entity objects.
- **`FXTR_CACHEABLE`**: Unique symbol indicating whether a loaded entity is immutable and safe to cache. If absent or false, each `load()` call re-decodes from CBOR.
- **`Entity`**: Non-generic interface. Every entity carries `[FXTR_CODEC]: EntityStoreCodec<this>` (polymorphic `this` type) and optional `[FXTR_CACHEABLE]`. All `EntityLoadCodec<T>`, `EntityStoreCodec<T>`, and `EntityRef<T>` require `T extends Entity`.
- **`EntityStoreCodec<T extends Entity>`**: Encodes a value to CBOR, stores it via `scope.storeCbor()`, returns an `EntityRef<T>`. Async (SHA-256).
- **`EntityLoadCodec<T extends Entity>`**: Decodes CBOR to a rich entity object. Has `supportsEntityType(typeId, scope)` for type dispatch and `decode(cbor, scope)`.
- **`EntityRef<T extends Entity>`**: Reference bundling an entity ID and a load codec. Methods: `load(scope)`, `withLoadCodec(codec)`, `copy({from, to})`, `lift(scope)`, `equals(other)`. Equality is by entity ID string + codec reference identity.
- **`UnknownEntity`**: Wrapper class for CBOR data whose type is not known. Has `raw_cbor: FxtrCbor` (deep-frozen at runtime), `[FXTR_CACHEABLE] = true`.
- **`unknownLoadCodec`**: Accepts any type, returns `UnknownEntity`. Cacheable. Used by `scope.loadCbor()`.

### `scope/unionLoadCodec.ts`

- **`unionLoadCodec(...codecs)`**: Dispatches to the first codec where `supportsEntityType` returns true. Uses `cachedSupportsEntityType` for cached type checks with scope isolation.

### `scope/scope.ts`

Entity lifetime management. Ported from Python's `entity_ref_scope.py`.

- **`FxtrScope`**: User-facing handle for a scope. Public operations:
  - **Store** (`PromiseOrEager`): `store(entity)`, `storeCbor(cbor)`.
  - **Load** (`PromiseOrEager`): `load(ref)`, `loadCbor(nodeId)`. Returns `Eager<T>` for cache hits; `Promise<T>` when async I/O is needed.
  - **Tracking**: `lift(node)`, `trackBuiltin(id)`.
  - **Copy**: `copyFromScope(node, sourceScope)`.
  - **Abandon**: `abandon(node)`.
  - **Read-only**: `isTracked(node)`, `getCachedValue(ref)`, `isAlive()`, `includedInScope(other)`, `getContextMarkerId()`.
  - **Freezing**: `freeze()`, `frozen`. Requires all parents frozen. After freezing: only read-only ops allowed.
  - **Lifecycle**: `scheduleClose()` (sync, safe for React cleanup), `dispose()` (async, waits for children). Three-state: alive → closing → closed. `Symbol.asyncDispose` supported if runtime allows.
  - **Internal API** (`scope[FXTR_SCOPE_INTERNALS]`): Symbol-gated access to internal operations. Import `FXTR_SCOPE_INTERNALS` from `@fxtr/core/internal` to opt in. Provides: `deepFetch(nodeId)`, `deepFetchSources`, `unsafeAttachFromExternalArena(nodeId, source)`, `mainArena`, `backingStore`, `state`. Used by `@fxtr/http-server` and `@fxtr/http-client`.
- **`createFxtrScope(opts)`**: Creates a scope. The caller manages cleanup via `scheduleClose()` or `dispose()`.
- **`withFxtrScope(opts, callback)`**: Convenience wrapper — creates scope, runs callback, cleans up on exit.
- **`GLOBAL_SCOPE`**: Permanently-open root scope for type builtins.
- **`cachedSupportsEntityType(codec, typeId, scope)`**: Cached type check with scope isolation.
- **`EntityNotLoadedError`**: Thrown by `getCachedValue` when a ref is not cached.

### `common/bicodec.ts`

Declarative codec builder for defining fxtr entity types. The TS equivalent of Python's mashumaro/DataclassEntity system. Provides two layers:

1. **Field codecs** (`FieldCodec<T>`) — bidirectional converters between TS values and FxtrCbor fragments. Each codec has a `kind` string tag for introspection. All `toCbor`/`fromCbor` methods return `PromiseOrEager`.
   - **Scalars**: `string()`, `number()`, `boolean()`, `bytes()`, `unknown()`. Passthrough codecs. Return `Eager`.
   - **FxtrID**: `fxtrId()` (weak, tag 33001), `childFxtrId()` (strong, tag 33002). Return `Eager`.
   - **Entity refs**: `entityRef(codec)`, `childRef(codec)`. Create `EntityRef` instances. Return `Eager`.
   - **Composites**: `object(schema)`, `array(item)`, `record(value)`, `nullable(inner)`. Sync when all children are sync.
   - **Combinators**: `mapped(inner, toTs, fromTs, opts?)` for type transforms, `lazy(fn)` for recursive types.
   - **Inline children**: `child(codec)` (strong), `linked(codec)` (weak). Auto-store on encode, auto-load on decode.

2. **Entity definitions** (`EntityDefinition<I> extends EntityLoadCodec<I>`) — wraps a field schema into a combined store + load codec with factory.
   - **`entity(typeId, schema)`**: Factory that returns an `EntityDefinition` with `.create()`, `.typeId`, `.supportsEntityType()`, and `.decode()`. The definition *is* the load codec.
   - **`create(fields)`**: Synchronous, returns a mutable entity instance with `[FXTR_CODEC]` and `.store(scope)`.
   - **Type inference**: `InferEntity<typeof _MyEntity>` extracts the instance type from a definition. Use with `interface extends` for named types: `interface MyEntity extends InferEntity<typeof _MyEntity> {}`, then `const MyEntity: EntityDefinition<MyEntity> = _MyEntity`. `FieldsOf<I>` extracts the fields type. `InferField<C>` extracts field type from a codec.

### `common/pyCompatSet.ts`

`PyCompatSet<T>` — a set-like collection backed by `Map<string, T>`. Needed because JS `Set` uses reference equality, which doesn't work for `FxtrID` or `EntityRef` objects.

- **`PyCompatSet<T>`**: Constructor takes a key function and optional initial items. Methods: `has`, `add`, `delete`, `values`, `size`, `[Symbol.iterator]`, `toArray`, `freeze`. Mutable by default; `freeze()` makes add/delete throw.
- **Key functions**: `stringKeyFn`, `numberKeyFn`, `bytesKeyFn`, `fxtrIdKeyFn`, `entityRefKeyFn`.
- **`pyCompatSet(itemCodec)`**: Field codec factory. Serializes as a sorted array matching Python's `_canonical_set_sort_key`. On decode, returns a frozen `PyCompatSet`.

### `common/systemEntities.ts`

- **`FxtrBasicType`**: Entity definition for the bootstrap type entity. Type ID is `!basic_type` (builtin). Fields: `namespace_uuid` (string), `type_name` (string).
- **`defineType(namespaceUuid, typeName, scope?)`**: Creates a content-addressed type ID by storing a `FxtrBasicType`. Returns `Promise<FxtrID>`. Deterministic: same namespace+name always produces the same ID. Defaults to `GLOBAL_SCOPE`.

### `namespace/readNamespace.ts`

- **`FxtrReadNamespace`**: Interface with `scope`, `retrieve(key)` / `retrieve(key, loadCodec)`, `tryRetrieve(...)`, `listPins(prefix?)`. When a `loadCodec` is provided, returns `EntityRef<T>` instead of raw `FxtrID`. This replaces Python's `type` parameter (no global type registry in TS).
- **`RetrieveOpts`**: Options for `retrieve`/`tryRetrieve`: `intoScope` (defaults to namespace scope) and `copyIntoScope` (copy vs lift).

### `http/server.ts` (`@fxtr/http-server`)

Hono-based HTTP server for serving fxtr entities. Port of Python's `asgi_app.py`.

- **`createFxtrApp(scope, pins)`**: Creates a Hono app with four routes. Accepts `Map<string, FxtrID>` or `Record<string, FxtrID>` for pins. No Node.js-specific serve wrappers — the caller decides how to host the Hono app.
- **Routes**:
  - `GET /preview/:id_string` — human-readable JSON pretty-print of entity CBOR
  - `GET /data/:id_string` — entity bytes + transitively-closed precomp map (application/cbor)
  - `GET /pin/*` — pin ID + precomp (application/cbor). Uses wildcard to support slashes in pin names.
  - `GET /pins?prefix=...` — pin listing (application/json)
- **`computePrecompClosure(arena, seedIds)`**: BFS that computes `unwraps_to`, `ctx_node`, and `backstep` for all provenance nodes reachable from the seed IDs.
- **Error handling**: 400 (invalid ID), 404 (not found), 503 (scope closed).

### `http/client.ts` (`@fxtr/http-client`)

HTTP client for consuming a fxtr HTTP server. Port of Python's `client.py`.

- **`FetchFn`**: Type alias for a `fetch`-compatible function. `(url: string, init?: RequestInit) => Promise<Response>`.
- **`withBaseUrl(baseUrl, fetchFn?)`**: Convenience that creates a `FetchFn` prepending a base URL to relative paths. Defaults to `globalThis.fetch`.
- **`HttpReadableArena`**: Implements `ReadableArena` backed by HTTP requests. Uses precomp maps from `/data/` responses for provenance operations (`unwrapProvenance`, `cNodeToContextData`, etc.). Internal state uses `Map<string, ...>` keyed by `id.key()`.
- **`HttpDeepFetchable`**: Implements `DeepFetchable`. `tryLookup` does a GET to `/data/`, creates a fresh `HttpReadableArena` with precomp populated. Replaces Python's `HttpObservableStore` (no `backrefs`).
- **`HttpFxtrReadNamespace`**: Implements `FxtrReadNamespace`. Internally owns an `HttpReadableArena` and adds an `HttpDeepFetchable` to its scope's deep fetch sources.
- **`createHttpReadNamespace(fetchFn)`**: Creates an HTTP-backed read namespace and returns it directly. The caller manages cleanup via `ns.scope.scheduleClose()` or `await ns.scope.dispose()`. Use when the namespace lifetime isn't tied to a single callback (e.g., React apps).
- **`withHttpReadNamespace(fetchFn, callback)`**: Convenience wrapper around `createHttpReadNamespace` + `ns.scope.dispose()`. Creates the namespace, runs the callback, cleans up. This is the primary entry point for consuming an HTTP namespace in scripts and request handlers.

Not ported from Python: `serve_until`, `serve_forever`, `serving_context` (Hypercorn-specific wrappers — the caller hosts the Hono app themselves).

### `@fxtr/mdx-transform`

MDX-to-FxtrCbor transform library. Custom JSX runtime that compiles MDX into FxtrCbor tuple trees. Depends only on `@fxtr/core`. See `packages/fxtr-mdx-transform/LOCAL_CODE_OVERVIEW.md` for the transform pipeline and the JSX runtime details.

## Key Differences from Python

| Aspect | Python | TypeScript |
|--------|--------|-----------|
| CBOR library | `cbor2` (Python) | `cbor2` (npm) |
| Canonical encoding | `cbor2.dumps(canonical=True)` | `encode(value, { sortKeys: sortLengthFirstDeterministic })` |
| CBOR mutability | `dict` vs `frozendict` | `FxtrCbor` is always readonly; `deepFreeze()` for runtime enforcement |
| Immutable dicts | `frozendict` | `{ readonly [key: string]: FxtrCbor }` + `Object.freeze()` |
| Immutable lists | `tuple` | `readonly T[]` + `Object.freeze()` |
| FxtrID equality | Custom `__eq__`/`__hash__` | Use `id.key()` string for Map/Set keys |
| FxtrID provenance | `_provenance_parent` field | Not ported |
| Store operations | sync (`hashlib.sha256`) | `PromiseOrEager` (currently async via `crypto.subtle.digest`, typed for future sync) |
| Load/decode sync path | N/A | `Eager<T>` — loads from memory arena with cached entities resolve synchronously |
| ObservableStore | `try_lookup` + `backrefs` | `DeepFetchable` (`tryLookup` only) |
| Context variables | `contextvars.ContextVar` | Not ported; scope passed explicitly |
| Context managers | `async with` | `withFxtrScope(opts, callback)` |
| Entity serialization | `Entity._fxtr_store_impl()` | `entity[FXTR_CODEC].store(value, scope)` |
| Entity deserialization | Global `EntityLoadHandler` registry | `EntityLoadCodec<T>` on `EntityRef` |
| Union dispatch | `_ENTITY_LOAD_REGISTRY.find_handler_or_union_handlers` | `unionLoadCodec(codecA, codecB, ...)` |
| Cacheable signaling | `DeserializedEntity.cacheable` flag | `[FXTR_CACHEABLE]` symbol on entity object |
| Unknown entity | `UnknownEntity(raw_cbor=...)` class | `UnknownEntity` class with `.raw_cbor` |
| Entity definition | `@dataclass class Foo(DataclassEntity):` | `const Foo = bicodec.entity(typeId, schema)` |
| Field codecs | mashumaro type annotations + strategies | Explicit `FieldCodec<T>` with `kind` tag |
| Type inference | Runtime type annotations | Compile-time `InferEntity<typeof Def>` + `FieldsOf<I>` |
| Set representation | `set` / `frozenset` | `PyCompatSet<T>` (Map-backed, freezable) |
| Inline children | Two-phase collect/rebuild | Direct `await scope.load()` inline |
| Context marker | Set at instantiation via context var | Added at store time from scope |
| HTTP server | Starlette ASGI + Hypercorn | Hono (runtime-agnostic) |
| HTTP client | `httpx.AsyncClient` | `FetchFn` (standard `fetch` signature) |
| Read namespace | `async with HttpFxtrReadNamespace(client) as ns:` | `withHttpReadNamespace(fetchFn, callback)` |
| Namespace retrieve | `type=SomeClass` (global registry lookup) | `loadCodec=SomeEntityDef` (explicit codec) |
| Scope internal access | `scope._main_arena` (private-by-convention) | `scope[FXTR_SCOPE_INTERNALS]` (symbol-gated, import from `@fxtr/core/internal`) |

## Testing

Run from the repo root (covers the whole workspace, not just these packages):

```bash
pnpm test        # run all tests (vitest)
pnpm test:watch  # run tests in watch mode
pnpm check       # type check (tsc --build --force)
pnpm lint        # biome lint
pnpm lint:fix    # biome lint + autofix
pnpm format      # biome format
```
