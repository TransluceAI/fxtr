# HTTP Protocol Spec

Wire format and API for the HTTP client/server — confirming compatibility with the Python implementation.

The goal is a 1:1 port: either client (Python or TS) should be able to connect to either server (Python or TS) and the behavior should be identical. The TS side also acquires a read-only namespace concept (`FxtrReadNamespace`) since it has no persistent namespaces yet.

## Wire format

### Routes

All routes are identical to the Python `asgi_app.py`:

| Route | Response | Content-Type |
|---|---|---|
| `GET /preview/:id_string` | Human-readable entity text | `text/plain` |
| `GET /data/:id_string` | Entity bytes + precomp map | `application/cbor` |
| `GET /pin/:name` | Pin ID + precomp map | `application/cbor` |
| `GET /pins?prefix=...` | Pin listing | `application/json` |

### `/data/:id_string` response format

The response body is **plain CBOR** (not fxtr-tagged, no canonical sorting). It is a map with:

```
{
  "bytes": <Uint8Array>,   // raw fxtr-tagged CBOR bytes (omitted for derived nodes)
  "precomp": {             // omitted if empty
    "<id_string>": {
      "unwraps_to": "<id_string>",  // present for c/p/u/n nodes
      "ctx_node": "<id_string>",    // present for c and p nodes
      "backstep": "<id_string>"     // present for p nodes (when applicable)
    },
    ...
  }
}
```

The `precomp` map is **transitively closed**: starting from the requested entity's deps plus the entity itself, every provenance relationship is walked (BFS), and any newly discovered IDs are also included. This means the client gets everything it needs to implement `unwrapProvenance`, `cNodeToContextData`, `pNodeToPathroot`, and `stepBackPCtx` without additional round-trips.

The inner `bytes` value contains the actual fxtr-tagged CBOR (with tags 33000/33001/33002). Clients decode this with `fxtrCborLoads()`.

### `/pin/:name` response format

Plain CBOR map:

```
{
  "id": "<id_string>",
  "precomp": { ... }       // same format, omitted if empty
}
```

### `/pins?prefix=...` response format

JSON map of `{ "<pin_name>": "<id_string>", ... }`. If `prefix` is omitted, returns all pins.

### `/preview/:id_string` response format

Plain text. For contentful nodes (d/n): a simple string representation of the decoded CBOR (TS can use `JSON.stringify(decoded, null, 2)` or similar — doesn't need to match Python's `pprint.pformat` exactly, just be human-readable). For derived nodes (c/p/u/./!): a text line like `Derived node (tag=X, no content)\nDeps: [...]`.

### Error responses

| Status | Meaning |
|---|---|
| 400 | Invalid ID string |
| 404 | Entity or pin not found |
| 503 | Server scope is closed |

All error bodies are plain text.

## Server: `src/http/server.ts`

A `createFxtrApp(scope, pins)` function that returns a Hono app. No Node.js-specific serve wrappers — the caller decides how to serve the Hono app (e.g., `Bun.serve`, `@hono/node-server`, `Deno.serve`, or just `app.request()` in tests).

### Interface

```typescript
function createFxtrApp(
  scope: FxtrScope,
  pins: ReadonlyMap<string, FxtrID> | Record<string, FxtrID>,
): Hono;
```

`pins` is read on each request (not snapshotted), so callers can mutate the underlying map/object. Using `ReadonlyMap | Record` gives callers flexibility — the implementation reads from whichever is provided.

### Precomp closure computation

Port `_compute_precomp_closure(arena, seedIds)` as a private helper. Same BFS algorithm: for each seed ID, check tag, compute unwraps_to/ctx_node/backstep, add targets to frontier.

### Route handlers

Each handler:
1. Checks `scope.isAlive()` → 503 if not
2. Parses/validates input → 400 if invalid
3. Uses `scope.deepFetch(nodeId)` to locate the entity → 404 if not found
4. Computes response and returns it

The Hono route parameter syntax uses `:param` rather than `{param}`. For the `/pin/` route which needs to match slashes in pin names, we'll use a wildcard or path parameter (Hono supports `*` or `/pin/:name{.+}`).

