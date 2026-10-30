# fxtr_publishing_demo

The publishing-flavored sibling of `fxtr_workstation_demo`: a template workdir
whose `flake.nix` builds a small **report graph** with nix — with provenance
threaded through — that is then served (with provenance exposed) over the
stateless HTTP stack by the general `fxtr_nix serve`, and viewed through the
shared `@fxtr-ui/published-app` frontend.

The demo ships **no backend and no per-demo frontend**: serving is `fxtr_nix
serve` (from `fxtr_nix_runner`); the UI is the demo-agnostic published viewer.
This example contributes a flake, a scaffold/seed flow, and an end-to-end test.

> Design: `projects/fxtr_workstation/plans/phase0/part7c_build_and_serve_demo/`.

## The report graph (what the flake builds)

A four-type graph, built bottom-up, exercising MDX reports + custom entity
renderers + figure dispatch + provenance:

```
seeded Notes (bare)  ──►  Stats (postprocess)  ──►  SubReport (MDX)  ──►  RootReport (MDX)
   demo_types.Note          demo_types.Stats        fxtr_report_builder   = fxtr.entities.report
```

- **`Note`** — seeded inputs, **bare** (the tsv-namespace / preload flow strips
  provenance). Custom renderer (`renderers/note`).
- **`Stats`** — aggregate over the notes, built by the postprocess step,
  **provenanced** (carries the nix build as provenance → layered id). Custom
  renderer (`renderers/stats`).
- **`SubReport`** / **`RootReport`** — MDX reports (`fxtr_report_builder`) whose
  `RenderFigure` nodes embed the notes/stats/subreport as figure refs. Rendered
  by the re-exported `@fxtr-ui/reports` handlers (`renderers/report`).

Provenance: inputs are bare; every derived entity is provenanced, so the served
`RootReport` is layered and its embedded stats/subreport figure refs carry
provenance (the nested-provenance carrier — load a figure ref through the report
and it comes back layered). The three builders are **pure** (read parents only);
the only FODs are the note **preloads** (they fetch the seeded note bytes from
the manager's repo-store rehost).

## Layout

```
fxtr_publishing_demo/
├── CODE_OVERVIEW.md
├── README.md                    # operator walkthrough (scaffold → build viewer → serve → view)
├── pyproject.toml               # scaffold/seed deps (fxtr2, fxtr_nix_runner, demo_types); dev: httpx, fxtr_report_builder
├── scaffold_workdir.py          # template → fresh git repo + seed + relock preload hashes
├── seed_data.py                 # the Note inputs + seed() (writes bytes to the repo store; asserts ids match the TSV)
├── tests/                       # conftest.py (nix_backend sweep) + test_publishing_e2e.py (provenance round-trip)
└── publishing_workdir/          # the template workdir (scaffolded into a fresh git repo)
    ├── flake.nix                # preloads + 3 provenanced builders + namespace export + uiPresets.demo
    ├── fxtr_namespace.toml      # the workdir is a tsv namespace (reserved_prefix=root, name=demo)
    ├── root.fxtr.tsv            # the seeded note pins (real content-addressed ids; bytes live in the repo store)
    ├── fod_lock.toml            # preload FOD hashes (empty; filled by `fxtr_nix update-hashes` at scaffold)
    ├── .gitignore               # ignores the local repo store (.fxtr/) + venv/build artifacts
    ├── packages/demo_types/     # Note + Stats entity types (shared by seed + builders + renderers)
    ├── builders/                # builder venv + postprocess / subreport / report entrypoints
    └── renderers/{note,stats,report}/   # index.tsx + fxtr_renderer.nix slices for uiPresets.demo
```

## Key conventions

- **The repo entity store (`.fxtr/`) is gitignored.** The committed `*.fxtr.tsv`
  is the git-tracked namespace *view* (key → id); the note *bytes* are seeded
  into the local store (so the preload FODs fetch them via the rehost) and are
  not committed. The TSV ids are real content-addressed ids — they must match the
  notes the seed writes (the seed asserts this).
- **Hand-pasted ids.** `flake.nix`'s `mkType` calls hardcode the `Note`/`Stats`
  type id_strings, and `root.fxtr.tsv` hardcodes the note ids. Reprint type ids
  with `python -c "import demo_types; print(demo_types.Note.fxtr_type.id_string())"`.
- **Comment-flip sources.** Every `pyproject.toml` with a fxtr-lib dependency
  uses the `# fxtr-lib-sub:` convention (relative paths active in the template;
  pinned git sources activated in the scaffolded workdir copy by
  `prepare_workdir_repo`).
- **One builder venv** (`builders/`, fxtr2 + demo_types + fxtr_report_builder)
  backs every `mkFxtrFnBuilder` step and `mkEntityFromPython` (the Note/Stats
  `forType`s). The report/subreport steps reach the MDX CLI through the builder's
  generic `env` (`FXTR_REPORT_BUILDER_CLI = ${lib.reportBuilderCli}/...`).
- **Renderers.** `note`/`stats` are custom (the four shell keys; figures fall
  through the `@fxtr-ui/reports` `Figure` no-match fallback to their `FullPanel`).
  `report` is a pure re-export of the `@fxtr-ui/reports` RootReport/SubReport
  handlers; its `forType`s come from `lib.fxtr.entities.{rootReport,subReport}`.
