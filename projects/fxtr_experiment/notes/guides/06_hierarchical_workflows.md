# Hierarchical workflows: tasks that create tasks

Guide 4's experiment was static: the user created every grid, and task
fns only read and wrote cells. But task fns hold the *same view
calculus* the user does — a task granted the right capabilities can
**open grids of its own, including new task grids**, either in its
private internals or in external subtrees it was granted write
authority over. This guide explains the two shapes, why you'd want
them, and the rules that keep them sane.

## The affordances

### `ctx.own_internals()`: the task's private workspace

Every task cell has a private subtree (`taskgrid{coords}/$subtree/...`
in storage terms). `ctx.own_internals()` returns a write-authority
subtree view of it, and a task may open anything there: data grids,
dataframe grids, checkpoint grids (that's where guide 4's
`open_checkpoint` lives), and further task grids.

Internals are **state-isolated by construction**: they are addressed
under the owning cell's coordinates, so two cells' internals can never
collide, and nothing outside the task can wire reads into them.
From the top level, internals are **browsable but read-only** — the
dashboard (and `wf.internals(task_view, **coords)`) lets you look at
everything a task built inside itself, but never demand or edit it.

### Write-granted external subtrees

A task's rule can also grant it **write authority over a subtree**
(`args={"workspace": some_subtree}` wired from an *undropped*,
write-mode subtree view). Write authority on a subtree means
create-authority for grids inside it, transitively. Grids the task
opens there are ordinary, externally visible grids — other rules
(created by the same task) can read them, the user can browse them,
and with adoption (below) the user can even target inside them.

Because opens are create-or-attach and the outer level replays
deterministically, task-side creation is idempotent across
runs/resumes exactly like everything else: the safety pass compares
the set of grids a generation asserted, so "what this task builds" is
part of its verified, recorded behavior.

### Forwarding capabilities into child rules

When a task opens a child task grid, the child's inputs are wired from
**views the creator holds** — its own inputs (possibly sliced or
renamed), grids it created, and coord views. There is deliberately no
"reach up to the parent namespace" affordance: a child can only be
wired to what its creator was granted, which is what makes authority
auditable (capability passing all the way down). Holding a view to
grant it onward records no observation — the creator doesn't go stale
when a grid it merely forwarded changes ("holding ≠ observing").

## Why you'd want this: two worked shapes

### Shape A — private trial pipelines (the ABR improver)

`examples/fxtr_experiment_demo/behavior/improve_sim_task.py` is the
live example. The flow task receives a pinned snapshot request (an
entity carrying configs + source evidence by ref) plus handle views of
the LLM services *and of the rollout/judge task fns*. Inside
`ctx.own_internals()` it builds a miniature copy of the evidence
pipeline:

```python
internals = ctx.own_internals()
subject_cfg = await internals.open_data("subject_cfg", ())
await subject_cfg.write(req.subject_config.entity_id)   # materialize the
bios = await internals.open_data("bios", iteration_axes) # pinned configs
trial_rollouts = await internals.open_task(
    "rollouts", axes=iteration_axes, task_fn=rollout_fn,   # the SAME fn
    args={"subject": subject_cfg.read_only(),
          "sim": bios.read_only(), ...,
          "replica": int_coord("iteration")})
trial_judgements = await internals.open_task(
    "judgements", axes=iteration_axes, task_fn=judge_fn,
    args={"transcript": trial_rollouts.output_entity(), ...})
```

then loops: ask the improver LLM (a checkpointed step) for a candidate
persona → write it into `bios[iteration=i]` → read
`trial_judgements`' output at `iteration=i` (which demands the nested
rollout + judgement) → feed the evidence back into the next round.

Why this shape is good:

- **Reuse without leakage.** The nested trials run the *same task fns*
  as the top-level evidence pipeline — same code, wired in a new place
  — but their state is isolated from `evidence/`, so trial rollouts
  never pollute the real sweep.
- **Everything inside is durable and inspectable.** Each nested
  rollout/judgement is a full task cell with checkpoints; crash
  mid-improvement and re-demand — completed improver steps and
  completed nested generations replay. The dashboard shows the whole
  nested tree under the flow cell.
- **The result carries the trajectory.** The returned entity holds
  refs to every candidate, trial transcript, and judgement — so the
  private workspace is *evidence*, not hidden state.

Note the pinning pattern: the flow **materializes the request's config
refs into internal data grids** before wiring the nested rules onto
them, rather than wiring the nested tasks to the live `configs/` grids.
That's what makes an improvement run a fixed-point-in-time experiment —
later edits to the live configs don't stale (or change) the trials.

### Shape B — "macro expansion": one setup task builds the experiment

