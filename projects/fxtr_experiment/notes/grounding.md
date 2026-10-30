# Grounding — the intent axis (design note)

Standing, self-contained description of the intended **grounding** model:
how durable user intent is declared (sourcing records and targets), how it
propagates through recorded execution to justify every fact in a workflow
(grids, cell values, task generations), the derived verdicts that fall out
(**wanted** / **would_start** / **grounded**, plus local *orphanhood*), and
how those verdicts feed execution gating, the linter/UI, and state export.
This is the *intent-axis* counterpart of the value-axis currency model in
`notes/execution_semantics.md`; terms used without definition here (grids,
cells, generations, the op record, observations, demand policy, staleness,
the safety pass) are defined there and in `view_spec.py`'s module
docstring. The rationale trail is in `notes/discussion_log.md`.

**Implementation status.** The base facts are implemented (phase-2
chunk 1, `plans/phase2_grounding/`): user sourcing records with the
maintained grid-before-cell invariant (auto-upserted by user
creates/writes; session-level add/get/list/revoke with a cascade
variant), grid assertions recorded on write-authority opens (monotone per
generation, safety-pass-compared), the reverse rule-reference index, and
denormalized target locations — see `storage.py` / `state_store.py`;
write claims are the ``wrote_cell`` observations themselves. The derived
predicates are implemented (chunk 2): `grounding.py` carries the LFP
evaluator, the tri-state stamps + the location-keyed `wanted` memo, the
mint-compat check, and the marking propagation — its module docstring is
the standing correctness argument (state ↔ datalog invariants,
per-mutation obligations), and `WorkflowSession.grid_grounding` /
`cell_grounding` are the query surface. The user surface is implemented
(chunk 4): sourcing CRUD on the wire (`source_grid` / `source_cell` /
`revoke_*_sourcing` with the ``force`` cascade / `list_sourcing`),
adoption with the internals refusal, the demand-requires-target rule
(`NotTargetedError`) with the wrapper's auto-target default, ``adopt=``
on writes (and idempotent writes upserting sourcing), grounding
verdicts in every introspection result (from one snapshot —
`WorkflowSession.cell_snapshot`), and `delete_grid`'s consequence
report. The gates are implemented (chunk 3): the strict/off knob on
`Executor` / `open_local` (strict default), serve-or-fail in single
session ops (`serve_check` for task-output serves — both axes, one lock
hold; `gated_read_cell` for data/handle reads — the engine's, run
prologue included, and the user surface's), self-vouching via the
durable request record (``request_edges``, written at request
commitment — see the fact discussion below), `UngroundedSubjectError`
(fault-class) with the diagnosis chains, the mint gate's
grounding-enriched failures, and the safety pass's request-edge
comparison (`notes/execution_semantics.md`). The linter is implemented
(chunk 5): the one-snapshot sweep is `WorkflowSession.lint`
(enumeration in `lint.py`), served on the wire as
`UserCapabilityProto.lint` returning the sectioned `LintReport` — see
"The linter" below.

## The two axes

The system derives exactly two orthogonal verdict axes over the durable
state:

- **Currency** (fresh / stale / unsettled) judges *values*: do a
  generation's recorded observations still match the world? It is the
  authority on "is this result still right".
- **Grounding** judges *intent and structure*: does this fact trace back
  to something the user has durably declared, and would a from-scratch
  replay from those declarations re-create it? It is the authority on
  "is this fact supposed to be here at all".

Grounding is deliberately **fingerprint-blind**: it asks whether a replay
would have what it needs, never whether it would reproduce the same bytes
(that is currency's job). The axes move independently — revoking a
sourcing record is not a value change, so no currency verdict moves;
a rerun that reproduces a value re-establishes freshness but does not
re-anchor intent. The precondition for meaningful export is the
conjunction: everything fresh *and* everything grounded, with no dangling
intent records — that conjunction is what makes the replay contract
(below) hold. The linter (below) checks exactly the intent-axis half of
it (structure and intent, never staleness); the freshness half is judged
per cell through the currency channels.

## Facts and their justifications

Every *existing* fact in a workflow needs a justification:

- a **grid** existing with its canonical config,
- a **data/handle cell** holding its current value,
- a **task cell's current generation** having been computed.

Justification kinds differ by fact kind, and this asymmetry is load-
bearing rather than incidental: for a *writable* grid the interesting
question is per-cell ("who wrote this value?") — the grid itself is just a
frame; for a *task* grid the interesting question is per-grid ("who
determines the contents?" — the rule is the content) plus a per-cell
question of a different character ("why is it important to compute this?"
— demand, not authorship). The same three derived predicates cover all of
it; they are simply informative about different fact kinds.

### Base facts

Types, with the variable naming convention used throughout:

```
Grid       -- a grid (identified by key path)                (variables g, g')
TaskCell   -- a cell of a task grid                          (c, c')
DataCell   -- a cell of a data or handle grid                (d)
Gen        -- one generation of one task cell                (e)
Config     -- a canonical grid-config fingerprint            (C)
Val        -- a value fingerprint (FxtrID)                   (v)
Out        -- a declared task-output name                    (o)
```

Total structural functions:

```
grid_of_task_cell :: TaskCell -> Grid
grid_of_data_cell :: DataCell -> Grid
cell_of_gen       :: Gen      -> TaskCell   -- every generation belongs to
                                            --   exactly one task cell
```

Extensional relations describing the current world (all finite; the
"≤ 1" notes are functional dependencies, not typing):

```
grid_current    :: Grid -> Config -> Bool   -- g exists with canonical
                                            --   config C (≤ 1 C per g)
data_current    :: DataCell -> Val -> Bool  -- d is populated, holding v
                                            --   (≤ 1 v per d)
gen_current     :: TaskCell -> Gen -> Bool  -- e is c's current generation
                                            --   (≤ 1 e per c)
grid_dep        :: Grid -> Grid -> Bool     -- g structurally needs g': g's
                                            --   enclosing subtree grid,
                                            --   plus, for task grids, every
                                            --   grid the stored rule
                                            --   references (input slices,
                                            --   the task_fn slice)
mint_compatible :: Grid -> Grid -> Bool     -- g's stored rule can mint its
                                            --   grants against g''s current
                                            --   config (derived, live)
```

**Editable user state** — the root facts. These are durable, declarative,
reversible records with the same doctrine as targets: pinned to a config
or value fingerprint, listable, revocable, *dangling* when the pinned
fingerprint no longer matches the world (surfaced as incoherent, never
silently rebound or dropped):

```
user_sources_grid :: Grid -> Config -> Bool
    -- "I assert this grid exists with config C"
user_sources_cell :: DataCell -> Val -> Bool
    -- "I assert this cell holds value v"
target_task :: TaskCell -> Config -> Bool
    -- "this task cell is a wanted DAG root"; pins the grid's expected
    --   canonical config (the existing target records)
target_data :: DataCell -> Config -> Bool
    -- data-cell targets: checked read-roots (see below); deliberately
    --   feed no derived predicate
```

**Record-derived claims** — read off generations' durable records, so
their lifecycle is automatic: they expire with generation turnover
(clearing a generation withdraws all its claims). ``wrote`` *is* the
``wrote_cell`` observation; ``asserted_grid`` rows live in their own
generation-cascaded table; ``read_out`` / ``read_data`` are **request
records** (``request_edges``), written at request *commitment* —
record-miss time, before the value resolves. A running cascade's demand
structure is therefore durable from the moment it exists, and an
aborted request's surviving edge is *correct* (the resume re-requests
at the frontier; the insert is idempotent). At completion the safety
pass enforces the match — every request answered, every read requested
(`notes/execution_semantics.md`) — so for complete generations the two
records coincide:

```
asserted_grid :: Gen -> Grid -> Config -> Bool
    -- e opened g through write authority, proposing canonical config C
wrote :: Gen -> DataCell -> Val -> Bool
    -- e wrote d with v (wrote_cell observation)
read_out :: Gen -> TaskCell -> Out -> Bool
    -- e read output o of task cell c
read_data :: Gen -> DataCell -> Bool
    -- e read data/handle cell d (handle-key reads included)
```

Two deliberate choices in `asserted_grid`:

- It is keyed on **opens through write authority, not creation events**
  (opens carry no mode — the chain's authority decides; see the
  open-modes decision in `plans/phase2_grounding/chunk0_open_modes.md`).
  A create-or-attach open with create authority semantically asserts
  "make it so this grid exists with this config" — it literally would
  create the grid in an empty world — so *any* such open claims the grid,
  reader or writer alike; which generation physically created it is
  schedule noise. Recorded **monotone-OR per generation**: the record
  accumulates "some open of this grid had write authority", replay
  re-accumulates the same OR by determinism, and the safety pass compares
  the final record — no per-op payload mismatch rule. (Schema
  implication: the would-create bit must be recorded on `grid_config`
  observations; today it is not.) A reader-asserter's replay
  creates-then-faults until the writer runs — the fixpoint iteration
  covers it.
- The user-side twin is `user_sources_grid` — the same durable,
  config-pinned assertion, made explicitly (and auto-recorded by user
  creates).

Per-output note: `read_out`'s output argument is notational — the stored
request record is cell-granular, matching wantedness below, which is
cell-granular anyway: a run computes all outputs, so reading any output
wants the whole cell.

## The derived predicates

One least fixed point over three mutually recursive predicates. All rules
are positive (no negation under recursion), so the LFP exists; anything
not derivable — including every cycle, whether a rule-reference cycle
built by delete-and-recreate or a demand cycle — is simply *not grounded*,
with no special-casing. This mirrors how the currency recursion's
well-founded treatment parks observation cycles at unsettled.

Signatures — *grounded* is a family of three well-typed predicates, one
per fact kind, spelled distinctly (there is no overloading; a question
like "is this grid wanted?" is not well formed):

```
wanted             :: TaskCell -> Bool
would_start        :: Gen      -> Bool
grounded_grid      :: Grid     -> Bool
grounded_data_cell :: DataCell -> Bool
grounded_task_cell :: TaskCell -> Bool
```

Clauses. Conventions: variables appearing only in a body are
existentially quantified; the ∀-clauses range over finite record/config
sets and mention derived predicates only positively, so the defining
operator stays monotone and the least fixed point exists.

```
-- WANTED: pure demand intent, transitively closed. "Would anything in a
-- replay ask for this task cell?"
wanted(c)  ⟸  target_task(c, C) ∧ grid_current(grid_of_task_cell(c), C)
              -- an *attached* target names c
wanted(c)  ⟸  read_out(e, c, o) ∧ would_start(e)

-- WOULD_START: a macro, not a fourth concept. "Would a from-scratch
-- replay start this generation's run?" — current, demanded, mintable.
would_start(e)  :=  gen_current(cell_of_gen(e), e)
                  ∧ wanted(cell_of_gen(e))
                  ∧ grounded_grid(grid_of_task_cell(cell_of_gen(e)))

-- GROUNDED: "would a from-scratch replay re-create this fact?"

grounded_grid(g)  ⟸
      grid_current(g, C)
    ∧ (  user_sources_grid(g, C)
       ∨ asserted_grid(e, g, C) ∧ would_start(e) )
    ∧ ∀ g' : grid_dep(g, g') ⇒ grounded_grid(g') ∧ mint_compatible(g, g')

grounded_data_cell(d)  ⟸
      grounded_grid(grid_of_data_cell(d))
    ∧ data_current(d, v)
    ∧ (  user_sources_cell(d, v)
       ∨ wrote(e, d, v) ∧ would_start(e) )

grounded_task_cell(c)  ⟸
      gen_current(c, e) ∧ would_start(e)
    ∧ ∀ d     : read_data(e, d)    ⇒ grounded_data_cell(d)
    ∧ ∀ c', o : read_out(e, c', o) ⇒ grounded_task_cell(c')
```

Reading guide, rule by rule:

- `wanted` — a task cell is wanted if a (non-dangling) target names it, or
  if some *would-start* generation recorded a read of one of its outputs.
  Wantedness is pure demand: it deliberately does
  *not* require the wanted cell's own grid to be healthy (a target on an
  unmintable grid still expresses want; it just grounds nothing).
- `would_start` — demand propagates *through* a generation exactly when
  its replay run would begin: it is its cell's current generation, the
  cell is wanted, and the cell's grid is grounded
  (mintable-in-the-replay). An unmintable cell's run never
  starts, so its recorded reads would never be re-issued — demand must
  not flow through it.
- `grounded_grid` — a grid would be re-created if the user asserts it, or
  some would-start generation's record asserts it; and every grid it
  structurally needs (enclosing subtree, rule references) is itself
  grounded and mint-compatible. For non-task grids the dependency
  quantifier covers only the enclosing subtree. This clause *is* grid
  well-foundedness: "grounded grid" literally means "a run of a cell here
  can structurally succeed in the replay world".
- `grounded_data_cell` — a data/handle value would be re-created if the
  user asserts it or a would-start generation's record wrote it. Handle
  grids: tasks never write handle cells (authority doctrine), so the
  writer clause is vacuous and handle values are grounded only by user
  sourcing — revoking a handle's sourcing is how you retire an external
  service from the experiment's intent.
- `grounded_task_cell` — a task generation would be re-created if its run
  would start *and* every subject its record shows it requests would
  exist in the replay world. Fingerprint-blind throughout: same-values is
  currency's question.

### Execution status and the predicates

The datalog is deliberately **status-blind**: ``gen_current`` holds for
running, interrupted, and errored generations alike, and the
record-derived facts are whatever the record contains *now*. This is
faithful to the replay reading in each case:

- **Running** — the record is a younger, monotonically growing estimate:
  unissued reads want nothing yet, unmade writes claim nothing yet.
  Verdicts track the growth (every insert marks in-transaction); the
  running generation's *own* verdicts are never cached.
- **Interrupted** — the recorded prefix is deterministic-given-inputs
  evidence a replay reaches the same reads and writes, and the fault was
  a not-ready dependency the fixpoint iteration resolves: the replay
  resumes and completes it. Counting its claims and demand edges is
  exactly the ``would_start``-not-would-complete design.
- **Errored** — the replay reproduces the error, but the writes before
  the raise happened deterministically-given-inputs, so its claims read
  "the replay would re-create this before erroring", and its demand
  edges are re-issued on retry.

One sharpening of the estimate-honesty caveat: the safety pass verifies
determinism only at *completion*, so claims from non-complete
generations rest on unverified determinism — best-effort, like every
verdict over dirty state (and non-complete generations are never
fresh-servable anyway). The only rules that mention execution status at
all are operational, not semantic: running generations never stamp
their own verdicts, and stamp-mark propagation treats running as
unstamped. A *cleared* generation withdraws its claims because turnover
deletes its rows, not via any status clause.

### Directionality

Along observation edges, `wanted` flows **consumer → producer** (opposite
to dataflow: targets are roots, and demand traces down into what the
demanders' records read), while the input conjuncts of `grounded_task_cell`
flow **producer → consumer** (with dataflow: a generation's verdict
conjoins its inputs' verdicts), plus grid-dependency edges for
`grounded_grid`. The two directions interlock — `wanted` consults
`grounded_grid` at every propagation hop via `would_start` — so this is
one joint fixed point, not
a down-sweep followed by an up-sweep; operationally it is computed as
alternating sweeps to quiescence.

### Why three predicates and not one

A natural simplification — a single merged "grounded" where demand edges
only count from *fully grounded* demanders — fails on the sub-workflow
pattern, which is worth recording because it forces the factoring:

Let targeted task **E** create internals grids, write an args cell, open a
child task grid, and read the child's outputs, with the **child** reading
the args cell. Under the merged predicate: the child is grounded only if
its demand edge counts, which needs E grounded; E is grounded only if its
observed subjects are grounded, which needs the child grounded. No LFP
derivation can enter the cycle, and the flagship composition pattern comes
out ungrounded.

The fix is what a replay actually does: E need not *complete* for the
child to be demanded — demands are issued mid-run. So demand edges count
from generations that would **start** (`would_start`), while "inputs would
exist" is a separate conjunct in `grounded_task_cell`. With the split, the
derivation goes through: E targeted → `would_start(E)` → child wanted and
args cell grounded (E's write) → child grounded → E grounded.

Two related notes:

- The split also keeps diagnosis local: a broken input under one branch of
  a fan-in does not withdraw wantedness from healthy sibling producers
  (their demander still *would start*), so verdicts point at the actual
  break.
- Literal one-generation self-justification (a generation reading a cell
  it wrote itself) cannot arise: read-xor-write forbids a generation
  reading any grid it wrote. The mutual E↔child shape is the real case.

## Local verdicts: orphaned and unwanted

The depth-1 approximations — cheap (one reverse-index lookup), definite,
and independent of anything downstream:

- A grid or data/handle cell is **orphaned** iff no matching user sourcing
  record exists and no *current* generation's record claims it
  (`asserted_grid` / `wrote` with the current config/value). Note
  generation turnover produces orphans automatically: clearing a writer
  orphans its side-written values until a rerun re-claims them.
- A task cell is **locally unwanted** iff it has no attached target and no
  current generation's `read_out` edge names it.

The tri-state parallel with currency is exact and worth internalizing:
*orphaned* is the definite state (no claimant at all — the analogue of
stale), while *ungrounded-but-claimed* is the cannot-currently-prove state
(a claimant exists but does not trace to current user intent — the
analogue of unsettled: nothing observable changed, but the flow of intent
no longer specifies what this fact is supposed to be).

## Gating

Execution gating over grounding verdicts is **pure policy over derived
state**, exactly like currency gating: the definitions above are
policy-neutral, verdicts are maintained regardless of the chosen policy
(the linter/UI need them anyway), and switching policy never changes
semantics or storage. The gate seam:

- **One policy decision point, both axes, one snapshot.** The demand/read
  hook already evaluates the currency verdict atomically with serving the
  value (value-pinning); the grounding verdict joins the same atomic
  evaluation. The policy receives (subject kind, currency verdict,
  grounding verdict) → serve or fault.
- **Requests are self-vouching because requests are durable.** A request
  edge is recorded at *commitment* (record-miss time, before the value
  resolves), so when a running generation `e` requests subject `s`, the
  edge (`e`, `s`) — and every edge above it in the cascade, down from
  the durable roots (targets) — is already in the state db, and for a
  task-output subject `wanted(s)` holds via `would_start(e)` from
  durable facts alone. This is not policy-dependent leniency; it is the
  runtime counterpart of the `would_start` split (demand flows from
  generations that have started): a whole fresh cascade vouches itself
  with no evaluation-time extras, no special evaluation mode, and full
  lint/UI visibility of in-flight demand. A data/handle read records a
  request edge too (the *requested* set is what `grounded_task_cell`'s
  input conjuncts range over), but a data edge contributes no
  wantedness — data stays the no-demand channel, so the cascade's
  task-output edges vouch the *writer's* wantedness at data reads,
  never the cell's.
- Consequently, **the grounding gate only has bite on data/handle-cell
  reads**: a task-output read can never fault for unwantedness (the read
  itself wants the subject; grid-grounding and the subject's own input
  conjuncts still apply, recursively, as the cascade executes). The gate
  polices exactly the one channel demand does not flow through —
  side-written data — forcing the writer's wantedness to be established
  some other way (a target on it, or a consumer of its outputs).
- **Fault taxonomy**: a grounding-gate fault is fault-class
  (`CellFaultError` flavor → interrupted), with a currency-style
  diagnosis chain naming the roots — e.g. "d's writer c is unwanted: no
  target, no current reader" or "grid g ungrounded: rule references
  missing grid g'".
- **User reads follow the same gate** (they already follow the demand
  policy for currency); `inspect_cell` remains the sanctioned
  labeled-value escape for looking at anything, and gains the grounding
  verdict alongside the currency verdict in its report.

Gate table:

| gate | when it runs | what it consults | policy levels |
| --- | --- | --- | --- |
| currency gate | serving any new observation | subject's currency verdict, value-pinned | strict (v0 default; relaxations deferred per `notes/execution_semantics.md`) |
| structural mint gate | grant minting, pre-run | `grid_dep` targets exist + `mint_compatible`, live | always on (it *is* mint failure, made immediate and well-diagnosed) |
| grounding gate | serving any new observation (self-vouched) | subject's grounding verdict | **strict** (v0 default): fault on ungrounded; **off**: lint only |

The default is **strict**, for consistency: the v0 currency policy already
faults on unsettled rather than serving plausible-but-unvouched values,
and fault-and-re-demand under churn is the accepted v0 texture — the
motivating exhibit: a pipeline driver S that asserted task grid T and
read its outputs is cleared for modification; T's old side-written value
d is untouched and fresh, but the flow of user intent no longer specifies
what d is supposed to hold (the new S may be about to assert a
conflicting T), and strict refuses to let new work silently bake d in.
The accepted texture cost is that a read can fault purely because a
*downstream sibling's bookkeeping* is mid-regeneration and succeed on
re-demand moments later. A **local-only middle mode** (fault only on
*orphaned* — the "gates consult a subject and its upstream, never records
downstream of its writer" principle) was designed and then dropped: its
only protection beyond ``off`` is authorless data, its only relief versus
strict is tolerating that flap, and the tri-state verdict survives as
*diagnosis* either way. Gate checks are **serve-or-fail, one operation**:
a single session op evaluates both axes and serves the value/results
from the very snapshot the verdicts were derived on, or faults — there
is no check-then-use window. Serve time is the re-check-on-arrival
moment: a revocation while a subject's run is in flight faults the
demander at arrival, exactly like currency.

## Maintaining the root facts (the user surface)

- **Sourcing records are first-class editable state**, doctrine-identical
  to targets: one record per grid / per cell identity, config- or
  value-pinned, listable, revocable, dangling on mismatch (surfaced,
  never silently rebound). User writes and creates **auto-upsert** the
  corresponding record (writing is sourcing); explicit sourcing of a fact
  the user did *not* author is also permitted — that is **adoption**
  (below).
- **Grid sourcing before cell intent** (API-enforced invariant, not a
  datalog condition): `add_target` and cell-sourcing on a grid require a
  user sourcing record for the grid first. This keeps declared intent
  self-contained — the user's root set is grids they assert, plus cell
  intent within them — and steers away from targets on task-created grids,
  which would be ungrounded anyway (nothing in a replay demands a grid's
  *creator*; demand flows through output reads, not through wishes that a
  grid exist). The derived semantics do not depend on the invariant; it
  exists to prevent incoherent intent from being declared.
- **Adoption**: user-sourcing a task-created grid (a config-pinned
  co-assertion alongside the task's `asserted_grid` claim) is the
  sanctioned way to promote an inner grid to directly-intended, after
  which targeting/sourcing inside it is allowed. Adopting a task grid
  installs only its config/rule (nothing runs), and its `grid_dep`
  references must still ground — the linter names exactly which further
  grids an adoption pulls in. Config-pinning does the right thing over time: if the creator
  evolves to assert a different config, the adoption record dangles and
  is surfaced.
- **User demands require targets; the method declares the intent.** User
  demands do *not* self-vouch (they record nothing durable — the
  principled asymmetry with task requests), and the rule is *stronger*
  than the gate: a user demand of a task cell requires an **attached
  target on that cell**, enforced at the capability level. The state
  never substitutes for the declaration — a cell that happens to be
  wanted through some other task's records is *not* thereby
  user-demandable (that would let current state stand in for intent the
  user never expressed); values that merely exist are looked at through
  the labeled introspection channels. Ergonomics: the `Workflow`
  wrapper's demand **auto-adds the target by default** (`target=False`
  opts out, faulting unless the cell is already targeted; the wire
  capability never mutates intent state on a read — the wrapper's
  auto-add is an ordinary `add_target`, event-logged). Composition with
  the invariant is strict all the way down: target requires the grid
  user-sourced, so demanding into a task-created grid faults with
  "adopt the grid first" — sugar never silently escalates to adoption.
  Cells a demand *cascades* to need no targets (wanted through the
  demander's records); and since task-internals grids are never
  user-sourceable, inner outputs are never demandable from the top —
  internals browsing is `inspect_cell`-only, as a rule rather than
  advice. Accepted cost: exploratory demands accumulate targets
  (visible in `list_targets`, removable, linter-surfaced), diluting
  "targets = sparse curated intent" in exchange for zero-ceremony runs.
- **Data-cell targets are checked read-roots**: "assert this cell
  resolves in a replay". They contribute no wantedness (nothing computes
  a data cell on demand) and no grounding (targets are demand intent, not
  sourcing); the linter checks each one's cell is grounded, and demanding
  one is a gated read.
- **Export/import**: the editable intent state (sourcing records +
  targets), plus the entity store carrying the pinned values, *is* the
  exportable root state. Import = install the records, then run the
  replay procedure below.

## Grid dependencies and deletion

`grounded_grid`'s dependency clause is maintained over a **reverse
rule-reference index** (grid → task grids whose rules or subtree
structure reference it). This makes deletion consequences immediate and
queryable instead of discovered downstream: `delete_grid` of a grid with
dependents flips those dependents' grounding verdicts (their `grid_dep`
references no longer ground) in the deleting transaction's marking pass, so the UI
can show "this breaks N grids" at the moment it happens. Whether the call
should *refuse* without a `force` flag or proceed-and-notify is a
UX-round followup; the mechanism is the same either way. Cells of deleted
grids also make their observers stale (missing subject) exactly as
before — the two axes each report their own damage.

Recreate after delete is just marking in the other direction: creating a
grid re-derives its dependents' verdicts from their references' current
state on next query. A rule-reference cycle built by delete-and-recreate
is handled by the LFP with no special case: not derivable, so not
grounded, diagnosed as such.

## The replay contract

The replay procedure, from an exported root state into an empty world:

1. **Install**: create every user-sourced grid (from its pinned config)
   and write every user-sourced cell value (from its pinned fingerprint;
   the entity store carries the bytes) whose preconditions currently
   exist (a record whose enclosing grid is missing simply stays pending).
2. **Demand**: demand every attached target.
3. **Iterate** 1–2 to quiescence.

**The contract**: if, in the exported state, every existing grid,
populated cell, and current generation is grounded; every generation is
complete and fresh; and no sourcing record or target is dangling — then
the procedure converges and re-creates every grid, re-runs every
generation, and reproduces the same values and results, up to the
task-determinism the safety pass already polices. Grounded is *designed*
to be exactly the set a replay re-creates; the linter's green state is
the hypothesis of this contract.

Two honest caveats, both by design:

- **Convergence is fixed-point iteration, not one pass.** Side-written
  data imposes orderings the demand graph does not express: if T writes d
  and R reads d, demanding R before T has run faults on the missing cell
  and succeeds on re-demand. Iterating to quiescence converges (each wave
  materializes another LFP layer); the semantic meaning of grounded is
  the fixed point, never a promise about execution order. A
  blocking/parking execution mode (park a faulting read until its subject
  appears — the same mechanism as the deferred await-instead-of-fault
  currency knob) would converge in one pass; the one-pass *discipline*
  without it is having R also read one of T's outputs as a completion
  token, making the ordering explicit in the demand graph. Recommended
  idiom, not enforced.
- **Estimate honesty on dirty states.** The record-derived facts are the
  *current generations' records* — a proxy for what a replay would demand
  and write, since data-dependent reads may differ when inputs change.
  On a fully fresh state the safety pass makes the records exact and the
  verdicts are exact; on dirty states grounding verdicts are best-effort
  diagnosis with the same epistemic status as currency's diagnosis
  chains, converging to exact as things become fresh.

## The linter

The clean-state inventory: one privileged query
(`user_api.UserCapabilityProto.lint`; the sweep is
`WorkflowSession.lint`, everything derived under a single hold of the
session lock, so the report is one coherent snapshot). Like the other
introspection channels it records nothing and nothing downstream can
depend on it; bulk derivation piggybacks on the verdict stamps, so the
first sweep pays the walk and repeats serve stamps.

**The linter is structural: grounding and dangling intent only —
staleness is deliberately out.** A finding means "this experiment is not
constructed properly: these concrete problems prevent it from working" —
problems one *fixes* (re-source, re-target, adopt, delete). Staleness is
the other axis: a normal operational state one *reruns*, judged per cell
(`inspect_cell` / `cell_currency`); folding it into lint would make red
routine (every mid-run or edited-input workflow) and kill the signal.
The linter thus checks the *structural half* of the replay contract's
hypothesis; the freshness/completeness half is currency's. A pleasant
consequence: a lint taken mid-run is typically green — running cascades
self-vouch through the durable request edges — so red genuinely means
broken construction.

The report's sections (`user_api.LintReport`):

- **Dangling sourcing records** and **dangling targets** — pinned
  fingerprints that no longer match the world (incoherent intent,
  surfaced, never silently rebound).
- **Target findings** — attached targets whose *materialized* fact is
  not grounded (task cells: the current generation's verdict; data-cell
  targets: the populated cell's — the checked-read-root rule above).
  An attached target with nothing materialized is **pending intent,
  deliberately not a finding**: there is no fact to grade, and its
  remedy — demand it — is operational, not structural.
- **The ungrounded inventory** — simply *every* non-grounded fact
  (orphaned vs unanchored, each with its diagnosis). "Garbage" is not
  what the section is, only what it *means* once the targets are green:
  grounding is cone-transitive, so a sound report implies every
  inventory entry sits outside every demanded cone — exactly the prune
  candidates. While a target is red, the same section also contains the
  *causes* of the redness (named by the red target's diagnosis chain),
  and pruning those would be wrong. The linter deliberately computes no
  cone membership to separate the two: the prune-candidate reading is
  exact once sound, best-effort diagnosis before that (the standing
  dirty-state epistemics). Locally-unwanted task cells need no separate
  section — the tri-state mapping renders them as orphaned task cells.

**The two-level clean state**:

- **Sound** — no dangling intent and no red targets. Checking only the
  target roots suffices because grounding is cone-transitive
  (`grounded_task_cell` conjoins its requested inputs' verdicts
  recursively): root checks *are* the cone check, and the diagnosis
  chains itemize any break.
- **Clean** — sound, with an empty ungrounded inventory: the structural
  half of the contract's global hypothesis (a grounded task cell is
  necessarily wanted, so "everything grounded" ≡ sound plus nothing
  outside the demanded cone that a replay would not re-create).

Because pending targets are invisible to findings, a state can be sound
while the inventory still names structural damage (e.g. a dependent
grid's broken rule reference after its dep was deleted and the
generations cleared — unanchored until the creator re-runs); **clean is
the green state**.

## Maintenance and encoding (sketch — details in an implementation round)

The same shape as currency maintenance: **verdict stamps** on the derived
predicates, cleared by **marks in the mutating transaction**, derived and
re-stamped on demand, with the stamps as the only memo. The mark triggers:

| mutation | grounding stamps affected |
| --- | --- |
| sourcing record add / revoke | the subject grid/cell, and onward |
| target add / remove | the cell's wanted-derived verdicts, and onward |
| request-edge insert (a running generation commits to a request) | task-output kind: the subject cell's wantedness; data kind: nothing (it feeds only the requester's own verdict, and running generations are never stamped) |
| observation insert (a resolution or write lands) | `wrote` / `asserted_grid`: the subject's groundedness; read resolutions: nothing (the request edge already marked at commitment) |
| generation turnover / clear | everything the departing record claimed or wanted |
| grid create / delete | dependents via the reverse rule-reference index, plus contents |

Propagation runs along *both* demand/record directions (wantedness marks
flow consumer→producer, groundedness marks flow producer→consumer), over
the request-edge location index, the observation indexes
(`observations_by_version` and the by-exec index), and the reverse
rule-reference index.

One soundness constraint worth pinning now: currency's marking exploits
its I2 invariant ("no new fresh edge attaches to unstamped support",
guaranteed by the strict gate) for its prune-at-unstamped walk. The
grounding analogue ("no new observation edge attaches to an ungrounded
subject") holds only while the strict gate is *on* — and the gate is a
knob. **Grounding-stamp maintenance must therefore be sound without it**
(a conservative marking rule on edge-insert); no future optimization may
quietly assume strictness.

Schema/API implications gathered from the above (all implemented — the
status paragraph at the top carries the code pointers), plus the request
record (``request_edges``) the self-vouching bullet describes:

- record the would-create bit (open-through-write-authority) on
  `grid_config` observations, monotone-OR per generation — these are the
  `asserted_grid` claims;
- sourcing-record storage (targets-shaped) + add/list/revoke surface, and
  auto-upsert on user writes/creates;
- the reverse rule-reference index;
- grounding verdict stamps + the marking table above;
- gate policy seam at the demand/read hook (both axes, one snapshot,
  self-vouching);
- launch-requires-target enforcement + auto-target sugar on user demand
  paths;
- surfacing: grounding verdicts/diagnoses in `KeyInfo` / `CellInfo` /
  `CellReport` / `TargetInfo`, and delete-consequence reporting.

## Recorded alternatives

- **A single merged predicate** ("grounded with demand edges from grounded
  demanders only") — rejected: not derivable for the sub-workflow pattern
  (the forcing argument above).
- **Grounding requiring freshness** (`grounded_task_cell` also demanding its
  inputs fresh) — rejected to keep the axes orthogonal; "clean" is the
  conjunction, and a stale-but-grounded pipeline reads as "intended,
  needs rerun", not "broken". Kept as a possible derived *combination*
  verdict if the UI wants one.
- **Auto-targeting on *inspection*** — rejected: casual looking must not
  mutate durable intent; the labeled inspection channels cover it.
  (Auto-targeting on *demand* was later adopted as the wrapper default —
  demands are programmatic driving, i.e. intent — see "maintaining the
  root facts"; the wire capability stays mutation-free either way.)
- **Launch-requires-target as a gate-emergent property** — considered
  and rejected in favor of the *explicit* capability-level rule "user
  demands require attached targets": the emergent version leaves a side
  channel (a cell wanted through some other task's records would pass
  the gate for a user demand the user never declared), and the method
  called — not the current state — is what expresses intent.
- **The local-only gate mode** — designed, then dropped (see the gate
  table's rationale); the orphaned verdict remains as diagnosis.
- **Gating on transitive verdicts evaluated without self-vouching** —
  rejected: deadlocks fresh cascades.

## Deferred followups

- The UX round owns the `delete_grid` refuse-vs-notify `force` toggle.
  (Two former UX-round items are settled: the gate policy is strict
  default with `off` as the toggle — local-only dropped — and demands
  auto-target by default at the wrapper.)
- A **pruning affordance** (bulk-delete orphaned / unwanted facts) —
  grounding makes it well-defined; deliberately not default behavior.
- The **blocking/parking execution mode** (one-pass replay convergence;
  pairs with the deferred await-instead-of-fault currency knob).
- **Gather/domain-cell interactions**: once open-slice (gather-driven)
  targets exist, their wantedness contribution follows the same rules
  over the enumerated cells.
