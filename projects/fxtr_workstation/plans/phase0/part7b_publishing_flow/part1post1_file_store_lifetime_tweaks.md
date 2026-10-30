# Part 1 post-1 — layered-file-store lifetime seam (proposal)

> Status: PROPOSAL (2026-06-14). Follow-up to `part1_runner_manager.md`.
> Goal: entities read out of built layers stay **lazily loadable for the
> manager's lifetime**, instead of dying when a per-request reader closes.

## Problem (current)

`EntitiesReadAdapter.retrieve` opens a `LayeredFileStoreReader` CM, retrieves,
and closes it. The reader owns the scope the entity is attached to **and** the
`_backend_lifetime` the arena is branded on — both die on CM exit, so the
returned id is backed by nothing the caller can reach. We want the *arena* back,
and the caller (who can guarantee lifetime) mounts it.

## New seam (fxtr2 `layered_file_store`)

A pin-oriented factory that returns an arena; no scope, no CM, no mounting.

```python
# layered_file_store/pin_arena.py (new)

def _arena_over_search_set(           # shared core: reader, open_pin_arena, AND the DeepFetchable
    search_set: _LayerSearchSet, seed_id: FxtrID, *,
    grant: Grant[B], source: DeepFetchable[B] | None,
) -> LayeredFileArena[B]:
    """Build a LayeredFileArena over `search_set` seeded at `seed_id`, branded on
    `grant`, pre-track the seed. Caller owns the arena (mount, close)."""

def resolve_pin(layer_dir: Path, pin: str) -> FxtrID:
    """layer-local pin -> id (thin wrapper over fmt.read_pins). KeyError if absent."""

def open_seed_arena(layer_dir, seed_id, *, grant, source, remap=None) -> LayeredFileArena[B]:
    """_LayerSearchSet([layer_dir]) + _arena_over_search_set. Single-layer entry."""

async def open_pin_arena(layer_dir, pin, *, grant, source, remap=None):
    """resolve_pin + open_seed_arena. Returns (FxtrID, LayeredFileArena[B])."""
```

This is the core of today's `LayeredFileStoreReader._make_arena_for_retrieve` +
pin lookup, minus the reader's scope/lifetime ownership. The split (search-set
core + single-layer + pin wrappers) lets the reader reuse its cached `pins.json`
and lets the deep-fetch view below reuse one long-lived search set. The manager
drives `open_pin_arena` directly.

### Required arena tweak — hold a grant, not a lease

The arena must **not** hold a lease on its own brand (you can't clean up a
lifetime you're pinning). It only needs identity + the ability to fall through:

- `_lifetime: UnconditionalLocalLifetime[B]` → `_grant: Grant[B]`. Used for
  `validate_lease_grant(self._grant, lease)` (compares `lease.grant` only) and
  the `token` property (`self._grant.subject -> LifetimeToken[B]`). A `Grant` is
  `Hashable` and holds only weak refs — holding it pins nothing.
- `_remote_lease: Lease[B]` → **dropped.** The remote source rides on the same
  brand `B`, so remote fallback uses the **incoming** `Lease[B]` (the one the
  caller/bridge passes to `load_flat`) — `source.try_lookup(id)` then
  `remote_arena.load_flat(incoming.clone(), id)`. Nothing held at rest.
- `copy_to_new_arena` carries `_grant` + `source` into the fork (both
  identity/reference; no lease cloning).

Net: `LayeredFileArena` holds **zero leases**. Refactor surface is just
`arena.py` + `reader.py`: the only two `LayeredFileArena` construction sites are
the reader and the arena's own `copy_to_new_arena`. (`writer.py` does *not* build
a `LayeredFileArena` — it calls `remote.try_lookup` itself with its own held
lease; untouched.)

## Lifetime model

- **Arena brand `B` = the store's grant**, threaded in. The layer is an overlay
  on the backing store: it can't outlive it, and the manager keeps materialized
  dirs on disk at least that long. No lifetime object, no lease in the arena.
- **Who holds the `B` lease:** the **scope's bridge**
  (`IndependentLocalBridge(store.get_lease())`) holds one at rest — that's the
  scope claiming the store while it needs the attach, the correct place for a
  lease. `Independent` because the store outlives the manager scope (part 1 §3).
- **Mount target = the manager scope** (a long-lived `FxtrScope` bound when the
  manager is entered, closed with the store).

## Consumer: FlakeManager

`FlakeManager` is an `anyio.AsyncContextManagerMixin` (the `fxtr_context_api`
namespace pattern): inert `__init__` records config; `__asynccontextmanager__`
opens the store + scope. No separate `open` classmethod, no cached grant field.

