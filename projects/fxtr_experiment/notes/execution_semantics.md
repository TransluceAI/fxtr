# Task execution semantics — design note

Standing, self-contained description of the intended execution model: how
task cells execute, what is durably recorded, and how replay, currency
(fresh / stale / unsettled), and demand interact. This is the *dynamic* counterpart to the static
addressing/view-calculus exposition in `fxtr_experiment/view_spec.py`'s
module docstring; the rationale trail for everything here is in
`notes/archive/discussion_log_2026_07_04.md`. None of this is implemented
yet — this is the
contract the runner is being built toward. Terms used without definition
here (grids, cells, views, outer/inner level, poisoning) are defined in
`view_spec.py`, `task_api.py`, and `errors.py`.

## Cells, generations, runs

A task cell's execution has three layers:

- The **cell** is the durable unit of result: demand-driven, with named
  outputs, an execution status (`pending | running | complete | errored |
  interrupted` — see *Errored vs interrupted* below), and derived currency.
- A **generation** is one logical execution identity of a cell. All durable
  execution state — the op record (below), checkpoint entries, observation
  records and grid-assertion claims, results — belongs to a generation. A new
  generation is minted only after the previous one's state has been
  explicitly cleared (v0: user-driven; see demand policy); old-generation
  state is deleted atomically *before* the new generation starts.
- A **run** is one in-memory invocation of the task's outer level against a
  generation's record. Runs are ephemeral and interchangeable: nothing
  in-memory is load-bearing across runs, so a run may crash or be evicted at
  any point and a later run continues where it left off. An *attempt* is
  simply a run that tries to carry the generation forward — the executor has
  no attempt-specific state or resume mode; a resume is an ordinary run
  whose early operations all hit the record.

## Currency: fresh, stale, unsettled

Currency and execution status are orthogonal axes: a generation can be
running and already stale (v0 lets it finish). Currency is *derived* from
recorded observations and current grid contents (the derived verdict is
cached in the state db — see "Currency maintenance in the database"
below — but the cache never changes the semantics):

- A subject's **settled value** is a definite current value: a data cell's
  user-written contents, or a completed generation's output. User actions
  (overwrite, delete) settle immediately; execution lifecycle (cleared
  awaiting rerun, running, errored awaiting retry) leaves a task output
  without a settled value until a generation completes again. Note a stale
  task still *has* a settled value — just possibly not a current one.
- A cell is **stale** iff some observation's subject currently holds a
  *settled* value different from the recorded fingerprint (including a
  data-cell subject the user deleted). Staleness is a definite, local
  fact; it never propagates transitively.
- A cell is **fresh** iff every observation's subject currently holds
  exactly the recorded fingerprint as its settled value and every observed
  task output's cell is itself fresh — with **well-founded support**: the
  justification must bottom out in data cells and settled task results, so
  observation cycles (possible across generations: A completed; B read A;
  A cleared and re-ran reading B) can never prove freshness.
- A cell is **unsettled** otherwise: nothing it observed has definitely
  changed, but its support DAG contains something stale, re-running,
  cleared-but-not-rerun, errored, or cyclic. It resolves to fresh or stale
  when its support settles. A cell is *settled* iff fresh or stale.

The downstream behavior falls out of the three-way split: if B observed
A's output and A's inputs change, A becomes stale but B merely becomes
unsettled — whether B is condemned depends on whether A's rerun actually
changes the output. A rerun reproducing the same value resolves B fresh
(resurrection is the ordinary path, not a special case); a different value
makes B stale. Observations of task cells carry the output name, so
currency is per-output. Evaluation is a memoized three-valued recursion
whose in-progress marker is unsettled — cycles need no special-casing.

## Currency maintenance in the database

Derived verdicts are cached as **stamps** on generation rows
(`storage.py`: ``currency_verdict`` / ``currency_reason`` /
``currency_as_of``, NULL together). There is deliberately **no separate
dirty bit**: "dirty" and "unstamped" provably never come apart (the only
writer of a stamp is verification, inserts are unstamped, and an
invalidated stamp is dead information), so *marking a generation dirty*
IS *clearing its stamp*. Two invariants carry the design:

- **I1 (the stamp invariant).** Any settled, currency-relevant change
  that could alter a stamped generation's verdict clears that stamp, in
  the *same transaction* as the change (which is what makes the
  invariant crash-safe with zero extra machinery). Contrapositive — the
  point: a live stamp on a settled generation is current and serves with
  no walk.
