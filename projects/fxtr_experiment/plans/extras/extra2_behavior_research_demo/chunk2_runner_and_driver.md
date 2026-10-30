# Chunk 2 — behavior runner + driver, orchestrator wiring

**Status: DONE.** Wires chunk 1's pieces into the demo: a second
supervised runner, the `behavior-demo-helper` driver, and the
`--allow-llm-apis` flag. See `overview.md` for the grid namespace and
stage map. All sections implemented as written, with these notes:

- `BehaviorRunner` is a dataclass subclass of the generic
  `ExperimentRunner` handle (task keys ride the existing `fn_keys`;
  `svc_keys` + `llm_mode` are the additions), so one supervisor type
  serves both scenarios.
- The driver's generic id export uses `scope.copy_from_scope` (raw
  closure copy — no typed loads), unlike the old driver's
  ExpNote-typed re-store; view-level writes go through
  `errors.typed_wire_call` so `WriteConflictError` crosses as its
  typed self (the handle helper branches on it for the mode-switch
  overwrite; config-write conflicts propagate to the panel by design —
  a review round replaced the initial first-write-wins suppression
  with error-out-and-show).
- Task cells expose no value through `inspect`, and `Workflow.demand`
  on a bare task view faults ("task reads require an output
  selection") — reading a result is `add_target` +
  `output_entity().read(**coords)` everywhere (driver and tests).
- A fourth test tier landed: **mode-switch staleness** (build + run
  under mock, remount off, re-press stage 1 → the handle-cell
  overwrite flips the mock evidence stale) — the distinct-keys
  guarantee, proven end to end.
- The full-size mocked stage-4 rerun runs ~25s, so the e2e (and the
  live smoke) re-arm the conftest 30s deadlock alarm.

## 1. RunnerSupervisor generalization (`experiment_runner.py`)

`RunnerSupervisor` currently hardcodes `mount_experiment_runner` and
the `experiment-workflow` name. Parametrize it with:

- `mount` — an async-CM factory `(data_dir) -> AsyncIterator[ExperimentRunner]`;
- `external_name` — the per-revival registration name.

`mount_experiment_runner` stays as-is (the old driver's mount);
`ExperimentRunner` (the yielded handle) is already generic enough —
`fn_keys` / `fn_instances` / `releases` just describe whatever the
mount bound. Existing call sites pass the old defaults; behavior stays
byte-identical for the old arc (existing demo tests are the guard).

## 2. The behavior runner mount (`behavior/behavior_runner.py`)

`mount_behavior_runner(data_dir, *, llm_mode: "off" | "mock" | "real",
anthropic_api_key: str | None)` — same scope topology as
`mount_experiment_runner` (host scope enclosing session + runner
scope; the module docstring notes are load-bearing, so reference them
rather than restating):

- always binds the three task fns (adapter + `wrap_singleton`
  connector, content-addressed `service_key` ids);
- `real`: constructs the shared `AsyncAnthropic` client (owned by the
  mount, closed on unwind) and binds the real impls under the
  `:anthropic` keys; `mock`: binds the mock impls (below) under the
  `:mock` keys; `off`: binds no LLM services — handle cells written in
  an earlier mode stay populated but unbound, so demands fault with
  the boundness error, which the driver surfaces verbatim;
- the yielded runner handle exposes the **mode's key table** (task keys
  + the LLM keys the mode bound) — the driver writes handle cells from
  it, so cells always name what this mount actually bound;
- data dir: `<workdir>/data_behavior/` (sibling of the old runner's
  `data/`; both share the workdir's lifetime and `--auto-workdir`
  wipes both).

Mock impls in one module, `behavior/mock_llm_services.py` (three tiny
`TypedServiceImpl`s of the same protocols): subject returns canned,
clearly-marked sales-pitch lines (`"[MOCK baseline reply #2] …"`,
varying with the message count + system prompt so transcripts aren't
degenerate); sim returns canned persona questions the same way; judge
returns `score = (stable hash of rubric + transcript) % 101 / 100` and
a `"MOCK VERDICT: …"` reasoning — deterministic, so the analysis table
is reproducible and visibly varied across (subject, judge).

Handle-cell coordinates don't change with mode: the mock subject/judge
keys are written at the same `provider="anthropic"` coordinates the
configs name (the coordinate is just an address; the *key entity* is
what identifies — and invalidates — the bound service). Configs are
therefore mode-independent, and a mode switch + `stage_structure`
re-press overwrites exactly the handle cells (see `overview.md` on the
staleness fan-out this deliberately causes).

## 3. The driver

Marker half
(`agent_workdir/packages/behavior_demo_helper/`): type id +
`BehaviorDemoHelperProto` with the button methods (all zero-arg,
returning `FxtrServiceData`):

- `stage_structure`, `stage_inputs`, `stage_analysis`, `stage_augment`
- `target_analysis`, `run_analysis`, `clear_stale`
- `status` — a small convenience summary (per-grid non-fresh counts /
  analysis cell state) so the panel can show where things stand without
  the dashboard.

Impl (`behavior/behavior_demo_helper_service.py`), the
`experiment_demo_helper_service` pattern throughout: holds the
(behavior) supervisor, looks the runner up per call, exports returned
ids into the dispatch call scope, plain conflict-erroring writes
(identical rewrites are no-op successes; a different existing value
errors the button — surfaced in the panel, never silently resolved),
`overwrite` for the stage-4 domain edits, and
`run_analysis` demands in the background under a cancel scope (the
`scenario_slow` pattern) so the button returns immediately and the
dashboard shows cells filling in.

Method behavior follows `overview.md`'s stage map; the wiring of the
three task grids follows its "Task wiring" section verbatim. Notes:

- `stage_structure` opens everything (including the empty `analyses/`
  subtree) and writes handle keys; re-pressing is create-or-attach.
- `stage_analysis` opens `analyses/sales_report` but does NOT target or
  demand — targeting and running are the separate buttons (both also
  doable from the normal UI).
- `clear_stale` sweeps `evidence/rollouts`, `evidence/judgements`, and
  `analyses/sales_report`, clearing cells whose report is non-fresh
  (inspect over the cross product enumerated from the current domain
  cells + sweep config — the driver is a user-surface client, so
  presence queries and labeled inspection are fine here).
- config content (car-sales system prompts, personas, rubrics; small
  default model ids and `max_turns=2`) lives in one constants module,
  `behavior/scenario_content.py`, so prompts are editable in one place.

## 4. Orchestrator wiring (`run_experiment_demo.py`)

- New mutually exclusive flags `--allow-llm-apis` (workstation-demo
  naming; reads `ANTHROPIC_API_KEY` → mode `real`) and
  `--mocked-llm-apis` (mode `mock`); neither → mode `off`. The parked
  banner states the mode (and, for `off`, that behavior demands will
  fault as unbound).
- Second `RunnerSupervisor` (behavior mount, name `behavior-workflow`)
  + `mount_behavior_demo_helper` under external name
  `behavior-demo-helper`; both supervisors get the registrar from the
  same peer-RPC session and revive at boot; shutdown crashes both
  before the peer link closes (same ordering rationale as today).
- `sys.path` additions for the new marker packages + `behavior/`.

## 5. Tests

- **Headless structural test** (mode `off`, nothing demanded): boot
  the behavior runner mount + driver against a tmp dir; press
  `stage_structure` / `stage_inputs` / `stage_analysis` /
  `target_analysis` / `stage_augment`; assert the grid tree, config
  cell contents, target presence, and that `stage_augment` flips the
  analysis cell's currency to non-fresh (it read the domain cells).
  This exercises every rule wiring (opens canonicalize + persist) —
  the part most likely to break — without executing anything.
- **Mocked end-to-end test** (mode `mock`, fresh state, CI-able): run
  stages 1–3 + `target_analysis` + a *foreground* demand; assert an
  `AnalysisTable` with the full (subject × judge) rows comes back,
  scores in [0, 1], and the analysis cell is fresh. Then the stage-4
  arc: `stage_augment` → analysis non-fresh → `clear_stale` +
  re-demand → new table includes the added subject/sim/judge and the
  bumped replica count. This is the whole scenario, deterministic and
  keyless — the primary regression net for the demo.
- **Env-gated live smoke** (`ANTHROPIC_API_KEY` present, else skipped;
  not in CI): shrink to 1 subject × 1 sim × 1 judge × 1 replica ×
  1 turn via the constants module, run `target_analysis` +
  a foreground demand, assert an `AnalysisTable` with one row comes
  back and cells are fresh.
- Relaunch/resume of a mid-sweep kill is deliberately not auto-tested
  (covered by the existing demo's arcs; here it's a manual flow in the
  README).

## 6. Wrap-up

- README: new "Behavior-research scenario" section (stage walkthrough,
  `--allow-llm-apis`, cost/scale note: replicas × sims × subjects ×
  (2·max_turns + judges) LLM calls).
- Update `projects/fxtr_experiment/CODE_OVERVIEW.md`'s demo-harness
  line if needed.
