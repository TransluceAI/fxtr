# fxtr_experiment_demo

A **persistent experiment backend** an operator can build real state in
and watch from the browser: a `fxtr_experiment` workflow runner (the
durable grid/task system from `projects/fxtr_experiment/`) hosted in a
fxtr workstation, plus a scenario **driver** whose buttons build
workflow state through the same user capability any client speaks.

This demo is the live harness for the experiment **dashboard**
renderer, which joins the workdir's UI preset via the repo nix lib
(`lib.fxtr.experimentRenderers`) — every fxtr_experiment renderer
serves through the same dynamic dispatch as the rest of the preset.
(It originated as chunk 3 of the monitoring-UI phase,
`projects/fxtr_experiment/plans/phase4_monitoring_ui/`.)

## Prerequisites

Same as `examples/fxtr_workstation_demo`: `uv`, `pnpm` (with the repo
workspace installed: `pnpm install` at the repo root), and nix or
podman for the renderer-preset builds (see the workstation demo's
README for the nix runner setup).

## Launch

One command, self-contained (re-scaffolds a throwaway workdir — wipes
workflow state on every launch):

    uv run python run_experiment_demo.py --auto-workdir

Or, to keep workflow state across relaunches (the resume arc), scaffold
a runtime workdir once and point at it:

    uv run python scaffold_workdir.py ~/fxtr_experiment_workdir
    ./start_demo.sh ~/fxtr_experiment_workdir

Then open the Vite frontend at <http://localhost:3471>. First boot is
slow (the renderer dispatcher batch-builds every renderer ESM +
typeEntity layer through nix); cached thereafter.

## Driving it

In the shell's roots panel, open **`experiment-demo-helper`** — the
scenario-picker panel. Buttons, by arc:

- **Pipeline**: build + run a three-cell `data → task → downstream`
  pipeline; overwrite input *b* to fan staleness through it; revert to
  resurrect freshness. Results are `ExpNote` refs — click any to open
  it. The **echo** task reads only `shout`'s output, so an input-*b*
  edit leaves its *b*-cell *unsettled* (observations still match,
  support stale) rather than stale.