The second shape scales the idea up: instead of a driver script (or
button) creating your whole namespace, make **the setup itself a task**
— a task fn whose rule grants it write authority over a workspace
subtree, and whose body opens all the grids of your experiment there
and reads the final outputs. `notes/example_mh_pilot.md` develops this
in full; the skeleton:

```
# user-created roots:
subjects/usims/judges/fns   handle grids
dom_*                       domain cells
mh                          subtree ()            ← the workspace
mh_pilot                    task ()               ← the one targeted root
    rule: task_fn=fns[fn="mh_workflow"],
          workspace: write mh{}, plus read grants on everything above

# mh_pilot's body:
rollouts = await workspace.open_task("rollouts", axes=(subject, usim), ...)
grades   = await workspace.open_task("grades", axes=(subject, usim, judge),
                                     args={"transcript": rollouts.output(...), ...})
agg      = await workspace.open_task("agg", axes=(),
                                     args={"grades": grades.output(...), ...})
return await agg.output().read()        # ← demand flows through this read
```

You target and demand exactly one cell — `mh_pilot` — and the whole
tree expands, wires, and executes under it. The key feature: **the
expanded tree lives in the external `mh` subtree, not inside
`mh_pilot`'s internals**, so the rollout/grade/agg grids are ordinary
first-class grids — browsable at full fidelity, gatherable, adoptable —
while still having a single user-sourced root as the source of truth
for their configuration.

Why you might structure an experiment this way:

- **One committed root.** The setup task's rule + fn are the single
  authoritative description of your experiment's shape. Re-running it
  after an edit re-asserts the tree (config drift shows up as loud
  errors or as new grids, never silent mutation), and the exported
  workflow's intent state is one target + a handful of root grids
  instead of a pile of driver-script side effects. Think of the demo's
  "Stage 1: structure" button becoming a task inside the system — with
  its execution recorded, verified, and replayable like everything
  else.
- **Parameterizable expansion.** Wrap the setup task in a grid with
  axes (the `mh_variant[variant]` form in the MH example) and you get a
  whole experiment *per variant* — the vmapping rules canonicalize the
  per-variant trees into one shared config on the workspace subtree's
  keys, so variants stay structurally identical by construction.
- **Sandboxable setup.** Because the setup is just a task fn bound to
  a handle, it can be an agent-written, sandboxed piece of code — the
  step toward "ask an agent for an experiment, get a verifiable
  recipe".

Two semantics notes to keep the shape working:

- **Demand flows through output reads.** The setup task must *read*
  the outputs you want computed (like `mh_pilot` reading `agg`) — just
  creating grids expresses no demand. If a setup task only builds
  structure and returns, the created task cells sit pending until
  something reads them.
- **Targeting inside the expanded tree requires adoption.** The
  expanded grids are task-created, so they are grounded through the
  setup task's assertions — but user targets require user-sourced
  grids. To demand an individual inner cell from the top (say, one
  grade while debugging), adopt its grid first
  (`wf.demand(view, adopt=True, ...)` or `wf.add_target(view,
  adopt=True)`); or just browse it via `inspect`, which needs nothing.
  **NEEDS CONFIRMATION**: is adopt-then-target the recommended
  debugging gesture for macro-expanded trees, or is the intended UX
  "always demand through the root and inspect the insides"?

## Rules and gotchas

- **Tasks never mint authority.** No task-side handle-grid creation or
  handle writes — external services enter only through user-populated
  handle grids, however deep the hierarchy. A task that wants its
  children to call a service must itself be granted the handle view
  and forward it (see `improve_sim_task`'s `rollout_fn`/`judge_fn`
  args).
- **Rules must stay symbolic** (cell-independent): wiring may use input
  names, child axes, coord views, and literals — never values read
  from cells. Data-dependent *reads* are fine (read a config, loop over
  its contents, write results); data-dependent *wiring* is rejected.
  If you need per-cell structure, put the variation in entities (Shape
  A's request pattern) or allocate UUID-prefix cells as per the design
  overview's irregularity recipe.
- **Read-xor-write per grid per generation**: one generation may not
  both read and write the same grid. Plan the dataflow so builders
  write and consumers read (the E-creates-child-and-reads-its-output
  pattern is fine — E writes the *args* grid and reads the *child's
  output* grid, two different grids).
- **Creating is asserting.** A write-authority open claims "this grid
  exists with this config" on behalf of your generation; clearing the
  generation withdraws the claim (the grids and their contents remain,
  but become unanchored until a rerun re-asserts them — visible in
  lint's inventory). Deleting grids is a user affordance, not a task
  one. **NEEDS CONFIRMATION**: can tasks delete grids they created?
  I believe not — `delete_grid` is capability-level only.
- **Private-subtree read grants onward are restricted** (open design
  question — see the design overview's open questions): the tentative
  rule is that only tasks created *inside* your internals may be
  granted reads of your internals. Wire external children only to
  external (input-reachable) grids.
