# Experiment demo workdir (template)

The committed template for the experiment demo's runtime workdir.
`run_experiment_demo.py --auto-workdir` (or a manual
`prepare_workdir_repo` scaffold) copies it to a fresh git repo,
substitutes the fxtr lib ref, and locks it; the demo's renderer
dispatcher builds the `default` UI preset from that repo's HEAD.

Contents:

- `flake.nix` — `fxtr.uiPresets.default`: the builtin slice + the
  `exp_note` entity renderer + the `experiment_driver` scenario-picker
  panel + the behavior scenario's `behavior_entities` (transcripts,
  configs, verdicts, the analysis table, service keys) and
  `behavior_driver` (stage-picker panel) slices, merged with
  `lib.experimentRenderers` — the repo nix lib's fxtr_experiment slice
  (the workflow dashboard, the frame-entity table, and the stored
  GridConfigEntity / SliceTemplateEntity renderers), whose source
  lives in the fxtr repo (`projects/fxtr_experiment/frontend`), not in
  this workdir.
- `packages/experiment_notes` — the demo's universal value entity type.
- `packages/experiment_demo_helper` — the driver service's type id +
  protocol (impl is host-side, next to the orchestrator).
- `packages/behavior_research_types` — the behavior scenario's entity
  types, LLM service protocols, and content-addressed handle keys.
- `packages/behavior_demo_helper` — the behavior driver service's type
  id + protocol (impl is host-side, in the demo's `behavior/`).
- `type_entities/` — the aggregator venv the typeEntity layers build
  from.
- `renderers/` — one dir per renderer module (each with its
  `fxtr_renderer.nix` slice).
- `data/` + `data_behavior/` (gitignored, created at runtime) — each
  workflow's two sqlite files. Workflow state shares this workdir's
  lifetime: `--auto-workdir` re-scaffolds (and wipes) on every launch;
  scaffold a workdir yourself and pass `--workdir` to keep state
  across relaunches.
