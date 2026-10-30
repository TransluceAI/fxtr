# Stage 11 — Nix Layers (overview)

## Goal

Open up a clean way to use **nix** to drive reproducible, sandboxed
workflows that produce and consume fxtr2 entities. Today fxtr2 is good at
imperative work: open a namespace, build entities, ship them over RPC,
pin them somewhere. Increasingly we want to express things like
"sandboxed execution environment + configuration entity → output
entity," or "fxtr entity + MDX bundle → renderable react tree entity,"
in a way that's reproducible, content-addressed, and naturally
composable. Nix flakes are very good at this shape; this stage builds
the bridge layer between fxtr's content-addressed entity store and the
nix-flake world.

The strategy is to factor cleanly:

- **Nix** is in charge of *building* and *configuring*: it describes the
  derivations, schedules them, sandboxes them, content-addresses their
  outputs. Nix doesn't know anything about fxtr semantics; it just sees
  derivation inputs and outputs as files.
- **fxtr2** is the *runtime*: it loads entities lazily through whatever
  namespaces / remote stores are available, and exposes the standard
  scope / arena / namespace APIs. fxtr doesn't know anything about nix;
  it just sees a particular file layout that happens to be produced by
  derivations.
- The bridge between them is a filesystem convention called a
  **layered file entity store**: a directory containing CBOR entity data
  + small JSON metadata, with a parent pointer to other layered file
  entity stores. Nix derivations emit these directories as outputs; fxtr
  reads them at runtime; the layering makes it natural for derivations
  to build on each other's results without copying the world.

## The pieces

There are three conceptual pieces; this stage builds the first one in
detail (part 1) and prototypes the second + third in a folder
(part 2). The third piece — nix tooling helpers, a `fxtr_imports.toml`
helper function, etc. — is left for a later stage once we understand
the shape of part 2's prototypes.

### 1. The layered file entity store (part 1)

A filesystem convention for representing "a layer of new fxtr entities
on top of a stack of parent layers, with anything else assumed to live
in a runtime-configured remote store." A layer is a directory with:

- `pins.json` — human-readable keys to FxtrIDs (this layer's own pins
  only; parent pins are *not* visible through this layer).
- `parents.json` — a list of absolute paths to other layered file
  entity store directories that this layer reads on top of.
- `data/<id_string>.cbor` — CBOR-encoded `FxtrEntityFlatData` for
  entities materialized in this layer.
- `precomp/<id_string>.json` — precomputed metadata
  (`unwraps_to` / `ctx_node` / `backstep`) for entities that this layer
  has *promised* will exist in the remote store but does not itself
  store. The precomp lets sync provenance walks succeed without round
  tripping to the remote.
- `layer_format.json` — `{"version": 1}` schema stamp.

The layer is read via a `LayeredFileStoreReader` (which behaves like a
`FxtrReadNamespace` and lazily loads data through an attached arena,
mirroring how `HttpFxtrReadNamespace` works) and written via a
`LayeredFileStoreIsolatedWriter` (an async CM that consumes a fxtr scope
and emits a fresh layer directory).

The remote store is injected as a `DeepFetchable[Any]` + a `Lease[Any]`;
the layered store knows nothing about *what* the remote is (HTTP, hub,
etc.) — it just knows that entities marked as remote can be resolved
through it at runtime, and that resolution failure is a clean runtime
error.

See `part1_layered_file_store.md` for the full design and
implementation plan.

### 2. Nix derivations that use the layered file store (part 2)

A folder of prototypes (`examples/nix_prototype/`) that wires the
layered file store into actual nix flakes. The goal is to demonstrate
the full pipeline end-to-end and surface design issues before we
commit to permanent tooling. Concretely:

- **Fixed-output derivations** that "preload" entity data: HTTP-fetch
  some subset of remote entities and write them to a layered store
  directory. Hash via `lib.fakeHash` on first use.
