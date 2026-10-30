# Stage 11 — Part 1: Layered file entity store

## Goal

Implement a filesystem convention — the **layered file entity store** —
plus the Python machinery to read and write it. A layered store is a
directory of CBOR + JSON that represents "some new fxtr entities on top
of a stack of parent layers, with anything else assumed to live in a
runtime-configured remote entity store." This is the bridge format that
will let nix derivations emit fxtr data as deterministic outputs (part
2), but it stands on its own: anything that wants to dump a fxtr scope
to disk or read a directory of layered scopes can use these classes
directly.

The deliverables are two classes — `LayeredFileStoreReader` and
`LayeredFileStoreIsolatedWriter` — plus the on-disk format definition,
both living in a new submodule `fxtr2/data/layered_file_store/`. The
reader implements `FxtrReadNamespace`; the writer is a write-only async
CM that takes a `FxtrScope` and emits a fresh layer directory.

## Background

The piece this builds on most closely is `HttpFxtrReadNamespace` /
`HttpReadableArena` / `HttpDeepFetchable` in
`fxtr2/experimental/http/client.py`. The HTTP namespace shows the
pattern: own a backend `UnconditionalLocalLifetime`, open a `FxtrScope`,
and on each `retrieve` build a *fresh* `HttpReadableArena` for the pin,
attach it via `lazily_attach_from_arena`, then call `arena.close()`. The
backend lifetime is what's long-lived; arenas are short-lived,
per-retrieve. The layered file reader follows the same shape, with
disk reads replacing HTTP requests.

(Side note: `HttpFxtrReadNamespace` currently uses manual
`__aenter__` / `__aexit__`. We prefer `anyio.AsyncContextManagerMixin`
with `__asynccontextmanager__` — same pattern `WorkspaceNamespace`
uses — so the layered reader/writer should be written that way from
the start.)

The precomp closure shape is also already established by
`_compute_precomp_closure` in
`fxtr2/experimental/http/asgi_app.py`: `dict[id_string,
{unwraps_to?, ctx_node?, backstep?}]`. We re-use it verbatim.

## On-disk format

Each layer is a single directory. The layout:

```
<layer_dir>/
├── layer_format.json     # {"version": 1}
├── pins.json             # {"<pin_name>": "<id_string>", ...}
├── parents.json          # ["<absolute_path>", ...]
├── data/
│   └── <id_string>.cbor  # FxtrEntityFlatData payload
└── precomp/
    └── <id_string>.json  # {"unwraps_to": "...", "ctx_node": "...", "backstep": "..."}
```

### `layer_format.json`

Single object with a `version` integer. Readers reject any version they
don't know. Initial version is `1`.

### `pins.json`

JSON object mapping pin name (string) to id_string (string). Pins are
**local to this layer only**: when you `retrieve` from a reader opened
on this layer, you only see pins listed in this layer's `pins.json`,
even though the data walk may reach into parent layers. (Parent pins
are not unioned in.) The file is **required** — a layer with no pins
must contain `pins.json` with `{}`; a missing `pins.json` is a
malformed layer.

### `parents.json`

JSON array of absolute paths to other layered file entity store
directories. Parents are *not* recursively unioned at open time; the
search set is extended *dynamically* as the reader walks (see below).
The file is **required** — a layer with no parents must contain
`parents.json` with `[]`; a missing `parents.json` is a malformed
layer.

### `data/<id_string>.cbor`

For each FxtrID materialized in this layer, a single CBOR file whose
top-level value is a 3-tuple matching `FxtrEntityFlatData`'s wire shape:
`[<entity_id_bytes>, <entity_bytes_or_null>, [[<dep_id_bytes>,
<dep_kind_int>], ...]]`. (This matches the format already used by
`remote/transfer_format.py` — we reuse the same encoder.)

We use `id_string()` (not the raw bytes) for the filename so the dir
listing is human-readable and the format is filesystem-portable.

The data folder stores **bare data IDs only** — no provenance parents
on disk. Provenance is captured indirectly via `unwraps_to` in the
precomp folder. Users who want to round-trip provenance can use
`FxtrScope.embed_as_provenance_parent` / `extract_provenance_parent`
externally.

### `precomp/<id_string>.json`

For entities that this layer **promises will exist remotely** but does
not itself materialize, we store precomp metadata so that sync
provenance walks succeed without round-tripping. The JSON object has
the same shape as a single entry in the HTTP precomp map:

```json
{"unwraps_to": "<id_string>", "ctx_node": "<id_string>", "backstep": "<id_string>"}
```

