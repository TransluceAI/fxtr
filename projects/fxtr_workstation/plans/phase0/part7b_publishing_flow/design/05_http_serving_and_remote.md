# Piece: HTTP serving, read namespaces, and the remote transfer protocol

> Status: provisional first pass (2026-06-12). Line numbers are approximate
> pointers, not contracts; re-verify when implementing.

The publishing flow ends with "a new python serving script which exposes the
union of (a) the layered entities built by nix + (b) the set of all entities
in the repo entity store … serv[ed] over the existing experimental
HTTP interface". This doc describes the serving-side machinery we have.

## 1. The experimental HTTP server (`v2/fxtr2/experimental/http/`)

`create_fxtr_app(scope: FxtrScope, pins: dict[str, FxtrID])`
(`v2/fxtr2/experimental/http/asgi_app.py:31`) builds a Starlette app with four
routes:

| Route | Response | Notes |
|---|---|---|
| `GET /preview/{id_string}` | text/plain | pretty-printed entity |
| `GET /data/{id_string}` | application/cbor | `{bytes?: <entity cbor>, precomp?: PrecompMap}` (`asgi_app.py:74-104`) |
| `GET /pin/{name}` | application/cbor | `{id: <id_string>, precomp?: PrecompMap}` (`asgi_app.py:106-129`) |
| `GET /pins?prefix=` | application/json | `{pin_name: id_string}` (`asgi_app.py:131-140`) |

Key facts:

- Every data walk goes through `scope._deep_fetch(node_id)` — **the serving
  scope must be able to deep-fetch every entity it serves** (either already
  tracked, or reachable through a registered deep-fetch source).
- The `pins` dict is read per-request and may be mutated externally
  (`asgi_app.py:36` docstring) — useful if the serving script wants to extend
  the pin set after a rebuild.
- Precomp maps are computed by the shared `compute_precomp_closure`
  (`v2/fxtr2/data/arena_util.py`) — same helper the layered-store writer uses,
  so the HTTP wire shape and the layer `precomp/` files carry the same info
  (`unwraps_to`, `ctx_node`, `backstep` id-strings per provenance node).
- Hypercorn helpers in `experimental/http/server.py`: `serve_until` (:25),
  `serve_forever` (:75), `serving_context` (:96) — the last is the natural
  fit for a long-running serving script.

## 2. `FxtrReadNamespace` — the read-only namespace ABC

`v2/fxtr2/experimental/read_namespace.py:17` — `scope` property +
`_retrieve_impl(key, type, into_scope, copy_into_scope, allow_not_found)` +
`list_pins(prefix)`. Implementations:

- `HttpFxtrReadNamespace` (`experimental/http/client.py:305`) — HTTP-backed.
- `LayeredFileStoreReader` (`data/layered_file_store/reader.py:44`) —
  layer-dir-backed (see `design/02`).
- `TsvFxtrNamespace` / `SQLiteFxtrNamespace` (`data/fxtr_context_api.py`) are
  the full read-write `FxtrNamespace` cousins.

## 3. The HTTP client (Python and TS)

`HttpReadableArena` (`experimental/http/client.py:64`) lazily fetches
`/data/{id}` per `load_flat`, caches precomp, and tracks discovered child ids.
`HttpDeepFetchable` (:280) implements `DeepFetchable.try_lookup` via one
`/data/` GET, returning a fresh pre-tracked arena or `None`.
`HttpFxtrReadNamespace._retrieve_impl` (:377) fetches `/pin/{key}` and
attaches the result to its scope via `lazily_attach_from_arena` with an
`IndependentLocalBridge`, so subsequent loads stream lazily.

The TS mirror is `@fxtr/http-client` (see `design/04` §2) — same routes, same
precomp handling. This symmetry is what lets the TS build step consume the
same serving endpoint (or a build-scoped rehost of it).

## 4. The remote transfer protocol (`v2/fxtr2/data/remote/`)

Distinct from the HTTP read API: this is the **store-to-store sync** layer.