- **Input-addressed derivations** that take preload outputs (and/or
  each other) as inputs and emit new layered stores: their Python
  workload opens a `LayeredFileStoreReader` over the input dirs +
  parents, builds new entities in a `FxtrScope`, and writes them
  with `LayeredFileStoreIsolatedWriter` into the derivation's
  output path. The remote store is *not* accessible during sandboxed
  builds; any remote entities the build needs must already have been
  preloaded by a FOD upstream.
- A **demo runner** that orchestrates the above: stands up a fake
  HTTP remote, drives `nix build`, then deeply loads the produced
  entities through a `LayeredFileStoreReader` configured with a
  *different* remote (to prove the remote-store config is dynamic and
  not baked into the layer).

See `part2_nix_exploration.md` for the design ideas and open
questions.

### 3. Nix tooling helpers (future)

Once the part 2 prototypes work end-to-end and we understand the
ergonomics, we'll wrap the pieces in a `fxtr_imports.toml` /
`fxtr_imports.lock` convention plus a nix-side helper that generates
the preload FODs from those files. This is explicitly out of scope for
this stage; it'll get its own plan once part 2 is in.

## Out of scope

- Persisting provenance through the layered file store: pins and the
  on-disk format store *bare data IDs only*, in keeping with the
  current `FxtrNamespace` convention. Users who want provenance can
  use `FxtrScope.embed_as_provenance_parent` / `extract_provenance_parent`
  themselves before pinning / after retrieving.
- Concurrent writers: the writer assumes single-writer ownership of the
  output directory. We hard-error if the output directory is non-empty
  on open.
- Migrations: schema version is stamped (`layer_format.json` carries
  `{"version": 1}`) and unknown versions are rejected, but we don't
  yet ship any migrations.
- Garbage collection / compaction inside a layer: layers are write-once.
- Nix tooling helpers (`fxtr_imports.toml`, FOD generation): future work
  once part 2 has settled.

## Interactions with existing systems

- **`FxtrReadNamespace` / namespaces**: the layered file store reader
  is an `FxtrReadNamespace` and uses the same retrieve / pin pattern.
  No changes are needed in the abstract namespace layer.
- **Arenas + `lazily_attach_from_arena`**: the reader uses the same
  pattern as `HttpFxtrReadNamespace` — an internal `ReadableArena`
  per backend lifetime that gets attached into the scope on demand.
- **`DeepFetchable`**: the remote store is passed in as a
  `DeepFetchable[Any]`. The reader itself does *not* implement
  `DeepFetchable`; it's only used through pin-based retrieval.
- **CBOR + precomp shapes**: the data files store
  `FxtrEntityFlatData` directly (entity bytes + deps), and the
  precomp files use the exact same shape produced by
  `_compute_precomp_closure` in `fxtr2/experimental/http/asgi_app.py`
  (id_string keys, `unwraps_to` / `ctx_node` / `backstep` string
  values).
- **`fxtr2.data` submodule**: the new code lives in
  `fxtr2/data/layered_file_store/` as its own package (similar to
  `git_store/`).

## Roadmap

- **Part 1 — Layered file store.** Build `LayeredFileStoreReader` and
  `LayeredFileStoreIsolatedWriter`, with tests covering: single layer,
  multi-layer parent walks, dynamic parent extension during walk,
  pin isolation between layers, remote resolution success and failure,
  `mark_as_remote` / `pin_to_this_layer` / `preload_data` semantics,
  and schema-version rejection.
- **Part 2 — Nix exploration.** Stand up `examples/nix_prototype/`:
  a fake remote HTTP server, a preload FOD, a pure derivation that
  augments entities, and a demo runner that drives the whole thing.
  Capture open questions for the future tooling stage.
- **Future — Nix tooling.** `fxtr_imports.toml` + lockfile, helper
  flake function, ergonomic story for declaring "this build depends on
  these remote entities." Not specified yet.
