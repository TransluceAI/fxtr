# Layered File Entity Store Module Overview

A filesystem convention for representing "a layer of new fxtr entities on top of a stack of parent layers, with anything else assumed to live in a runtime-configured remote entity store." Designed to bridge fxtr2 into and out of nix-style content-addressed derivation outputs (see `plans/phase3/stage11_nix_layers/`), but usable anywhere you want to dump a fxtr scope to disk as a layer that other layers can build on top of.

## Modules

- `errors.py`: Public error types: `LayerFormatVersionError`, `LayerRemoteNotConfiguredError`, `LayerEntityMissingError`, `LayerOutputDirNotEmptyError`, `LayerMalformedError`.
- `layer_format.py`: On-disk format constants and sync read/write helpers. Atomic tmp+rename writes for every metadata file. Encodes data files using the same CBOR wire format as `fxtr2.data.remote.transfer_format` (a 1-entity stream).
- `search.py`: `_LayerSearchSet` — shared parent-walking search-set state used by reader + writer. Dynamic parent expansion: a layer's `parents.json` is read lazily the first time the layer is touched, so transitively-reachable layers join the search set on demand. Also defines `ParentPathRemap`, an optional read-side prefix-swap applied to the absolute paths read from `parents.json` (for reading layer closures that were built elsewhere — e.g. nix store paths materialized into a host bind-mount store; see below).
- `arena.py`: `LayeredFileArena` — `ReadableArena` over the search set. Mirrors `HttpReadableArena` (tracked-set + precomp dicts + opportunistic lazy fill). **Holds no leases** — only a `Grant[B]` for its brand (used to validate the leases callers pass in, and to expose `token` via `grant.subject`). The optional `source` (a `DeepFetchable[B]`) rides on the **same brand `B`**: on precomp-only hits the arena threads the caller's *incoming* lease through to a one-shot `source.try_lookup` + `load_flat`, closing the temporary remote arena immediately (`DeepFetchable`'s per-id contract makes that safe). Because it holds nothing, a clone-and-mount caller governs its lifetime via a bridge.
- `pin_arena.py`: the lower-level seam that **returns an arena** (no scope, no mounting — the caller mounts + owns lifetime). `arena_over_search_set` (core), `resolve_pin` (pin name → its `FxtrID`, layered for a provenanced pin) / `open_seed_arena` (single-layer arena seeded on a given id), and `LayeredStoreDeepFetchable` — a by-id `DeepFetchable[B]` over a whole multi-layer set (search set **fully expanded at construction**, so `try_lookup` is a pure stable read; for registering as a scope-level deep-fetch source, e.g. serving). There is deliberately **no** "open an arena for a pin" helper: the caller `resolve_pin`s, then opens a seed arena on whichever id it actually needs loadable — for a layered pin that is the bare prov node (`id._provenance_parent`, whose `UNWRAPS_TO` pulls the data node along), which it also attaches + `embed`s. Keeping the seed explicit at the call site avoids a "pin arena" whose contents silently depend on layeredness.
- `reader.py`: `LayeredFileStoreReader` — `FxtrReadNamespace` + `anyio.AsyncContextManagerMixin`, reimplemented on the `pin_arena` seam. Brands its arena on the **store**'s grant when a remote is configured (overlay-on-store), else on its own disk lifetime; the reader holds the store lease for its CM and feeds the deep-fetch bridge from it. The reader's scope is explicitly **not deeply fetchable**. Pins are layer-local: `list_pins` / `retrieve` only see this layer's `pins.json`. In a **provenanced** layer each pin is stored as a `[data, prov]` pair, so a pin reads back layered `(data, prov)`: `retrieve` attaches the arena on the bare prov node and reconstructs the layered id via `FxtrScope.embed_as_provenance_parent` (the validated form); `list_pins` returns the stored layered ids directly as a **pure read** (no attach/embed), so it yields the same id as `retrieve`. A bare layer returns ids unchanged. The by-id `LayeredStoreDeepFetchable` never embeds — provenance reconstruction lives only at the pin boundary.
- `nix_builder.py`: the **builder primitive** — run user Python that writes into a layer writer, from inside a nix build (the `lib.fxtr.mkFxtrFnBuilder` / `mkFxtrScriptBuilder` Python half). The nix-agnostic core `_run(entrypoint, *, out_dir, parents, remote, spec, provenanced=False, provenance_info=None)` wires an open `LayeredFileStoreIsolatedWriter` over `$out` (provenanced when the flag is set — pins round-trip provenance) + one open `LayeredFileStoreReader` per named parent + a builder `FxtrScope`, and hands them to `entrypoint(ctx: BuilderContext)`. The entrypoint is mode-agnostic — it pins whatever id it has and the writer projects to the prov form. `amain` reads `builderSpec.provenanced` (default false) + `builderSpec.provenanceInfo` (JSON, frozen via `freeze_fxtr_cbor`; the helper only emits the key when set) from the nix contract and threads them through. Parent readers are opened `isolate_scope=True` and `ctx.scope` is opened non-isolated with `include=[reader.scope, …]` (active), so a bare `ctx.readers["x"].retrieve(key)` lands accessible in `ctx.scope` and entity construction / pin walks default to it. **Purity is whether `remote` is configured**: no remote → an undeclared read fails naturally; remote set (FOD) → `ctx.remote` + `ctx.writer.preload` work. **Default build provenance**: when `provenanced`, `_run` mints a build-step c-node (`nix_build_provenance.mint_build_provenance_c_node`) in a small bare minting scope and opens `ctx.scope` with it as `provenance_context`, so every entity the entrypoint stores carries the build as its provenance with no per-script work. The build kind selects the payload — an input-addressed build (`remote is None`) anchors on `out_dir` (`$out`) + optional `provenance_info`; a fixed-output build (`remote is not None`) has no safe `$out` anchor and so **requires** `provenance_info`. The thin nix-coupled surface — `main(entrypoint)` / `run_entry("mod:fn")` / `amain` / `__main__` — reads the `builderSpec` structured attr (`parents`/`spec`/`entry`/`provenanced`/`provenanceInfo`), `$out`, and builds the remote from `FXTR_REMOTE_*` via `http_deep_fetchable` (lazy import). `_run` owns the master remote lease: hands a clone to the writer + each reader and discards the master last via a stack callback (so a mid-setup raise can't leak it).
- `nix_build_provenance.py`: the nix-build provenance c-node entities + `mint_build_provenance_c_node(scope, *, output_store_path, provenance_info)`. Two `DataclassEntity` payloads under one nix-build `define_type` namespace, split by build kind: `NixInputAddressedBuildProvenance` (`output_store_path` = `$out`, the machine anchor a receiver walks back to the `.drv` via `nix-store --query --deriver`; + optional declared `provenance_info`) and `NixFixedOutputBuildProvenance` (`provenance_info` only — a FOD has no safe `$out` anchor, so the author must declare it). Both carry `value = "."` (c-node unwraps to ground). The helper supplies an explicit ground `$fxtr.ctx` on the entity so the convert→`create_c_node` never depends on an active scope (the c-node is minted before the builder scope exists), selects the payload by `output_store_path is None`, and raises if a fixed-output build has no `provenance_info`. Mirrors `fxtr_sandboxing`'s `SandboxProvenanceContext`.
- `writer.py`: `LayeredFileStoreIsolatedWriter` — write-only async CM. Three public ops:
  - `pin_to_this_layer(name, entity_id, from_scope)`: forks `from_scope._main_arena` via `copy_to_new_arena`, does a DFS post-order walk (mirroring `SQLiteNamespaceBackend._discover_sweep_for_copy`), writes each not-already-covered entity to `data/`.
  - `mark_as_remote(entity_id)`: validates remote has the id (via `try_lookup`), computes the precomp entry synchronously from the arena's pre-populated precomp dicts (no `load_flat` needed — the closure rides with `try_lookup` per the `DeepFetchable` convention).
  - `preload(ref)`: fetches a known-remote entity's bytes into `data/` (via the wiretap scope) and returns the decoded entity.
  - `validate_on_close=True` (default) walks every `data/` file at the end and asserts each dep is resolvable in this layer or some parent; failure leaves the directory without `layer_format.json` so a reader rejects it cleanly.
  - **`provenanced` flag** (recorded in `layer_format.json`) is the **preserve-vs-strip** toggle. Branching is always on the layering (`_provenance_parent`), never on an id's tag.
    - **preserve** (`provenanced=True`): `pin_to_this_layer` records the id in its *natural* form — a layered `(data, prov)` id as a `[data, prov]` pair (`read_pins` returns it layered), a bare id as a plain id (**not** `u`-wrapped). So a provenanced layer can hold mixed pins (genuinely-provenanced → pair, bare → string). The walk seeds on the prov node for a layered id (its `UNWRAPS_TO` pulls the data node + child prov forms along), else on the id itself (no walk change).
    - **strip** (`provenanced=False`, default): the id is stripped to its bare data form (recorded + walked as a plain id), dropping any provenance — byte-for-byte unchanged from a pre-provenance layer.
    - `mark_as_remote` / `preload` split a *layered* id into its two existing ids (the bare prov-node id and the bare data id) and cover **both** closures, so a later reader can serve either the layered or the bare form offline; a *bare* id is handled as its own single id, as in a bare layer.
  - The `_WiretapArena` (the writer's remote-fetch load surface) lazily populates its precomp dicts from on-disk data/precomp files (`_lazy_populate_precomp`, mirroring `LayeredFileArena`), so `unwrap_provenance` / the c/p accessors answer from the deterministic on-disk truth — required once prov nodes flow through the wiretap (a provenanced `preload`), since a stale `None` would mean "no unwrap" rather than "not loaded".

## Key Concepts

### On-disk format

```
<layer_dir>/
├── layer_format.json     # {"version": 1[, "provenanced": true]}  — written LAST on clean exit; absent = malformed layer
├── pins.json             # {"<pin_name>": "<id_string>" | ["<data_id>","<prov_id>"], ...}  (REQUIRED; empty {}; the [data,prov] pair form = a provenanced pin)
├── parents.json          # ["<absolute_path>", ...]            (REQUIRED; empty layer has [])
├── data/
│   └── <id_string>.cbor  # 1-entity FxtrEntityFlatData CBOR stream
└── precomp/
    └── <id_string>.json  # {"unwraps_to": "...", "ctx_node": "...", "backstep": "..."}
```

**Pins are layer-local.** A reader on a child layer sees only `pins.json` in that layer — not parents'. Data walks, however, do reach into parents transitively.

**Data files are bare-data CBOR.** A `data/<id>.cbor` payload never carries a `_provenance_parent` — provenance edges are captured indirectly via `unwraps_to` in precomp (and in the data file's own deps). Provenance is round-tripped at the **pin** level, not inside the CBOR: a pin value is either a plain bare-data id string or a `[data, prov]` pair. A **strip** layer (`provenanced=False`, default) stores every pin as a bare string; a **preserve** layer (`provenanced=True`) stores each pin in its natural form — a layered id as a `[data, prov]` pair, a genuinely-bare entity as a bare string (no `u`-wrap). `read_pins` returns the layered id for a pair (the reader still `embed`s on `retrieve` for the validated form; `list_pins` returns the stored pair as a pure read). The on-disk closure is the same set of bare-data CBOR files regardless; only the `pins.json` value (and the flag) differ.

**Atomic finalization.** The writer writes `data/*.cbor` and `precomp/*.json` incrementally, but `pins.json` / `parents.json` / `layer_format.json` are only written on clean exit (in that order). On exception, finalization is skipped and the directory is left without `layer_format.json` — readers reject it with `LayerMalformedError`, so partial writes can't be mistaken for a finished layer.

### Search-set semantics

The `_LayerSearchSet` keeps a dict of `(canonical_abspath → _LayerDirEntry)` and looks layers up in insertion order. On every `find_data` / `find_precomp` it expands the touched layer's `parents.json` into the set (idempotently), so deeper layers come into view only as we touch their descendants. This gives us:

- **Dynamic walks.** A grandchild → child → parent chain works even when the grandchild doesn't enumerate the parent up-front.
- **Cycle safety.** Canonicalized paths dedup; an a↔b cycle in `parents.json` just halts expansion. Not expected in practice — layers are write-once — but cheap to make robust.

### Parent-path remap (reading relocated closures)

`parents.json` records *absolute* parent-layer paths, which assumes the layer is read where it was built. When a closure is built in one place and read in another — the motivating case is the podman nix backend (`fxtr_nix_runner`), which builds layers at in-container `/nix/store/...` paths then *materializes* (closure-copies) them into a host bind-mount store at `<root>/nix/store/...` — the recorded parent paths don't exist at the read site. `LayeredFileStoreReader(..., parent_path_remap=ParentPathRemap(old_prefix, new_prefix))` rewrites parent paths on the fly with a prefix swap, applied in `_LayerSearchSet.expand_parents_of` (so it covers every depth) and *only* to discovered parents — never the root `layer_dir`, which is already a real read-site path. This is **read-side only**: the writer records real local parent paths and needs no remap. Keeping the hook here means the closure copy-out can stay verbatim (no re-parsing of the layer format in the copier). `ParentPathRemap` is a prefix-pair today; `apply()` is the single seam, so a more general callable could slot in later.

### Remote resolution

`LayeredFileArena` brands on a `Grant[B]` and carries an optional same-branded `source: DeepFetchable[B]`. On a precomp-only hit it threads the caller's *incoming* `Lease[B]` into a one-shot `await source.try_lookup(id)` + `load_flat`, returns the bytes as if found locally, and closes the temporary remote arena. The `DeepFetchable` contract guarantees subsequent dep lookups can be served by a fresh `try_lookup`, so nothing is lost by closing eagerly. The arena holds **no lease of its own** — the caller's lease (from a scope's deep-fetch bridge, or passed directly) covers each operation, so the arena never pins its own lifetime.

The reader's `FxtrScope` is marked `_mark_not_deeply_fetchable()`; the source stays internal to the per-retrieve arena rather than registered as a scope-level deep-fetch source. (The standalone `LayeredStoreDeepFetchable` is the thing you *do* register, for by-id resolution across a whole layer set.)

If no source is configured and a load hits a precomp-only entry: `LayerRemoteNotConfiguredError`. If neither data nor precomp exists anywhere: `LayerEntityMissingError`.

### Lifetime assumption (layer dirs are immutable while in use)

The whole module **assumes its layer dirs are neither deleted nor modified while any reader / arena / deep-fetch source built over them is in use.** This is what makes branding an overlay arena on the *store*'s lifetime sound (the disk layer is treated as available for at least as long as the store session). Consumers that keep arenas/sources alive across a long span (e.g. `fxtr_nix_runner`'s flake manager over materialized nix layers) must keep those dirs present for that span — GC-rooting materialized dirs against `nix` GC is the consumer's responsibility.

### Writer is "isolated" (no internal scope, no backend lifetime)

The writer holds nothing but:
- The search-set machinery (for parent-layer lookups)
- A clone of the caller's remote lease (if a remote was supplied), discarded on exit

`pin_to_this_layer` walks through the *caller's* `from_scope` (after forking via `copy_to_new_arena` for isolation). `mark_as_remote` and `preload_data` talk to the caller-supplied remote directly. There is no fxtr in-memory state to manage on the writer side beyond the in-memory pins dict, which is persisted only at finalize time.

## Public API

Importable from `fxtr2.data.layered_file_store`:

| Name                                | Kind  | Purpose                                              |
|-------------------------------------|-------|------------------------------------------------------|
| `LayeredFileStoreReader`            | class | Read pins / load entities from a layer + parents     |
| `LayeredFileStoreIsolatedWriter`    | class | Emit a fresh layer directory                         |
| `ParentPathRemap`                   | class | Read-side prefix-swap for relocated parent paths     |
| `resolve_pin` / `open_seed_arena` / `arena_over_search_set` | fns | Pin/seed-oriented seam: resolve a pin to its id + return a `LayeredFileArena` (seeded on a caller-chosen id) the caller mounts + owns |
| `LayeredStoreDeepFetchable`         | class | By-id `DeepFetchable` over a whole layer set (register as a deep-fetch source) |
| `LAYER_FORMAT_VERSION`              | int   | Current on-disk format version (1)                   |
| `PrecompEntry`                      | type  | TypedDict for a single precomp entry (re-exported from `fxtr2.data.arena_util`) |
| `LayerFormatVersionError`           | exc   | Unsupported layer format version                     |
| `LayerRemoteNotConfiguredError`     | exc   | Hit a remote-only id with no remote configured       |
| `LayerEntityMissingError`           | exc   | Id genuinely missing from layer + remote             |
| `LayerOutputDirNotEmptyError`       | exc   | Writer found a non-empty output directory            |
| `LayerMalformedError`               | exc   | Layer dir missing/invalid `pins.json`/`parents.json` |

## Tests

`tests/test_layered_file_store/`:
- `test_format.py` — format constants, round-trips, strict-on-missing-files, writer rejects non-empty output.
- `test_multi_layer_walk.py` — single + two-layer + grandchild dynamic-walk + parents.json cycle.
- `test_parent_remap.py` — `ParentPathRemap` prefix swap + reading a "materialized" (relocated, originals deleted) layer closure through the remap, transitively; negative control without the remap; no-remap path unchanged.
- `test_pin_isolation.py` — `list_pins` is layer-local, unknown pin errors, duplicate pin in same layer errors, `list_pins(prefix=...)`.
- `test_remote_resolution.py` — `mark_as_remote` + read via remote, no-remote-configured error, missing-in-remote error, `preload_data` promotion + dep precomp.
- `test_writer_walk.py` — transitive walk, dedup vs. parent data, parent-precomp-inherited (no re-marking), unreachable root errors, repeat-pin-no-op.
- `test_writer_cleanup.py` — exception body → no `layer_format.json` → reader rejects with `LayerMalformedError`; clean exit finalizes.
- `test_writer_validation.py` — `validate_on_close` true/false behavior.
- `test_pin_arena.py` — the seam + `LayeredStoreDeepFetchable`: a layer with a **local** entity A referencing a **remote-only** entity B; mounting A via the seam into a long-lived scope and closing the transient arena leaves A loadable and A→B resolvable through the threaded `source` (the lifetime-critical lazy case); deep-fetch hit/miss + registered-source closure resolution.