Any subset of the three keys may be present. Precomp files only exist
for IDs that are remote-from-this-layer's-perspective, i.e. IDs that
appear as pointers in this layer's data (or pins) but are not in any
local-data folder (this layer's `data/` or some parent's `data/`).
Pointers to entities in another local layer don't need precomp here —
we'll read them from that layer's filesystem.

## `LayeredFileStoreReader`

Lives in `fxtr2/data/layered_file_store/reader.py`. Read-only.
Implements `FxtrReadNamespace`. Mirrors the HTTP namespace pattern.

### Construction

```python
class LayeredFileStoreReader(FxtrReadNamespace, anyio.AsyncContextManagerMixin):
    def __init__(
        self,
        layer_dir: str | os.PathLike,
        *,
        remote: tuple[DeepFetchable[Any], Lease[Any]] | None = None,
        isolate_scope: bool = False,
        name: str = "layered_file_store",
    ): ...

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncIterator[Self]:
        ...
```

- `layer_dir` is the root layer (the "top of the stack"). Its
  `parents.json` defines the initial parent set.
- `remote` is an optional `(DeepFetchable, Lease)` pair that the reader
  uses to resolve IDs known to be remote. The lease is one the caller
  minted; the reader clones it as needed and discards its clone on exit.
  If `remote is None`, encountering a remote-tagged ID at load time
  raises `LayerRemoteNotConfiguredError`.
- `isolate_scope` mirrors `HttpFxtrReadNamespace`.

### Async CM lifecycle

The reader is an `anyio.AsyncContextManagerMixin`. Its
`__asynccontextmanager__` nests:

1. Validate the layer dir: read `layer_format.json`, reject unknown
   versions, read `pins.json` and `parents.json`. (The actual data /
   precomp dirs are read lazily; the search-set data structure is
   constructed here so retrieves don't have to.)
2. `async with UnconditionalLocalLifetime("layered_file_store") as backend_lifetime`.
3. `async with FxtrScope(name=self._name, isolated=self._isolate_scope) as scope`,
   then immediately call `scope._mark_not_deeply_fetchable()`. We
   deliberately don't expose deep-fetch on the reader's scope — the
   remote (if any) is used *internally* by `LayeredFileArena` when
   it hits a precomp-only entry, not via the scope's deep-fetch path.
4. `yield self` — caller uses `retrieve` / `list_pins`. On each
   `retrieve` we build a *fresh* `LayeredFileArena` over the
   reader's search set (with the remote `DeepFetchable` + lease
   wired in so the arena can resolve remote-only entries on
   `load_flat`), attach the requested entity into the scope via
   `lazily_attach_from_arena`, then call `arena.close()` — same
   per-call arena lifecycle as `HttpFxtrReadNamespace`'s retrieve
   path.

Teardown is the inverse of the nested `async with` blocks. No manual
`__aexit__` plumbing.

### Search-set machinery

The reader maintains a "search set" of layer directories: the set of
layer dirs whose `data/` we consult when looking for entity bytes. It
starts with `{layer_dir} ∪ parents_of(layer_dir)` and grows
**dynamically** as we walk:

- When we successfully read a CBOR entity from a layer at path P that
  isn't already in the search set, we union in the parents declared by
  P's own `parents.json` before recursing into that entity's deps.

We hold this on the reader (not per-arena), as a `dict[Path,
LayerDirEntry]` where `LayerDirEntry` carries the cached `pins.json` /
`parents.json` contents and a flag for "have we already added this
layer's parents to the search set." Looking up an ID does
`sorted(self._search_set)` (or whatever ordering policy we settle on) and
returns the first hit.

Cycle handling: we union parents idempotently (the dict's key is the
canonicalized abspath), so a cycle in `parents.json` just stops adding
new entries. We don't expect this to happen in practice — layers are
write-once and not modified after creation, so a cycle would require
someone to construct one adversarially — but the search-set logic is
robust against it anyway.

### `LayeredFileArena`

Lives in `fxtr2/data/layered_file_store/arena.py`. A `ReadableArena[Any]`
parameterised on the reader's backend lifetime. Closely modeled on
`HttpReadableArena` — same precomp dict shape, same tracked-set
discipline — with disk reads in place of HTTP requests, plus an
internal "fall through to the remote" path for precomp-only entries.

Construction:

```python
LayeredFileArena(
    backend_lifetime,
    search_set,                  # the reader's _LayerSearchSet
    remote: tuple[DeepFetchable[Any], Lease[Any]] | None,
)
```

The arena holds the remote pair directly; it doesn't go through the
reader's scope to reach it. The lease the arena gets is a *clone*
the reader minted on entry; the arena does not own the lifetime.

