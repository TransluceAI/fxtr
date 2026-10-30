# Chunk 3 — renderers

**Status: DONE.** Browser-side polish: the driver panel plus
entity renderers for every new type, following the existing workdir
renderer conventions (`agent_workdir/renderers/*/` — TSX module +
`fxtr_renderer.nix` slice, curated externals only, keyed by type-id
string in the slice). Implementation notes:

- **One entity slice, ten subtypes** (`renderers/behavior_entities/`) —
  a single esbuild module with per-type dispatch entries, not
  per-type dirs (one nix derivation instead of ten; the builtin slice
  is the precedent). Small factory helpers (`entryFor` /
  `linksFor` / `hoverFor` / `panelFor`) keep the ten types mechanical.
- The entity type ids became named module constants
  (`TRANSCRIPT_TYPE_ID`, …) so `mkEntityFromPython` can reference them
  as `behavior_research_types.entities:<CONST>`.
- Verified live: `--auto-workdir` boot batch-builds the preset with
  the two new slices; the dispatcher's eager set_preset validation
  checks every hand-pasted id-string against the built typeEntities,
  so a clean boot proves the flake wiring end to end.

## 1. Driver panel — `renderers/behavior_driver/`

Fork of the `experiment_driver` scenario-picker (keyed by the new
driver service type id): grouped buttons with operator guidance text —

- **Stage 1–4** group (the narrative arc, guide text explaining what
  each stage builds and what to look at in the dashboard after);
- **Run controls** group (`target_analysis`, `run_analysis`,
  `clear_stale`, `status`), with the run button's guide noting the
  background demand + where to watch progress (the rollout cells' run
  logs stream per-turn `ctx.log` lines);
- result cards render returned dicts with `RefLink`s (configs,
  transcripts, results all clickable), as today.

## 2. Entity renderers

One directory per type (or one `behavior_entities/` module with
multiple dispatch keys if the nix-slice overhead dominates — decide at
implementation; leaning per-type for consistency with the existing
workdir):

- **Transcript** — chat-bubble list (user left / assistant right),
  port the workstation demo's `renderers/transcript/` look; link +
  panel contexts.
- **SubjectModelConfig / UserSimConfig / JudgeConfig** — small labeled
  cards (provider/model chips + the prompt/rubric text preformatted).
- **RolloutConfig / SweepConfig / ActiveIds** — one-line value chips.
- **JudgeResult** — score badge (0–1, color ramp) + reasoning text.
- **AnalysisTable** — a plain sortable-enough HTML table from
  `columns` + `rows` (generic: renders any AnalysisTable, not just this
  analysis).
- **ServiceKey** — label + detail line (handle cells render readably).

## 3. Wiring

- `type_entities/pyproject.toml` aggregator gains
  `behavior_research_types` (and the driver marker package if its type
  id needs the entity layer); remember the committed-`uv.lock` gotcha
  for workdir packages.
- `flake.nix`: new renderer slices join `uiPresets.default`.
- Smoke: relaunch with `--auto-workdir`, press through the stages,
  click every returned ref class once. Frontend checks are the repo
  root's usual vitest/tsc/biome sweep (no new unit tests unless a
  renderer grows logic worth pinning — the AnalysisTable
  column/row mapping may deserve one).

## 4. Wrap-up

- README layout section + `agent_workdir/README.md` renderer list.
