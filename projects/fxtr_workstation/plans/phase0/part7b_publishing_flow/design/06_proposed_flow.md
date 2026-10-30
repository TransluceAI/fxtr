# The proposed publishing flow — glue design

> Status: third pass (2026-06-12) + **fourth-sync reshape (2026-06-13)**.
> Resolved decisions are inlined with **[DECIDED-n]** markers (details in
> `../discussion_topics.md`); remaining **[OPEN-n]** markers point there too.
> Deferred wants live in `../followups.md`.
>
> **Fourth-sync reshape (authoritative: `../discussion_topics.md` "Decided
> (fourth sync)", RUNNER-1..8).** Two structural changes ripple through this
> doc; read them first:
> 1. The "index namespace" was decoupled into a standalone **repo entity
>    store** (`fxtr2.data.repo_storage.RepoEntityStore`); `GitNamespaceBackend`
>    became `TsvNamespaceBackend` (a view over the store); the committed marker
>    was renamed `fxtr_store.toml` → `fxtr_namespace.toml`. (Prereq:
>    `v2/plans/phase3/stage14_repo_store_refactor/` — **LANDED**.)
> 2. **`fxtr_nix_runner` is promoted to the fxtr2-aware flake manager.** It —
>    not the serving script — discovers/opens the repo entity store, stands up
>    the build-time rehost (unconditionally), drives eval/build/materialize,
>    and exposes the attrset as two read adapters (entities + namespaces) whose
>    readers carry the repo store as their remote. The serving script becomes
>    a thin example-side driver over the manager.
> Where older passages below say "the serving script opens the git store /
> rehosts / builds", read "the manager does this; the serving script drives
> the manager." And read "git store" → **repo entity store** (the by-id store,
> `RepoEntityStore`) or **tsv namespace** (the pinned view,
> `TsvNamespaceBackend`) per context.

## Goal restated

A new example workdir under `examples/` (working name:
`examples/fxtr_publishing_demo/`) that models **publishing a report as a nix
flake** — what a workstation project graduates to. Running nix on the flake
runs all preprocessing and yields layered file stores whose data resolves
against remote storage; a new Python serving script builds them, backs them
with the repo entity store, and serves everything over the
experimental HTTP interface.

## Headline decisions shaping this design

- **[DECIDED-3]** Purity is a hard constraint for the final flake. Network
  access is confined to fixed-output derivations (FODs) with pinned hashes;
  v0 uses `lib.fakeHash` + manual re-pinning (FOD lock file: `followups.md`).
  Rehost reachability from inside builds (host + podman) is scoped in
  `07_build_rehost_networking.md`.
- **[DECIDED-6]** No `v2/ts` in part 7b. The MDX step is
  **`fxtr_report_builder`** (Python-driven; its tiny self-contained TS
  subprocess) plumbed into nix. The TS-native layer reader/writer and the
  v2/ts redo are deferred (`followups.md`).
- **[DECIDED-9]** The "remote store" is the publishing repo's **repo entity
  store** (`RepoEntityStore`), exposed by the serving script — a local
  stand-in for an arbitrary blob store (eventually the behaviors hub, where
  the whole flow incl. builds runs server-side).
- **[DECIDED-1]** The flake dir's pins live in a **real tsv namespace**
  (`fxtr_namespace.toml` + TSVs); prefer the existing config overrides
  (`sqlite_store_path`, `repo_config_path` — `fxtr2.data.repo_storage_config`,
  re-exported from `fxtr2.data.tsv_namespace.config`) for placing the demo's
  backing sqlite before tweaking tsv-namespace code.
- **[DECIDED-5]** Published reports reuse `RootReport`/`SubReport`
  (`projects/fxtr_report_builder/fxtr_report_builder/entities.py`) so
  `@fxtr-ui/reports` renders them as-is. Subject to change later.
- **[DECIDED-10]** **The flake's `fxtr` attrset is the source of truth for
  what gets exposed; pin names are never merged.** The flake *reexports* the
  tsv namespace through nix in two forms:
  - per-entity: a nix value that points at one pinned entity in the
    namespace and *is* a normal `fxtr_layer_entity` (usable anywhere layer
    entities are);
  - whole-namespace: a new **`fxtr.namespaces.<name>`** attrset slice that
    names an entire TSV namespace; nix points at the whole store, and
    the *server* decides how to expose it (route prefix, etc.).
- **[DECIDED-12]** The report-builder CLI bundle is built **pure
  pnpm-in-nix, inside `lib.fxtr`** (pinned by the fxtr flake, built once,
  consumed by many user flakes). No vendored artifacts.
- **[DECIDED-7]** Serving keeps crawled layer entities **scope-only** (an
  in-memory "known loadable from the layer" set / lazy adapter); no
  write-back into the repo entity store (soft preference — revisit if
  scope-only turns out harder).

