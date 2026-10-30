# Worked example: MH-pilot-shaped workflow

A concrete walkthrough used to stress the rule DSL, rule canonicalization
(root-relative vs capability-relative paths), subtree regularity, and the
capability-passing model. Working notes, co-designed; proposals here are
candidate decisions until promoted into `discussion_log.md`.

Notation: `grid[axis=X, ...]` addresses a cell; `X` a literal, `*` a kept axis,
`$a` a template binding to axis `a` of the grid whose rule we're writing (its
*flattened* axes — see proposal P1). Paths are `/`-separated subtree steps
written `subtree{cell coords}/key`.

## 1. Basic (non-nested) version

### Top-level namespace (user-created)

| name | kind | axes | contents |
|---|---|---|---|
| `subjects` | handle | (subject: str) | subject model handles |
| `usims` | handle | (usim: str) | user simulator handles |
| `judges` | handle | (judge: str) | judge handles |
| `fns` | handle | (fn: str) | task fns: `rollout`, `grade`, `aggregate`, `mh_workflow` |
| `dom_subjects` | dataframe | () | schema `(subject: str)`: active subjects (domain cell) |
| `dom_usims` | dataframe | () | schema `(usim: str)`: active usims |
| `dom_judges` | dataframe | () | schema `(judge: str)`: active judges |
| `mh` | subtree | () | workspace for rollouts/grades/aggregates |
| `mh_pilot` | task | () | the scheduler task (rule below) |

### The scheduler's rule (stored form)

```
mh_pilot rule:
  task_fn: fns[fn="mh_workflow"]
  inputs:
    fns:       read  fns[fn=*]
    subjects:  read  subjects[subject=*]
    usims:     read  usims[usim=*]
    judges:    read  judges[judge=*]
    dom_subjects: read dom_subjects[]        # never read by the scheduler —
    dom_usims:    read dom_usims[]           # held purely to grant onward (P5)
    dom_judges:   read dom_judges[]
    workspace: write mh{}                    # subtree slice; write = may create
```

### The scheduler task fn (pseudo-code, all outer-level)

Written in the **view calculus** (`views.py`): args arrive as named-axis view
capabilities; wiring a new task grid is name-matching — each arg view's axes
whose names are in the strip-set (the subtree view's axes + the new
coordinate names) get vmap-consumed, and every remaining axis name becomes
the official name the child task fn receives.

```python
async def mh_workflow(ctx, *, workspace, fns, subjects, usims, judges,
                      dom_subjects, dom_usims, dom_judges):
    # args arrive as keyword views; workspace is a zero-axis SubtreeView
    # (fully consumed upstream), fns a HandleView with axis (fn), etc.

    rollouts = await workspace.open_task(
        "rollouts",
        axes=(subject: str, usim: str),
        task_fn=fns.at(fn="rollout"),
        args={
            "subject": subjects,              # axis "subject" consumed → scalar per cell
            "usim": usims,                    # axis "usim" consumed → scalar per cell
        },
        outputs={"transcript": EntityOutput()})   # → TaskView (subject, usim)

    grades = await workspace.open_task(
        "grades",
        axes=(subject: str, usim: str, judge: str),
        task_fn=fns.at(fn="grade"),
        args={
            "transcript": rollouts.output("transcript"),  # both axes consumed → scalar per cell
            "judge": judges,                  # axis "judge" consumed → scalar per cell
        },
        outputs={
            "scores": FrameOutput(schema=(score: float, provider: str)),
            "warnings": FrameOutput(schema=(message: str,)),   # advisory, viz-surfaced
        })                                    # → TaskView (subject, usim, judge)

    agg = await workspace.open_task(
        "agg",
        axes=(),
        task_fn=fns.at(fn="aggregate"),
        args={
            "grades": grades.output("scores"),  # nothing consumed → received as (subject, usim, judge)
            "dom_subjects": dom_subjects,
            "dom_usims": dom_usims,
            "dom_judges": dom_judges,
        },
        outputs={"summary": EntityOutput()})  # → TaskView ()

    # bare return: mh_pilot declares a single output, so no mapping needed;
    # .output() with no arg selects agg's sole output ("summary").
    return await agg.output().read()
```

### The aggregate task fn (shows tables/gather)

```python
async def aggregate(ctx, *, grades, dom_subjects, dom_usims, dom_judges):
    # Domain cells are zero-axis dataframe cells — enumeration is native.
    doms = await dom_subjects.read_table()              # pl.DataFrame (subject)
    doms = cross(doms, await dom_usims.read_table())    # local pure computation
    doms = cross(doms, await dom_judges.read_table())   # frame (subject, usim, judge)

    # gather = the fault-on-missing join: demands exactly the active
    # grades cells (forcing execution, concurrently), joins each cell's
    # score rows on (an empty cell contributes zero output rows).
    scored = await grades.gather(doms, on={"subject": "subject",
                                           "usim": "usim",
                                           "judge": "judge"})
    # scored: frame (subject, usim, judge, score, provider) — materialized,
    # so from here it's plain pure computation: loop, or polars/duckdb it.
    return store_entity(summarize_by(scored, ["subject", "judge"]))
    # (bare return OK: this grid declares exactly one output; it wraps as
    # {"summary": ...}. grade, with two outputs, must return the mapping.)
```

