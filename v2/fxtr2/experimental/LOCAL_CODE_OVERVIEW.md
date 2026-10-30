# Experimental Module

`fxtr2/experimental/` contains experimental additions to fxtr where the API may change. Code here is functional and tested but not yet considered stable — interfaces may be revised as we learn from usage.

## File Structure

```
fxtr2/experimental/
├── __init__.py
├── read_namespace.py       # FxtrReadNamespace ABC (shared across features)
├── dynamic_dispatch.py     # Renderer entity types + type-id constants for the frontend dispatch protocol
├── LOCAL_CODE_OVERVIEW.md  # This file
└── http/                   # HTTP API for serving/consuming entities
    ├── __init__.py
    ├── asgi_app.py         # Starlette ASGI app (entity data/pin/preview routes)
    ├── server.py           # Hypercorn wrapper
    ├── client.py           # HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace
    ├── workspace_namespace.py  # WorkspaceNamespace, WorkspacePinningBackend (hub workspace)
    ├── service_codec.py        # list-escape wire codec for service invocations (resolved mode)
    ├── service_dispatch.py     # shared impl shape + invoke_impl dispatch helper
    ├── service_rest_endpoint.py # GET/POST service invocation route
    ├── service_ws_endpoint.py   # ws RPC endpoint + per-connection table (callbacks + transients)
    └── service_registry.py      # ServiceRegistry → routes() + /discovery
```

## Shared Utilities

### `read_namespace.py` — FxtrReadNamespace ABC

A read-only abstract base mirroring `FxtrNamespace`'s read interface:

- `scope` property
- `retrieve(key, type, into_scope, copy_into_scope)` with overloads
- `try_retrieve(...)` — same but returns None on missing
- `list_pins(prefix)` → `dict[str, FxtrID]`

Subclasses implement `_retrieve_impl` and `list_pins`. This ABC is not HTTP-specific — any read-only data source could implement it.

### `dynamic_dispatch.py` — renderer entity types

The backend side of the frontend's runtime renderer-resolution protocol: the
entity types (`RendererEntity {module: bytes}`, `ReloadEverythingNotification`)
and their type-id constants (`DYNAMIC_DISPATCH_NS`, `RENDERER_ENTITY_TYPE_ID`,
`RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID`, `WORKSTATION_DISPATCHER_TYPE_ID`). They
live here, below any serving layer, so both `fxtr_nix_runner`'s published-flake
serve and `fxtr_workstation`'s dispatcher impls can mint/serve them without a
cross-package dependency. The dispatcher *protocol* + the `RendererResolution`
dataclass stay in `fxtr_workstation.dynamic_dispatch` (it re-exports these types
for its existing import sites). Type ids are content-addressed by fixed UUIDs, so
the definitions' location does not affect any entity's id or wire form.

## HTTP API (`http/`)

An HTTP-based interface for serving and consuming fxtr entities. Enables remote processes to read from a live fxtr namespace without embedding the full store.

### Background: Deep Fetchability

The HTTP server relies on `FxtrScope._deep_fetch(node_id)`, which resolves entities by checking the scope memory store, parent scopes, and registered **`DeepFetchable`** sources (`register_deep_fetch_source(backend, key_lease)`). When a source returns an arena (via lease-free `source.try_lookup(node_id)`), the scope adopts it via **`lazily_attach_from_arena`**, threading the stored `key_lease` + `converter`, so later loads hit the cache.

### `http/asgi_app.py` — Starlette ASGI App

`create_fxtr_app(scope, pins)` creates a Starlette app with these routes:

| Route | Response | Description |
|-------|----------|-------------|
| `GET /preview/{id_string}` | `text/plain` | Pretty-printed entity content |
| `GET /data/{id_string}` | `application/cbor` | Entity bytes + precomp map |
| `GET /pin/{name:path}` | `application/cbor` | Pin ID + precomp map |
| `GET /pins?prefix=...` | `application/json` | Dict of pin names to ID strings |

Every request checks `scope.is_alive()` first (503 if closed). The shared `compute_precomp_closure(arena, lease, seed_ids)` helper in `fxtr2.data.arena_util` walks provenance edges using leased arena methods (`unwrap_provenance`, `c_node_to_context_data`, etc.).

### `http/server.py` — Hypercorn Wrapper

- `serve_until(scope, pins, done, port, host)` — serves until `done` is set
- `serve_forever(scope, pins, port, host)` — serves until cancelled
- `serving_context(scope, pins, port, host)` — async context manager for a background server

### `http/client.py` — HTTP Client

**`HttpReadableArena`**: `ReadableArena` backed by `httpx` against `GET /data/{id}`. Constructed with `(client, UnconditionalLocalLifetime)`. Methods take a **`Lease`** (validated against the arena lifetime) per the shared arena lease pattern.

**`HttpDeepFetchable`**: `DeepFetchable` — `try_lookup(node_id)` performs `GET /data/`, builds a tracked `HttpReadableArena` with precomp. No backend lease; the returned arena is lease-gated as usual.

