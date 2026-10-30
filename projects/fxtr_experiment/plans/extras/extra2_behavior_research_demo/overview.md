# Extra 2 — behavior-research demo (ABR-inspired)

**Status: planned.** A realistic-use-case exercise: extend
`examples/fxtr_experiment_demo` with a second scenario driver that
builds a **mini behavior-research pipeline** — subject models × user
simulators × judges, rolled out over replicas and aggregated into a
score table — inspired by ABR's rollout/evidence-store shape,
without implementing that
proposal. Where the existing driver's toy tasks probe the system for
bugs, this one demonstrates the operations a researcher would actually
perform, stitching together existing patterns: the fxtr_experiment
view calculus + durable execution, and the workstation demo's
LLM-service building blocks.

## End state

- `examples/fxtr_experiment_demo` gains, alongside the old driver:
  - a **second workflow runner** (own sqlite pair under the workdir,
    registered as `behavior-workflow`) so the two scenarios don't share
    a workflow namespace;
  - a **`behavior-demo-helper`** driver service whose buttons walk one
    scenario in four stages (below), plus target / run / clear-stale
    controls;
  - real-LLM subject-model / user-simulator / judge services (host-side
    impls calling the Anthropic API), bound only when the demo is
    launched with `--allow-llm-apis` and `ANTHROPIC_API_KEY`;
  - custom entity types (transcripts, configs, judge results, the
    analysis table) in a new workdir marker package, with renderers for
    each plus the driver panel.
- Three LLM-binding modes: **real** (`--allow-llm-apis` + key),
  **mocked** (`--mocked-llm-apis` — fake service impls behind the same
  handle keys, returning obviously-faked deterministic data, so the
  whole workflow runs end to end with no key), and **off** (neither
  flag: handles stay unbound, demands fault with the boundness error —
  itself a legible state). The mock mode is the bring-up and test
  path; real prototyping uses small models and small turn counts.

## The scenario's grid namespace

All built by the driver through the user capability (so every write is
user-sourced and grounded):

| path | kind | axes | contents |
|---|---|---|---|
| `tasks/` | subtree | () | one scalar handle grid per task fn |
| `tasks/rollout`, `tasks/judge`, `tasks/analyze` | handle | () | task-fn handle keys |
| `services/` | subtree | () | LLM service handles |
| `services/subject_model_services` | handle | (provider: str) | chat-completion services (v0: `anthropic`) |
| `services/user_simulator_service` | handle | () | the single user-sim service (not a sweep axis) |
| `services/judge_services` | handle | (provider: str) | judge services, keyed by provider |
| `configs/` | subtree | () | config entities |
| `configs/subject_models` | data | (subj_id: str) | `SubjectModelConfig(provider, model, system_prompt)` |
| `configs/user_sims` | data | (sim_id: str) | `UserSimConfig(task_prompt, persona)` |
| `configs/judges` | data | (judge_id: str) | `JudgeConfig(provider, model, rubric)` |
| `configs/rollout_config` | data | () | `RolloutConfig(max_turns)` — deliberately **not** in sweep_config, so editing replica count never invalidates rollouts |
| `domain/` | subtree | () | what's active (the open-map principle's domain cells, zero-axis-frame form) |
| `domain/active_subject_models` | dataframe | () | schema `(subj_id: str)` — the active ids, one per row |
| `domain/active_user_sims` | dataframe | () | schema `(sim_id: str)` |
| `domain/active_judges` | dataframe | () | schema `(judge_id: str)` |
| `domain/sweep_config` | data | () | `SweepConfig(replicas)` |
| `evidence/` | subtree | () | the computed evidence |
| `evidence/rollouts` | task | (subj_id, sim_id, replica: int) | `tasks/rollout`; output = `Transcript` entity |
| `evidence/judgements` | task | (subj_id, sim_id, replica, judge_id) | `tasks/judge`; outputs = `verdict` (`JudgeResult` entity) + `data` (frame `(score: float)`, one row) |
| `analyses/` | subtree | () | empty until stage 3 |
| `analyses/sales_report` | task | () | `tasks/analyze`; outputs = `report` (`AnalysisTable` entity) + `evidence` (frame: the gathered per-judgement table) (opened in stage 3) |

Domain cells are zero-axis dataframe cells (the canonical domain-cell
shape) since chunk 5 — the analysis builds its driving table from them
and `gather`\ s the judgement `data` frames across the whole cross
product, with the verdict entities point-read only for the per-row
provenance refs (`chunk5_dataframe_domains.md`).

