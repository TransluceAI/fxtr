# Stage 1: HTTP API for Serving Fxtr Entities

## Overview

This plan covers two pieces of work:

1. **Deep fetchability** (already implemented): An internal feature on `FxtrScope` that allows fetching any entity reachable from a scope's tracked set by ID alone, without maintaining scope state on the server side.
2. **HTTP serving layer** (to be implemented): A set of modules under `fxtr2/experimental/http/` that expose a REST API for reading entities, plus a client that can consume it.

The goal is to bootstrap a simple REST server that can serve fxtr entity data over HTTP, with a compatible read-only client, so that remote processes can read from a live fxtr namespace without needing the full Python-side scope machinery.

## Part 1: Deep Fetchability (already done)

### What changed

`FxtrScope` gained three new internal attributes:
- `_deeply_fetchable_flag: bool | None` — whether the scope supports deep fetching
- `_deep_fetch_sources: set[ObservableStore]` — additional stores to search beyond the main arena
- `_deep_fetch(node_id) -> ReadableArena | None` — searches main arena store, then deep_fetch_sources, then parents recursively

Deep fetchability is **inferred** at scope entry time: a scope is deeply fetchable if all its parents are (root scopes default to True). It can also be explicitly marked not-deeply-fetchable before entry.

`FxtrNamespace._get_scope()` now has an invariant: if the backend implements `ObservableStore`, add it to `_deep_fetch_sources`, and otherwise, mark the store as non-deep-fetchable. Both `SQLiteFxtrNamespace` and `GitFxtrNamespace` add to `_deep_fetch_sources`.

### Why this matters for HTTP

