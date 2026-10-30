# Part 5 — provenance through the layered file store

> Status: SCOPED + code-checked, design questions resolved (2026-06-21), not yet
> implemented. Inserted as 7b part 5 when the publishing flow was replanned around
> provenance (the old part 5/6 — serving + example — moved to
> `../part7c_build_and_serve_demo/`). This part is the **build-side** half of the
> provenance arc: make a layered file store able to *carry* provenance. The
> serve-side halves are 7a parts 6/7 (HTTP wire + frontend) and the combined demo
> is 7c. Design context: `v2/fxtr2/data/layered_file_store/` + the
> `preserve_provenance` persistence cap in
> `projects/fxtr_workstation/src/fxtr_workstation/persistence.py`
> (`_layer_for_storage` / `_restore_layered` — the pattern this generalizes).
>
> **Why it's small (verified against the code):** (1) pinning a prov node already
> pulls its data node into the layer — the walk follows the `UNWRAPS_TO` dep; (2)
> the layer arena already answers `unwrap_provenance` from precomp/data, so the
> read-side `embed` works; (3) `store_cbor`'s `"auto"` mode already returns a
> layered `(data, prov=n-node)` id for an entity whose children carry provenance,
> so a built report *has* a prov form to extract. The net new code is a
> layer-format flag + project-on-write / embed-on-read in the **two**
> pin-mounting paths + the builder flag.
>
> **Decisions (see Approach + Resolved):** pin value = bare prov-node `id_string`
> + embed on read, `@`-free (7a part 6 chose an explicit `[data, prov]` wire
> shape, so the layer has no reason to store a composite `data@prov` string).
> Projection to prov form is done by **one** new primitive,
> `FxtrScope.extract_provenance_parent(id, extract_as_u_if_missing=True)`, shared
> by `persistence.py` and the writer (so the two can't drift); the bare-prov-tag
> *rejection* in `persistence.py` is **removed** (a bare prov tag is wrapped with
> `create_u_node`, same as bare data). `mark_as_remote` / `preload` gain
> layered-id support: a *layered* id is split into its two existing ids (prov +
> data) and both closures are covered; a *bare* id (any tag) is handled as its own
> single id exactly as today. The builder-minted `provenance_context` idea is
> split out to **7b.6** (`part6_builder_provenance_context.md`).

## Goal

Today the layered file store is **bare-data on disk**: it stores data entities,
and the `(data, prov)` layering of a `FxtrID` (its `_provenance_parent`) is a
scope-side overlay that's dropped on store. So a built entity that carried
provenance loses it the moment it's pinned to a layer.

Goal: a **provenanced-mode layer** that round-trips provenance — so a node with
provenance can be threaded through a nix build (a part-3/4 builder), pinned, and
read back with its provenance intact, all the way to serving (7a/7c). The
**tsv-namespace pin flow** stays provenance-stripping for now (the no-prov
persistence cap, so namespace-sourced inputs come back bare) — a deliberate
boundary, noted as a future followup. (Note: the repo entity store / rehost can
*hold and serve* prov nodes — they're ordinary entities by id; the boundary is
that the demo doesn't *pin* provenance into the namespace, not that the store
can't carry it.)

## Key insight (why this is small)

Provenance is **never in the serialized CBOR** — `fxtr_cbor_dumps` only ships
each id's bare `_raw_data_id`. A provenance node (`n`/`c`/`p`/`u`) is itself a
real content entity, and the layer format **already records `unwraps_to` in
precomp** — which is exactly the input `embed_as_provenance_parent` needs. So
provenanced mode needs no new on-disk machinery for the provenance edge; it just
stores the *provenance form* of each pin and re-embeds on read. This is the
`persistence.py` `preserve_provenance` pattern (`_layer_for_storage` /
`_restore_layered`) pushed into the layer format itself + made self-describing.

## Current state (code orientation)

The pieces this part touches, with what they do today (file:line):

- **`store_cbor` already produces a prov form** (`entity_ref_scope.py:3469`).
  Under `store_provenance="auto"` (the scope default), if any non-marker child
  ref carries a provenance parent it takes the **prov path** (`:3543-3559`):
  projects children to prov form, substitutes the `$fxtr.ctx` marker with the
  scope's layered marker, stores an **n-node**, and returns
  `FxtrID(data, _provenance_parent=n-node)`. So `ctx.scope.store(report)` for a
  report whose figure refs are layered already returns a *layered* id — there is
  a prov form to extract + pin. (`_store_cbor_prov_path` `:3581`; bare path
  `:3561` strips.)
- **The pin walk already follows `UNWRAPS_TO`.** `load_flat` emits
  `(node.unwraps_to, DepKind.UNWRAPS_TO)` for both contentful and derived nodes
  (`memory_arena.py:1812-1821`); `LayeredFileStoreIsolatedWriter._walk_and_write`
  recurses on **all** `flat.deps` regardless of kind (`writer.py:594-633`). So
  pinning a prov node stores its data node (via the unwrap edge) **and** its
  child prov nodes (an n-node's child refs are prov forms). Confirmed.
- **The layer arena answers `unwrap_provenance`** (`arena.py:341`): from
  `_precomp_unwraps_to`, lazily populated either from the data file's deps
  (`derive_precomp_entry_from_deps`, `arena.py:187-195` / `arena_util.py:233`) or
  from a `precomp/<id>.json` file (`arena.py:197-206`). `_ingest_precomp_entry`
  tracks **both** the prov id and its unwrap target (`arena.py:154-157`). So
  `embed_as_provenance_parent` (`entity_ref_scope.py:2186`, needs the prov node
  tracked + `unwrap_provenance` + the unwrapped data tracked, `:2225-2240`) is
  satisfiable for both local and precomp-only (remote) prov nodes.
- **Two pin-mounting paths exist** — both need the embed step in provenanced mode:
  1. `LayeredFileStoreReader._retrieve_impl` (`reader.py:194`): attaches the pin
     into its own scope, then lifts/copies into the target.
  2. the manager's `FlakeManager.mount_pin` (`manager.py:223`) via `open_pin_arena`
     (`pin_arena.py:93`) → attaches into the manager scope; the
     `EntitiesReadAdapter` calls this (`entities.py:185`) and returns the bare id.
  The **by-id** path `LayeredStoreDeepFetchable.try_lookup` (`pin_arena.py:137`)
  is per-id and stays bare.
- **`mark_as_remote` / `preload` today** (`writer.py:418` / `:470`): operate on a
  single id; `mark_as_remote` validates it against the remote + writes a precomp
  closure (`compute_precomp_closure`), `preload` fetches its bytes via the
  wiretap. Neither is provenance-aware yet (they'd record/fetch whatever id
  they're handed).
- **Builder flag plumbing** (the path a `provenanced` flag travels): nix helpers
  `mkFxtrFnBuilder`/`mkFxtrScriptBuilder` → `_runFxtrBuilder` `builderSpec`
  (`{parents; spec; entry?}`) → `nix_builder.amain` reads it → `_run` opens the
  writer (`nix_builder.py` `_run`, the `LayeredFileStoreIsolatedWriter(...)`
  construction). A `provenanced` bool would ride `builderSpec` and select the
  writer mode; `ctx`/the entrypoint stay mode-agnostic.
- **Namespace adapter is unaffected.** `NamespaceReadAdapter` reads the pure
  `fxtr_namespace_export` manifest (`namespaces.py`), not built layers, so
  provenanced *layers* don't touch it. Provenanced mode is an **entities-adapter
  + `LayeredFileStoreReader`** concern, not a namespace one.

## Approach (the scoped mechanism)

A layer is marked **provenanced** (a flag in `layer_format.json`; absent ⇒
today's bare behavior, fully back-compatible). In a provenanced layer:

- **pins map name → a *provenance-node* id** (an `n`/`c`/`p`/`u` node that
  unwraps to the data id), not the bare data id. One `pins.json`; the
  `layer_format.json` flag disambiguates (DECIDED: not a separate file).
- **writer**, on every pin: project the pinned id to its provenance form first,
  via the single shared primitive
  `from_scope.extract_provenance_parent(id, extract_as_u_if_missing=True)`:
  - layered `(data, prov)` → the bare `prov` half (today's
    `extract_provenance_parent` behavior);
  - bare id with no `_provenance_parent` (data tag **or** prov tag) →
    `create_u_node(id)` (the new `extract_as_u_if_missing` branch).

  The projected prov-node id becomes the `pins.json` value; the data node rides
  into `data/` automatically because the existing walk follows the prov node's
  `UNWRAPS_TO` dep (no walk change). See "The shared projection primitive" below.
- **reader**, on every pin it returns: `embed_as_provenance_parent` to re-pair
  the prov node with its data half (the precomp `unwraps_to` makes that a sync
  read), yielding a layered `(data, prov)` id.

**Granularity — DECIDED: all-or-nothing per layer, mixed chains fine.**
`provenanced` is a per-layer mode: *every* pin in a provenanced layer reads back
layered — a genuinely-provenanced entity via its `n`/`c`/`p` node, a bare-data
pin via a `u`-wrap (`create_u_node`). **Mixed parent chains are fully supported
and expected** (the demo's report layer is provenanced over *bare* figure-input
parent layers): provenanced-ness is purely about a layer's **own pins**, and pins
are layer-local — a reader only ever looks at its own layer's pins and walks
parents for *data*, never for their pins, so a child never cares whether a parent
layer is provenanced.

### What gets the flag / mode

- **Builders** (part 3): a `provenanced ? false` flag on `mkFxtrFnBuilder` /
  `mkFxtrScriptBuilder` (DECIDED: a flag, not two helpers) → flows through
  `nix_builder` → opens `ctx.writer` in provenanced mode. The entrypoint is
  mode-agnostic (it pins whatever id it has; the writer extracts/wraps).
- **Reader split** (DECIDED): the **pin-mounting** paths embed — both
  `LayeredFileStoreReader._retrieve_impl` (`reader.py:194`) *and* the manager's
  `mount_pin` (`manager.py:223`, used by `EntitiesReadAdapter`, `entities.py:185`).
  The **by-id** path (`LayeredStoreDeepFetchable.try_lookup`, `pin_arena.py:137`,
  i.e. `/data/<id>`) stays bare — a prov node is just another entity by id;
  provenance reconstruction belongs at the *pin* boundary. (The
  `NamespaceReadAdapter` is **not** in scope — it reads the pure namespace-export
  manifest, not built layers.)
- **Other readers** (DECIDED, minimal): the dispatcher `set_preset` reader and
  the MCP `retrieve_nix_entity` should "just work" on a node with a provenance
  parent by ignoring it (they read bare today; no change expected — confirm).

## Resolved (from the code)

- **Pin walk follows `UNWRAPS_TO`** → YES (`memory_arena.py:1812-1821` emits the
  unwrap dep; `writer.py:594-633` recurses all deps). Pinning a prov node stores
  its data node + child prov nodes. No change needed to the walk itself.
- **`embed` is satisfiable on a layer reader** for both local and precomp-only
  (remote) prov nodes — the arena derives `unwraps_to` from the data file's deps
  or a precomp file and tracks both ends (`arena.py:154-157,187-206,341`).
- **The report has a prov form to extract** — `store_cbor` `"auto"` already
  returns a layered `(data, n-node)` id when children carry provenance
  (`entity_ref_scope.py:3543-3559`). So "extract the prov form, pin it" is a real
  operation, not hypothetical.
- **Nested composition is structurally sound** (not yet a separate worry): an
  n-node's child refs are themselves prov forms (the prov path projects every
  child), and the walk stores the whole closure, so pinning the report's prov
  form carries the figures' provenance. **Still must be a regression test**, and
  the impl **fixed if a gap shows** — but reading the code, it should compose;
  it's no longer flagged as the scary unknown.
- **Reader split is coherent** — `resolve_pin`/`open_pin_arena` (pin) vs
  `LayeredStoreDeepFetchable.try_lookup` (by-id) are distinct seams
  (`pin_arena.py`), so embedding at the two pin-mounting callers without touching
  by-id is clean.
- **Builder flag path is a straight plumb** — `provenanced` rides `builderSpec`
  → `nix_builder._run` selects the writer mode; entrypoint unaffected.

## The shared projection primitive

`store_cbor`'s prov path, the `persistence.py` cap, and the new provenanced
writer all want the same operation: "given an id (possibly layered, possibly
bare), give me its bare provenance-node id." Rather than three copies, add an
optional keyword to the existing scope method:

```
FxtrScope.extract_provenance_parent(id, *, extract_as_u_if_missing=False)
  # _provenance_parent set  → bare prov half (today's behavior)
  # no _provenance_parent + extract_as_u_if_missing  → create_u_node(id)
  # no _provenance_parent + not the flag             → raise (today's behavior)
```

`create_u_node` already accepts any bare id (it only rejects an id that *already*
carries a `_provenance_parent`), so a bare prov-tag node `n` legitimately becomes
`u(n)` — no special case. With this:

> **Invariant — branch on layering, never on tags.** Every decision in this part
> keys off either the `provenanced` layer-format flag (which *mode* a layer is in)
> or whether an id has a `_provenance_parent` (layered vs. bare). Nothing inspects
> an id's tag. A bare id is a single data-level entity whatever its tag; a layered
> id is the only thing carrying two ids. The sole tag *reference* anywhere is the
> defensive assertion **inside** `embed_as_provenance_parent` (it asserts the
> prov-node id is `n`/`c`/`p`/`u`); we rely on it but never branch on it, and the
> writer's projection guarantees every provenanced pin value is a prov node
> (bare data is `u`-wrapped), so it never trips in normal operation.

- `persistence.py`'s `_layer_for_storage` collapses to a single call
  `call_scope.extract_provenance_parent(entity, extract_as_u_if_missing=True)`.
  Its bare-prov-tag **rejection** branch (and the `_PROVENANCE_NODE_TAGS`
  constant) are deleted; the class/function docstrings drop the "reject as
  ambiguous" language. `test_persistence_7.py::test_preserve_prov_rejects_bare_prov_tag_input`
  flips from asserting a raise to asserting a `u(u(d))` round-trip.
- the provenanced writer's `pin_to_this_layer` calls the same primitive on
  `from_scope` to get the `pins.json` value.

## Resolved decisions

- **Pin walk follows `UNWRAPS_TO`** (`memory_arena.py:1812-1821` emits the unwrap
  dep; `writer.py:594-633` recurses all deps). Pinning a prov node already stores
  its data node + child prov nodes. **No walk change.**
- **`embed` is satisfiable on a layer reader** for both local and precomp-only
  (remote) prov nodes — the arena derives `unwraps_to` from the data file's deps
  or a precomp file and tracks both ends (`arena.py:154-157,187-206,341`). The old
  "embed cascade" worry is gone: the root cause (a cap_ir_arena packet-construction
  bug) was diagnosed and **fixed** (`stage7_impl_notes.md` "Upstream v2 bug —
  fixed"), so `embed` works for `u`-tag prov nodes loaded from a remote.
- **Pin-value encoding — bare prov-node id + embed-on-read, `@`-free.** A
  provenanced `pins.json` value holds the bare prov-node `id_string` (a real
  `n`/`c`/`p`/`u` entity id); the reader `embed`s to re-pair it with its data
  half. We rejected the composite `data@prov` string because 7a part 6 chose an
  explicit `[data, prov]` wire shape — the serving script translates the reader's
  layered `FxtrID` into that wire form. The *stored closure* (prov node + data +
  children) is identical either way.
- **`mark_as_remote` / `preload` — split a layered id, leave a bare id alone.**
  The distinction is purely whether `_provenance_parent` is set:
  - **layered `(data, prov)`** → cover **two** ids: the `prov` half **and** the
    bare `data` half (each its own precomp/preload closure). This is the load-
    bearing requirement: preloading only the prov node would leave the data node a
    remote precomp marker, so after `preload` *neither* form would fully load
    offline. Covering both means the reader can later return **either** the layered
    or the bare form without hitting the remote.
  - **bare id (any tag, incl. `n`/`c`/`p`/`u`)** → a single data-level entity by
    id; handled exactly as today. No unwrapping, no minting — a bare prov tag is
    not split into a second level.

  A prov node is an ordinary entity by id, so a remote (the rehost over the repo
  store) can hold/serve one; `compute_precomp_closure` over an `n`-node already
  records its `unwraps_to`→data + `ctx`/`backstep` + child edges, and the wiretap
  reader `embed`s a precomp-only/remote prov node (`arena.py:154-206`). So this is
  the *same* machinery applied to (at most) two ids — no remote-vs-local special
  casing. Tested edge cases: local layered id; **remote prov node fetched →
  embedded → preloaded, then both forms load offline**; **embed a node, then
  mark it remote** (both levels marked); `c`/`p` prov nodes; nested provenance
  (the report case); all-local; a bare-id mark/preload unchanged.
- **Nested composition is structurally sound** — an n-node's child refs are
  themselves prov forms (the prov path projects every child) and the walk stores
  the whole closure, so pinning a report's prov form carries the figures'
  provenance. Must have a regression test; fix the impl if a gap shows.
- **Reader split is coherent** — `resolve_pin`/`open_pin_arena` (pin) vs
  `LayeredStoreDeepFetchable.try_lookup` (by-id) are distinct seams
  (`pin_arena.py`), so embedding at the two pin-mounting callers without touching
  by-id is clean.
- **Builder flag path is a straight plumb** — `provenanced` rides `builderSpec`
  → `nix_builder._run` selects the writer mode; entrypoint unaffected.

## Details to settle during implementation

- **Scope-tracking for `embed`.** Both mount paths attach the *pin* (prov-node)
  id into the scope; `embed` needs the unwrapped **data** id tracked in that
  **scope** too. The arena tracks it (via the `UNWRAPS_TO` precomp), but the
  attach lifts only the pin id — so lift the data id (from `unwrap_provenance`)
  before calling `embed`, or attach both. Settle in `mount_pin` / `_retrieve_impl`.
- **Determinism.** Provenanced pin ids must stay byte-stable for fixed inputs
  (same gate as the existing FOD/layer determinism test). Should hold (content
  addressing), but cover it.

## Rough implementation steps

- [x] Shared primitive: `extract_as_u_if_missing` kwarg on
      `FxtrScope.extract_provenance_parent` (+ unit tests for the new branch).
- [x] Refactor `persistence.py` onto it: `_layer_for_storage` becomes one call;
      delete the bare-prov-tag rejection (kept `_PROVENANCE_NODE_TAGS` — still used
      by the retrieve-side `_restore_layered` guard); fix docstrings; flip
      `test_preserve_prov_rejects_bare_prov_tag_input` →
      `test_preserve_prov_bare_prov_tag_input_is_u_wrapped` (`u(u(d))` round-trip).
- [x] `layer_format.json` `provenanced` flag + read/write plumbing (back-compat:
      absent ⇒ bare). New `read_layer_format` returning `LayerFormat(version,
      provenanced)`; `write_format_version(..., provenanced=False)` writes the key
      only when True.
- [x] Provenanced **writer** mode (constructor flag): `pin_to_this_layer` projects
      to the prov-node id (layered → bare prov half, pure; bare → `create_u_node`
      on the source scope — mirrors `extract_provenance_parent(extract_as_u_if_missing=True)`,
      branching on layering not tag); the walk pulls the data node via `UNWRAPS_TO`
      (no walk change). `mark_as_remote`/`preload` split a layered id into prov +
      data and cover both; a bare id is unchanged. (Fixed a latent
      `_WiretapArena` bug surfaced here: its `unwrap_provenance` returned `None`
      for prov nodes because its precomp dicts were never populated from disk —
      added `_lazy_populate_precomp` mirroring `LayeredFileArena`, so a
      provenanced `preload` scope-loads both levels with a consistent unwrap
      view.)
- [x] Provenanced **reader** in `LayeredFileStoreReader._retrieve_impl` (chunk 3):
      auto-detects the `provenanced` flag from `layer_format.json` at enter, then
      `embed_as_provenance_parent` on retrieve → layered id. (The attach cascade
      already tracks the data id in-scope via `UNWRAPS_TO`, so no explicit lift
      was needed — confirmed by the round-trip test.) The by-id
      `LayeredStoreDeepFetchable` stays bare. **`FlakeManager.mount_pin` is chunk 4.**
      Round-trip test: write a nested provenanced entity → fresh reader → layered
      id reconstructed, loads, nested child still provenanced; bare layer returns
      bare.
- [x] (chunk 5) Builder flag: `provenanced ? false` on `mkFxtrFnBuilder` /
      `mkFxtrScriptBuilder` + `_runFxtrBuilder.nix` → threaded into `builderSpec`
      → `nix_builder.amain` reads it → `_run(provenanced=…)` opens `ctx.writer`
      provenanced. Tests (no nix): `_run(provenanced=True)` sets the flag +
      u-wraps a bare pin; composes over a provenanced parent (the report case);
      `amain` reads `builderSpec.provenanced` from a faked build contract. Plus a
      **real-nix E2E** (`test_nix_e2e.py::test_provenanced_builder_through_manager`,
      `@slow`): a pure `mkFxtrFnBuilder` with `provenanced = true` built through
      real nix + read back via `mgr.entities(span)` as a layered id — proving the
      whole chain (nix scaffold → `builderSpec.provenanced` → writer → `mount_pin`
      embed). New fixture `_workdir_templates/provenanced_builder/`. (2 passed in
      ~92s locally.)
- [x] (chunk 4) `FlakeManager.mount_pin` auto-detects the `provenanced` flag and
      embeds (returns a layered id); `EntitiesReadAdapter` passes it through
      unchanged. The by-id path / `NamespaceReadAdapter` are out of scope. Confirmed
      the MCP `retrieve_nix_entity` + dispatcher `set_preset` reader are unaffected:
      they don't route through `mount_pin` (separate paths), and bare layers — the
      only kind produced until the chunk-5 builder flag — read back bare. Serving
      provenanced layers through them (getting a layered id, loading the data) is a
      7c concern. nix_runner tests: provenanced `mount_pin` → layered id +
      nested-provenance load; bare layer → bare.
- [x] Tests (regression, not throwaway): v2-level nested round-trip of a
      genuinely-provenanced node through writer+reader (`test_provenanced_round_trip`);
      writer on-disk-correctness + mark/preload-both-levels
      (`test_provenanced_writer`); `mount_pin` nested round-trip
      (`test_provenanced_mount`); `_run` / `amain` provenanced builder
      (`test_nix_builder`); bare-layer + by-id controls; a real-nix provenanced
      builder E2E (`test_nix_e2e::test_provenanced_builder_through_manager`).
- [x] Update `LOCAL_CODE_OVERVIEW.md` (layered_file_store: format / writer /
      reader / nix_builder), `data/LOCAL_CODE_OVERVIEW.md` (the
      `extract_as_u_if_missing` primitive), `nix/CODE_OVERVIEW.md` (builder flag),
      `fxtr_nix_runner/CODE_OVERVIEW.md` (`mount_pin` awareness). *(`v2/CODE_OVERVIEW.md`
      itself needed no change — the primitive lives in `data/LOCAL_CODE_OVERVIEW.md`.)*

> **Follow-up (7b.6):** builder-minted default `provenance_context` is a separable
> chunk, split out to `part6_builder_provenance_context.md`. Scope it after this
> core lands.

## Suggested commit sequence

Break the work into the natural seams below (all `fxtr ws 7b.5 …`) rather than
one commit — it spans four projects with separate test suites, and the v2
data-layer half is independently valuable and testable before any nix wiring.
**Pause for review at each commit boundary.**

1. **Primitive + `persistence.py` refactor** (`v2` + `fxtr_workstation`). Add
   `extract_as_u_if_missing` to `extract_provenance_parent` (+ unit tests);
   collapse `_layer_for_storage` onto it; delete the bare-prov-tag rejection (the
   `_PROVENANCE_NODE_TAGS` constant stays — the retrieve-side `_restore_layered`
   guard still uses it); fix docstrings; flip the bare-prov-tag test to a
   `u(u(d))` round-trip. A standalone cleanup that lands the reusable primitive.
2. **Layer-format flag + provenanced writer** (`v2`). `layer_format.json`
   `provenanced` flag + read/write plumbing (back-compat: absent ⇒ bare); the
   provenanced writer mode (pin projection via the primitive; `mark_as_remote` /
   `preload` split a layered id into prov + data and cover both closures, bare id
   unchanged) + writer unit tests.
3. **Provenanced reader + v2 round-trip** (`v2`). `LayeredFileStoreReader`
   auto-detects the flag, lifts the data id, then `embed`s; the nested
   genuinely-provenanced end-to-end test. After this the v2 store fully
   round-trips provenance on its own; the by-id path stays bare.
4. **nix_runner pin-mounting** (`fxtr_nix_runner`). `FlakeManager.mount_pin`
   auto-detects + embeds; `EntitiesReadAdapter` honors it; nix_runner tests.
   Confirm the dispatcher `set_preset` reader + MCP `retrieve_nix_entity` are
   unaffected.
5. **Builder flag** (`nix/` + `nix_builder`). `provenanced` on `mkFxtrFnBuilder` /
   `mkFxtrScriptBuilder` → `builderSpec` → `nix_builder._run` opens the writer
   provenanced; the nix builder integration test (heavy — isolated here so the
   fast v2 commits stay green on their own).
6. **Provenanced pins store `[data, prov]`** (`v2` + `fxtr_nix_runner`).
   `pins.json` records a provenanced pin as a `[data, prov]` pair (bare pin stays
   a string), so `read_pins` returns the layered id and `list_pins` returns the
   **same** id as `retrieve` as a pure read (fixes the bug where `list_pins`
   returned the bare prov-node id, inconsistent with `retrieve`). The writer
   records the layered pin value (still seeds the walk on the prov node);
   `retrieve` / `mount_pin` keep `embed` (validated) but source the prov node from
   the layered pin and seed/attach on it explicitly. Dropped the `open_pin_arena`
   convenience (its seed silently depended on layeredness) — callers `resolve_pin`
   then `open_seed_arena` on the id they actually need.
7. **Review cleanups** (`v2` + `fxtr_workstation`). From a diff review: rename
   `write_format_version` → `write_layer_format` (it writes the flag too; pairs
   with `read_layer_format`); drop the now-unused `read_layer_format` / `LayerFormat`
   package re-exports; assert `arena_over_search_set`'s seed is bare (a layered
   seed would silently track the data node, not the prov node); add the
   provenanced-FOD-bare-input writer test (mark+preload+pin a bare remote id with
   no `from_scope`); fix stale comments (`writer` constructor,
   `persistence._restore_layered`'s "upstream bug" note — that bug is fixed).
8. **Preserve mode stores bare entities bare** (`v2` + `fxtr_nix_runner`). Drop
   the `u`-wrap: in preserve mode a genuinely-bare entity is stored as a plain
   bare id (not `(d, u(d))`), so a provenanced layer holds **mixed pins**
   (genuinely-provenanced → pair, bare → string) with no meaningless trivial-`u`
   nodes. `pin_to_this_layer` collapses to "record the id in its natural form;
   seed the walk on the prov node (layered) or the id itself (bare)" — drops
   `_project_provenanced_pin` + the writer's `create_u_node` call + the
   `self._scope` walk fallback. The `provenanced` flag stays (preserve-vs-strip).
   Tests flip "bare → u-wrap" → "bare → bare"; the layered/nested paths +
   E2E produce a *genuinely*-provenanced entity (via a `provenance_context`) to
   still assert layered round-trips.

CODE_OVERVIEW / LOCAL_CODE_OVERVIEW updates ride along in whichever commit touches
that area, not a doc commit at the end.

## Out of scope (future followups)

- **Namespace-pin provenance.** The tsv-namespace pin flow stores entities bare
  (the no-prov persistence cap), so namespace-sourced inputs come back without
  provenance; making the namespace *pin* path preserve provenance (the
  preserve-prov cap) is a separate future followup. (The store/rehost itself can
  already hold + serve prov nodes by id — this followup is about what the demo
  pins *in*, not the store's capability.)
- **HTTP wire + frontend.** Exposing/consuming provenance over HTTP is 7a parts
  6/7; the combined demo is 7c.
- **Builder-minted default `provenance_context`.** Split to 7b.6
  (`part6_builder_provenance_context.md`) — independent of this core mechanism.