Method behavior:

- `load_flat`: walk the search set looking for
  `data/<id_string>.cbor`. On hit, decode and return — same as
  `HttpReadableArena.load_flat`, populating the tracked-set and
  ingesting any sibling `precomp/<id>.json` opportunistically.

  On total miss for `data/`, look for `precomp/<id_string>.json` in
  the search set:
  - If a precomp file exists *and* `self._remote is not None`: call
    `remote_arena = await remote_dfetch.try_lookup(node_id)`;
    if it returns `None`, raise `LayerEntityMissingError`. Then
    `flat = await remote_arena.load_flat(remote_lease.clone(),
    node_id)`, close the temporary remote arena, return the flat
    data. (The bytes get returned exactly as if they had come from
    a local data file; the calling scope never sees the difference.)
  - If a precomp file exists but `self._remote is None`: raise
    `LayerRemoteNotConfiguredError`.
  - If neither data nor precomp exists anywhere in the search set:
    raise `LayerEntityMissingError`.

  **Why it's safe to close the temporary remote arena immediately:**
  the contract of `DeepFetchable` is exactly that any ID can be
  resolved independently via `try_lookup(id)` — there's no
  "you must keep this arena open to walk its children" requirement.
  `load_flat` already returns the deps list as part of the flat
  data, and when the scope later wants to follow one of those deps,
  the `LayeredFileArena` will be asked to `load_flat` it (because
  the scope's memory store doesn't have it yet); that hits the same
  remote-fallback path and issues a fresh `try_lookup`. So the
  per-precomp-hit remote arena is just a one-shot transport for the
  bytes; nothing depends on its survival. (Contrast with an arena
  obtained from, say, a pin retrieval that tracks a specific
  subgraph — that one *does* need to stay open to follow children.
  We avoid that pattern here because we're going through the
  deep-fetch interface, not the per-pin retrieve interface.)

- `load`: same as `HttpReadableArena.load` but reading from disk;
  populates the tracked-set + ingests precomp opportunistically.
  Uses the same fallback path as `load_flat` when the entity isn't
  materialized in any layer.
- `is_tracked`: as in HTTP.
- `unwrap_provenance` / `c_node_to_context_data` /
  `p_node_to_pathroot` / `step_back_p_ctx`: precomp dicts are
  populated from two sources — (a) the `precomp/` JSON files
  encountered while walking, (b) on-the-fly closure walks of CBOR we
  loaded. We don't precompute the whole closure on open; we lazy-fill
  as we touch entities.
- `copy_to_new_arena`: mirrors HTTP — fork tracked set + precomp
  dicts (and carry over the remote pair, with a fresh `lease.clone()`).
- `close`: clear tracked-set + precomp dicts.

### How remote resolution flows

The reader's scope is explicitly marked *not* deeply fetchable
(`scope._mark_not_deeply_fetchable()` in the CM body), and the remote
`DeepFetchable` is **not** registered on the scope. Remote resolution
is entirely internal to `LayeredFileArena`: when `load_flat` hits a
precomp-only entry, the arena synthesizes the flat data by calling
the remote's `try_lookup` + `load_flat` inline and returning the
result. From the scope's point of view, the layered arena is just a
normal arena that happens to have all the data it needs.

This keeps deep-fetch complexity out of this stage: we don't try to
add a new `DeepFetchable` shim, we don't have to think about
scope-level fall-through ordering, and the caller can't accidentally
reach the remote via a deep-fetch path we didn't intend.

### Public API

```python
class LayeredFileStoreReader(FxtrReadNamespace):
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *a) -> None: ...
    @property
    def scope(self) -> FxtrScope: ...
    async def _retrieve_impl(...) -> FxtrID | EntityRef[T] | None: ...
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]: ...
```

Note `list_pins` returns *only this layer's* pins, matching the
"pins are layer-local" rule.

## `LayeredFileStoreIsolatedWriter`

Lives in `fxtr2/data/layered_file_store/writer.py`. Write-only. Async
CM.

### Construction

```python
class LayeredFileStoreIsolatedWriter(anyio.AsyncContextManagerMixin):
    def __init__(
        self,
        output_dir: str | os.PathLike,
        *,
        parents: list[str | os.PathLike],
        remote: tuple[DeepFetchable[Any], Lease[Any]] | None = None,
        name: str = "layered_file_store_writer",
        validate_on_close: bool = True,
    ): ...

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncIterator[Self]:
        ...
```

