# The concept model in more depth

Guide 4 showed the workflow system from the outside. This guide
explains the model underneath — enough to predict what the system will
do in the situations the tutorial glossed over. The canonical
expositions are `fxtr_experiment/view_spec.py`'s module docstring (the
static concepts), `notes/execution_semantics.md` (the dynamic model),
and `notes/grounding.md` (intent); this guide is the compressed,
user-facing tour.

## Grids, axes, and subtrees

A workflow is a tree of **grids**. Each grid has a **kind** (data,
dataframe, handle, task, checkpoint, subtree) and **axes** — named,
typed (`str`/`int`) dimensions addressing its cells. A **subtree** grid
is a grid whose cells contain further grids: a grid created inside
subtree `variants_ws[variant]` under key `rollouts` with own axes
`(subject,)` really has the flattened axes `(variant, subject)` — the
enclosing subtrees' axes (its **prefix**) followed by its own.

Two things about axes that surprise newcomers:

- **Names are local; storage is positional.** The stored addressing is
  by ordered positions; the names you see belong to your *view* and can
  be renamed freely (`rename_axes`) without changing anything stored.
  Two views of the same grid may call the same axis different things.
  The only names that are identity-bearing are a grid's *own* declared
  axis names and the *received names* of a task's inputs — i.e. exactly
  what task code can observe.
- **Regularity: one config per subtree key.** Every cell of a subtree
  holds structurally identical children — `variants_ws[v1]/rollouts`
  and `variants_ws[v2]/rollouts` are the same grid config by
  construction. If you need genuinely irregular shapes, emulate them
  with a data grid of allocated UUID prefixes (see the design overview);
  in practice, per-cell *variation belongs in the data*, not the shape
  (the demo's improvement flow puts per-request variation inside the
  request entity, keeping every `flow` cell identically configured).

### The open-map principle and domain cells