`_deep_fetch` is what the HTTP server calls to look up an entity by ID. Unlike the normal `load()` path (which requires the ID to be tracked in the scope's arena), `_deep_fetch` searches all backing stores directly. This is safe because deep fetchability guarantees that every reachable node can be found in one of the stores.

## Part 2: HTTP Serving Layer

### Architecture

```
fxtr2/experimental/
├── __init__.py
├── read_namespace.py          # FxtrReadNamespace ABC
└── http/
    ├── __init__.py
    ├── asgi_app.py             # Starlette ASGI app
    ├── server.py               # Hypercorn wrapper (serve_until, serve_forever, serving_context)
    └── client.py               # HttpReadableArena + HttpFxtrReadNamespace
```

### Module 1: ASGI App (`asgi_app.py`)

A Starlette app bound to a particular `FxtrScope`, with a dict of pin names to FxtrIDs.

**Routes:**

- `GET /preview/{id_string}` — Human-readable pretty-printed view of an entity
  - Parse `id_string` via `FxtrID.from_id_string()`
  - Call `scope._deep_fetch(fxtr_id)` to get a `ReadableArena`
  - If found, call `arena.load_flat(fxtr_id)`, decode the CBOR bytes, and return
    a pretty-printed plain text representation
  - Content-Type: `text/plain`
  - Returns 404 if entity not found or if `entity_bytes` is None (derived nodes)
  - Returns 503 if scope is no longer alive

- `GET /data/{id_string}` — Fetch entity bytes + provenance precomp map
  - Same initial steps as `/preview/`: deep-fetch to get an arena, call `load_flat`
  - Additionally, for each dep ID from `load_flat`, compute the provenance relationships
    by calling `arena.unwrap_provenance()`, `arena.c_node_to_context_data()`,
    `arena.p_node_to_pathroot()`, and `arena.step_back_p_ctx()` on the arena
  - Return a CBOR-encoded dict:
    ```
    {
      "bytes": <raw entity CBOR bytes>,
      "precomp": {                              // omitted if empty
        <id_string>: {                          // entries with no relationships are omitted
          "unwraps_to": <id_string>,            // omitted if not applicable
          "ctx_node": <id_string>,              // omitted if not applicable
          "backstep": <id_string>               // omitted if not applicable
        },
        ...
      }
    }
    ```
  - The precomp map is **transitively closed**: it contains entries for all dep IDs,
    plus any IDs that appear as values in those entries, recursively. This ensures
    the client can do provenance operations on any ID it discovers without additional
    fetches. (In practice provenance chains are shallow so this is small.)
  - Content-Type: `application/cbor`
  - Returns 404 if entity not found or entity_bytes is None
  - Returns 503 if scope is no longer alive

- `GET /pin/{name:path}` — Look up a pin by name (supports slashes in name)
  - Look up name in the pins dict
  - Deep-fetch the pinned ID to get an arena, compute provenance relationships for the
    ID itself (unwraps_to, ctx_node, backstep)
  - Return a CBOR-encoded dict:
    ```
    {
      "id": <id_string>,
      "precomp": { ... }            // same format as /data/, omitted if empty
    }
    ```
  - The precomp map is transitively closed (same as `/data/`): it contains the entry
    for the pinned ID itself plus any IDs reachable through its provenance relationships,
    so the client can immediately track it with full provenance data without a second request
  - Content-Type: `application/cbor`
  - Returns 404 if pin not found
  - Returns 503 if scope is no longer alive

- `GET /pins?prefix=...` — List pins matching a prefix
  - Filter the pins dict by optional `prefix` query param (default: all pins)
  - Return a JSON dict of `{name: id_string}` with Content-Type `application/json`
  - Returns 503 if scope is no longer alive

**Key design decisions:**
- The app holds a reference to the scope and the pins dict. It does NOT own them.
- On every request, check `scope.is_alive()` first; return 503 if closed.
- The pins dict is passed in at construction and can be mutated externally (the app reads it on each request).

```python
def create_fxtr_app(scope: FxtrScope, pins: dict[str, FxtrID]) -> Starlette:
    ...
```

### Module 2: Server Wrapper (`server.py`)

Convenience layer over hypercorn for serving the ASGI app.

**API:**

```python
async def serve_until(
    scope: FxtrScope,
    pins: dict[str, FxtrID],
    done: anyio.abc.Event,
    port: int,
    host: str = "127.0.0.1",
) -> None:
    """Serve the ASGI app until the event is set."""

async def serve_forever(
    scope: FxtrScope,
    pins: dict[str, FxtrID],
    port: int,
    host: str = "127.0.0.1",
) -> None:
    """Serve the ASGI app until the task is cancelled."""

@asynccontextmanager
async def serving_context(
    scope: FxtrScope,
    pins: dict[str, FxtrID],
    port: int,
    host: str = "127.0.0.1",
) -> AsyncIterator[None]:
    """Start a background server in a task group, shut down on exit."""
```

**Implementation approach:**
- Use `hypercorn.asyncio.serve()` with `shutdown_trigger` (same pattern as thingwatcher)
- `serve_until`: create app, configure hypercorn, call `serve(app, config, shutdown_trigger=done.wait)`
- `serve_forever`: same but use a never-set event (relies on cancellation)
- `serving_context`: spawn `serve_until` in an anyio task group with a local event, yield, then set the event on exit

### Module 3: Read Namespace and HTTP Client

#### `read_namespace.py` — `FxtrReadNamespace` ABC

A read-only subset of `FxtrNamespace`'s interface:

```python
class FxtrReadNamespace(abc.ABC):
    @property
    def scope(self) -> FxtrScope: ...

    async def retrieve(self, key, type=None, into_scope=None, copy_into_scope=False): ...
    async def try_retrieve(self, key, type=None, into_scope=None, copy_into_scope=False): ...
    async def list_pins(self, prefix="") -> dict[str, FxtrID]: ...
```

The method signatures and overloads should mirror `FxtrNamespace` exactly for the read methods, so that code written against `FxtrReadNamespace` can accept either a full namespace or a read-only one. (We can consider making `FxtrNamespace` extend `FxtrReadNamespace` in the future, but for now they're separate.)

#### `http/client.py` — `HttpReadableArena` + `HttpFxtrReadNamespace`

**`HttpReadableArena(ReadableArena)`:**

Backed by an httpx `AsyncClient` pointing at the server's base URL. Follows the same precomp
pattern as `SQLiteNamespaceArena`, but keyed directly by `FxtrID` instead of internal IDs.

**State:**
- `_tracked: set[FxtrID]` — IDs known to be available on the server
- `_precomp_unwraps_to: dict[FxtrID, FxtrID]` — unwrap_provenance relationships
- `_precomp_ctx_node: dict[FxtrID, FxtrID]` — c_node_to_context_data / p_node_to_pathroot
- `_precomp_step_back_p_ctx: dict[FxtrID, FxtrID]` — step_back_p_ctx relationships

**Methods:**
- `is_tracked(node_id)` → check `_tracked` set
- `load(node_id)` → `GET /data/{id_string}`, decode the CBOR envelope, populate
  precomp dicts from the `precomp` map, decode `bytes` with `fxtr_cbor_loads`, walk the
  decoded CBOR to discover child IDs and add them to `_tracked`, return FxtrCbor
- `load_flat(node_id)` → same fetch as `load`, but return `FxtrEntityFlatData` instead of
  decoded CBOR (entity_bytes + deps extracted from the CBOR walk)
- `unwrap_provenance(node_id)` → look up in `_precomp_unwraps_to`, track result, return
- `c_node_to_context_data(node_id)` → look up in `_precomp_ctx_node`, track result, return
- `p_node_to_pathroot(node_id)` → look up in `_precomp_ctx_node`, track result, return
- `step_back_p_ctx(node_id)` → look up in `_precomp_step_back_p_ctx`, track result, return
- `abandon(node_id)` → remove from `_tracked` and clean up precomp entries (can use a
  simpler cleanup than SQLiteNamespaceArena since we don't need refcounting — just remove
  the direct entries; orphaned precomp entries are harmless and will be overwritten)
- `copy_to_new_arena(node_ids)` → create new `HttpReadableArena` with same client, copy
  tracked set and transitive closure of precomp data for the given IDs
- `close()` → clear all state (does not close the httpx client, which is owned externally)

**`HttpObservableStore(ObservableStore)`:**

Wraps the `HttpReadableArena` to implement the `ObservableStore` interface, so the
client-side scope (and any child scopes) can deep-fetch through the remote server.

- `try_lookup(node_id)` → `HEAD /data/{id_string}` (or a lightweight check). If 200,
  return the `HttpReadableArena` (after adding the ID to its tracked set). If 404,
  return None.
- `backrefs(node_id)` → return empty list (not supported over HTTP)

**`HttpFxtrReadNamespace(FxtrReadNamespace)`:**

- Constructor takes `base_url: str` (or an `httpx.AsyncClient`)
- Creates a `FxtrScope` (isolated) with:
  - The `HttpReadableArena` attached via `unsafe_lazily_attach_from_arena` so the
    scope's main arena can load data that has been fetched into the HTTP arena
  - An `HttpObservableStore` added to `scope._deep_fetch_sources` so the scope
    (and any child scopes) can deep-fetch entities from the remote server
- `retrieve(key)` → `GET /pin/{key}`, decode CBOR response, parse `id` as
  `FxtrID.from_id_string()`, populate arena's precomp dicts from `precomp` map, add to
  tracked set, return
- `list_pins(prefix)` → `GET /pins?prefix=...`, parse JSON response

### Dependencies to add

In `v2/pyproject.toml`:
- `httpx` — for the HTTP client
- `hypercorn` — for serving the ASGI app

(starlette and cbor2 are already present)

## Part 3: Unit Tests

### Tests for `_deep_fetch`

In `tests/test_entity_ref_scope/test_experimental_deep_fetch.py`:

Uses mock `ObservableStore` implementations to test without real backends.

- **test_root_scope_deeply_fetchable**: Root scope defaults to deeply fetchable
- **test_child_inherits_deeply_fetchable**: Child of deeply-fetchable parent is deeply fetchable
- **test_child_inherits_not_deeply_fetchable**: Child of not-deeply-fetchable parent is not deeply fetchable
- **test_mark_not_deeply_fetchable**: Explicit marking works
- **test_mark_not_deeply_fetchable_after_true_raises**: Can't mark False after already True
- **test_deep_fetch_finds_entity_in_main_store**: Entity tracked in scope's main arena is found
- **test_deep_fetch_finds_entity_in_deep_fetch_source**: Entity in an ObservableStore added to `_deep_fetch_sources` is found
- **test_deep_fetch_finds_entity_in_parent**: Entity in parent scope is found via recursion
- **test_deep_fetch_returns_none_for_missing**: Returns None for unknown ID
- **test_deep_fetch_raises_if_not_deeply_fetchable**: Raises RuntimeError if scope not deeply fetchable
- **test_deep_fetch_skips_store_lookup_for_cached_entity**: After deep-fetching A which references B, deep-fetching B should use the cached arena without calling `try_lookup` on the observable stores again. Verified by mocking the store and asserting `try_lookup` is only called once (for A).
- **test_sqlite_namespace_adds_to_deep_fetch_sources**: SQLiteFxtrNamespace adds its backend to scope's deep_fetch_sources

### Tests for HTTP modules

In `tests/test_experimental/test_http/`:

- **test_asgi_app.py**: Test the Starlette app directly using Starlette's `TestClient` or httpx `ASGITransport`
  - test_preview_found, test_preview_not_found, test_preview_derived_node_404
  - test_data_returns_bytes_and_precomp
  - test_data_precomp_is_transitively_closed
  - test_pin_found, test_pin_not_found, test_pin_includes_precomp
  - test_pins_list, test_pins_list_with_prefix
  - test_scope_closed_returns_503

- **test_server.py**: Integration test using `serving_context` with real HTTP
  - test_serving_context_starts_and_stops
  - test_serve_until_stops_on_event

- **test_client.py**: Test `HttpReadableArena` and `HttpFxtrReadNamespace`
  - Can use httpx `ASGITransport` to test against the ASGI app directly (no real server needed)
  - test_retrieve_pin, test_load_entity, test_load_tracks_children
  - test_load_populates_precomp, test_unwrap_provenance_from_precomp
  - test_list_pins

## Implementation Steps

- [x] Add `httpx` and `hypercorn` to `v2/pyproject.toml` dependencies
- [x] Write tests for `_deep_fetch` in `tests/test_entity_ref_scope/test_experimental_deep_fetch.py`
- [x] Create `fxtr2/experimental/__init__.py` and `fxtr2/experimental/http/__init__.py`
- [x] Implement `fxtr2/experimental/http/asgi_app.py` (Starlette ASGI app with preview, data, pin, and pins-list routes)
- [x] Write tests for the ASGI app
- [x] Implement `fxtr2/experimental/http/server.py` (hypercorn wrapper)
- [x] Write tests for the server wrapper
- [x] Implement `fxtr2/experimental/read_namespace.py` (FxtrReadNamespace ABC)
- [x] Implement `fxtr2/experimental/http/client.py` (HttpReadableArena + HttpFxtrReadNamespace)
- [x] Write tests for the HTTP client
- [x] Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md
- [x] Write provenance node tests (u/c/p via direct pin, parent load, and deep fetch)
- [x] Create `examples/server_demo/` with `serve.py` and `query.py` end-to-end demo

## Implementation Notes

### Changes beyond original plan

**`HttpFxtrReadNamespace` takes `isolate_scope` parameter** (default False). Matches
the pattern from `SQLiteFxtrNamespace`. Non-isolated (default) makes the namespace scope
the active scope, which is needed for `EntityRef.load()` to work naturally.

**`HttpObservableStore.try_lookup` does a full GET** instead of HEAD. This is necessary
because `_adopt_inheritance_arena_claim` calls `unwrap_provenance` on the returned arena
immediately, so the arena must have its precomp dicts populated before being handed to the
memory store. The `HttpReadableArena` docstring documents the tracking invariant: an ID
should only be added to `_tracked` if its precomp has been populated (or it's a ground node).

**`/preview/` returns 200 for derived nodes** with a text description, rather than 404.
**`/data/` omits `"bytes"` for derived nodes** but still sends the precomp map.

**`serving_context` uses a Starlette lifespan event** to signal readiness instead of a
sleep. `serve_until` accepts an optional `started` event parameter.

### Bug fixes in `memory_arena.py` (discovered during provenance testing)

1. **`_adopt_inheritance_arena_claim` missing `wrapped_by`**: When adopting a provenance
   node (c/p/u/n), the unwrapped target node's `wrapped_by` set was not updated. This
   caused an assertion failure in `_save_or_upgrade_node` when later upgrading the node
   from `InheritedNode` to `DerivedNode` (via `_ensure_own_derived`). Every other code
   path that sets `unwraps_to` also sets `wrapped_by`; this was the only gap. Fixed by
   adding `unwrapped_node.wrapped_by.add(node_id)` after the recursive adoption call.

2. **`step_back_p_ctx` inverted None check**: The condition `if backstep_node_id is None:
   assert backstep_node_id in self._entity_states` always fails (`None` is never in
   `_entity_states`). Should be `is not None`. Root p-nodes legitimately have no backstep.
   Fixed by changing `is None` to `is not None`.