Notes on what this exercises:

- **Wiring is written against the creator's capabilities.** Every reference
  is a view the task holds — received as an arg, opened, or a rewrite
  (`rename_axes` / `at`) of one. A task cannot name anything it wasn't
  granted — capability passing; there is no "walk up to a parent" affordance
  at all. Views' internal specs are symbolic, so identical code in every cell
  produces identical canonical configs (regularity by construction).
- **Pull-based execution.** The scheduler reads only `agg`'s output.
  `aggregate` reads the three domain dataframes, crosses them locally, and
  its `gather` demands exactly the active `grades` cells; each grade reads
  its `rollouts` output. Nobody enumerates a grid — domains are always
  explicit data (zero-axis dataframe cells).
- **Indexing/validation without a special mechanism.** `grades` emits a
  `scores` dataframe output (the "indexer" role) and an advisory `warnings`
  table (surfaced by viz, non-blocking); a *checker* would be one more task
  grid reading `grades` and erroring on invalid cells, with downstream args
  pointed at it instead.
- **Staleness flows without the scheduler reading domains.** Domain change →
  `agg` directly stale (it read the domain cells) → `mh_pilot` stale (it
  observed the `agg` task). Judge-handle swap → only that judge's `grades`
  cells stale (they read its key) → `agg` → `mh_pilot`. The scheduler
  itself observes almost nothing: the grid configs it created and the `agg`
  result.
- **Holding ≠ observing (P5).** The scheduler holds `subjects`/`dom_*` etc.
  only to grant them onward into created rules; granting records no
  observation, so e.g. editing `dom_judges` does not make the scheduler
  *directly* stale.

### The grade task fn (shows steps/checkpoints)

```python
async def grade(ctx, *, transcript, judge):
    result = await transcript.read()                    # forces the rollout cell (demand)
    judge_key = await judge.read_key()                  # observes handle identity
    calls = await ctx.own_internals().open_checkpoint("judge_calls", axes=(provider: str,))
    rows, warns = [], []
    for provider in ("p1", "p2", "p3"):                 # deterministic loop, outer level
        r = await calls.run_step(
            JUDGE_CALL_STEP,
            args={"transcript": result, "judge": judge_key},  # plain data only
            coords={"provider": provider})
        # the step: async def run(sc, /, *, transcript, judge):
        #     svc = await sc.connect(judge)             # judge is a handle key
        rows.append((r["score"], provider))
        if r["hedged"]:
            warns.append((f"{provider}: hedged verdict",))
    return {"scores": make_frame(SCORES_SCHEMA, rows),      # pl.DataFrame;
            "warnings": make_frame(WARNINGS_SCHEMA, warns)} # or plain polars
    # NB error contract: if e.g. transcript.read() faults, this task is
    # errored the moment the error is raised — catching it cannot undo that.
```

## 2. Nested version: variants

User adds at the root:

| name | kind | axes | contents |
|---|---|---|---|
| `variant_cfg` | data | (variant: str) | per-variant config entities |
| `variants_ws` | subtree | (variant: str) | one workspace cell per variant |
| `mh_variant` | task | (variant: str) | same `mh_workflow` fn, per variant |

```
mh_variant rule:
  task_fn: fns[fn="mh_workflow"]
  inputs:
    ... fns/subjects/usims/judges/dom_* exactly as before ...
    cfg:       read  variant_cfg[variant=$variant]
    workspace: write variants_ws{variant=$variant}
```

The task fn is **unchanged and cannot tell the difference**: its `workspace`
input is a fully-bound (single-cell) subtree slice either way, addressed as
`cell={}`; its `cfg` input is a single cell.

### The canonicalization question (this is the crux)

When `mh_variant[variant="v1"]` runs `ws.open_task(cell={}, key="rollouts", ...)`,
what does the *stored* config for `rollouts` look like? Naive absolutization
would literalize the creator's coordinates:

```
variants_ws{variant="v1"}/rollouts   inputs: ..., cfg-derived refs → variant_cfg[variant="v1"]
```