### CBOR encoding

The server encodes response bodies (for `/data/` and `/pin/`) using the `cbor2` npm package's `encode()` — plain CBOR, no fxtr tags, no canonical sorting. This matches the Python server which uses `cbor2.dumps(result)` without `canonical=True`.

## Client: `src/http/client.ts`

### Fetch function type

The client accepts a fetch-compatible function. We define a narrow type:

```typescript
type FetchFn = (url: string, init?: RequestInit) => Promise<Response>;
```

This matches the standard `fetch` signature (available in browsers and Node.js 18+). The caller provides their own `fetch` — e.g., the global `fetch`, or a wrapper with auth headers baked in.

### Base URL helper

A small convenience for the common case of connecting to a server at a known base URL:

```typescript
function withBaseUrl(baseUrl: string, fetchFn?: FetchFn): FetchFn;
```

Returns a new `FetchFn` that prepends `baseUrl` to relative paths. If `fetchFn` is omitted, uses `globalThis.fetch`. This keeps the core API simple (just `FetchFn`) while making the common browser/Node.js use case easy:

```typescript
const fetchFn = withBaseUrl("http://localhost:8080");
// or with custom fetch:
const fetchFn = withBaseUrl("http://localhost:8080", myAuthFetch);
```

### `HttpReadableArena`

Implements `ReadableArena`. 1:1 port of Python's `HttpReadableArena`.

```typescript
class HttpReadableArena implements ReadableArena {
  constructor(fetchFn: FetchFn);
}
```

Internal state:
- `_tracked: Set<string>` — tracked node IDs (keyed by `id.key()`)
- `_trackedIds: Map<string, FxtrID>` — reverse lookup for key → FxtrID
- `_precompUnwrapsTo: Map<string, FxtrID>` — id key → target
- `_precompCtxNode: Map<string, FxtrID>` — id key → target
- `_precompStepBackPCtx: Map<string, FxtrID>` — id key → target

Uses `Map<string, ...>` keyed by `id.key()` since TS `Set`/`Map` use reference equality.

Methods (all matching the `ReadableArena` interface):
- `isTracked(nodeId)`: Check `_tracked`
- `load(nodeId)`: GET `/data/{id}`, ingest precomp, decode bytes with `fxtrCborLoads`, walk CBOR to track child IDs via `visitIds`
- `loadFlat(nodeId)`: GET `/data/{id}`, ingest precomp, walk CBOR to extract deps with proper `DepKind` classification (strong tag → `STRONG_REF`, weak/marker → `WEAK_REF`)
- `unwrapProvenance(nodeId)`: Lookup in `_precompUnwrapsTo`
- `cNodeToContextData(nodeId)`: Lookup in `_precompCtxNode`
- `pNodeToPathroot(nodeId)`: Lookup in `_precompCtxNode` (same map, same as Python)
- `stepBackPCtx(nodeId)`: Lookup in `_precompStepBackPCtx`
- `abandon(nodeId)`: Remove from tracked + precomp maps
- `copyToNewArena(nodeIds)`: Create new `HttpReadableArena` with same `fetchFn`, copy transitive precomp closure
- `close()`: Clear all maps

Additionally, expose `_ingestPrecomp(precomp)` and `_tracked` for use by `HttpDeepFetchable` and `HttpFxtrReadNamespace` (these are internal but accessed by sibling classes, same pattern as Python).

### `HttpDeepFetchable`

Implements `DeepFetchable`. TS equivalent of Python's `HttpObservableStore` (minus `backrefs`).

```typescript
class HttpDeepFetchable implements DeepFetchable {
  constructor(fetchFn: FetchFn);
  tryLookup(nodeId: FxtrID): Promise<ReadableArena | null>;
}
```

`tryLookup`: GET `/data/{id}`. If 404 or non-200, return `null`. Otherwise, create a fresh `HttpReadableArena`, add the node ID to tracked, ingest precomp, return the arena.