- **I2 (no fresh edges onto unstamped support).** A new task-output
  observation edge only ever attaches to a verified-fresh upstream:
  every record-miss task-output read goes through the demand hook, whose
  strict gate verifies (and stamps) the upstream before serving.

**Granularity: marks per generation, precision per edge.** Marking never
needs to know *which* output changed — per-output precision already
lives in the observation edges' fingerprints, which verification
compares edge by edge. A rerun that leaves an output byte-identical
conservatively clears its observers' stamps; their next verification
compares fingerprints and stamps them right back (resurrection is the
ordinary cycle, not a special case).

**The trigger table** (each in the mutating body's own transaction; the
bodies in `state_store.py` each carry a "Marks:" docstring line — the
greppable audit trail):

| mutation | stamps cleared |
| --- | --- |
| data/handle cell write, value actually changes | observers of `(g, c)` (idempotent same-value writes clear nothing) |
| grid create | observers of `(g, ·config·)` — resurrection/condemnation of a deleted predecessor's observers |
| grid delete | observers of every subject under `g` |
| generation turnover (`delete_generation` / atomic clear) | observers of `(g, c, *)` |
| terminal status flip (incl. the startup crash-flip) | observers of `(g, c, *)` (the completion is the visibility event for results — result recording itself clears nothing) |
| re-run flip (status → running) | the generation's own stamp only (observers were cleared when whatever made it re-runnable happened) |

Record traffic (observations, failures, checkpoints) and result
recording during a run clear nothing: a running generation is never
stamped (it always derives live — its record is a moving target), so
nothing cached can depend on an in-flight record, and its own verdict is
gated behind the status flip.

**Propagation is eager, transactional, and prunes at unstamped nodes**:
clearing walks the reverse observation index (``subject_versions``'s
identity index + ``observations_by_version``) up the observer DAG,
recursing only from rows whose stamp this walk actually cleared. The
prune is sound by I2 — while a generation is unstamped, no *new* edge
can attach to its outputs without a verification re-stamping it first,
so everything downstream of a continuously-unstamped node was cleared
when it lost its stamp; running observers are covered because they are
unstamped by rule. Cycles terminate for free (a member reached twice is
already clear), and each generation pays at most one clear per verify
cycle, so total marking work is bounded by verification work, which is
bounded by demand traffic.

**Verification** (`WorkflowSession.cell_currency`, implementation
`currency.evaluate_cell`) is one atomic body: a settled generation with
a live stamp serves in O(1); otherwise one recursive CTE bulk-loads the
support cone (expanding only through unstamped-or-running nodes — clean
support contributes its stamp without expansion), the three-valued
recursion runs in Python over the loaded cone, and derived verdicts of
settled generations are stamped back in the same transaction. **All
verdicts stamp**, including stale and unsettled: a stale stamp is
invalidated by the revert-write's mark; an unsettled-because-running
stamp by the status flip; an unsettled-because-cycle stamp correctly
persists until some member changes (a changeless cycle can never
resolve). Running generations never stamp. The stamps double as the
cross-query memo — verifying one cell stamps its settled support cone,
so a fan of queries (e.g. ``list_cells`` reporting currency) pays one
walk, not one per cell; there is no in-process memo or change counter at
all.

## The op record and the universal record-check

Each generation owns a durable **op record**: a *keyed set* (not a
sequential log) of every effectful operation its outer level has performed.
Keys are subjects — grid path + coordinates (+ output where applicable);
the entries double as the observation records currency is derived from.
One mechanism, two consumers.

**Universal record-check.** Every effectful op, on every run, first consults
the record:

- **hit** — the recorded outcome is served; live state is not touched.
- **miss** — the op executes live, and its outcome is recorded.

There is no separate live/replay/checked mode. Consequences:

- *Resume is free.* Re-running a crashed generation replays its past through
  hits and goes live at the frontier.
- *Per-subject coherence.* Within one generation, two reads of the same cell
  can never return different values, even in a first live run — the first
  read pins the value.
- *Coherence decouples from currency.* A completed execution is always a
  coherent function of one snapshot-per-subject; currency separately says
  whether that snapshot is still current. If an input changes mid-run, the
  execution does not see the change, completes coherently, and is simply
  stale — and if the change is later reverted, the result becomes fresh
  again rather than being permanently diverged.