## The cast (see the per-piece docs)

| piece | doc |
|---|---|
| repo entity store + tsv namespace: TSV pins, by-id store, sqlite backing | `01_repo_store_and_tsv_namespace.md` |
| layered file store: format, writer/reader, remote fallback | `02_layered_file_store.md` |
| nix lib, writers, fxtr_nix_runner, existing build→read paths | `03_nix_build_layer.md` |
| TS stack (deferred) + `fxtr_report_builder` (chosen MDX path) | `04_typescript_stack.md` |
| HTTP serving, FxtrReadNamespace, remote transfer protocol | `05_http_serving_and_remote.md` |
| build-time rehost networking (host + podman) | `07_build_rehost_networking.md` |

## The flake schema (extends part5's three slices)

```
fxtr
  .entities.<name>          # existing: { type="fxtr_layer_entity"; layer; layerKey }
                            #   NEW source: one tsv-namespace pin materialized
                            #   into a layer (a FOD) — only needed when nix-side
                            #   build steps must READ the entity
  .namespaces.<name>        # NEW slice: a whole TSV namespace, published
                            #   as a PURE export derivation (no FOD, no layer):
                            #   parse/validate/normalize the committed TSVs
                            #   into a publication format. Only the SERVER
                            #   reads this; format need not be the layer format
  .services / .workstation  # unchanged (not used by this example)
```

**[DECIDED-15]** Namespace exports are pure; FODs exist only where entity
*bytes* must enter a layered store for in-build reads (per-step preloads and
git-pin-sourced `fxtr.entities.<name>` — the same mechanism). Rationale: no
nix-sandboxed builder ever reads the namespace export; the serving script
has the repo entity store directly and computes precomp at serve time, so no
`mark_as_remote` round-trip is needed for publication. Namespace exports
therefore never need hash re-pinning and stay automatically in sync with the
TSVs.

Exact attr shapes + the publication format (verbatim TSV copy vs. normalized
JSON manifest) are plan-time details; the invariants to keep are part5's two
(buildable leaves are derivations; metadata leaves are cheap `nix eval`).
The demo's report output is itself exposed as `fxtr.entities.report` (its
layer = the report layer below — a pure layer, no FOD).

## Proposed dataflow

```
              the publishing flake dir (a git repo, itself a tsv namespace)
  ┌────────────────────────────────────────────────────────────────────┐
  │ flake.nix          fxtr_namespace.toml + *.fxtr.tsv   postprocess/  report/ (MDX)
  └────────────────────────────────────────────────────────────────────┘
        │ nix build (driven by the serving script;
        │  FXTR_REMOTE_BASE_URL/_BEARER_TOKEN reach FODs only)
        │
        ├─ namespace export(s) (PURE): TSVs → publication-format manifest
        │                              (server-only artifact; no layer)
        │
        ▼
  preload layer(s)    (FOD): entity bytes (+ precomp) for the inputs each
        │                    pure step declares; also the single-pin
        │                    fxtr.entities-from-git-pin case
        ▼ parents
  postprocess layer   (pure): python writer, no remote configured;
        │                     pins derived entities
        ▼ parents
  report layer        (pure): fxtr_report_builder (node + lib.fxtr-built
                              main.cjs); pins RootReport roots
        │
        ▼
  fxtr_nix_runner (manager):  discover/open repo entity store ── rehost for builds
                   nix eval fxtr attrset  ── source of truth
                   build the declared exports + layers
                   read adapters: entities (layer + repo-store remote),
                                  namespaces (export manifest + repo store)
  serving script (example):  drive the manager; expose entities + namespaces,
                   /data/<id> via layers ∪ repo entity store
```

Purity structure: **all network lives in the preload FODs** (pinned
hashes); namespace exports, postprocess, and report layers are ordinary
pure derivations. The pure layer steps read only parent layers — enforced
structurally by opening their readers/writers with no remote
(`LayerRemoteNotConfiguredError` on any undeclared input;
`02_layered_file_store.md` §3).

### Namespace exports (pure) + preload FODs