- `output_dir` is where the new layer will be written. **Must be
  empty (or non-existent — we create it) on entry; non-empty is a
  hard error.**
- `parents` is the parents.json list that will be persisted into the
  output layer. The writer uses the same `_LayerSearchSet` machinery
  internally for "does this entity already exist in a parent layer?"
  queries — direct disk lookups, no namespace stack.
- `remote` works the same as for the reader, used by `mark_as_remote`
  (validates id existence + fetches precomp) and by `preload` (the
  wiretap arena's fetch path). Required for `mark_as_remote` and
  `preload`; optional if only `pin_to_this_layer(...)` paths are used.
- `validate_on_close` (default `True`) toggles the closure-validity
  pass on clean exit (see "Validation pass on close" below). Disable
  for large layers where the extra pass is prohibitive, or when
  building up a layer in stages that won't be self-contained until
  later.

### Async CM lifecycle

The writer is an `anyio.AsyncContextManagerMixin`. Its
`__asynccontextmanager__` does:

1. Create `output_dir` (error if it exists and is non-empty).
2. Build search-set machinery directly from `parents` (reuse the
   `_LayerSearchSet` infrastructure from the reader). Used to answer
   "does this ID already exist in a parent layer (as data or
   precomp)?" via direct disk-level lookups.
3. If `remote` is configured, set up the internal scope + wiretap:
   - Open an `UnconditionalLocalLifetime("writer_wiretap")` for the
     wiretap arena's lifetime.
   - Construct a `_WiretapArena` (private class, defined in
     `writer.py`) over that lifetime + the writer's remote.
   - Open an isolated `FxtrScope(name=..., isolated=True,
     autostart=False)` and call `_mark_not_deeply_fetchable()`. This
     is the writer's internal load surface — only the wiretap is
     reachable from it.
   - Hold these for the body's lifetime; tear down on exit.
4. `yield self` — caller uses `mark_as_remote` / `pin_to_this_layer`
   / `preload`. Each call writes `data/` and `precomp/` files
   incrementally, but the in-memory pins dict is *not* persisted
   until exit.
5. **Finalization on clean exit (after the `yield`)**:
   1. If `self._validate_on_close`, run the closure-validity pass
      (see below) over everything we wrote in `data/`. If any dep
      is unresolvable, raise — the layer is *not* finalized.
   2. Atomically write `pins.json` and `parents.json` (tmp + rename).
   3. Write `layer_format.json` *last* as the "this layer is
      complete" marker.

   On exception during the body (including a failed validation
   pass), skip steps 2–3 entirely — the directory is left without
   `layer_format.json`, so a follow-up read fails cleanly with the
   version error and partial writes can't be mistaken for a
   finished layer.

   Finalization runs *before* the wiretap scope / lifetime teardown
   so the scope can serve any in-flight reads up to the yield exit.

### Validation pass on close

If `validate_on_close=True`, the writer walks every
`data/<id>.cbor` it wrote in `output_dir`, parses each file's deps
list, and asserts that for every dep ID:

- It is in this layer (`data/` or `precomp/`), **or**
- It is in some layer reachable through the writer's `_LayerSearchSet`
  (parent's `data/` or `precomp/`, transitively).

Any unresolvable dep raises a clear error naming the offending entity
and its missing dep, *before* `layer_format.json` is written, so the
broken layer can't be mistaken for a finished one. The check uses
the same disk-level lookups as `pin_to_this_layer`'s "is it already
covered?" guard, so it adds one pass over the layer's own `data/`
folder plus per-dep filesystem lookups in the search set.

Disable with `validate_on_close=False` for large layers where the
extra pass is prohibitive, or when building up a layer in stages.

When `remote` is configured, the writer owns an isolated internal
fxtr scope and a `_WiretapArena` attached to it. This is the load
surface that `preload(ref)` delegates to — `await self._scope.load(ref)`
goes through the wiretap, which fetches from the remote and writes the
fetched data to disk as a side effect. `mark_as_remote(id)` is
responsible for attaching new ids into the wiretap so subsequent
`preload(ref)` calls can find them. `pin_to_this_layer` still accepts
an optional `from_scope` for the "bring in new data from a build
scope" case; if `id` is already known here (data/precomp/known_remote)
the from_scope is unused.

When `remote` is *not* configured, there's no wiretap or internal
scope; only `pin_to_this_layer` with an explicit `from_scope` works.

### Internal `_WiretapArena`

A private `ReadableArena[B]` defined inside `writer.py`. Backs the
writer's internal scope so `preload(ref)` can just delegate to
`self._scope.load(ref)` and get loaded entities back. The wiretap's
`load_flat` is where the "fetch from remote + save to disk" side
effect lives.

Construction: `_WiretapArena(writer, lifetime)`. Holds a reference to
its enclosing writer (for output_dir, search_set, and the configured
remote) and its own backing lifetime.

State, mirroring `LayeredFileArena`:
- `_tracked: set[FxtrID]`
- precomp dicts populated lazily as ids are encountered.

There is **no per-arena `known_remote` set** — the on-disk precomp
files (in this layer or any parent) are the canonical "this id is
remote-resolvable" signal, and `_has_locally_or_in_parents(id)` is
the check used by `preload`'s precondition.

Surface: standard `ReadableArena` methods. The non-trivial one is
`load_flat`:

1. `remote_arena = await writer._remote_dfetch.try_lookup(id)`.
   Raises `LayerEntityMissingError` if `None`. (We don't precheck
   any known-remote membership — the memory store routes loads here
   only for ids it considers reachable via this wiretap, so being
   asked about a truly-unknown id surfaces as the remote returning
   None.)
2. `flat = await remote_arena.load_flat(writer._remote_lease.clone(),
   id)`.
3. Write data file (`fmt.write_data_file(writer._output_dir, flat)`).
4. Delete this layer's precomp file for `id` if present (the data
   file supersedes any "marked-as-remote-here" promise).