```python
class FlakeManager(anyio.AsyncContextManagerMixin):
    def __init__(self, repo, *, rev=None, backend=None, config=None, isolate_scope=False):
        ...  # record config; _store / _scope / _backend set on enter

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self):
        async with RepoEntityStore.open(self._repo, create=True) as store, \
                   FxtrScope(name="flake-manager", isolated=self._isolate_scope) as scope:
            self._backend, self._store, self._scope = backend, store, scope
            try:
                yield self
            finally:
                self._backend = self._store = self._scope = None

    def mount_pin(self, layer_dir, pin, *, remap) -> FxtrID:
        """Attach a built layer's pin into the manager scope; loadable for life."""
        store, scope = self._get_store(), self.scope
        fxtr_id, arena = open_pin_arena(layer_dir, pin, grant=store.grant, source=store, remap=remap)
        scope._main_arena.lazily_attach_from_arena(
            scope._current_auth_lease().clone(), fxtr_id, arena,
            IndependentLocalBridge(lease=store.get_lease()),
        )
        arena.close()                            # no-op for leases; clears tracked sets
        return fxtr_id
```

## Consumer: LayeredFileStoreReader (still works on the seam)

The reader reimplements on the seam, confirming the seam covers both consumers:

- **Keeps**: its CM scope (still `_mark_not_deeply_fetchable`); its held foreign
  store lease `_remote_lease` for the CM duration (a reader legitimately claims
  the store it was handed — that's not an arena self-lease); and its own
  `UnconditionalLocalLifetime` **only** for the `source=None` (pure-disk) case.
- **Brand**: `grant = remote_lease.grant` when it has a remote (brand on the
  store, like the manager), else `backend.root_lease.grant`. So the arena holds
  zero leases either way.
- **`_retrieve_impl`** becomes: `id = self._pins[key]` (cached) →
  `arena = open_seed_arena(self._layer_dir, id, grant=<brand>, source=self._remote_dfetch, remap=...)`
  → `scope.lazily_attach_from_arena(..., IndependentLocalBridge(lease=<brand lease clone>))`
  → `arena.close()` → existing `into_scope` copy/lift → return.

No observable change for reader callers: returned ids and `into_scope` copies are
identical; the lazy attach into `reader.scope` dies with the CM regardless; and
the `source=None` precomp-only hit still raises `LayerRemoteNotConfiguredError`
(so `test_remote_pointer_layer_is_genuinely_remote` still passes).

## Consumer: EntitiesReadAdapter.retrieve

```python
async def retrieve(self, name, type=None, *, into_scope=None, copy_into_scope=True):
    meta = await self._eval_metadata(name)                       # {type, layerKey} (span-bound)
    if meta["type"] == "builtin":
        return _builtin_id(meta)                                 # short-circuit, unchanged
    mat = await self._build_layer(name)                          # build .layer + materialize (span-bound)
    remap = ParentPathRemap(*mat.remap) if mat.remap else None
    fxtr_id = self._manager.mount_pin(                            # mount into MANAGER scope (sync)
        mat.host_path, meta["layerKey"], remap=remap,
    )
    if into_scope is not None:                                   # optional independent copy
        await into_scope.copy_from_scope(fxtr_id, source_scope=self._manager.scope)
    return EntityRef(fxtr_id, type) if type else fxtr_id
```

Default home is the manager scope (lazily loadable for its lifetime). Serving
(part 5) makes its scope `include=[mgr_scope]` for zero-copy inheritance;
one-shot/MCP callers pass `into_scope` (eager copy) and drop it — so the manager
scope stays "the published set" and doesn't grow unbounded on their behalf.

Note the split: retrieval still needs the `BuildSpan` (rehost feeds preload FODs
during the `.layer` build); the mounted result lives on the manager and outlives
the span.

## Deep-fetch view (scoped now, for part 5)

By-id resolution across a whole published layer set is a `DeepFetchable[B]`
wrapping a long-lived `_LayerSearchSet` + the same arena core — added now so part
5 just instantiates + registers it.

A `DeepFetchable` must be **complete and stable from its first request**, so the
search set is **fully expanded at construction** (walk every layer's
`parents.json` to fixpoint). After that the set is effectively immutable —
`try_lookup` is pure read, `expand_parents_of` is a no-op, and concurrent lookups
don't race. (Construction does the parents walk; an async factory is an option if
we want to keep that I/O off the event loop.)

```python
class LayeredStoreDeepFetchable[B](DeepFetchable[B]):
    def __init__(self, layer_dirs: list[Path], *, grant: Grant[B],
                 source: DeepFetchable[B] | None = None,   # backing store, brand B
                 remap: ParentPathRemap | None = None):
        self._search_set = _LayerSearchSet(layer_dirs, parent_remap=remap)
        self._search_set.expand_fully()                    # new: walk all parents to fixpoint
        self._grant, self._source = grant, source

    async def try_lookup(self, node_id) -> LayeredFileArena[B] | None:
        if not self._search_set.has_data_or_precomp(node_id):     # pure read (set is complete)
            return None
        return _arena_over_search_set(self._search_set, node_id,
                                      grant=self._grant, source=self._source)
```