> **Reshaped 2026-06-14 (fifth sync), refined 2026-06-19 (sixth sync).** The
> sections below describe the earlier `preload_from_remote` + `mkProcessedLayer`
> shape. Two changes; `part2_namespace_export_and_preload.md` /
> `part3_python_builders.md` / `part4_report_builder_nix.md` are authoritative:
> 1. **No generic `preload_from_remote` writer.** "Preload" is a *capability*:
>    any FOD that reads `FXTR_REMOTE_*` and pins an `outputHash`. Part 2 ships
>    one such helper, **`entityFromTsvPin`** (the single-pin case); custom
>    in-build fetching is a part-3 builder FOD.
> 2. **No purity-enforcing `mkProcessedLayer`.** Part 3 ships one primitive —
>    *run a user python that writes into a layer writer, in a locked venv, pure
>    or FOD by flag* — split by what it runs: `mkFxtrFnBuilder` (an importable
>    `mod:fn`) and `mkFxtrScriptBuilder` (a loose script file), over a shared
>    `_runFxtrBuilder` scaffold, with the `fxtr2…nix_builder` helper handing the
>    code an open writer + bare `ctx`. Purity is the `outputHash` flag, not
>    structurally enforced. The report step (part 4) is just a part-3 builder
>    whose venv has
>    `fxtr_report_builder` and whose generic `env` points at a nix-built CLI —
>    no report-builder-specific nix code.

Two distinct mechanisms after the [DECIDED-15] simplification:

- **Namespace export** (pure; backs `fxtr.namespaces.<name>`): parse +
  validate the committed TSVs (reuse `fxtr2.data.tsv_namespace.tsv_file` +
  `key_utils`) and emit a publication manifest (JSON manifest, full keys keep
  `prefix:` — see part 2 §2). No network, no precomp, no layer; only the
  serving side reads it.
- **`entityFromTsvPin`** (FOD; the single-pin
  `fxtr.entities.<name>`-from-a-TSV-pin case): a `fxtr_nix_writer` kind
  (`kind="entity_from_tsv_pin"`) that resolves a pin key → id from the
  committed TSVs, fetches via the rehost, and writes a one-pin layer: data
  bytes for the closure it pulls, `mark_as_remote` precomp markers for
  anything left remote, `pins.json` for the declared name. Declares
  `impureEnvVars = [FXTR_REMOTE_BASE_URL, FXTR_REMOTE_BEARER_TOKEN]` +
  `outputHash` (v0: `lib.fakeHash` + manual re-pin; lock file
  `part2b_fod_lock.md`). A drifted hash means the input id changed → re-lock.

### Build steps (`mkFxtrFnBuilder` / `mkFxtrScriptBuilder`; pure or FOD)