The set of populated cells of a grid is deliberately **not an
observable fact**: no operation asks "what cells exist", and *adding* a
fact never changes any old result — only modifying or removing facts
can. Enumeration is always driven by **domain cells**: ordinary
dataframe cells whose contents list the active ids. Reading "all of
axis X" means joining against the domain cell, and widening the domain
is an ordinary write with ordinary staleness. (Top-level presence
queries — `list_cells` / `list_keys` — are the one exception: they are
privileged, record nothing, and nothing downstream can depend on them.
They're for humans and dashboards, not for wiring.)

## Vmapping: how one rule becomes many cells

A task grid stores one **rule**: which task fn (via a handle slice) and
which input views, written positionally against the grid's flattened
axes. Each input binding is, per target axis, one of:

- **literal** — fixed by code (`view.at(fn="rollout")`); same for every
  cell.
- **param** — bound to one of the task grid's flattened axes; each cell
  gets its own slice ("the config for *my* subj_id"). This is what
  wiring-by-name-matching produces (guide 4): consumed axes become
  params.
- **kept** — left open; the task fn receives the axis, under the name
  it was wired with, and can read any coordinate along it (or forward
  it onward).

Think of the whole grid as a `vmap` of the task fn over its axes: each
cell runs the same code, with param-bound inputs sliced to its
coordinates. Two consequences of that framing are enforced, not just
encouraged:

- **Cell-independence (batch invariance).** Rules are canonicalized
  symbolically from the *creator's* wiring, never from per-cell values
  — so wiring that would have to differ per cell is rejected at create
  time rather than producing subtly different configs. Corollary for
  task authors: rules mention input names, child axes, and literals
  only; anything per-cell-varying belongs in data.
- **Consumed axes are invisible** — a task fn cannot see its own
  coordinates except through an explicitly wired coord view
  (`int_coord`/`str_coord`), which is why that mechanism exists.

The full machinery (holders, frames, the strip-set, how a creator's
wiring translates into the child's stored rule) is `view_spec.py`'s
module docstring — read it when you start doing hierarchical wiring
(guide 6); for flat experiments the guide-4 rules of thumb suffice.

## Execution: cells, generations, runs

A task cell's execution has three layers:

- The **cell** is the durable unit of result: named outputs, an
  execution status (`pending | running | complete | errored |
  interrupted`), and derived currency.
- A **generation** is one logical execution identity of the cell. All
  durable execution state — the op record, checkpoints, observations,
  results, failures — belongs to a generation. Clearing a cell ends its
  generation; the next demand mints a fresh one.
- A **run** is one in-memory attempt to carry a generation forward.
  Runs are ephemeral and interchangeable: nothing in-memory is
  load-bearing across runs, so a run can crash or be evicted at any
  point and a later run continues.

The mechanism that makes this work is the **universal record-check**:
every effectful op a task performs, on every run, first consults the
generation's record. A **hit** serves the recorded outcome without
touching live state; a **miss** executes live and records. So:

- **Resume is free.** Re-running a crashed generation replays its past
  through hits (including checkpointed steps) and goes live exactly at
  the frontier. There is no separate replay mode.
- **Per-subject coherence.** Within one generation, two reads of the
  same cell always agree — the first read pins the value. If an input
  changes mid-run, the run completes coherently against its snapshot
  and is simply stale afterwards.
- **Determinism is checked.** Writing the same cell twice with
  different values, or replaying differently than recorded, raises
  `NondeterminismError`. And when a generation completes, the runner
  re-runs the outer level once against the frozen record (the **safety
  pass**) and requires every op to hit and every recorded fact to be
  re-produced — so "the outer level is deterministic given its inputs"
  is verified per completion, not assumed. This is what lets everyone
  trust replays. (The cost: your outer level really must be
  deterministic — no completion-order-dependent output assembly, no
  wall-clock, no RNG outside steps.)

### Errored vs interrupted, and poisoning

Failed generations come in two flavors, split by whose fault it is:

- **errored** — the task itself went wrong: its logic raised, a step
  raised, or it violated integrity (write conflict, nondeterminism...).
- **interrupted** — the task wanted something that isn't ready (a
  missing/stale/unsettled/errored input, an unbound handle) or its run
  was cut off (crash, shutdown). Morally "pending, with progress made
  and a recorded reason". Binding the handle / fixing the input and
  re-demanding resumes it.

Faults propagate as interruptions: one broken input surfaces at the top
as one root cause plus a tree of interrupted dependents — not a cascade
of errors. And errors **poison**: a workflow error raised by an op is
persisted at raise time and fails the generation *even if task code
catches it*. You may catch exceptions for cleanup/logging, but you
cannot compute around a failed op — a task that "handled" a missing
input and returned a fallback would otherwise hold a result the system
could never invalidate. (Full contract: `errors.py`'s docstring.)

Retry behavior: demanding an errored-but-fresh cell retries once by
default (a fresh run of the same generation — completed steps replay);
demanding an interrupted-fresh cell resumes it; stale or unsettled
cells fault the demand until you clear them.

## Currency: fresh, stale, unsettled

Every effectful read doubles as an **observation**: "this generation
saw value fingerprint V at subject S". Currency is derived by comparing
recorded observations against the current world:

- **stale** — some observed subject now definitely holds a *different*
  settled value (or was deleted). Definite, local, never transitive.
- **fresh** — every observed subject holds exactly the recorded value,
  and every observed task output is itself fresh, with the whole
  justification bottoming out in data cells and settled results
  (cycles can never prove freshness).
- **unsettled** — neither: nothing observed has definitely changed, but
  the support contains something stale / running / cleared / errored.
  Unsettled resolves to fresh or stale when the support settles — and
  resurrection is the ordinary path: if a stale upstream reruns and
  reproduces the same value, downstream resolves fresh with no
  recompute.

Points that matter in practice:

- **Per-output granularity**: observations of task cells name the
  output, so a rerun that changes output A but reproduces output B
  stales only A's readers.
- **Currency and status are orthogonal**: a running generation can
  already be stale (v0 lets it finish); a stale cell still *has* its
  old result (inspectable, labeled) — stale means "not current", not
  "gone".
- **Value-pinned serving**: a demand validates and serves in one atomic
  operation — what you observed is exactly what was checked, so a
  concurrent clear/rerun can never slip a different value between check
  and read.
- Verdicts are cached (stamped) and invalidated transactionally; you
  never need to "refresh" anything — `inspect`/`list_cells`/the
  dashboard always show current verdicts.

### Tracing observations

The observation records are also your provenance browser:
`wf.inspect_generation(view, **coords)` reports a generation's
observations (what it read, with fingerprints), failures, checkpoints,
and request edges; `wf.observers(...)` is the reverse direction — who
observed this cell (what a change here would stale). The dashboard's
plate/graph view (`grid_graph` / `watch_graph`) renders the same
information at grid granularity. This is the "trace any result back
through the process that produced it" promise made concrete: in-data
refs (guide 2) for semantic provenance, observation records for
operational provenance.

## Tasks vs steps: the execution-semantics summary

| | task outer level | step fn |
| --- | --- | --- |
| arguments | views (capabilities) | plain data + handle keys |
| may perform | reads/writes/opens/gather/run_step | pure compute + `connect(key)` |
| determinism | required given the record; verified by the safety pass | none required — that's the point |
| durability unit | the generation (op record) | the checkpoint entry |
| failure | poisons the generation (errored/interrupted) | fails the run, no entry written; re-demand re-attempts |
| replay | ops re-served as hits | completed steps served from checkpoints |

## Grounding, briefly

Currency judges *values* ("is this result still right?"). The system
derives one more axis, **grounding**, which judges *intent and
structure*: does every fact — every grid, every cell value, every
computed generation — trace back to something you durably declared
(your sourcing records and targets), such that a from-scratch replay
would re-create it?

As a casual user you mostly meet grounding as **a lint affordance**:

- `wf.lint()` returns a structural report — dangling intent (sourcing
  records or targets whose pinned fingerprint no longer matches), red
  targets, and an inventory of ungrounded facts. Two clean levels:
  *sound* (no dangling intent, no red targets) and *clean* (also
  nothing ungrounded — the state a from-scratch replay fully
  re-creates). Lint is the pre-export / what-did-I-just-import check;
  green lint plus everything-fresh is the replayability contract.
- The rules you'll actually bump into: **user demands require a
  target** (auto-added by the wrapper); **targets require the grid to
  be user-sourced** (your creates auto-source; task-created grids need
  explicit **adoption** — `add_target(..., adopt=True)` /
  `demand(..., adopt=True)` — before you can target inside them);
  deleting a grid that other rules reference is surfaced immediately
  as broken grounding on the dependents.
- Grounding never affects computed *values* — it is fingerprint-blind
  and orthogonal to currency by design. (There is a strict read gate
  that can fault reads of ungrounded side-written data;
  **NEEDS CONFIRMATION**: the design docs say the v0 default gate
  policy is *strict* — should this guide warn users they can hit
  `UngroundedSubjectError` during normal editing churn, and describe
  the off/lint-only knob, or is strict rarely visible in practice?)

For the full model (sourcing records, wanted/would_start/grounded, the
replay contract), read `notes/grounding.md` — but you can build and run
experiments happily on just the bullets above.