5. For each dep in `flat.deps` not already covered locally / in
   parents, compute its precomp via
   `compute_precomp_closure(remote_arena, ..., uncovered_deps)` once,
   then write `precomp/<dep>.json` for each. This is the persistent
   "the loaded entity's deps are also remote-resolvable" signal —
   subsequent `preload(dep_ref)` calls see precomp on disk and
   succeed.
6. Expand `_tracked` with each dep so `is_tracked` queries work.
7. Close the temporary remote arena.

This method is the *only* place fetching and disk-writing both
happen. `mark_as_remote` doesn't fetch entity bytes — it only
records the precomp.

### Methods

#### `mark_as_remote(entity_id: FxtrID) -> None`

- Async.
- Validates `self._remote_dfetch is not None`; otherwise raises
  `LayerRemoteNotConfiguredError`.
- Error if `data/<entity_id_string>.cbor` already exists for this id
  in this layer (the data file would supersede the precomp signal,
  and mark_as_remote contradicts that).
- **Skip-when-covered**: if `_has_locally_or_in_parents(entity_id)`
  is True (the id already has data or precomp in this layer or any
  parent), return immediately. No remote round-trip, no precomp
  re-write. A child layer doesn't need to re-mark something a parent
  already covered.
- Otherwise: calls `remote.try_lookup(entity_id)` (else
  `LayerEntityMissingError`), computes the precomp entry
  synchronously via `compute_precomp_closure(arena,
  remote_lease.clone(), [entity_id])`, writes
  `precomp/<entity_id_string>.json`, closes the temporary remote
  arena.
- **Does *not* touch the wiretap.** Wiretap attach is preload-driven
  (see `preload`). This is what lets a child layer `preload` a
  parent-marked id without re-marking it.

#### `preload(ref: FxtrID | EntityRef[T]) -> FxtrCbor | T`

- Async.
- Overloads:
  - `preload(self, ref: FxtrID) -> FxtrCbor` — returns the loaded CBOR.
  - `preload[T: Entity](self, ref: EntityRef[T]) -> T` — returns the
    decoded entity.
- Validates the writer was configured with a remote (i.e. the
  internal scope + wiretap exist); otherwise raises
  `LayerRemoteNotConfiguredError`.
- **Precondition**: `_has_locally_or_in_parents(entity_id)` must
  hold — the id must have a data or precomp file somewhere in this
  layer's stack. Otherwise raises `LayerEntityMissingError` (it's
  not something the writer knows about). For mark-as-remote'd ids
  this is true via precomp; for parent-marked ids it's true via
  the parent's precomp; for transitive deps it's true via precomp
  files the wiretap's `load_flat` wrote on prior preloads.
- **Lazy wiretap attach**: if `entity_id` hasn't been attached yet
  this writer session (tracked via `_wiretap_attached: set[FxtrID]`
  on the writer), call
  `self._scope._main_arena.lazily_attach_from_arena(scope_lease,
  entity_id, self._wiretap, IndependentLocalBridge(...))` and add
  to the dedup set. This is the only path that wires up the
  wiretap routing for `id`.
- Delegates: `await self._scope.load(ref)` for the EntityRef form,
  or `await self._scope.load_cbor(ref)` for the bare-FxtrID form.
  The scope routes the load through the wiretap, whose `load_flat`
  does the fetch + disk-write + dep-precomp. Returns whatever the
  scope returns.