The primitive (part 3, `part3_python_builders.md`): a derivation that runs a
**command in a user-supplied venv**; the command runs user python that imports
`fxtr2.data.layered_file_store.nix_builder` to parse what nix passed and get an
**open `LayeredFileStoreIsolatedWriter`** over `$out` (with the declared
`parents` + optional remote). The entrypoint does its own reads and pins.
**Pure** when no remote is configured (reads only its parents —
`LayerRemoteNotConfiguredError` on an undeclared input); a **FOD** when the
part-1 rehost env + `outputHash` are wired (the helper builds the remote from
`FXTR_REMOTE_*`). Purity is the `outputHash` flag, **not** a structural wall.
Two faces over one scaffold (`_runFxtrBuilder`), split by *what they run*:
`mkFxtrFnBuilder` (an importable `mod:fn`) and `mkFxtrScriptBuilder` (a loose
script file), both over a `mkVenv` venv in v0 — one venv backs many scripts, so
new steps are cheap with no per-step dir (inline-metadata scripts are a
follow-up). Generalizes `mkEntityFromPython`'s caller-venv pattern;
`mkEntityFromPython` stays (it pins a *pre-existing* entity, not a function, so
it's not a special case).

### Report step (a part-3 builder with `fxtr_report_builder`)

**Not a special recipe** (part 4, `part4_report_builder_nix.md`): just a part-3
builder whose venv includes `fxtr_report_builder`, with the CLI wired in via the
builder's **generic `env` arg**. Nix has zero report-builder-specific code.

- **[DECIDED-12]** new `lib.fxtr` member (working name `reportBuilderCli`):
  pure pnpm-in-nix build of `projects/fxtr_report_builder/ts/packages/cli/` →
  the single `main.cjs`, then `makeWrapper` so `$out/bin/fxtr-report-builder`
  is a self-contained `node main.cjs` (node bundled in). Pinned by the fxtr
  flake; consumers wire one path, build no node_modules.
- `fxtr_report_builder`'s `resolve_ts_dist_path`
  (`fxtr_report_builder/_ts_dist_path.py`) gets a generic resolution order
  (explicit arg → env var → dev relative fallback). The demo sets
  `env.FXTR_REPORT_BUILDER_CLI = "${lib.fxtr.reportBuilderCli}/bin/fxtr-report-builder"`
  on the builder — generic wiring (the store-path reference *is* the build dep),
  not a system-injected special case.

The entrypoint reads MDX sources from the flake dir (pure inputs), loads
referenced entities from parents (`ctx.readers["<name>"]`), calls
`build_root_report` / `build_sub_report` (provenance projection handled
Python-side around the subprocess — resolves OPEN-6), and pins the roots.

### The serving script (now a thin driver over the manager)

After the fourth-sync reshape, the build/store/rehost machinery lives in the
**`fxtr_nix_runner` manager** (parts 1–2); the example-side script
(`serve_published.py`, part 5) just drives it and serves the result. Sequence:

1. `FlakeManager.open(repo, rev=…)` — the manager discovers (creating if
   absent) and opens the repo entity store, ready to rehost (RUNNER-2/3/4).
2. In a build span the manager stands up the rehost (unconditionally) and
   drives eval/build/materialize of the declared attrset.
3. **Expose exactly what the attrset declares** [DECIDED-10], via the
   manager's two read adapters:
   - `fxtr.entities.<name>` → the **entities adapter** (part 1, RUNNER-5a):
     `retrieve(name)` opens the built layer with the repo store as `remote=`,
     so remote pointers resolve; attach into the serving scope (scope-only,
     no write-back [DECIDED-7]).
   - `fxtr.namespaces.<name>` → the **namespace read view** (part 2,
     RUNNER-5b): iterate the export manifest's key→id map; data resolves
     through the repo store.
   - `/data/<id>` resolves through the serving scope: the manager *exposes*
     its repo entity store (`mgr.repo_store`, a `DeepFetchable` + lease) and
     the serving script mounts it on its own scope
     (`register_deep_fetch_source`) — the manager never mutates the caller's
     scope.
4. Serve with `create_fxtr_app` + `serving_context`
   (`05_http_serving_and_remote.md` §1; may need a small extension if pins
   get namespaced routes rather than one flat dict). Pin-name scheme is
   server policy (no cross-namespace merging).

_Daniel note:_ A parallel change will be scoping out the exact form of the
API that the serving script should publish. For now, the simplest thing is to
expose both the entities and the namespaces as pins with a prefix, e.g. a
virtual pin name "fxtr.entities.foo" or "fxtr.namespaces.bar/{namespace pin}".
This part will be refactored later so we can aim for simplicity.

## New code inventory (≈ the step-plan skeleton)

0. **(Prereq) Repo entity store** (`fxtr2.data.repo_storage`) — **LANDED**:
   decoupled the index namespace into `RepoEntityStore`; `GitNamespaceBackend`
   → `TsvNamespaceBackend` (a view); `fxtr_store.toml` → `fxtr_namespace.toml`.
   (`v2/plans/phase3/stage14_repo_store_refactor/`.)
1. **Runner-manager** (part 1): two-tier `fxtr_nix_runner`; repo-store
   discovery (+create-if-absent); the unconditional rehost over the store;
   `Handle` expose-endpoint/extra-build-env hooks (host + podman, after the
   `design/07` spike); the **entities** read adapter (remote resolves via the
   repo store).
2. **Namespace export** (part 2; pure: TSV parse/validate → manifest; the
   `fxtr.namespaces` attr convention bound to `fxtr_namespace.toml`) + the
   manager's **namespace** read view (a real `FxtrReadNamespace`) + the
   **`entityFromTsvPin` writer kind** in `fxtr_nix_writer` (FOD; fetches a
   single TSV pin via the rehost) + `lib.fxtr` wrapper.
2b. **FOD lock** (part 2b): `fod_lock.json` + runner-owned, self-contained
   `fxtr_nix update-hashes`.
3. **Python builders** (part 3): the `fxtr2…nix_builder` helper + the
   `_runFxtrBuilder` scaffold + `mkFxtrFnBuilder` (importable `mod:fn`) +
   `mkFxtrScriptBuilder` (loose script file), both over a `mkVenv` venv. User
   python writes into a handed-in layer writer; pure or FOD by `outputHash`.
   (Follow-up: inline-metadata scripts — `mkFxtrScriptBuilder` with `venv`
   defaulted from the script's PEP-723 block; git/registry-dep flakes only.)
4. **`lib.fxtr.reportBuilderCli`** (pnpm-in-nix bundle wrapped as a
   self-contained `node` executable) + the generic `resolve_ts_dist_path`
   resolution order; the report step is a part-3 builder with
   `fxtr_report_builder` in its venv and the CLI wired via the builder's generic
   `env`. No report-builder-specific nix code.
5. **The example serving script** driving the manager's read adapters;
   lazy layer→scope exposure for `/data/<id>` (scope-only).
6. **The example workdir** (flake + tsv namespace + postprocess + report) +
   scaffolding (**[OPEN-11]**) + tests at each seam (**[OPEN-14]**) +
   the optional MCP port onto the entities adapter (RUNNER-8).

Rough sequencing: 0 is the prerequisite; 1 → 2 → 2b in order (shared
manager/rehost machinery); 3–4 are independent and testable with
self-contained layers + an in-process repo store; 5–6 assemble.