**Hit-mismatch rule.** Ops that carry a request payload — writes (the
value), opens (the config), steps (the invocation) — must match the recorded
payload on a hit; a mismatch raises `NondeterminismError` (conflicting with
your *own record* is nondeterminism — e.g. writing one cell twice with
different values in one generation is caught immediately rather than
surfacing as a self-write-conflict). A record-*miss* write that conflicts
with a live cell's existing different value is an ordinary
`WriteConflictError`.

**Hits do not re-demand.** Serving a recorded read of a task output does not
re-demand the subject, even if the subject has meanwhile become errored,
stale, or unsettled: it was fresh when observed, the dependency is already
recorded, and the freshness invariant (below) governs only *new*
observations.

**Request = observation.** A task observes a subject the instant it
*requests* it, independent of what it does with the result: dropping or
internally cancelling a pending read/demand is equivalent to catching its
error, and is not a separately-reasoned case. Mechanically, op arrival at
the engine registers an *obligation* (arrival is the commitment point —
task-side cancellation cannot retract it); the durable observation is
written if and when the value resolves; and a run completes only once its
obligation set has drained and its failure set (see *Errors* below) is
empty. An obligation that resolves to a fault records a failure, and one
that is **cancelled before resolution** — the task dropped it, or the run
was cut off externally — records a fault-class ``RequestAbortedError``
failure and lets the cancellation proceed *immediately*: committed work
is never waited on (cancelling a runner must not block on an in-flight
upstream execution); the run can then only end interrupted, the aborted
op recorded no observation, and a later run re-attempts it at the
frontier. Either way, a task that returned early while a doomed demand
was pending ends interrupted exactly as if it had awaited it. Corollary:
a demand cycle can never resolve to anything — its members can only
fault.

Per-op-kind record shapes: `cell_value` (read/gather — the recorded
fingerprint *is* the returned id), `wrote_cell` (payload: the written id;
a hit serves a no-op), `grid_config` (payload: the canonical config; a hit
serves it), checkpoint entries (payload: the step invocation; a hit serves
the stored result). Handle identity is observed by the outer level reading
the handle cell's *key* (an ordinary `cell_value` observation); steps
receive keys as plain data, and connect authority follows the record — only
keys the generation has read *from handle grants* may be connected
(authority never flows through data; tasks never mint it). The task-fn read
is part of the **run prologue**: every run — including the safety pass —
begins by reading the task-fn handle cell *through the engine*, via a grant
minted from the rule's ``task_fn`` slice, so it record-checks like any
other op: the first run records it, every replay hits it, and the safety
pass's set comparison needs no exemptions. Serving hits needs no data
beyond what the observation records already hold.

## Demand policy

Demanding a task cell (reading any output, directly or via gather) resolves
per (execution status × currency):

- **not yet run**: execute it; use the result if it completes, fault if it
  fails (errored or interrupted).
- **running**: await it, then re-check currency *on arrival* — a result
  that arrives stale or unsettled faults rather than being observed.
- **interrupted, fresh**: resume it — an ordinary run of the same
  generation (replay hits to the frontier, then retry the blocked operation
  live: it succeeds if the blockage has resolved, faults again cheaply if
  not). No special-casing is needed: a fault records no observation, so the
  blocked op simply sits at the frontier.
- **errored, fresh**: policy choice — retry (a fresh run of the same
  generation, replaying the record) or fault. Default: retry once; if the
  retry also errors, fault.
- **stale** (whatever its execution status): fault immediately. v0 never
  silently discards a generation's state — the user explicitly clears it,
  and the next demand then executes a fresh generation.
- **unsettled** (whatever its execution status): fault, with the diagnosis
  naming the stale / running / cleared / errored roots the currency
  evaluation found in the support DAG.

**Freshness invariant (the v0 strict currency policy).** A new observation
never sees a cell that is not both complete and fresh: a demand either
blocks until the value is ready or faults (`CellFaultError`, poisoning the
demander). The fact a demand establishes is **value-pinned**: currency is
a property of a *(generation, results)* pair at one snapshot, never of a
cell's ambient state, and the demand *returns* the validated results —
the whole evaluation (walk + the values it vouches for) runs atomically
over one storage snapshot, and the demander serves and records exactly
what was checked. Check and read are one operation, so a concurrent
clear/re-run can never slip a different value in between them. This avoids input version skew — global correctness would
require re-running the stale input anyway. Execution gating is *pure
policy* over the derived currency, so relaxations are policy options, not
semantic changes; planned-but-deferred: a permissive mode (observe
anything — a completion computed over non-fresh inputs derives as
non-fresh automatically through the ordinary closure, with no extra
bookkeeping), an await-instead-of-fault mode for targets unsettled only
because their support is currently running, and a derived **settling**
state (would resolve fresh if every running task in the support resolves
to what its observers recorded) enabling optimistic downstream parking
when a stale task is retriggered.