**`HttpFxtrReadNamespace`**: `FxtrReadNamespace`. On enter, creates an `UnconditionalLocalLifetime`, `FxtrScope`, registers `HttpDeepFetchable` with `root_lease.clone()`, and on exit discards the backend root lease after closing the scope.

**`retrieve` path**: loads pin CBOR, builds a temporary `HttpReadableArena`, then **`lazily_attach_from_arena(scope._main_lease.clone(), id, arena, backend_lifetime.root_lease.clone(), None)`** so the entity lives in the scope memory store.

### Stateless service layer (`service_*`)

A thin service layer over the entity endpoints, for the
**client-state HTTP connector** (`@fxtr-ui/connector-http`) — a
stateless, CDN-friendly alternative to the full fxtr-RPC workstation.
Services are plain async functions, not fxtr-RPC caps; the only fxtr2
touchpoint is entities appearing in invocation args/results. See
`projects/fxtr_workstation/plans/phase0/part7a_stateless_ws_server/`.

- **`service_codec.py`** — list-escape codec (the resolved-mode twin of
  the workstation's `wire_escape.py`). `escape_value` /
  `unescape_value` / `unescape_args`. Entity tags carry the full
  `id_string` (no server short ids); service tags carry a
  self-describing `ServiceDescriptor` (`{kind: http|ws|transient, url,
  method, name, id, service_type_id}`); callback tags carry an inbox
  uuid → opaque `CallbackAddress`. Pure + sync (entity tags decode to a
  bare `FxtrID`, parse only — no scope / fetchability check). A
  `TransientService(impl)` in a result escapes via a caller-supplied
  `register_transient` (ws only). `ServiceCodecError` for malformed
  input; no `error_type`.
- **`service_dispatch.py`** — the shared `ServiceImpl` shape (a
  callable `(method, args, kwargs, ctx)` or a `{method: fn}` map) and
  `invoke_impl(...)` (+ `UnknownServiceMethodError`).
- **`service_rest_endpoint.py`** — `rest_service_route(path, impl, *,
  scope, method)` (+ `make_rest_handler`). GET
  `?method&payload=<urlencoded {args,kwargs}>` or POST
  `{method,args,kwargs}`; body = escaped result; errors → non-2xx JSON
  `{message}`. `RestServiceContext(scope, request)` — callbacks inert
  over http.
- **`service_ws_endpoint.py`** — `ws_services_endpoint({name: impl}, *,
  scope)`. One endpoint per ws url, `invoke`/`result`/`error`/`notify`
  frames, accept → read-loop + write-queue siblings (mirrors the
  workstation `client_app.py` shape), per-invoke task spawn. The
  per-connection `_ConnectionTable` holds inbox uuids (for
  `ctx.notify`) + transient services (`ctx.transient`); both die with
  the connection.
- **`service_registry.py`** — `ServiceRegistry(scope, *, base_url=None)`
  with `add_entity` / `add_rest_service` / `add_ws_service`; emits
  `routes()` (REST `Route`s + one `WebSocketRoute` per ws path) and the
  matching `/discovery` payload (`{entities, services}`, each service
  descriptor carrying `service_type_id`). Discovery listing is
  **optional**: pass `discovery_name=...` to list a service, or omit it
  for a non-discoverable service (the route/impl is still mounted, just
  left out of `/discovery`). The wire identity is separate: a REST
  service is addressed purely by url+method (no routing key), while a ws
  service needs an explicit `routing_name` (the descriptor `name` the
  client sends as `service`) — the routing name and discovery name are
  independent and never defaulted to each other. Both `add_*_service`
  calls **return the `ServiceDescriptor`**, which an impl hands out by
  returning it from an invoke (the way to reference a non-discoverable
  service). Leaves `create_fxtr_app` untouched — the author composes
  entity routes + `registry.routes()` + discovery into one app.

The TypeScript twin of this layer is deferred to a follow-up.

### `http/workspace_namespace.py` — Hub Workspace

**`WorkspacePinningBackend`**: `PersistentPinningBackend` over HTTP (`PUT/DELETE/GET` workspace pin routes) plus `RemoteClient` for entity push when pinning from a local arena. Backend methods take no backend lease; `pin_entity_from_arena(key, entity_id, source, src_lease, *, overwrite)` walks the source arena with **`src_lease`** clones (`load_flat`, `is_tracked`).

**`WorkspaceNamespace`**: `FxtrNamespace` + `anyio.AsyncContextManagerMixin` — same pattern as `TsvFxtrNamespace` / SQLite: **`__asynccontextmanager__`** nests `async with httpx.AsyncClient(...)`, `async with UnconditionalLocalLifetime(...)`, and `async with` the scope; `register_deep_fetch_source(HttpDeepFetchable(...), root_lease.clone())`; **`_backend_lease()`** returns `self._backend_lifetime.root_lease.clone()`. Lifetime exit uses ULL’s async-CM teardown (discard root + drain on normal exit).
