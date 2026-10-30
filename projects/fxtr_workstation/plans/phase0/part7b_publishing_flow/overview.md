# Part 7b — Publishing flow (orientation)

> Status: RESHAPED (2026-06-13). After the fourth design sync,
> `fxtr_nix_runner` is promoted to the fxtr2-aware **manager** of the whole
> fxtr nix flake system (rehost, repo-store wiring, read adapters, relock).
> This pulls in a fxtr2-level **prerequisite** (the repo entity store
> refactor) and reshapes parts 1 / 2 / 2b. See `discussion_topics.md`
> "Decided (fourth sync)" for the decisions; `design/06_proposed_flow.md` is
> being updated to match. `sketch.md` is the original brain-dump; where they
> disagree, the design docs win.
>
> **Parts 1, 2, 2b are LANDED.** **Parts 3 & 4 are LANDED (2026-06-19/20)**
> around one minimal builder primitive (see those bullets + `design/06`).
>
> **Replanned around provenance (2026-06-20).** The old parts 5 (serving) and 6
> (example) moved to a new sibling **`../part7c_build_and_serve_demo/`** (the
> build-and-serve demo genuinely combines this part's build side with 7a's HTTP
> serve side, so it's its own part now). In their place, **new part 5 =
> provenance through the layered file store** (`part5_layer_provenance.md`), the
> build-side half of a provenance arc whose serve-side halves are
> **`../part7a_stateless_ws_server/` parts 6 (HTTP wire) + 7 (frontend)**. Goal
> of the arc: a node carrying provenance threads build → layer → serve → client
> with provenance intact. See `part5_layer_provenance.md` + the 7c overview.

> NOTE: Older passages in this plan refer to "GitNamespace" and "git stores".
> That concept has now been **renamed and split** (the prerequisite below —
> stage 14, which has **landed** in fxtr2). The pieces and their new names:
> - **repo entity store** (`fxtr2.data.repo_storage.RepoEntityStore`): the
>   by-id blob store (entities keyed by their own id, no pin names) — the
>   artifact formerly known as the "index namespace". A `DeepFetchable`.
> - **tsv namespace** (`TsvNamespaceBackend`, `fxtr2.data.tsv_namespace`,
>   formerly `GitNamespaceBackend`): the git-tracked pinned *view* (a mapping
>   of pin names → entity IDs) over a repo entity store. CLI `fxtr-tsv-ns`.
> - committed marker renamed `fxtr_store.toml` → `fxtr_namespace.toml`;
>   `.fxtr/fxtr_store_repo_config.toml` kept its name.
> Where a passage says "git store", read "repo entity store" (the by-id store)
> or "tsv namespace" (the pinned view) depending on which is meant.

## Goal

Build the glue that lets a workstation project **graduate to a published
report**: a new example workdir whose `flake.nix` runs all preprocessing
(tsv-namespace pins → layer; python postprocessing; an MDX report compile via
`fxtr_report_builder`) and yields a layered file store pointing at remote
storage — plus a new Python serving script that builds it, unions the result
with the repo entity store (the backing blob store with every pinned entity
keyed by ID), and serves everything over the existing experimental HTTP
interface.

End state for this part: design pinned down well enough to split into
sequential step plans (`part1_*.md`, …), each independently implementable,
tested, and documented, that together achieve `sketch.md`'s goal.

## What's in this directory

- `sketch.md` — the original goal statement (input to all of this).
- `overview.md` — this file.
- `discussion_topics.md` — decisions so far + remaining open questions
  (numbered OPEN-n; the design docs reference these).
- `followups.md` — known wants that are explicitly out of scope (the v2/ts
  redo that eventually retires `fxtr_report_builder`; behaviors-hub hosting;
  porting the dispatcher's `set_preset` onto the manager).
- **Prerequisite** (fxtr2-level, tracked outside this dir) — **LANDED**:
  - `v2/plans/phase3/stage14_repo_store_refactor/overview.md` — decoupled the
    index namespace into a standalone **repo entity store**
    (`fxtr2.data.repo_storage.RepoEntityStore`); `GitNamespaceBackend` →
    `TsvNamespaceBackend` (a view over the store); renamed `fxtr_store.toml`
    → `fxtr_namespace.toml`. Part 1 builds directly on the shipped API.
- **Step plans** (reshaped 2026-06-13; implement 1 → 2 → 2b in order, since
  they share the runner-manager + repo-store + rehost machinery):
  - `part1_runner_manager.md` — promote `fxtr_nix_runner` to the fxtr2-aware
    **flake manager**: repo-store discovery (+ create-if-absent) and the
    **unconditional** build-time rehost over it; the `Handle`
    expose-endpoint / extra-build-env hooks (host + podman, after the
    `design/07` spikes); the **entities** read adapter with transparent
    remote-pointer resolution. (Was `part1_build_rehost.md`.)
  - `part2_namespace_export_and_preload.md` — the pure `namespace_export`
    writer + `fxtr.namespaces` convention + the manager's **namespace** read
    view (a real `FxtrReadNamespace`); the `entityFromTsvPin` FOD writer +
    `lib.fxtr` wrapper (a single TSV pin fetched via the rehost). (Reshaped
    2026-06-14: the generic `preload_from_remote` writer is dropped — "preload"
    is now just "any FOD that reads `FXTR_REMOTE_*`"; `entityFromTsvPin` is the
    one helper here, arbitrary fetching is the part-3/4 builder path.)
  - `part2b_fod_lock.md` — `fod_lock.toml` (a single `[hashes]` table) + a
    standalone, helper-agnostic `lib.fxtr.fodLockReader { lockFile }` (dropped in
    wherever `lib.fakeHash` would go — decoupled from the FOD helpers) + a fully
    runner-owned `fxtr_nix update-hashes` (opens the repo store, stands up its
    own rehost, drives builds; sentinel hashes, `--keep-going` mismatch-parse
    loop). **Incremental by key set**: the default locks newly-declared slots and
    drops stale ones; `--all` / explicit `-k` re-lock existing slots (in-place
    content edits aren't auto-detected — that's `nix build --check`);
    `--dry-run` reports. Targets declared as `fxtr.preloads.<lockKey>`. (Was
    `part5b_fod_lock.md`; pulled forward next to 1/2.)
  - `part3_python_builders.md` — **reshaped (2026-06-19):** one primitive —
    *run user python that writes into a fxtr layer writer, in a locked venv,
    pure or FOD by flag* — split by what it runs: `mkFxtrFnBuilder` (an
    importable `mod:fn`) + `mkFxtrScriptBuilder` (a loose script file), both
    over a `mkVenv` venv, sharing the `_runFxtrBuilder` scaffold + the
    `fxtr2…nix_builder` helper (hands the code an open writer + bare `ctx`).
    Purity is the `outputHash` flag, not structurally enforced. One venv backs
    many scripts → cheap new steps, no per-step dir. **Follow-up:**
    inline-metadata scripts (`mkFxtrScriptBuilder` with `venv` defaulted from
    the script's PEP-723 block) — git/registry-dep flakes only.
    `mkEntityFromPython` stays (pins a pre-existing entity, not a special case).
  - `part4_report_builder_nix.md` — **reshaped (2026-06-19):** the report step
    is just a part-3 builder whose venv includes `fxtr_report_builder`,
    with the CLI wired via the builder's **generic `env`** —
    `lib.fxtr.reportBuilderCli` is a pnpm-in-nix bundle wrapped as a
    self-contained `node` executable, and `resolve_ts_dist_path` gets a generic
    arg→env→dev order. **Nix has zero report-builder-specific code.**
  - `part5_layer_provenance.md` — **(scoped 2026-06-20, not yet built)**
    provenance through the layered file store: a provenanced-mode layer whose
    pins are provenance-node ids (writer extracts the prov form on pin, reader
    re-embeds on retrieve), a `provenanced` flag on the part-3 builders, and the
    manager adapters honoring it. The build-side half of the provenance arc
    (serve side = 7a parts 6/7; combined demo = 7c). Store/namespace stays
    provenance-free for now.
  - **Serving + example moved to `../part7c_build_and_serve_demo/`** (was parts
    5/6 here): the serving script (`7c.1`) + the `examples/fxtr_publishing_demo/`
    template + E2E (`7c.2`), now the combined 7a+7b build-and-serve demo with
    provenance end-to-end. They need a replan against 7a + provenance — see the
    7c overview + those files' headers.
- `design/` — per-piece orientation docs. Each describes an existing
  subsystem, with file/line pointers for re-orientation, and ends with
  "what part 7b needs from it":
  - `01_repo_store_and_tsv_namespace.md` — TSV pin format, worktree namespace
    vs the **repo entity store** (the by-id store), the sqlite backing, the
    `RepoEntityStore` + `TsvNamespaceBackend` APIs + CLI.
  - `02_layered_file_store.md` — the on-disk layer format (the nix↔fxtr
    bridge), writer/reader APIs, the `(DeepFetchable, lease)` remote-fallback
    mechanism, feasibility notes for TSV→layer rewriting.
  - `03_nix_build_layer.md` — `lib.fxtr` helpers, the `fxtr_nix_writer`
    writer convention, `fxtr_nix_runner`, and the two existing
    build-a-layer-and-read-it paths (`retrieve_nix_entity`, the dispatcher's
    `set_preset`) whose patterns the serving script reuses.
  - `04_typescript_stack.md` — the v2/ts stack (deferred for part 7b) and
    `fxtr_report_builder` (the chosen MDX path), with a comparison table.
  - `05_http_serving_and_remote.md` — the experimental HTTP routes +
    serving helpers, `FxtrReadNamespace`, the remote transfer protocol /
    behaviors hub, and how (non-)union composition works today.
  - `06_proposed_flow.md` — **the glue design** (start here after this
    file): headline decisions, the build pipeline (pure namespace exports;
    preload FODs; pure postprocess + report layers), the serving script's
    sequence, and the new-code inventory the step plans follow.
  - `07_build_rehost_networking.md` — scoping doc for reaching the
    build-time store rehost from inside nix builds on both runner backends
    (host + podman); verification-spike checklist.

## The flow in one paragraph

The publishing flake dir is itself a **tsv namespace** (a `fxtr_namespace.toml`
+ committed TSVs) over a repo's **entity store** (the by-id blob store under
`.fxtr/`, formerly the "index namespace"), and its `fxtr` attrset is the
**source of truth for what gets exposed**: a new `fxtr.namespaces.<name>`
slice publishes whole TSV namespaces via **pure** export derivations (a
server-only publication manifest — no layer, no network); **preload FODs**
(the only networked build steps, pinned hashes) materialize declared input
entities into layered stores for in-build reads; a **pure** python layer
derives new entities on top; a **pure** report layer compiles MDX into
`RootReport`/`SubReport` entities via `fxtr_report_builder` (node running the
`lib.fxtr`-built CLI bundle). **`fxtr_nix_runner`, now the flake manager**,
discovers (creating if absent) and opens the repo entity store, **rehosts it
unconditionally** to the build as `FXTR_REMOTE_BASE_URL`, drives the eval /
build / materialize, and exposes the attrset as two read adapters (entities +
namespaces) whose readers carry the repo store as their remote so
remote-pointer entities resolve transparently. The example-side **serving
script** drives the manager and serves exactly what the attrset declares over
`create_fxtr_app` (`/pin`, `/pins`, `/data`, `/preview`) — no pin-name
merging anywhere.

## Key background reading outside this dir

- `../part5_nix_workdir/overview.md` — the nix workdir system this builds on
  (entity attr shape, DATA-via-entities vs CODE-via-git split).
- `../part5_nix_workdir/design_notes/07_remote_entity_store.md` — the
  pinned build-time remote env-var contract (`FXTR_REMOTE_BASE_URL`); part 7b
  is its first real consumer (its "intermediate state", publishing-flavored).
- `v2/plans/phase3/stage11_nix_layers/part1_layered_file_store.md` — the
  layered-store design doc.
- `projects/fxtr_report_builder/CODE_OVERVIEW.md` — the chosen MDX path.
- `examples/fxtr_workstation_demo/` — the live demo this example is a
  publishing-flavored sibling of.