Faults propagate as *interruptions*: a fault inside task A makes A
interrupted, which faults A's readers in turn, up to whoever demanded the
chain — so a single broken input surfaces at the top level as one root cause
plus a tree of interrupted dependents, not as cascading errors.

Longer-term, these branch points are consulted through a user-supplied
execution policy, possibly interactive ("clear and rerun these X stale
cells?").

## The safety pass

The outer level must be deterministic-given-the-record, and this is
verified, not assumed. When a generation's outer level completes (v0:
always, before the cell is marked `complete`; a per-task opt-out for
known-deterministic tasks may come later), the runner re-runs the outer
level and requires:

1. every effectful op is a hit (the frontier is never reached),
2. the set of ops performed equals the recorded set — observation
   subjects ∪ checkpoint subjects (nothing recorded is left unvisited),
3. the replay's would-create claim set equals the generation's stored
   grid assertions,
4. the generation's request edges match its recorded reads, cell for
   cell (every request answered, every read requested — see below), and
5. the returned outputs equal the recorded results.

By (1) the pass is pure compute with no live effects. A violation of
(1)–(5) is a `NondeterminismError` and poisons the task. But the pass
**re-invokes the task fn**, so its *setup* — grant minting, the task-fn
binding spawn and connect — shares a run's not-ready surface, and an
exception escaping the pass classifies by the same rule as one escaping
a run (the errored-vs-interrupted split below): a fault-class
`CellFaultError` escape (e.g. the binding vanished between the run and
the pass) records fault-class evidence and lands **interrupted** — it
means "couldn't finish", never "finished differently" — and the next
demand retries. Only genuine violations and error-class escapes gate
completion as `errored`.

**Every durable per-generation fact is compared at completion.**
Anything the engine persists as part of a generation's state —
observation rows, checkpoints, grid assertions, request edges — is an
obligation the replay must re-produce, or the generation is
nondeterministic; nothing durable is exempt. For (4): request edges are
written when the run *asks*, observations when the answer *arrives* —
so an interrupted run can legitimately have asked-but-unanswered
requests on file (the resume re-asks; the insert is idempotent), but
completion requires that asked and answered match exactly. The check
is a pure DB-side comparison, so the frozen pass stays write-free.

## Errors: errored vs interrupted

The poisoning contract (see `errors.py` for the full statement): any
workflow error raised by an outer-level op, and any error escaping a step
fn, is persisted at raise time and forces the generation into a *failed*
state, even if task code catches it. Failed states have two flavors, split
by whose fault it is:

- **errored** — the task itself went wrong: its own logic raised, an error
  escaped a step, or it violated integrity (write conflict, grid-config
  mismatch, view misuse, nondeterminism).
- **interrupted** — the task wanted something that isn't ready
  (`CellFaultError`: subject missing, errored, interrupted, stale, or
  unsettled) or its run was cut off externally (crash/shutdown).
  Conceptually: "pending, but with progress attempted and a recorded
  reason".

Both fault consumers who read them identically; they differ in top-level
surfacing (one root cause, a tree of interrupted dependents). The split
falls exactly along the error taxonomy: `CellFaultError` → interrupted;
everything else → errored.

**Failure records.** Failures are persisted per generation, *plural* —
concurrent branches of one run may each fault or error — and are written
by the **engine at raise time**, the side where the original typed
exception exists. This covers op failures of *any* type, not just the
workflow taxonomy: an unexpected internal error inside an op (an
engine/storage bug) records an error-class failure too — otherwise task
code catching its wrapped form could complete cleanly around an op that
failed mid-effect. Errors that cross a service boundary do so as fxtr2
``IRException`` transit forms and are informational only: nothing durable
is ever classified from them. While a run is in flight, "running with a
non-empty failure set" is a well-defined poisoned state (the run may
legitimately keep draining other branches); when the run actually stops,
the executor derives the terminal status from the failure set — any
errored-class failure → errored; only fault-class failures → interrupted;
an exception escaping the run with *no* recorded failure is classified by
the executor: a runner-side typed `CellFaultError` (the one raiser is the
task-fn spawn finding its binding missing — raised in-process, so typing
is trustworthy) records fault-class → interrupted, exactly like a step's
unbound-handle connect; anything else → errored (by
construction the task's own logic raised, since every engine-op failure is
recorded engine-side; the executor records it as a ``TaskRaisedError``
failure so the record stays complete).