### `FxtrReadNamespace` interface: `src/http/readNamespace.ts`

Abstract interface for read-only namespaces. Port of Python's `FxtrReadNamespace`, adapted for TS:

```typescript
interface FxtrReadNamespace {
  readonly scope: FxtrScope;

  retrieve(key: string): Promise<FxtrID>;
  retrieve<T extends Entity>(
    key: string,
    loadCodec: EntityLoadCodec<T>,
    opts?: { intoScope?: FxtrScope; copyIntoScope?: boolean },
  ): Promise<EntityRef<T>>;

  tryRetrieve(key: string): Promise<FxtrID | null>;
  tryRetrieve<T extends Entity>(
    key: string,
    loadCodec: EntityLoadCodec<T>,
    opts?: { intoScope?: FxtrScope; copyIntoScope?: boolean },
  ): Promise<EntityRef<T> | null>;

  listPins(prefix?: string): Promise<ReadonlyMap<string, FxtrID>>;
}
```

Key differences from Python:
- `type` parameter → `loadCodec` parameter (no global registry)
- `into_scope` / `copy_into_scope` moved to an options object for cleaner overloads
- `into_scope` defaults to the namespace's own internal scope (not an "active scope" since TS has none)
- When `loadCodec` is provided, returns `EntityRef<T>`; when omitted, returns `FxtrID`
- `list_pins` → `listPins`, returns `ReadonlyMap` (more idiomatic than plain object)

The `intoScope` / `copyIntoScope` behavior mirrors Python:
- If `intoScope` is set and `copyIntoScope` is false: `intoScope.lift(nodeId)` (the node must already be in a parent scope)
- If `intoScope` is set and `copyIntoScope` is true: `await intoScope.copyFromScope(nodeId, namespaceScope)`

### `HttpFxtrReadNamespace`: `src/http/client.ts`

Implements `FxtrReadNamespace`. 1:1 port of Python's `HttpFxtrReadNamespace`.

```typescript
function withHttpReadNamespace(
  fetchFn: FetchFn,
  callback: (ns: HttpFxtrReadNamespace) => Promise<void>,
): Promise<void>;
```

Internally:
1. Creates an `HttpReadableArena` and `HttpDeepFetchable` from the `fetchFn`
2. Creates a scope via `withFxtrScope` (the callback pattern)
3. Adds the `HttpDeepFetchable` to `scope.deepFetchSources`
4. Runs the user callback with the namespace
5. Cleans up (closes arena) after callback completes

The namespace object (passed to the callback) implements `FxtrReadNamespace`:

- `scope`: The internally-created scope
- `retrieve(key, loadCodec?, opts?)`: GET `/pin/{key}`, parse response, ingest precomp into the internal arena, attach to scope via `unsafeLazilyAttachFromArena`, handle `intoScope`/`copyIntoScope`, return `FxtrID` or `EntityRef<T>`
- `tryRetrieve(...)`: Same but returns `null` on 404 instead of throwing
- `listPins(prefix?)`: GET `/pins?prefix=...`, parse JSON, return `Map<string, FxtrID>`

#### Scope interaction detail

