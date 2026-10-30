# Part 7c.2 — the publishing demo flake, the shared viewer app, and end-to-end

> Status: scoped (reshaped 2026-06-28). Two headline reshapes vs. the original
> scope:
>
> 1. **No per-demo frontend.** Instead of mirroring `fxtr_http_demo/web/` into the
>    demo, we build **one demo-agnostic HTTP viewer app** (`@fxtr-ui/published-app`)
>    that any published flake can be viewed with, and `fxtr_nix serve` serves its
>    built bundle via a new general `--web-dist` static mount. The demo itself ships
>    **no `web/`** — just the flake + scaffold + seed + E2E + README.
> 2. **Backend-first commit order.** Build the flake (entities + renderers) and get
>    `fxtr_nix serve` serving the right thing *first*, then build the viewer against
>    a server that already has something to show.
>
> Other locked decisions: input notes are the **only FODs** (preloads); every
> builder is **pure-with-parents** + `provenanced`; the E2E depends on a configured
> nix backend (no non-nix smoke path in this part).

## Goal

`examples/fxtr_publishing_demo/` — the publishing-flavored sibling of
`fxtr_workstation_demo`: a template workdir (git repo + tsv namespace + flake)
that builds a small **report graph** via nix with provenance threaded through,
plus a scaffold/seed flow, an end-to-end test, and an operator README. It is
*viewed* through the shared `@fxtr-ui/published-app` served by `fxtr_nix serve`
— the demo contributes no frontend code.

What it demonstrates, end to end: opening the served `RootReport`, its MDX
`contents` tree renders (HTML + `RenderFigure` dispatches); each figure ref
resolves via the connector and dispatches to a renderer — **both** the
re-exported `@fxtr-ui/reports` handlers (for the nested `SubReport`) **and** the
demo's **custom** `Note` / `Stats` renderers; and the derived entities come back
**layered**, so clicking a figure / requesting metadata shows its
`provenance_parent` — provenance threaded build → layer → serve → client, with
report renderers and custom entity renderers all working together.

## Demo content — the report graph

A four-type graph that exercises MDX reports, custom entity renderers, figure
dispatch to both, and provenance on the derived entities:

- **`Note`** (custom type, `packages/demo_types`; custom renderer). The seeded
  **inputs** (~3). Plain `{ title, body }`. Seeded into the repo store + committed
  TSVs by `seed_data.py`; **bare** (no provenance — inputs are ground truth).
- **`Stats`** (custom type, `demo_types`; custom renderer). Aggregate over the
  notes (e.g. `note_count`, `total_words`, `longest_title`). Built by the
  **postprocess** step, **provenanced → layered**.
- **`SubReport`** (`fxtr_report_builder`; re-exported `@fxtr-ui/reports`
  renderer). One MDX section (`build_sub_report`) that references a couple of
  notes + the stats as `RenderFigure`s. **Provenanced → layered**.
- **`RootReport`** (re-exported renderer). The served `fxtr.entities.report`. MDX
  (`build_root_report`) with prose + a `RenderFigure` per note, one for the stats,
  and one for the `SubReport`. **Provenanced → layered**.