Poisoning follows workflow *operations*, not levels. The one workflow op
inside steps is `connect`: a connect failure poisons the *step invocation*
at raise time — no checkpoint entry is written, even if step code catches
the error and returns a value — and fails the task per the error's class.
An unbound handle binding (`HandleUnboundError`) is a not-ready dependency
(→ interrupted: bind it and re-demand; completed checkpoints replay) — it
*must* poison, because bindings are untracked runner config, so a fallback
result computed around a missing binding could never be invalidated once
the binding appears. Connecting a key the generation never read is an
integrity violation (→ errored).

Steps are the retry unit — transient failures should raise (a later run
replays completed checkpoints and re-attempts the failed step); evidential
failures (refusals, parse issues) should be *returned* as data and flagged
by downstream checker tasks. A task *completing with* an invalid value is
the task asserting the error is representative/visible. Each checkpoint
cell admits **one ``run_step`` call per run**: a second call — concurrent
or sequential, identical invocation or not — is an integrity error
(→ errored); deduplicating identical work belongs in task logic. Across
runs the rule is per-run: a resumed run's single call is served from the
recorded entry as usual.

## Scheduling notes (v0)

- Single runner process. A `running` status found at startup is a crashed
  run and is flipped to `interrupted`; the generation simply resumes via
  record-check on its next demand.
- Fan-out: the runner keeps an *intent set* of demanded cells and a smaller
  *actively-running set*, feeding the latter in batches. Because runs are
  ephemeral and replayable, long-blocked executions may be evicted and
  resumed later.
- Demand cycles: under *request = observation* a cycle can never resolve —
  its members can only fault. Acceptable v0 behavior is to block until user
  interrupt, optionally with a background checker surfacing an informative
  diagnosis. (Rare; better handling deferred.)

## Embodiment (pointers)

The fxtr2 service-layer embodiment is captured as python interface sketches
whose docstrings are the writeup; the load-bearing commitments so far:

- **Scope tree**: runner scope (entity-store namespace attached; external
  handle bindings) → per-*run* scope → per-step scope (bounds a step's
  connected ServiceRefs structurally).
- **Entity-liveness invariant**: an FxtrID is recorded in the workflow state
  db only after its closure is persisted in the entity store, so the state
  db never dangles; a crash in between leaves only an unreferenced entity.
- **Service boundaries**: task fns sit behind a ServiceRef boundary
  (RPC-capable in principle); every effectful op proxies through one mounted
  *run capability* per run; steps are invoked as ServiceRefs given a
  narrower *step capability* and consume only plain data (handles cross as
  previously-read keys); task-side views are opaque tokens + local axis
  shadows — absolute spec paths never cross the boundary.
- **Storage**: two sqlite files side by side (workflow state db + fxtr
  entity store); v0 retains all entities — pinning/GC arrives with the
  version log. Handle *entities* are durable data; handle *bindings*
  come in two classes, both registered through the user capability and
  neither identity-bearing (``notes/sandboxed_bindings.md`` is the whole
  writeup): *ephemeral* bindings (``bind_handle``; connectors are
  ServiceRefs, so registration works over RPC) return a release
  capability (workstation ``ReleaseCallbackProto`` pattern: hold to
  keep, drop to auto-release, ``release()`` for graceful unbind) and are
  additionally bounded by the connector service's own lifetime;
  *sandboxed* bindings are durable runner-owned records (state db +
  exported bindings file), served by spawning the bound nix defn via
  ``fxtr_sandboxing``, removable by plain state mutation.
- **Top level**: the user is a holder outside the deterministic world —
  same view calculus, but no op record and no observations (the user sits
  outside the currency model entirely);
  writes/creates mark user-sourcing, and *targets* (durable declarative
  DAG-root state, decoupled from ephemeral materialization events) are part
  of exportable state. The top-level surface is a service protocol sibling
  of the run capability (`user_api.py`). The intent-axis model rooted in
  these facts — sourcing records, targets, and the derived wanted /
  would_start / grounded predicates — is `notes/grounding.md`.