— but then `v1`'s and `v2`'s creates of the same key have *different* configs,
violating regularity ("every key has the same config across all cells of the
subtree"), even though the creators issued identical drafts. Hence:

**P1 — configs attach to subtree keys and are expressed over flattened axes.**
A child grid's canonical axes are the enclosing subtree's axes *prepended* to
its own (`rollouts` is really `(variant; subject, usim)`). The stored config
for a key is a single, cell-independent value (this is just the regularity
invariant taken seriously: config is per-key subtree state, which the model
draft already says). Coordinate templates in a stored rule may bind any
flattened axis — including prefix axes:

```
variants_ws/rollouts  (prefix axes: variant; own axes: subject, usim)
  task_fn: fns[fn="rollout"]
  inputs:
    subject: read subjects[subject=$subject]
    usim:    read usims[usim=$usim]
    # if the draft had granted cfg onward:
    cfg:     read variant_cfg[variant=$variant]      # bound to the PREFIX axis
```

**P2 — symbolic canonicalization at creation time.** (Construction surface is
now the view calculus — "a draft" below means a view's internal symbolic
spec.) A draft slice's root is a
creator capability; the draft itself mentions only input names, child axes,
and literals — task code never references its own grid's axes or coordinates
(vmap-style: the creator's rule is the wrapper, and only the runtime holds
it). Canonicalization is a metadata-level composition of two *symbolic*
artifacts: the creator's rule binding for the root capability, then the
draft's further slicing. Creator-grid axes appearing in that rule binding are
normalized into the child's stacked prefix axes wherever the creation path
correlates them (the creator's workspace binding says which creator axis
selects the subtree cell), and literalized otherwise. Deliberately *not* the
alternative of abstracting the per-cell instantiated capability by
value-matching literals against the creation cell's coordinates — that gets
the same answer in the happy case but establishes correlation by value
equality, which is coincidence-prone; going through the creator's rule makes
the correlation structural, and makes identical drafts from different cells
canonicalize identically trivially (per-cell values never enter at all):

- The creator's `workspace` binding is `variants_ws{variant=$creator.variant}`,
  so creator-axis `variant` ↔ child prefix axis `variant`. The creator's `cfg`
  binding `variant_cfg[variant=$creator.variant]` therefore canonicalizes to
  `variant_cfg[variant=$variant]` — identical for every creator cell. ✓
- A creator axis with no correlate in the creation path (say the creator also
  had a `seed` axis) cannot be re-expressed — and is **rejected** at create
  time (decided later the same day, superseding the original
  literalize-and-let-regularity-object behavior): since every cell runs the
  same code, per-cell-literalized wiring is always a guaranteed regularity
  conflict — it implicitly collapses a grid of distinct configs into a
  smaller-dimensional key (batch-invariance principle, with the created grid
  as the out-parameter). Canonicalization takes no per-cell input at all.

**P3 — corollary: drafts must be symbolic.** Since every cell of a task grid
runs the same code and each create must canonicalize to the same per-key
config, task code should express rules purely in terms of input names, child
axes, and literals — never values computed per cell. Anything per-cell-varying
in a rule is structurally a regularity conflict. (Good error messages needed.)

**P4 — the task's private subtree is a reserved key on the task grid.** To give
private subtrees paths, treat a task grid as also being a subtree grid with the
reserved key `$subtree` (checkpoint grids and internal state live under
`taskgrid{coords}/$subtree/...`). Regularity and flattened-axis rules then
apply uniformly to task-created internals.

**P5 — inputs are grantable capabilities; holding is not observing.** A task
may reference any of its inputs (further sliced) in rules of grids it creates.
Only reads/demands/connects/writes record observations.

### Variant: the outer task generates its own user sims

Instead of a root `usims` handle grid, `mh_workflow` could run a generator.
A generated usim's natural shape is a **config entity** (plain data), not a
handle — tasks never mint authority (no handle-grid creation or cell writes
from tasks, only read-attaches; connect authority comes only from
handle-grid reads). The root instead carries one shared simulator handle, say
`usim_svc` (handle grid, axes `()`), which the user created and bound:

```python
gen = await ws.open_data("gen_usim_cfgs", axes=(usim: str,), mode="write")
names = await checkpointed generator step(s)           # nondeterministic, durable
for name, cfg_entity in names:                         # replayed deterministically
    await gen.write(cfg_entity, usim=name)             # configs are plain data
dom = await ws.open_data("gen_usim_names", axes=(), mode="write")   # the domain cell
await dom.write(store_entity(sorted(names)))
# rollouts is opened with args={"usim_cfg": gen.read_only(), "usim_svc": usim_svc, ...};
# a rollout step connects to the shared usim_svc handle and passes its cfg
# entity to configure the simulator for the call (services can return
# services, so the factory can hand back a configured session for the step).
```

This exercises: a task writing a created data grid (write-on-create
capability), the written domain cell replacing the root `dom_usims`, and
created-grid-to-created-grid granting. All rule references still go through
the workspace capability, so P2 applies unchanged. Note the generated names
feed the *domain cell contents* (data), while the grid writes are keyed by
those names — the rules themselves stay symbolic (P3 respected). Staleness
tracks both halves of a generated simulator: its config by data read, the
shared service by handle-key read. Generation produces data that
*parameterizes* user-granted authority — never authority itself.

## 3. New questions this surfaced (resolutions 2026-07-02 in the log)

1. Is `write` on a subtree slice the right way to say "may create grids here"?
   → Resolved: write = create, and implies write/create on everything
   transitively inside.
2. Should each executing cell observe its own task_fn handle cell value?
   → Resolved: yes, always read at execution time (source of `executed_fn`).
   Task-fn handles threaded as plain *args* are observed by whichever cell
   ultimately resolves/uses them.
3. Is correlation through *data* ever needed for creation? → Resolved: no,
   banned for creation (P3); data-dependent *reads* are fine and just create
   richer observation dependencies.
4. Is read-granting out of a private internals subtree allowed? → Open
   (tracked in the log): tentative compromise is internals-resident grantees
   only; watch for consequences rather than settling now.