- `RemoteClient` protocol (`remote_client.py:28`): `negotiate(ids) ->
  (known, missing)`, `push_entities(flat_data) -> PushResult`,
  `pull_plan(ids) -> [(id, size)]` (topo-ordered closure), `pull_data(ids)`.
- Transfer format (`transfer_format.py:34`): per-entity CBOR
  `[id_bytes, entity_bytes_or_null, [[dep_id_bytes, dep_kind_int], ...]]`,
  topological order; dep kinds 0..4 = STRONG_REF, WEAK_REF, CTX_NODE,
  BACKSTEP, UNWRAPS_TO.
- `TransferEntitySource` (`transfer_entity_source.py:59`) — in-memory
  `FlatEntitySource` for feeding pulled entities into
  `pin_entity_from_arena`.
- `WorkspaceNamespace` / `WorkspacePinningBackend`
  (`experimental/http/workspace_namespace.py:155/:71`) — a full read-write
  `FxtrNamespace` over a hub workspace (`{hub}/api/v1/repos/{repo}/workspace`),
  combining `HttpDeepFetchable` reads with negotiate+push writes. This is the
  closest existing thing to "a remote store you can pin to and lazily read
  from over HTTP".

So when the sketch says "loads some entities from a remote store" and
"pointing them at the same remote store", the candidates are:
(a) the behaviors hub via `RemoteClient`/`WorkspaceNamespace`, or
(b) the simpler experimental HTTP read API (what the part5 note
`07_remote_entity_store.md` pinned as the `FXTR_REMOTE_BASE_URL` build-time
contract). Which one a published flake's layers should name as their remote is
a top discussion item.

## 5. Serving a union of sources — what exists, what doesn't

There is **no union-namespace class today**. Composition happens at the scope
level:

- `FxtrScope.register_deep_fetch_source(source, bridge)`
  (`data/entity_ref_scope.py`, deep-fetch section) appends a
  `(DeepFetchable, lease)` entry; `_deep_fetch_scan_sources` walks
  self + ancestor sources in registration order, first match wins, and adopts
  hits into the scope via `lazily_attach_from_arena` (so repeats are cached).
- Pin listings must be merged by hand (`list_pins()` over each source into one
  dict handed to `create_fxtr_app`).
- Note the layered-store reader does **not** participate in scope-level
  deep-fetch: its remote fallback is internal to `LayeredFileArena`
  (`data/layered_file_store/arena.py`), and the reader's scope is explicitly
  marked not deeply fetchable. A serving scope that should expose
  layer-resident entities by bare id (the sketch's "crawl the layered file
  store from its roots, maybe storing those entities back into its backing
  store") therefore needs to either copy entities out of the reader into the
  serving scope, or wrap the reader in a `DeepFetchable` adapter — new code
  either way. See `design/06` and `discussion_topics.md`.

## 6. The serving script's likely shape (preview)

Updated for the synced design (authoritative sequence: `design/06`). After the
fourth-sync reshape the store/rehost/build machinery lives in the
**`fxtr_nix_runner` manager** (parts 1–2); the serving script is a thin driver:
1. `FlakeManager.open(repo, …)` — the manager discovers (creating if absent)
   and opens the **repo entity store** (the `RepoEntityStore` `DeepFetchable` +
   `try_lookup`, see `design/01`),
2. in a build span the manager stands up the rehost over the repo store for
   the build's preload FODs (the `FXTR_REMOTE_BASE_URL` contract from part5's
   note 07; reachability: `design/07`),
3. the manager `nix eval`s the flake's `fxtr` attrset (the source of truth) and
   builds the declared exports/layers (patterns: `design/03`),
4. the serving script exposes exactly what the attrset declares via the
   manager's read adapters — entity pins from their layers (scope-only, no
   write-back), namespace exports' key→id maps with data resolved via the repo
   entity store (mounted on the serving scope as a deep-fetch source) — and
   serves with `create_fxtr_app` + `serving_context`. No pin merging.