- **Slow checkpointed task + crash/revive**: start the multi-step task
  in the background, then press **Crash runner** mid-run — the runner
  (session + executor + capability) is torn down in-process via task
  cancellation; the running generation records a fault-class
  `RunnerCrash` failure and shows *interrupted*, while the driver
  panel survives (it holds the runner's supervisor, not the runner).
  **Revive runner** brings a fresh runner up over the same durable
  state (fns re-bound, `experiment-workflow` re-registered), and
  re-demanding resumes the same generation with completed steps served
  from durable checkpoints. The hard variant — `kill -9` the whole
  backend mid-run, then relaunch against the same workdir — exercises
  the startup running→interrupted flip instead. Known issue: after an
  in-process crash, final backend shutdown may hang on abandoned
  finalizer tasks (an fxtr2 teardown bug — see the followup in the
  chunk-3 plan file); `kill -9` it if so.
- **Flaky task**: the first demand errors (a planted failure); retry
  completes.
- **Handle boundness**: a task wired to an unbound handle faults;
  bind the fn, re-run to complete, release to flip back.
- **Subtrees + run-time task creation**: a `variants` subtree whose
  child grids span the subtree's own axis, and a `planned` task whose
  run creates grids — a `draft` data grid plus a nested `polish` task
  — inside its own internals.

The workflow capability itself is exposed as
**`experiment-workflow`**. To look at a workflow, open it from the
shell's **Roots** panel — every registered workflow capability (this
one and the behavior scenario's) renders through the dynamically
dispatched experiment dashboard (the repo nix lib's
`experimentRenderers` slice in the workdir preset).

**Renderer dev loop**: with the demo running against a durable
`--workdir`, edit any renderer (repo-lib or workdir-local) and run
`uv run python reload_renderers.py --workdir <same workdir>` — it
re-pins the workdir to your current working copy, re-pushes the
preset, and open panels reload in place (that script's module
docstring is the full contract).

## The behavior-research scenario

A second, separate workflow (own runner + sqlite pair at
`<workdir>/data_behavior/`, capability exposed as
**`behavior-workflow`**) with its own driver,
**`behavior-demo-helper`**: a mini behavior-research pipeline instead
of toy tasks — AI **car-sales assistants** (subject models swept by
system prompt) talk to simulated customers (persona user sims), LLM
judges score each transcript against rubrics (pushiness /
effectiveness / ethics), and an analysis task aggregates the mean
score per (subject model, judge) over sims × replicas into a result
table. Everything is built through the same user capability; demand
flows top-down (targeting + running the one analysis cell pulls every
needed judgement, which pulls every needed rollout), and every LLM
call is a checkpointed step (kill mid-sweep and resume).

LLM services bind per launch flag:

- `--allow-llm-apis` — real Anthropic services (needs
  `ANTHROPIC_API_KEY`).
- `--mocked-llm-apis` — deterministic fakes (obviously tagged output;
  judge scores are a stable hash of rubric + transcript). No key
  needed; the bring-up/test mode.
- neither — LLM handles stay unbound; runs fault with the boundness
  error until a binding-capable relaunch.

Real and mock services bind under **distinct content-addressed handle
keys**, so switching flags and re-pressing the structure button
overwrites the handle cells and fans staleness through everything
computed against the other mode — mock results are never silently
served as real ones.

Open **`behavior-demo-helper`** in the shell's roots panel (the
stage-picker renderer) and press, in order: **Stage 1: structure**
(open every grid, write the current mode's handle keys), **Stage 2:
inputs** (write the configs + domain cells), **Stage 3: analysis**
(open `analyses/sales_report`), then **Target analysis** / **Run
analysis** (background demand — watch cells fill in) / **Status**.
**Stage 4: augment** widens the domain (a new subject model, persona,
and judge; more replicas): the analysis goes stale while existing
evidence stays fresh, and **Clear stale** + **Run analysis** computes
exactly the new cells. Every value the workflow produces renders
through the `behavior_entities` slice — transcripts as chat bubbles,
configs as cards, judge verdicts as score badges, the analysis as a
table. Cost note (real mode): one full run makes
`subjects × sims × replicas × (2·max_turns + judges)` LLM calls —
the shipped stage-2 content is 2×2×1×(8+2) = 40 small-model calls.

Two more arcs in the panel:

- **Transform: improve a user sim** — the ABR-style content rewrite.
  Enter a sim/subject/judge id + an instruction: the driver snapshots
  the completed rollout+judgement pairs into a pinned
  `UserSimImprovementRequest` (configs travel by ref inside it —
  deliberately detached from the live `configs/` grids), writes it at
  `transformations/user_sim_improve[request_id]/request`, and demands
  the sibling `flow` task in the background. The flow asks the
  improver model for a persona rewrite, trials each candidate with
  **nested rollout+judge runs in its own internals** (the same task
  fns, wired in a new place, state-isolated from `evidence/`),
  iterates, then makes a final choice; the result carries the whole
  trajectory (candidates, notes, trial evidence) as clickable refs.
- **Crash runner / Revive runner** — the behavior workflow's
  in-process interruption arc: crash mid-run (background demands are
  cancelled first, so running generations interrupt cleanly), revive
  over the same durable state, re-run — completed improver steps and
  completed nested generations replay from durable state.

## Sandboxed task fns (optional)

`--enable-sandbox` mounts the sandbox spawner + internet authorizer +
the agent-MCP persistence namespace, exactly as in the workstation
demo — so an MCP-driven agent can spawn sandboxed task-fn services and
bind them to workflow handles (`cp agent_workdir/.fxtr_workstation
.toml.example <workdir>/.fxtr_workstation.toml` and point the plugin at
the workdir). The standard scenarios are all host-side and need no
sandbox.

## Layout

- `run_experiment_demo.py` — the orchestrator (workstation + mounts +
  Vite child). `scaffold_workdir.py` / `start_demo.sh` — workdir
  scaffolding + launch.
- `experiment_runner.py` — the persistent runner mount
  (`experiment-workflow`): session under `<workdir>/data/`, executor,
  capability, auto-bound standard task fns; plus `RunnerSupervisor`,
  which owns each runner as a crashable/revivable supervised task and
  registers the capability with the workstation per revival. The
  module docstring documents the (load-bearing) scope topology.
- `demo_task_fns.py` — the standard task fns (uppercase / combine /
  slow-checkpointed / flaky / the grid-creating plan / the
  deliberately unbound reverse).
- `experiment_demo_helper_service.py` — the driver impl
  (`experiment-demo-helper`).
- `behavior/` — the behavior-research scenario's host-side code (see
  the scenario section below): the Anthropic-backed subject-model /
  user-simulator / judge services (one file each, plus the shared
  `anthropic_client.py` seam and their `mock_llm_services.py` fakes),
  the `rollout` / `judge` / `analyze` task fns with checkpointed
  LLM-call steps, the scenario's prompt content
  (`scenario_content.py`), its runner mount (`behavior_runner.py`),
  and its driver (`behavior_demo_helper_service.py`). Vocabulary
  (entity types, service protocols, handle keys) lives in
  `agent_workdir/packages/behavior_research_types/`; the driver's
  marker protocol in `agent_workdir/packages/behavior_demo_helper/`.
- `agent_workdir/` — the runtime-workdir template (UI preset, marker
  packages, renderers); see its README.
- `frontend/` — the Vite shell app (pnpm member
  `@fxtr-experiment-demo/frontend`, port 3471).
- `tests/` — headless smoke for the host core (scenario arcs +
  relaunch persistence).