### Task wiring (stage 1 opens these rules)

`evidence/rollouts` — axes `(subj_id, sim_id, replica)`:

- `subject`: `configs/subject_models` read-only (axis `subj_id`
  vmap-consumed → one config per cell)
- `sim`: `configs/user_sims` read-only (`sim_id` consumed)
- `rollout_cfg`: `configs/rollout_config` read-only (scalar)
- `replica`: `int_coord("replica")` — the sanctioned way a cell learns
  its own replica index (consumed axes are otherwise unobservable)
- `subject_services`: `services/subject_model_services` read-only,
  `provider` axis **kept** — the task does a data-dependent
  `read_key(provider=cfg.provider)`
- `sim_service`: `services/user_simulator_service` read-only (scalar)

`evidence/judgements` — axes `(subj_id, sim_id, replica, judge_id)`:

- `transcript`: `rollouts.output_entity()` (three axes consumed)
- `judge_cfg`: `configs/judges` read-only (`judge_id` consumed)
- `judge_services`: `services/judge_services` read-only, `provider` kept

`analyses/sales_report` — axes `()` (stage 3):

- `judgements`: `judgements.output_entity("verdict")` with **all four
  axes kept** — point-read per coordinate for the provenance refs
- `judgement_data`: `judgements.output_frame("data")`, same axes kept
  — the `gather` target; the gather's request edges demand exactly the
  cross product the domain cells enumerate
- `active_subjects` / `active_sims` / `active_judges` /
  `sweep`: the four `domain/` cells, read-only (the first three are
  dataframe views the driving table is built from)

Demand flows through requests (self-vouching), so targeting + demanding
the single analysis cell pulls every needed judgement, which pulls every
needed rollout — the pull-based MH-pilot shape
(`notes/example_mh_pilot.md`), driven from the top-level user surface
instead of a scheduler task.

## The toy domain: car-sales assistants

Subject models are assistants tasked with selling a car; user sims are
personas investigating a purchase; judges score how the assistant
approaches the sale.

- Stage-2 subject models: `baseline` (helpful salesman) and `pushy`
  (close-the-deal-today system prompt); stage 4 adds `principled`
  (transparent, no-pressure).
- Stage-2 sims: `cautious_buyer`, `eager_enthusiast`; stage 4 adds
  `skeptical_bargainer`.
- Stage-2 judges: `pushiness`, `effectiveness`; stage 4 adds `ethics`.
- Judge scores in [0, 1]; the analysis reports the mean per
  (subject_model, judge) over sims × replicas.

## Stages → driver buttons

1. **`stage_structure`** — open every grid above (including the empty
   `analyses/` subtree), write the task-fn + service handle keys.
2. **`stage_inputs`** — write the stage-2 config entities, the
   `rollout_config` (small `max_turns`), the three `ActiveIds` domain
   cells, and `SweepConfig(replicas=1)`.
3. **`stage_analysis`** — open `analyses/sales_report` wired as above.
   Separate controls (also doable from the normal UI — the buttons are
   conveniences): **`target_analysis`** (add_target),
   **`run_analysis`** (demand in the background, the `scenario_slow`
   pattern — LLM sweeps are slow and watching cells fill in the
   dashboard is the point), **`clear_stale`** (the v0 stale-rerun
   gesture: clear non-fresh task cells across `evidence/` +
   `analyses/`).
4. **`stage_augment`** — write the new subject model / sim / judge
   configs, **overwrite** the three `ActiveIds` cells and
   `SweepConfig(replicas=N)`. The analysis goes stale (it read the
   domain cells); old rollouts/judgements stay fresh; `clear_stale` +
   `run_analysis` computes only the new cells — the incremental-compute
   payoff.

Buttons are idempotent-ish like the existing driver's: create-or-attach
opens, and rewriting identical values is a no-op success. A cell already
holding a *different* value errors the button with the write conflict
(shown in the panel — never silently resolved); the deliberate
overwrites are stage 4's domain edits and stage 1's handle cells on a
mode switch.

## Key design decisions

- **Extend `fxtr_experiment_demo` in place** (second runner + driver +
  renderers) rather than fork a new example: reuses the orchestrator,
  workdir scaffolding, frontend shell, and nix build caches. The two
  runners are fully separate (own data dirs, own external names).