Needs a new `_LayerSearchSet.expand_fully()` (loop `layers()` → `expand_parents_of`
until no new layers appear). Part 5 builds one over the published layer dirs
(grant + source = the repo store) and `register_deep_fetch_source`s it on the
serving scope; layer-local ids resolve from disk, remote-pointer closures fall
through to the store. (Whether to *also* register the bare repo store for ids
referenced by no layer is a part-5 assembly call.)

## Decisions / notes

- **Q1 — DECIDED:** add a public `RepoEntityStore.grant` accessor (returns the
  session's canonical grant). Trivial; used by the manager + part 5.
- **Q2 — DEFERRED (documented assumption):** the layered file store **assumes its
  layer dirs are neither deleted nor modified while in use** (a reader/arena/
  deep-fetch source is live). Branding overlay arenas on the store lifetime is
  safe only under that assumption. Document it in the layered-store module
  docstring + `LOCAL_CODE_OVERVIEW`. GC-pinning materialized dirs for a
  long-lived serving manager (gcroots) is its own follow-up.

## Implementation steps

fxtr2 (`v2/`) first, then the runner; each layer keeps existing callers green.

- [x] **`RepoEntityStore.grant`** — public accessor returning the session's
      canonical grant. Unit test: `store.grant == store.get_lease().grant`.
- [x] **`LayeredFileArena` refactor** (`arena.py`): `_lifetime` → `_grant: Grant[B]`
      (validate via `.grant`, `token` via `.subject`); **drop `_remote_lease`** —
      remote fallback threads the incoming `Lease[B]` to
      `source.try_lookup`/`load_flat`; `copy_to_new_arena` carries `_grant` +
      `source` (no lease cloning). Arena now holds zero leases. Update the
      arena's own unit tests.
- [x] **Seam module** `layered_file_store/pin_arena.py`: `_arena_over_search_set`
      (core), `resolve_pin`, `open_seed_arena`, `open_pin_arena`. Plus
      `_LayerSearchSet.expand_fully()` (loop `layers()` → `expand_parents_of` to
      fixpoint). Unit-test the seam against a hand-built layer + in-process store
      (no nix).
- [x] **Reimplement `LayeredFileStoreReader` on the seam**: cached-pins +
      `open_seed_arena`; brand on the store grant when it has a remote, else its
      own disk lifetime; keep `_mark_not_deeply_fetchable`. Confirm existing
      reader tests pass unchanged (incl. `test_remote_pointer_layer_is_genuinely_remote`).
      Document the "layer dirs immutable while in use" assumption in the module
      docstring + `data/layered_file_store/LOCAL_CODE_OVERVIEW.md`.
- [x] **`LayeredStoreDeepFetchable`** + its unit tests (by-id hit/miss across a
      multi-layer set; remote-pointer fall-through to the source).
- [x] **Update `v2/CODE_OVERVIEW.md` + `layered_file_store/LOCAL_CODE_OVERVIEW.md`**
      (the seam, the grant-based arena, the deep-fetch view).
- [x] **`FlakeManager`** (`fxtr_nix_runner`): open a long-lived manager `FxtrScope`
      next to the store; stash `store.grant`; add `_mount_pin(layer_dir, pin, *,
      remap) -> FxtrID` (seam → `lazily_attach_from_arena` into the manager scope
      with `IndependentLocalBridge(store.get_lease())` → `arena.close()`).
- [x] **Rewrite `EntitiesReadAdapter.retrieve`** onto `_mount_pin` (eval →
      build+materialize → `read_pins` → mount into the manager scope → return id).
      `into_scope` becomes an optional eager `copy_from_scope(id, source_scope=mgr_scope)`;
      drop the per-request `LayeredFileStoreReader`.
- [x] **Tests — lifetime/lazy-load:**
  - [x] **The key case (no nix):** hand-build a layer holding a **local** entity
        `A` whose data references a **remote-only** entity `B` (B's bytes live
        only in the in-process `RepoEntityStore`, recorded in the layer as a
        precomp/remote pointer, *not* as data). Mount `A` via the
        manager/`_mount_pin` (or the seam directly) into a manager-lifetime scope,
        then **tear down the build span** (drop the rehost/builder; the store
        stays open). After teardown: load `A` from the manager scope (local disk,
        via the search set) and follow `A → B`, asserting `B` is fetched from the
        **`source` deep-fetchable** (the repo store) and its content matches.
        This exercises: A survives the span (local, branded on store grant,
        mounted in the manager scope), B resolves lazily through the threaded
        source after the span, and no held lease was required.
  - [x] Reader-parity + `LayeredStoreDeepFetchable` cases (above).
  - [x] Existing `fxtr_nix_runner` `test_entities.py` / `test_nix_e2e.py` adapted
        to the manager-scope home; nix E2E still covers the full eval→build path.
- [x] **Update `projects/fxtr_nix_runner/CODE_OVERVIEW.md`** (manager scope +
      `_mount_pin`, entities-adapter lifetime change).