Provenance story: **inputs (notes) are bare; every derived entity (stats,
subreport, root report) carries build provenance.** The report's embedded
stats/subreport figure refs are layered ids, so storing the report provenanced
projects them to prov form and loading the report's prov form rewraps them to
layered ids — the nested-provenance carrier the E2E asserts (load the stats /
subreport *through* the report's figure ref and see it come back layered). The
note figure refs stay bare — a deliberate contrast.

## Contents

### The demo (`examples/fxtr_publishing_demo/`)

```
examples/fxtr_publishing_demo/
├── README.md                  # operator walkthrough (scaffold → seed → build web → serve → view)
├── scaffold_workdir.py        # template → standalone git repo + tsv namespace + seed + relock
├── seed_data.py               # pins demo Note inputs into the scaffolded repo store + writes TSVs
├── pyproject.toml             # scaffold/seed deps (fxtr2, fxtr_nix_runner[manager], demo_types)
├── tests/                     # E2E (nix_backend sweep)
└── publishing_workdir/        # TEMPLATE (scaffolded out, like agent_workdir/)
    ├── flake.nix              # preloads + postprocess + subreport + report builders;
    │                          #   fxtr.entities.report + fxtr.namespaces.demo + fxtr.workstation.uiPresets.demo
    ├── fxtr_namespace.toml    # the workdir IS a tsv namespace
    ├── *.fxtr.tsv             # seeded note pins (written by seed_data.py, committed)
    ├── fod_lock.toml          # preload FOD hashes (init empty; filled by `fxtr_nix update-hashes`)
    ├── type_entities/         # aggregator venv root → mkEntityFromPython for Note/Stats forType
    ├── packages/demo_types/   # Note + Stats entity types (shared by seed + builders + renderers)
    ├── builders/              # builder venv (uv project: fxtr2 + demo_types + fxtr_report_builder)
    │                          #   + the postprocess / subreport / report entrypoints
    └── renderers/
        ├── note/              # custom renderer: index.tsx + fxtr_renderer.nix
        ├── stats/             # custom renderer: index.tsx + fxtr_renderer.nix
        └── report/            # re-export @fxtr-ui/reports handlers: index.tsx + fxtr_renderer.nix
```

> No `web/` and no Python backend in the demo. Serving (incl. the dispatcher) is
> the general `fxtr_nix serve`; the UI is the shared viewer app below.

### The shared viewer app (`@fxtr-ui/published-app`)

A new, **demo-agnostic** Vite app — the canonical PanelViewer-over-HTTP client,
lifted from `fxtr_http_demo/web/` and made flake-agnostic. Lives as a new
pnpm-workspace member at `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-published-app/`
(app-shaped: `index.html` + `vite.config.ts` + builds to `dist/`).

It contains exactly what `fxtr_http_demo/web/` already contains, minus anything
demo-specific:

- **`App.tsx`** — `new HttpConnector({ baseUrl, storage })`, discover the
  `dispatcher` ws service by name, `createDynamicDispatcher`, mount `PanelViewer`
  with the generic panel registrations, open on Roots. Verbatim-generic already.
- **`panels.tsx`** — the connector-generic panels: **Roots** (lists whatever
  `connector.listEntities` / `listServices` returns — the served `report` shows up
  here to click), **Storage inspector**, **Dispatcher debug**. (Confirm on lift
  that these carry no demo-specific imports — the http demo's do not.)
- **`main.tsx`** — `installFxtrExternals(...)` with the **union** of all
  renderer-facing externals: the base set (`react`, `react/jsx-runtime`, `jotai`,
  `jotai-effect`, `@fxtr-ui/core`, `@fxtr-ui/state-util`, `@fxtr-ui/react`,
  `@fxtr-ui/react-dispatch`, `@fxtr-ui/workstation`, `@fxtr-ui/workstation-dispatch`)
  **plus** `@fxtr-ui/reports`, `@fxtr-ui/agent-plugin-renderer`,
  `@fxtr-ui/rd-types-channel`, `@fxtr-ui/rd-types-stream`. This list is a maintained
  superset (the frontend analogue of `lib.fxtr.workstationExternals` /
  `DEFAULT_EXTERNALS`); a new renderer-facing package gets added here once.
- **`baseUrl` default**: `import.meta.env.VITE_PUBLISHED_BACKEND_URL` if set (the
  vite-dev case, pointing at a separately-running serve), else
  `window.location.origin` (the served-static case — same origin as the API).

Wiring work: add the package to `pnpm-workspace.yaml` globs + the root
`tsconfig.json` references; it depends on the existing `@fxtr-ui/*` packages
(workspace deps) — no new library code.

## Moving parts

### General `fxtr_nix serve --web-dist` (serve.py addition)

A small, **flake-agnostic** addition to 7c.1's `serve.py` / CLI: an optional
`web_dist: Path | None` on `published_serving_context` (+ `--web-dist DIR` on the
`fxtr_nix serve` CLI). When set, mount `StaticFiles(directory=web_dist,
html=True)` on the Starlette app **after** the API routes (`/data`, `/pin`,
`/pins`, `/preview`, `/discovery`, the service routes, `/ws`) so the SPA is only
the catch-all fallback and never shadows an API route. Serves any built SPA;
nothing demo-specific. Renderer blobs are already same-origin (`/data/<id>`), so a
same-origin static bundle needs no base-url plumbing.

### Scaffolding (`scaffold_workdir.py`)

Mirrors `fxtr_workstation_demo/scaffold_workdir.py` (over
`fxtr_workdir_harness.prepare_workdir_repo`): copy `publishing_workdir/` template →
pin the `fxtr` flake input to this checkout → `uv lock` / `nix flake lock` →
`git init` + commit. Publishing-specific additions, in order:

1. Template already ships `fxtr_namespace.toml` (the workdir is a tsv namespace).
2. Run `seed_data.py` against the scaffolded repo (pins note inputs → repo store +
   writes `*.fxtr.tsv`), then commit the TSVs.
3. **FOD relock**: template ships an empty `fod_lock.toml`; after seeding, run
   `fxtr_nix update-hashes` (runner-owned) to lock the `fxtr.preloads.<key>` slots,
   then commit the lock. No bespoke hash dance.

### Repo entity store + seeding (`seed_data.py`)

The scaffolded repo's backing sqlite lives at the default
`.fxtr/repo_backing_storage.sqlite`; the manager's create-if-absent means no
explicit store init beyond what `seed_data.py` triggers. `seed_data.py`:
construct the `Note` entities (from `demo_types`), open the repo's
`RepoEntityStore`, `write_from_arena` each note, and write the `*.fxtr.tsv` pins
(`note_<k> → id`). Bare entities — the repo store / tsv-namespace flow is
provenance-free.

### Flake wiring (`publishing_workdir/flake.nix`)

One `mkVenv` builder venv (`builders/`: fxtr2 + demo_types + fxtr_report_builder)
backs all three builder steps; a `type_entities/` aggregator venv (fxtr2 +
demo_types) backs `mkEntityFromPython` for the custom `forType`s. Then:

- **Preloads (the only FODs).** Each seeded note → `fxtr.preloads.note_<k> =
  lib.entityFromTsvPin { key = "note_<k>"; outputHash = (fodLockReader …).note_<k>; }`
  — a one-pin layer fetched via the build-time rehost. These are the builders'
  `parents`.
- **postprocess** — `lib.mkFxtrFnBuilder { venv = builders; entry =
  "publishing_builders.postprocess:build"; parents = { note_0 = …; … };
  provenanced = true; provenanceInfo = { desc = "..."; }; }`. Pure (reads parents
  only): reads the notes, computes `Stats`, pins it. → stats layer.
- **subreport** — same shape; `parents` = the note layers + the stats layer; the
  entrypoint calls `build_sub_report` with the note/stats refs as `inputs` and MDX
  that `RenderFigure`s them. → subreport layer.
- **report** — `parents` = notes + stats + subreport; entrypoint calls
  `build_root_report` referencing all of them; `provenanced = true`. → the report
  layer, wrapped as `fxtr.entities.report = { type = "fxtr_layer_entity"; layer;
  layerKey; }`.
- **`fxtr.namespaces.demo`** — `lib.tsvNamespaceExport { namespaceRoot = ./.; name
  = "demo"; }` over the committed note TSVs (bare `"demo:note_<k>"` pins).
- **`fxtr.workstation.uiPresets.demo`** — `lib.mkUiPreset` over the merged renderer
  slices (note // stats // report).

Builders are `provenanced` + input-addressed (pure), so `provenanceInfo` is
optional; we set a `{ desc }` on each for a readable build-provenance node.
`reportBuilderCli` is wired into the subreport/report builders via the generic
`env` (`FXTR_REPORT_BUILDER_CLI = "${lib.reportBuilderCli}/bin/fxtr-report-builder"`),
exactly as the `py_testing` report-step fixture does — nix has no
report-builder-specific code.

### Renderers (`renderers/*`)

Three slices, each `{ renderer = lib.esbuildRenderer { src = ./.; externals = …; };
subtypes."<k>" = { forType; forDispatchKeys = [ … ]; }; }`, merged in the flake and
keyed by a shared `keyIds` UUID dict (mirrors the workstation demo):

- **`note/`** (custom) — `Note` `FullPanel` + `Figure` (+ links if cheap). `forType
  = mkType "<Note id>" "demo_types:Note.fxtr_type"` over the `type_entities` venv.
  Externals = `lib.workstationExternals` (imports only base + workstation).
- **`stats/`** (custom) — `Stats` `FullPanel` + `Figure`. `forType` likewise.
- **`report/`** (re-export) — `index.tsx` re-exports the `@fxtr-ui/reports`
  handlers (`RootReportFullPanel` / `…BlockLink` / `…InlineLink` / `…HoverPreview`
  + the `SubReport` + `Figure`-key handlers), registered under the `RootReport` /
  `SubReport` type ids. `forType` reuses **`lib.fxtr.entities`** for the report
  types (built from the `fxtr_lib_type_ids` stub venv — the same way the
  workstation demo's `builtin/` renderer routes its report slots), so the demo
  needs no `fxtr_report_builder` venv just for the type entities. Externals =
  `lib.workstationExternals ++ [ "@fxtr-ui/reports" "@fxtr-ui/workstation" ]`.

### E2E test (`tests/test_publishing_e2e.py`)

`nix_backend`-sweep marked + `skip_if_runtime_unconfigured` (the
`fxtr_nix_runner.testing` helper). Because the preloads are FODs, this exercises
the build-time rehost and runs where a backend is configured (no non-nix path).

1. scaffold into tmp (incl. seed + relock) → 2. `published_serving_context`
   against it (in-process, ephemeral port, `provenanced=True`) →
   3. `HttpFxtrReadNamespace` client: list pins; retrieve `report` → assert
   `RootReport` shape and that the id comes back **layered**
   (`_provenance_parent is not None`); load a figure ref out of the report's
   `contents` tree (the stats / subreport) and assert it too is **layered**
   (nested provenance via the prov-form load); load a **note** figure ref and
   assert it is **bare** → 4. fetch a namespace pin (`demo:note_<k>`) and assert it
   is **bare** + resolves its bytes, plus a raw `/data/<id>`.

The headless E2E is the acceptance gate; the shared viewer is the demo payoff
(hand-tested, not in CI).

### Docs

- Example README: operator walkthrough — scaffold → (seed/relock happen inside
  scaffold) → `pnpm --filter @fxtr-ui/published-app build` → `uv run fxtr_nix serve
  <workdir> --port 8080 --web-dist <published-app>/dist` → open `localhost:8080` →
  "what just happened" (map each served entity to the build step that produced it +
  point out bare-vs-layered). Plus the two-terminal vite-dev recipe (set
  `VITE_PUBLISHED_BACKEND_URL` to the running serve) for the frontend dev loop.
- Update `projects/fxtr_workstation/CODE_OVERVIEW.md` (plans tree + the new
  `frontend_lib/packages/fxtr-ui-published-app` member); update `fxtr_nix_runner`'s
  CODE_OVERVIEW for `--web-dist`; repo-root README if examples are indexed there.

## Commit plan (backend-first)

1. **Demo flake + types + renderers.** `demo_types`, the three builder
   entrypoints, `flake.nix` (preloads + postprocess + subreport + report,
   `provenanced`), namespace export, and the `uiPresets.demo` renderer slices
   (note + stats custom, report re-export). Buildable + eval-checkable in isolation.
2. **Scaffold + seed + relock.** `scaffold_workdir.py`, `seed_data.py`,
   `pyproject.toml`, the empty `fod_lock.toml`; scaffolding produces a servable
   repo (`fxtr_nix serve <wd>` shows the right entities/namespaces/dispatcher over
   the API — verifiable headless before any frontend exists).
3. **`fxtr_nix serve --web-dist`.** The general static-mount addition to `serve.py`
   + CLI.
4. **`@fxtr-ui/published-app`.** The shared viewer (lift + generalize from
   `fxtr_http_demo/web/`, union externals, origin base-url), pnpm-workspace +
   tsconfig wiring.
5. **E2E + README + CODE_OVERVIEW.** The provenance round-trip test + the operator
   README + doc updates.

## Implementation steps

- [x] `packages/demo_types` (`Note`, `Stats`) + `builders/` venv + the postprocess
      / subreport / report entrypoints.
- [x] `flake.nix`: preload FODs, the three `provenanced` builders, namespace
      export, entities attr, and `fxtr.workstation.uiPresets.demo` (note + stats +
      report renderer slices). Update CODE_OVERVIEW.md (examples index).
- [x] `scaffold_workdir.py` + `seed_data.py` + `pyproject.toml` + empty
      `fod_lock.toml` (namespace init already in template; seed + relock + commit).
- [x] `fxtr_nix serve --web-dist` static mount (serve.py + CLI). Update
      `fxtr_nix_runner` CODE_OVERVIEW.md.
- [x] `@fxtr-ui/published-app` (lift from `fxtr_http_demo/web/`, union externals,
      origin base-url) + pnpm-workspace + tsconfig wiring. Update CODE_OVERVIEW.md.
- [x] E2E test (headless provenance round-trip over the `nix_backend` sweep).
- [x] README + final CODE_OVERVIEW.md pass.

## Implementation notes (as built)

- **In-report figure refs are *all* layered, even the bare notes.** `build_*_report`
  forcibly projects every input through the provenance form (`create_u_node` for a
  bare data id → re-embedded as `(data, u(data))` on decode — see
  `fxtr_report_builder.mdx_builder`). So inside `RootReport.contents`, note figure
  refs come back layered-with-a-trivial-u-node, while stats/subreport refs are
  layered with a real build c-node. The clean bare-vs-layered contrast is therefore
  **namespace pin (`demo:note_<k>`, bare) vs. report + its figures (layered)** — the
  E2E asserts the namespace pin is bare and a stats/subreport figure ref is layered
  with real provenance, rather than claiming an in-report note figure is bare.
- **Provenanced builders just `store` + `pin`.** `provenanced = true` mints the
  build c-node and opens `ctx.scope` with it (per `nix_builder`), so the three
  entrypoints need no manual provenance-context dance (unlike the 7c.1 /
  `provenanced_builder` fixtures, which build an explicit c-node to test that path).
  Each carries a `provenanceInfo.desc` (optional for an input-addressed build).
- **Real ids in `root.fxtr.tsv`** (not `@…@` markers): the note content is fixed,
  so the content-addressed ids are committed directly (consistent with the
  hand-pasted type id_strings). `seed_data.py` writes the note *bytes* into the
  gitignored repo store and asserts the committed ids match what it stored.
- **E2E asserts report-level provenance + figure resolution, not figure-ref-level
  layeredness.** The served `RootReport` comes back layered (provenance survived
  build→serve), its `contents` embed exactly the 3 notes + stats + subreport, and
  every figure ref resolves through the serve. Per the layer format, data-CBOR
  refs are bare on the wire (provenance rides at the pin level / via `/data`
  precomp), so the headless gate does **not** assert an in-`contents` figure ref
  is itself layered — figure-level provenance is surfaced via `/data` precomp and
  shown in the visual viewer's metadata. A stricter assertion can be added once the
  exact served-`contents` ref shape is confirmed on a real backend run.
- **Validated on the podman backend** (`pytest -k podman`, ~100s): the full
  scaffold → seed → relock → serve → read round-trip passes (report layered, 5
  figure refs resolve, namespace pin bare, `--web-dist` SPA served). One bug
  surfaced + fixed: the builder venv's `extra-build-dependencies` key must be the
  **canonical/hyphenated** name `fxtr-report-builder` (uv ignores the underscored
  form), else the git-sourced `fxtr_report_builder` hatchling-backed wheel build
  fails with `ModuleNotFoundError: hatchling`. (`py_testing` never hit this — its
  `fxtr_report_builder` is an editable path, never wheel-built.)

## Notes / follow-ups

- **Serve the viewer bundle *by default*, built by nix** (tracked in
  `part3_cleanups.md`): have the **root `fxtr` flake declare a derivation that
  builds `@fxtr-ui/published-app`'s static assets**, so `fxtr_nix_runner` builds
  them through the same nix path it uses for everything else and serves them with
  **no `--web-dist` flag** (à la the workstation server's debug-UI dist). This is
  the (b) variant of the serve-the-frontend decision; (a), the `--web-dist` flag,
  ships here.
- **Vite dev loop**: covered by the README two-terminal recipe
  (`VITE_PUBLISHED_BACKEND_URL` → a running serve). A `fxtr_nix serve --web-dev`
  that spawns vite itself is a possible later convenience, not in this part.
- **Hub story** (pushing to a shared bare repo) is out of v0 — the scaffolded repo
  is strictly local; a "push to a shared remote" README stanza is a later addition.
</content>
</invoke>
