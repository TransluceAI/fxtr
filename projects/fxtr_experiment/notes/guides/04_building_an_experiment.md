# Building your first experiment

This is the main tutorial: how to express a multi-step — but statically
shaped — experiment in fxtr_experiment. The running example is the
behavior-research scenario from `examples/fxtr_experiment_demo`
(`behavior/` + the `behavior-demo-helper` driver), simplified. Read it
with the demo source open; every snippet here has a fuller, working
counterpart there.

The experiment: AI **car-sales assistants** (subject models swept over
system prompts) talk to simulated customers (persona-driven user
sims); LLM **judges** score each transcript against rubrics; an
**analysis** task aggregates the mean score per (subject model, judge)
into a table. In this guide the user creates *all* the task grids up
front — no tasks creating tasks (that's guide 6).

## 0. The shape you are aiming for

A workflow is a tree of grids. The demo builds this namespace:

```
tasks/            handle grids: rollout, judge, analyze     (the task fns)
services/         handle grids: subject_model_services[provider],
                  judge_services[provider], user_simulator_service
configs/          data grids: subject_models[subj_id], user_sims[sim_id],
                  judges[judge_id], rollout_config
domain/           dataframe cells: active_subject_models, active_user_sims,
                  active_judges  (+ data cell sweep_config)
evidence/
  rollouts        task grid  [subj_id, sim_id, replica] → Transcript
  judgements      task grid  [subj_id, sim_id, replica, judge_id]
                             → verdict (entity) + data (frame)
analyses/
  sales_report    task grid  [] → report (entity) + evidence (frame)
```

Data flows purely by wiring: `judgements` reads `rollouts`' output;
`sales_report` gathers `judgements`' frames over the domain cells.
Demanding the one analysis cell pulls the whole pipeline.

## 1. Get a workflow

Assemble a runner as in guide 3 (or copy
`examples/fxtr_experiment_demo/experiment_runner.py`), ending with a
`Workflow` wrapper `wf`. Everything below goes through `wf` and the
views it hands out. `wf.root` is the root namespace: a write-mode,
zero-axis subtree view.

All the `open_*` calls below are **create-or-attach**: they create the
grid if absent, or attach to it if it already exists *with the same
config* (differing configs are an error, never silent). So your setup
code can be re-run idempotently — the demo's stage buttons rely on
this.

## 2. Open the data-shaped grids and write inputs

```python
from fxtr_experiment import model as m

SUBJ_AXES = (m.AxisSpec("subj_id", "str"),)

configs = await wf.root.open_subtree("configs", ())
subject_models = await configs.open_data("subject_models", SUBJ_AXES)
rollout_config = await configs.open_data("rollout_config", ())
```

Data cells hold entity ids. Writes go through the view (or the
`Workflow` methods for privileged variants):

```python
value_id = SubjectModelConfig(system_prompt=..., model=..., provider="anthropic") \
    .store(runner_scope).entity_id
await subject_models.write(value_id, subj_id="pushy_seller")
```

Semantics worth knowing at write time:

- Writing the identical value again is a **no-op success** (idempotent
  — buttons and setup scripts stay re-pressable).
- Writing a *different* value where one exists raises
  `WriteConflictError`; replacing an existing value is a deliberate
  gesture — `await wf.overwrite(view, value_id, **coords)` — and it
  marks every downstream result that observed the old value stale.
- User writes are automatically recorded as **user-sourced** (you are
  the provenance of this cell; guide 5's grounding section).

### Domain cells

Grids are **open maps**: nothing can ask a grid "what cells do you
have?" as an input to computation. The set of "active" coordinates is
itself data, stored in **domain cells** — by convention, zero-axis
dataframe cells with one column per semantic axis:

```python
domain = await wf.root.open_subtree("domain", ())
active_subjects = await domain.open_dataframe(
    "active_subject_models", (), (m.ColumnSpec("subj_id", "str"),))

from fxtr_experiment import tables
await active_subjects.write_table(
    tables.make_frame(active_subjects.schema, [("pushy_seller",), ("honest_seller",)]))
```

Expanding your sweep later = overwriting the domain frame (plus writing
the new configs). That's an ordinary data change, so exactly the
consumers that enumerated the domain go stale — this is how "add a
subject model, recompute only the new cells" works with no framework
magic. Name domain columns after the axes they enumerate so joins read
as identity maps.

### Handle grids

```python
services = await wf.root.open_subtree("services", ())
subject_svcs = await services.open_handle("subject_model_services",
                                          (m.AxisSpec("provider", "str"),))
await wf.write_handle(subject_svcs, runner.svc_keys["subject_model"],
                      provider="anthropic")
```

Handle cells hold **keys** — content-addressed entities naming service
slots (guide 3). The demo's mode switch (real vs mocked LLMs) is
nothing but writing different keys into these cells: results computed
against the mock keys can never be silently served as real ones,
because the key is an observed input like any other.

## 3. Open the task grids — wiring is the interesting part

```python
evidence = await wf.root.open_subtree("evidence", ())
rollouts = await evidence.open_task(
    "rollouts",
    axes=(m.AxisSpec("subj_id", "str"), m.AxisSpec("sim_id", "str"),
          m.AxisSpec("replica", "int")),
    task_fn=rollout_h,                      # the tasks/rollout handle view
    args={
        "subject":          subject_models.read_only(),
        "sim":              user_sims.read_only(),
        "rollout_cfg":      rollout_config.read_only(),
        "replica":          int_coord("replica"),
        "subject_services": subject_svcs.read_only(),
        "sim_service":      user_sim_svc.read_only(),
    },
)   # default output: one entity output named "result"
```

What the `args` mapping means — this is the **vmapping** rule, and it's
worth reading twice:

- Each arg is a *view you hold*, and its **axis names are matched
  against the new grid's axis names**. An arg axis whose name is one of
  the task grid's axes is **consumed**: each task cell gets that axis
  bound to its own coordinate. `subject_models` has axis `subj_id`; the
  grid has axis `subj_id`; so each `rollouts[subj_id=s, ...]` cell
  receives `subject` as a *fully-bound, single-cell* view of
  `subject_models[subj_id=s]`.
- An arg axis whose name is **not** among the grid's axes stays
  **kept**: the task fn receives a view with that axis still open, and
  the name it kept is the name task code addresses it by.
  `subject_svcs` keeps its `provider` axis — the task fn picks a
  provider at run time (`read_key(provider=...)`).
- Name collisions are resolved by renaming your own view first
  (`view.rename_axes(old="new")`); matching is by name, deliberately —
  so make the names line up before wiring.
- `int_coord("replica")` wires the cell's **own coordinate** in as an
  input — the sanctioned way for a task to learn "which replica am I?",
  since consumed axes are otherwise invisible. Reading a coord is free
  (a coordinate can never change, so there's nothing to observe).
- `read_only()` marks the input as a read grant. Inputs default to the
  view's own mode, so an undropped write-authority view would wire as a
  write grant — the demo drops everything it wires as plain input.
  **NEEDS CONFIRMATION**: should the guide advise `read_only()` on
  *every* non-workspace arg as a blanket rule?

The second task grid shows output selection and multiple outputs:

```python
judgements = await evidence.open_task(
    "judgements",
    axes=(*ROLLOUT_AXES, m.AxisSpec("judge_id", "str")),
    task_fn=judge_h,
    args={
        "transcript":     rollouts.output_entity(),   # the upstream output view
        "judge_cfg":      judges.read_only(),
        "judge_services": judge_svcs.read_only(),
    },
    outputs={
        "verdict": m.EntityOutput(),
        "data":    m.FrameOutput(schema=JUDGE_DATA_SCHEMA),
    },
)
```

`rollouts.output_entity()` is a data-shaped view of the rollout grid's
output — wiring it consumes `subj_id`/`sim_id`/`replica` (names match)
and each judgement cell reads exactly its rollout's transcript.
Reading a task output **demands** it: if that rollout hasn't run yet,
reading triggers it. This wiring is the entire scheduler.

Task grids declare **named outputs** (`EntityOutput` for entity ids,
`FrameOutput(schema=...)` for dataframes; omitting `outputs=` gives the
common single `EntityOutput` named `"result"`). Staleness is tracked
per-output.

One rule to remember about task grids: **the rule is the grid.**
Changing a task grid's wiring or axes means a new grid (delete and
recreate, or a new key) — v0 has no rule migration. Config drift on an
`open_task` of an existing grid is an error, not an upgrade.

## 4. Write the task fns

The rollout fn, abbreviated from `behavior/rollout_task.py`:

```python
@step_fn(type_id=define_type(BEHAVIOR_STEPS_NS, "subject_turn"))
async def subject_turn_step(ctx: StepContext, *, key: FxtrID,
                            system_prompt: str, model: str,
                            messages: list) -> str:
    svc = await ctx.connect(key)                       # key → live service
    subject = await svc.replace_service_type(SubjectModelProto).resolve()
    return await subject.reply(system_prompt, model, messages)

@task_fn
async def rollout_task(ctx: TaskContext, /, *, subject: DataView,
                       sim: DataView, rollout_cfg: DataView,
                       replica: CoordView, subject_services: HandleView,
                       sim_service: HandleView) -> FxtrID:
    # 1. Read inputs. Keep the refs — they feed the output's provenance.
    subj_ref = EntityRef(await subject.read(), SubjectModelConfig)
    subj_cfg = await subj_ref.load()
    run_cfg = await EntityRef(await rollout_cfg.read(), RolloutConfig).load()
    subject_key = await subject_services.read_key(provider=subj_cfg.provider)
    sim_key = await sim_service.read_key()

    # 2. A checkpoint grid for the LLM calls: one durable step per turn.
    turns = await ctx.own_internals().open_checkpoint(
        "turns", axes=(m.AxisSpec("i", "int"),))
    messages = []
    for turn in range(run_cfg.max_turns):
        await ctx.log(f"turn {turn + 1}/{run_cfg.max_turns}")
        user_text = await turns.run_step(sim_turn_step,
            args={"key": sim_key, ...}, coords={"i": 2 * turn})
        messages.append({"role": "user", "text": user_text})
        assistant_text = await turns.run_step(subject_turn_step,
            args={"key": subject_key, ...}, coords={"i": 2 * turn + 1})
        messages.append({"role": "assistant", "text": assistant_text})

    # 3. Return an entity that refers back to what it is "about".
    transcript = Transcript(messages=..., subject_config=subj_ref,
                            sim_config=sim_ref, rollout_config=run_ref)
    return transcript.store().entity_id
```

The division of labor:

- **The outer level** (the task fn body) does reads, control flow, and
  assembly. It must be deterministic given what it read — same inputs,
  same sequence of effectful ops. Plain `for` loops, dict/list
  assembly, `anyio` task groups: all fine, as long as nothing recorded
  depends on completion order.
- **Steps** hold the nondeterminism. Each `run_step(step_fn, args=...,
  coords=...)` call on a checkpoint grid is durably recorded: the first
  run executes the step and stores the result; any later run of the
  same generation (after a crash, an interruption, a retry) serves it
  from the checkpoint. Kill the demo mid-conversation and re-demand:
  completed turns replay instantly, the conversation resumes at the
  first missing turn. Give each invocation distinct `coords` — one
  checkpoint cell is one durable slot, and calling `run_step` twice on
  the same cell in one run is an error.
- **Step failure ≠ checkpointed failure.** A step that raises records
  no entry; a re-demand re-attempts it. So put call + parse + retry
  *inside* the step and raise on garbage (the improver step in
  `behavior/improve_sim_task.py` is the worked example) — never return
  a malformed placeholder, because a *returned* value is checkpointed
  and will replay forever. Conversely, an *evidential* failure (a
  refusal you want to study) is data: return it, and let a downstream
  task flag it.
- `await ctx.log(...)` emits run-log lines the dashboard streams live.
  Logs are runtime-only (never persisted).

## 5. The analysis: dataframes and gather

`behavior/analyze_task.py`, abbreviated:

```python
@task_fn
async def analyze_task(ctx, /, *, judgements: DataView,
                       judgement_data: DataFrameView,
                       active_subjects: DataFrameView, active_sims, active_judges,
                       sweep: DataView):
    subjects_df = await active_subjects.read_table()      # pl.DataFrame
    sims_df = await active_sims.read_table()
    judges_df = await active_judges.read_table()
    sweep_cfg = await EntityRef(await sweep.read(), SweepConfig).load()

    driving = (subjects_df.join(sims_df, how="cross")
        .join(pl.DataFrame({"replica": list(range(sweep_cfg.replicas))}), how="cross")
        .join(judges_df, how="cross"))

    evidence = await judgement_data.gather(
        driving, on={"subj_id": "subj_id", "sim_id": "sim_id",
                     "replica": "replica", "judge_id": "judge_id"})
    # evidence: driving columns + the judgement frame's columns, one
    # pl.DataFrame — from here it's plain polars/duckdb.
    ...
    return {"report": table.store().entity_id, "evidence": evidence}
```

- Dataframe cells and frame outputs hold **row multisets over declared
  scalar schemas** (`str`/`int`/`float`/`bool`/`bytes`; no entity
  columns — do relational work on scalars, then point-read entities by
  key). In memory they are ordinary `polars.DataFrame`s. Stored row
  order never carries meaning; add a rank column if order matters.
- **`gather` is the fan-out primitive**: for each driving row it binds
  the view's axes from the key columns and joins the addressed cell's
  rows on. It *demands* missing cells — concurrently — so this one call
  is what makes the analysis pull the whole evidence pipeline: gather →
  runs every needed judgement → each judgement reads (demands) its
  rollout. Missing/broken cells fault; empty cells contribute zero
  rows.
- The analysis also point-reads each verdict entity afterwards — purely
  to embed per-row provenance refs in the report table. Relational data
  answers "what are the numbers"; entity refs answer "show me the
  evidence behind this number".

## 6. Target it and run it

```python
analysis = await analyses.open_task(ANALYSIS_KEY, axes=(), task_fn=analyze_h,
                                    args={...}, outputs={...})
await wf.add_target(analysis)                       # durable intent
report_id = await analysis.output_entity("report").read()   # demand → run
# or equivalently: await wf.demand(analysis.output_entity("report"))
```

- A **target** is durable, declarative intent: "this cell is a wanted
  root of my experiment". Targets are exported with the workflow and
  are what a from-scratch replay demands. **User demands require a
  target on the demanded cell** — the `Workflow` wrapper's `demand`
  auto-adds it by default, so in practice you just demand; the point is
  that your experiment's roots end up explicitly declared rather than
  implicit in your shell history.
- A **demand** materializes a cell now. Demanding a task output runs it
  if needed, awaits it if running, serves it if complete-and-fresh —
  and recursively demands whatever *it* reads. Cells the demand
  cascades into need no targets of their own.
- Demands can take a while (the demo runs them in a background task and
  polls status); watch progress in the dashboard, or via
  `wf.inspect(view, **coords)` / `wf.summarize()`.

## 7. Change something and re-run: staleness in practice

The casual user's contract with the currency system:

- **Edit an input** (overwrite a config cell, widen a domain frame,
  swap a handle key) → every result that observed the old value becomes
  **stale**, transitively-dependent results become **unsettled**
  ("something under me changed; my own fate is unknown until it
  reruns"). Nothing recomputes by itself.
- **Demands refuse non-fresh inputs** (v0 strict policy): demanding
  something whose support is stale faults with a diagnosis naming the
  stale roots, rather than silently serving or silently recomputing.
- **You clear, then re-demand.** Clearing a task cell discards its
  result (v0 never discards state behind your back); the next demand
  runs a fresh generation. The demo's "Clear stale" button is just:
  inspect every cell, `wf.clear(view, **coords)` the non-fresh ones,
  re-demand the analysis. Only the affected cells rerun — after a
  domain widening, existing evidence stays fresh and only the new
  cells (plus the analysis) compute.
- **Reverting an edit resurrects freshness**: currency compares values,
  not timestamps, so putting the old value back makes downstream
  results fresh again with no recompute.
- To *look* at anything — including stale or errored cells — use
  `wf.inspect(...)` / the dashboard: inspection serves values labeled
  with their verdicts and never triggers execution. Demand is for
  materializing; inspect is for looking.

**GUIDE WRITER SIDENOTE**: deliberately not covered here — the
demand-requires-target rationale, grounding/adoption, and unsettledness
subtleties (guide 5); tasks creating grids (guide 6); export (guide 7).

## Checklist for a new experiment

1. Define entity types + handle-key types + step type ids in a small
   types package (guide 2).
2. Write task fns and step fns beside them (guide 3); steps own all
   nondeterminism; outputs carry refs back to their inputs.
3. Write a runner mount that opens the session and binds fns + external
   services under content-addressed keys (copy
   `behavior_runner.py`).
4. Write setup code (a script, or a driver service with buttons) that
   opens the namespace — subtrees, configs, domains, handles, task
   grids — through the `Workflow` wrapper, idempotently.
5. Write inputs; target the root analysis cell(s); demand.
6. Iterate: edit inputs → clear stale → re-demand; add renderers for
   your types as you start wanting to *see* results (guide 8).