- This is the **only** "fetch + save data" entry point — there is
  no separate `preload_data` method.

#### `pin_to_this_layer(name: str, entity_id: FxtrID, from_scope: FxtrScope | None = None) -> None`

- Async.
- Errors if `name` is already in this writer's pins map (single layer:
  no duplicate pin names allowed).
- **Fast path — entity already known**: if `entity_id` is covered
  locally (`data/<id>.cbor` or `precomp/<id>.json` exists in
  `output_dir`) or in any parent layer (the writer's
  `_LayerSearchSet` says so), just record the pin and return.
  `from_scope` is ignored in this path (the data is already in our
  story).
- **Walk path — bringing in new data**: if `entity_id` isn't known
  and `from_scope` is provided, walk it the same way as before:
  - `source = from_scope._main_arena`,
    `src_lease = from_scope._main_lease.clone()`.
  - `source_copy = source.copy_to_new_arena(src_lease.clone(),
    [entity_id])` to isolate the walk.
  - DFS post-order via worklist (`Discover` / `WriteData` dataclass
    items), structurally similar to
    `SQLiteNamespaceBackend._discover_sweep_for_copy`. Stop at any
    id already known (data/precomp here or in a parent layer);
    otherwise `load_flat` and enqueue `WriteData(flat)` + per-dep
    `Discover`. Post-order writes deps first.
  - For each `WriteData(flat)`: write `data/<flat.entity_id_string>.cbor`
    atomically (tmp + rename), encoded via
    `remote/transfer_format.py`.
  - Close `source_copy` after the walk.