When `retrieve` fetches a pin:
1. Parse the response to get `fxtrId` and precomp
2. Add `fxtrId` to `_arena._tracked` and ingest precomp
3. Call `scope._mainArena.unsafeLazilyAttachFromArena(fxtrId, _arena)` (this is the key integration point — the HTTP arena acts as a lazy backing store for the scope's memory arena)
4. If `intoScope` is specified:
   - `copyIntoScope=true`: `await intoScope.copyFromScope(fxtrId, scope)`
   - `copyIntoScope=false`: `intoScope.lift(fxtrId)`

This requires `unsafeLazilyAttachFromArena` to be accessible on the scope's main arena. The Python version accesses `scope._main_arena` directly (private-by-convention). In TS we use a **symbol-keyed method** to restrict access to internal code only.

**Decision**: Add a symbol-keyed method on `FxtrScope` via a symbol exported from `scope/internal.ts`:

```typescript
// scope/internal.ts
export const UNSAFE_ATTACH_FROM_EXTERNAL_ARENA = Symbol("unsafeAttachFromExternalArena");

// scope/scope.ts
[UNSAFE_ATTACH_FROM_EXTERNAL_ARENA](nodeId: FxtrID, source: ReadableArena): void {
  this._state.checkAlive();
  this._state.checkNotFrozen("attachFromExternalArena");
  this._state.arena.unsafeLazilyAttachFromArena(nodeId, source);
}

// http/client.ts
import { UNSAFE_ATTACH_FROM_EXTERNAL_ARENA } from "../scope/internal.js";
scope[UNSAFE_ATTACH_FROM_EXTERNAL_ARENA](nodeId, arena);
```

The symbol is **not** re-exported from `index.ts`, so external consumers can't access it without deliberately importing from an internal path. This is the closest TS gets to "friend" / package-private access.

## File structure

```
v2/ts/src/http/
├── readNamespace.ts    # FxtrReadNamespace interface
├── server.ts           # createFxtrApp() → Hono app
└── client.ts           # HttpReadableArena, HttpDeepFetchable,
                        #   HttpFxtrReadNamespace, withHttpReadNamespace,
                        #   FetchFn, withBaseUrl
```

## Implementation order

### Step 1: `FxtrReadNamespace` interface
- [x] Define `FxtrReadNamespace` interface in `src/http/readNamespace.ts`
- [x] Export `FetchFn` type from `src/http/client.ts`

### Step 2: Server
- [x] Implement `computePrecompClosure` helper
- [x] Implement `createFxtrApp` with all four routes
- [x] Server tests via Hono's `app.request()`:
  - Preview endpoint (found, not found, derived node)
  - Data endpoint (bytes + precomp, not found)
  - Pin endpoint (found, not found, precomp)
  - Pins list (all, with prefix)
  - Scope closed → 503

### Step 3: Client — `HttpReadableArena`
- [x] Implement `HttpReadableArena` (all `ReadableArena` methods)
- [x] Tests: load, load tracks children, loadFlat, precomp population, abandon, copyToNewArena, close

### Step 4: Client — `HttpDeepFetchable`
- [x] Implement `HttpDeepFetchable`
- [x] Tests: tryLookup found, tryLookup not found, returned arena has correct precomp

### Step 5: Client — `HttpFxtrReadNamespace`
- [x] Add `scope/internal.ts` with `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` symbol
- [x] Add symbol-keyed method to `FxtrScope`
- [x] Implement `withHttpReadNamespace` and `HttpFxtrReadNamespace`
- [x] Tests:
  - retrieve (untyped → FxtrID)
  - retrieve (with codec → EntityRef, load round-trip)
  - tryRetrieve not found → null
  - retrieve not found → throws
  - listPins, listPins with prefix
  - Provenance nodes: direct pin, via parent load, via deep fetch

### Step 6: Cross-language integration tests (optional, manual)
- [ ] Verify TS client can talk to Python server and vice versa (can be done manually or as a separate integration test script)

### Step 7: Wrap up
- [x] Update `v2/ts/CODE_OVERVIEW.md`
- [x] Check off Phase 6 in overview.md

## Testing approach

All automated tests use Hono's `app.request()` (or `app.fetch()`) to test server routes in-process, and wire up the client classes against a Hono test server via a custom `FetchFn` that calls `app.request()`. This mirrors the Python pattern of using `httpx.ASGITransport`.

```typescript
// Test helper: create a FetchFn backed by a Hono app
function appFetch(app: Hono): FetchFn {
  return (url, init) => app.request(url, init);
}
```

This lets client tests run against a real server implementation without actual network I/O.

## Resolved questions

- **Hono path matching for `/pin/:name`**: Hono does not support `:name` matching across slashes. Resolved by using `/pin/*` wildcard and extracting the pin name from `c.req.path.slice("/pin/".length)`.
- **`scope.attachFromExternalArena`**: Implemented as a symbol-keyed method via `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` in `scope/internal.ts`. Checks alive and not-frozen state before delegating.