- **Real LLMs flag-gated; mocks at the service boundary, under
  distinct keys.** `--allow-llm-apis` + `ANTHROPIC_API_KEY` binds the
  real impls (shared `AsyncAnthropic` client, host-side);
  `--mocked-llm-apis` binds fake impls of the *same three protocols*
  (canned sales-pitch/persona lines; judge scores a deterministic hash
  of rubric + transcript, so the analysis table shows varied,
  reproducible numbers). Mocking at the service level — not a fake
  HTTP client — keeps the fake honest without prompt-sniffing (the
  judge mock must return a well-formed verdict). Task fns are always
  bound. Each mode binds **its own content-addressed keys**
  (`svc:subject:anthropic` vs `svc:subject:mock`, …) and
  `stage_structure` writes the current mode's keys into the handle
  cells: the handle trust boundary is key identity, so switching mode
  and re-pressing stage 1 overwrites the handle cells and fans
  staleness through every rollout/judgement — mock results are never
  silently served as real ones, and the mock→real switch is itself a
  demo of provenance doing its job.
- **Provider-keyed subject + judge services; scalar user sim.** The
  `provider` axes exist as real handle-grid axes so a second provider is
  an ordinary handle write + binding; the user sim is deliberately not a
  sweepable axis.
- **`rollout_config` split from `sweep_config`** so replica-count edits
  (read only by the analysis) never staleness-propagate into rollouts.
- **One file per task fn / per service**, each using its decorator
  (`@task_fn`, `@step_fn`); every LLM call happens inside a checkpointed
  step, so kill/resume mid-sweep works for free.
- **Entity/proto marker package** `behavior_research_types` in
  `agent_workdir/packages/` (the `experiment_notes` pattern): entity
  types + the three service protocols + the content-addressed
  handle-key helper, importable host-side and by the type_entities
  aggregator for renderers.

## Chunks

1. `chunk1_types_and_tasks.md` — the marker package (entities, service
   protocols, key helpers), the three LLM service impls, the three task
   fns; entity round-trip tests.
2. `chunk2_runner_and_driver.md` — RunnerSupervisor generalization, the
   behavior runner mount (auto-bind + flag-gated LLM bindings), the
   driver service + protocol, orchestrator wiring (`--allow-llm-apis`),
   headless structural tests + an env-gated live smoke.
3. `chunk3_renderers.md` — the driver panel renderer and entity
   renderers (transcript, configs, judge result, analysis table), flake
   preset + type_entities wiring.
4. `chunk4_sim_improvement.md` — the ABR-style user-sim improvement
   transformation: a pinned-snapshot request entity, a standalone
   `transformations/` flow task that reruns the same rollout/judge
   logic in its own internals per candidate, panel inputs, and the
   behavior driver's crash/revive controls.
5. `chunk5_dataframe_domains.md` — dataframe domain cells + the
   judgement `data` frame output + `gather` in the analysis (the
   phase-5 followup; `ActiveIds` deleted).

## Post-review refinements (after chunk 3)

- **Concurrent analysis fan-out.** `analyze_task` demands the whole
  judgement cross product through one anyio task group (results land
  in a coordinate-keyed dict; aggregation iterates the domain lists,
  so the output is deterministic). Replay-safe because generation
  records are subject-keyed, not an ordered log.
- **Atomic create-or-attach in the state db** (core fix the fan-out
  surfaced): concurrent first-runs of cells in one task grid raced
  their check-then-create of the shared checkpoint grid
  (`IntegrityError: UNIQUE constraint failed: grids.key_path`).
  `state_store._create_grid` now re-checks inside the insert
  transaction — same kind + config is a no-op attach (no marks, no
  events), a mismatch raises `GridConfigMismatchError` like the
  callers' own pre-checks.
- **XML judge verdicts + retry.** The judge asks for
  `<reasoning>…</reasoning>` then `<score>…</score>` (tags beat
  JSON-in-prose for small models; the old first-`{`-to-last-`}`
  parser also made any stray trailing bracket fatal) and retries
  unparseable completions up to 3 fresh samples before erroring the
  generation.

## Deferred followups

- **Sandboxing a service** (e.g. the judge) via the workstation demo's
  spawn machinery, to show the trust-boundary upgrade. Needs scoping
  once the host-side version works.
- ~~Crash/revive buttons for the behavior runner~~ (landed with
  chunk 4).
- **Judgement fan-out per provider** (majority vote à la MH pilot) if a
  second provider is ever bound.