- **No-walk no-scope error**: if `entity_id` isn't known and
  `from_scope` is `None`, raise `ValueError` — we have nothing to
  walk from. (The error message says "id not known; pass
  `from_scope=` to bring in the data".)
- After either path, record the pin in `self._pins` (persisted on
  clean exit only).

### Internal helpers

- `_have_locally(id) -> bool` — checks this layer's `data/` and
  `precomp/`.
- `_have_in_parents(id) -> bool` — uses the writer's parent-layer
  search-set machinery to check whether the ID is anywhere in the
  parent stack (data or precomp).
- `_write_data_file(id, flat_data)` — encode `FxtrEntityFlatData` to
  CBOR and write atomically (tmp + rename).
- `_write_precomp_file(id, entry)` — write JSON atomically.

## Error types

In `fxtr2/data/layered_file_store/errors.py`:

- `LayerFormatVersionError(int_version)` — reader hit an unknown
  `layer_format.json` version.
- `LayerRemoteNotConfiguredError(fxtr_id)` — `LayeredFileArena.load_flat`
  needed the remote but no `(DeepFetchable, Lease)` pair was
  configured.
- `LayerEntityMissingError(fxtr_id)` — an entity referenced by this
  layer (or a parent) is genuinely missing: not in any local data
  folder, no precomp file, *or* precomp exists but the configured
  remote returned `None` from `try_lookup`.
- `LayerOutputDirNotEmptyError(path)` — writer found non-empty output.

## Testing plan

In `tests/test_layered_file_store/`:

- `test_format.py` — write + read a single layer; round-trip pins +
  data; reject mismatched schema version; reject non-empty output
  directory on writer open; reject a layer dir that's missing
  `pins.json` or `parents.json` (even if the data dir is well-formed).
- `test_multi_layer_walk.py` — write a "base" layer, write a "child"
  layer that has the base as a parent, read the child and verify
  loads transparently reach into the base. Includes the dynamic
  parent-walk case: child → mid → base, where mid's parents.json
  contains base, and base isn't in child's parents.json — the
  reader should pick up base after first reaching into mid. Also
  covers a `parents.json` cycle (a ↔ b): the reader doesn't loop
  forever. (Not an expected case in practice — layers are write-once
  — but the search-set dedup makes it cheap to cover.)
- `test_pin_isolation.py` — pins are layer-local: `list_pins` on a
  child returns only the child's pins, not the parent's; same name
  pinned in parent and child resolves to the child's version *only*
  if the user calls retrieve on the child.
- `test_remote_resolution.py` — using a stub `DeepFetchable`
  (in-memory dict, not actual HTTP):
  - `mark_as_remote` writes precomp; subsequent `LayeredFileStoreReader`
    can resolve via the same stub at read time.
  - Read with no remote configured raises
    `LayerRemoteNotConfiguredError`.
  - `preload(ref)` promotes a marked-remote entity to a data entity,
    returns the loaded entity (via the wiretap's
    decoded-by-scope path), and writes precomp for newly-exposed
    deps; transitively, `preload` on a dep that wasn't explicitly
    marked-as-remote but was discovered as a dep of a previously
    preloaded entity succeeds (the wiretap's `_known_remote` set
    expands on each load).
- `test_writer_walk.py` — `pin_to_this_layer` walks transitively when
  `from_scope` is supplied; data already in parents isn't redundantly
  stored; nodes unreachable from `from_scope` raise `ValueError`.
  Also covers the "parent-precomp-inherited" case: when a dep is only
  in a parent's `precomp/` (marked remote there), the walk stops at
  that node and the child layer does *not* re-mark it locally —
  future readers resolve via the parent's precomp → remote. Plus
  the "fast path" case: pinning an id already known here (via
  `mark_as_remote`, or in a parent layer) with `from_scope=None`
  just records the pin.
- `test_writer_cleanup.py` — exception during writer body leaves the
  directory without `layer_format.json`, so a follow-up read fails
  cleanly with the version error.
- `test_writer_validation.py` — `validate_on_close=True` (the
  default) rejects a layer whose data files reference deps not
  resolvable in this layer or any parent (no `layer_format.json` is
  written). `validate_on_close=False` lets the same layer through;
  reading it later surfaces the issue lazily.
- `test_seed_precomp.py` — pin retrieval populates the seed's
  precomp closure into the reader's per-retrieve arena so sync
  `unwrap_provenance(seed)` succeeds *before* any explicit
  `.load()`. Covers a `u`-node pin.
- `test_transitive_remote.py` — marked-remote parent transitively
  pulls unmarked deps through the reader's `_known_remote`
  expansion; an id that's neither local-data nor known-remote-via-
  any-source raises `LayerEntityMissingError`.
- `test_preload_returns_entity.py` — `preload(EntityRef[T])`
  returns the decoded `T`; chained `preload(parent)` then
  `preload(parent.child)` works without an explicit
  `mark_as_remote` on the child (the wiretap's `load_flat` writes
  precomp/<dep>.json on the parent load, so the dep is "known
  remote" on disk); `pin_to_this_layer` fast-path with no
  `from_scope` after `mark_as_remote`; error when pinning an
  unknown id with no `from_scope`.
- `test_cross_layer_preload.py` — child layer preloads an id that
  was marked remote *by a parent* without re-marking it locally
  (the wiretap attach inside `preload` is lazy and per-writer, not
  driven by `mark_as_remote`); `mark_as_remote` is a no-op when
  the id is already covered upstream (no redundant precomp file).

## Implementation steps

Code under `fxtr2/data/layered_file_store/` is new — no migration
risk.

- [x] 1. **Scaffold the package.** Create
   `fxtr2/data/layered_file_store/__init__.py` (re-exports
   `LayeredFileStoreReader`, `LayeredFileStoreIsolatedWriter`, the
   error types, and any format dataclasses we want public — pattern
   matching `fxtr2/data/git_store/__init__.py` with an explicit
   `__all__`), `format.py` (constants, dataclasses for
   `LayerFormatV1` etc.), `errors.py`. No top-level
   `fxtr2/data/__init__.py` exists today, so no parent-level
   re-export needed — callers import from
   `fxtr2.data.layered_file_store` directly.
- [x] 2. **On-disk format codec.** In `format.py`, helper functions
   `read_pins`, `read_parents`, `read_format_version`,
   `read_data_file_flat`, `read_precomp_file`, plus the mirror writer
   helpers (with atomic tmp+rename). Use the existing
   `remote/transfer_format.py` CBOR encoder for the flat-data file
   contents.
- [x] 3. **Search-set data structure.** A `_LayerSearchSet` helper class in
   `search.py` that holds `dict[Path, LayerDirEntry]`, lazily reads
   `parents.json` for layers it touches, and exposes
   `find_data(id) -> Path | None` / `find_precomp(id) -> Path | None`.
   This is shared by reader and writer.
- [x] 4. **Lift `_compute_precomp_closure`.** Move it out of
   `fxtr2/experimental/http/asgi_app.py` into a shared spot (likely
   `fxtr2/data/arena_util.py`); update `asgi_app.py` to import the
   relocated helper. Pure refactor.
- [x] 5. **`LayeredFileArena`.** In `arena.py`. Implement the `ReadableArena`
   surface, modeled directly on `HttpReadableArena`. Use the search
   set for lookups; on precomp-only hit, fall through inline to the
   configured remote `DeepFetchable` and synthesize the flat data
   from `remote.try_lookup(...).load_flat(...)`.
- [x] 6. **`LayeredFileStoreReader`.** In `reader.py`. Use
   `anyio.AsyncContextManagerMixin` with `__asynccontextmanager__`,
   `FxtrReadNamespace` impl, following the `HttpFxtrReadNamespace`
   shape (but with the mixin pattern, not manual `__aenter__` /
   `__aexit__`). Backend ULL, scope (marked not deeply fetchable),
   per-retrieve `LayeredFileArena` instances. The remote is passed
   into the arena directly so it can fall through inline on
   precomp-only loads; no deep-fetch registration on the scope.
- [x] 7. **Update `CODE_OVERVIEW.md` mid-flight** with the new package
   listing before moving on to the writer + tests.
- [x] 8. **`LayeredFileStoreIsolatedWriter`.** In `writer.py`. Use the same
   `anyio.AsyncContextManagerMixin` pattern. The walks reuse
   `arena_util.visit_ids`. Atomic pins/parents/format finalization
   on clean exit; skip finalization on exception so partial layers
   are unreadable. Includes `validate_on_close` pass.
- [x] 9. **Tests.** As above. Run with `uv run pytest -n auto` from `v2/`.
- [x] 10. **Add `LOCAL_CODE_OVERVIEW.md`** for
   `fxtr2/data/layered_file_store/` and finalize `CODE_OVERVIEW.md`.

## Open questions / things to revisit

- **`copy_to_new_arena` on `LayeredFileArena`.** The HTTP version copies
  the tracked-set and precomp dicts; we'll do the same. Note that the
  copy shares the same backend lifetime — by design, since arenas are
  views over the same data.
- **Filename safety.** `id_string()` is already filesystem-safe
  (base32-style), so we can use it directly without escaping.
- **Update CODE_OVERVIEW.md** — last step.

## Incidental changes outside `layered_file_store/`

These came up during implementation and are part of the same diff,
captured here so a reviewer doesn't get surprised:

- **`fxtr2/data/arena_util.py`** — lifted `_compute_precomp_closure`
  out of `fxtr2/experimental/http/asgi_app.py` and exposed it as
  `compute_precomp_closure`, alongside a new
  `PrecompEntry` TypedDict + `PrecompMap = dict[str, PrecompEntry]`
  type alias. `asgi_app.py` updated to import the shared helper; the
  layered-file writer uses the same. No behavior change.
- **`fxtr2/data/memory_arena.py`** — `MemoryArena.copy_to_new_arena`
  now wraps its body in `with lease:` so an early raise (untracked
  id, mismatched subject) cleanly discards the supplied lease instead
  of leaking it. `discard` is idempotent, so the trailing
  with-exit discard is a no-op on the happy path where `new_arena`
  already consumed the lease. Surfaced by a layered_file_store
  test that called `pin_to_this_layer` with an untracked root.
- **`fxtr2/data/entity_ref_scope.py`** — `FxtrScope.__init__` and
  `_with_derived_lifetime` now accept `autostart: bool = True`. When
  set to `False`, the bare-usable auto-start branch is suppressed
  (the scope stays in pre-CM state until `__aenter__`), leaving a
  window for the caller to call `_mark_not_deeply_fetchable` before
  the flag commits. The default is unchanged so existing callers
  see no difference. The layered reader uses `autostart=False` so it
  can mark its scope not-deeply-fetchable regardless of the
  `isolate_scope` toggle — previously this would have required
  `mountable=True` (opening an empty `anyio` task group per reader
  invocation), which felt worse.

## Followups landed in support of part 2

These came up while writing part 2's plan and are part of the same
overall change set:

- **`LayeredFileArena` `_known_remote` set.** Refined the load
  semantics: an id is "known to be remote-resolvable" iff (a) some
  layer in the search set has a `precomp/<id>.json`, or (b) it was a
  dep of an entity previously loaded from the remote (by fxtr2's
  complete-subgraph invariant). `load_flat`'s fall-through to remote
  is gated on this set instead of requiring an explicit per-id
  precomp file. This lets `mark_as_remote(c)` implicitly cover c's
  unmarked descendants (d, e) — the demo in part 2 relies on this.
  New tests: `tests/test_layered_file_store/test_transitive_remote.py`.
- **`HttpFxtrReadNamespace.as_deep_fetch_source()`** — public
  accessor returning `(DeepFetchable, Lease)`, the shape the
  layered-store writer + reader consume as their `remote=` argument.
  Replaces what would have been a private-API peek in the preload
  FOD's workload script.
