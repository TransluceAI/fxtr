# Chunk 3 — gates: the policy seam

**Status: complete** (landed 2026-07-09, in the option-(B) request-edge
form — see the landing record at the bottom). **Ran after chunk 4**
(2026-07-07 order swap — the behavior change lands once the
user-surface vocabulary its test updates need is in place). Resolutions from that round: **serve-or-fail
in one op** (a single session op evaluates both axes and serves the
value/results from the very snapshot the verdicts derive on — no
check-then-use window); **two policy modes** (``strict`` default /
``off``; local-only dropped — its only protection beyond off is
authorless data, its only relief versus strict is tolerating
downstream-bookkeeping flap); **launch-requires-target is emergent**
(strict gate + non-self-vouching user demands reject
launch-without-target with no separate precondition); serve time = the
re-check-on-arrival moment (mid-run revocations fault the demander at
arrival, like currency).

Grounding verdicts start affecting execution, per the gate table in
`notes/grounding.md`. Gating is pure policy over chunk 2's derived state;
this chunk adds the seam and the default policy, changing no definitions.

## The fresh-cascade vouching problem (worked example; 2026-07-07)

The design ground for how self-vouching must work. Setup: task grids P,
C, T in user-created (sourced) grids; **only P is targeted**; P's fn
demands C's output; C's fn demands T's output. The user demands P into
an empty world. Durable-state predicate values at each step, under
resolution-time observation recording (the pre-chunk-3 record
semantics — a ``read_out`` row is written when the demanded value
*resolves*):

| # | event | durable demand facts | wanted(P) | wanted(C) | wanted(T) |
| - | ----- | -------------------- | --------- | --------- | --------- |
| 1 | target(P) added | target(P) | **T** | F | F |
| 2 | user demands P; gen(P) starts | 〃 | T | F | F |
| 3 | P requests C (obligation registered — commitment; **no row yet**: the observation waits for C's value) | 〃 | T | F | F |
| 4 | executor starts gen(C) | 〃 | T | **F** | F |
| 5 | C requests T; the gate evaluates T's serve | 〃 | T | F | **F** |

At step 5 the strict gate needs ``grounded(T)`` ⇐ ``wanted(T)`` ⇐
``read_out(C, T) ∧ would_start(C)`` ⇐ ``wanted(C)`` ⇐ ``read_out(P, C)``
— and *no durable row for any of these edges exists yet*, because
nothing in a fresh cascade has resolved. C **is operationally running**,
but the evaluator derives from records, and the record is silent about
*why* C runs. The single-edge self-vouch (inject only (C, T)) fixes step
5's last link and still fails at ``wanted(C)`` — every cascade deeper
than one hop deadlocks.

Two structures close the gap:

- **(A) Thread the in-flight chain** (implemented first): the executor
  passes the whole pending-edge chain [(P,C), (C,T)] into the gate's
  evaluation as if recorded. Works, but: threading through every
  demand/read hook; pending evaluations must be derive-only (extras are
  not durable facts) and must distrust negative stamps; the in-flight
  demand structure is invisible to lint/UI (it lives in the executor's
  call stack).
- **(B) Record request edges at commitment** (the re-plan candidate):
  the fingerprint is what forces observations to wait for resolution —
  the *demand* fact is fingerprint-blind and irrevocable at arrival
  (request = observation's own commitment-point rule), so write a
  durable ``request_edges(exec_id, subject)`` row at record-**miss**
  time, before going live (miss-only keeps the frozen safety pass
  write-free; insert-or-ignore keeps aborted-then-resumed requests
  idempotent; rows cascade at turnover like every claim — an aborted
  request's surviving edge is *correct*, the resume re-requests at the
  frontier). ``wanted`` (and the grounded input conjuncts) then read the
  request record; at step 3 the edge (P,C) is already durable, so
  ``would_start(C)`` holds from state the moment C starts, and the whole
  chain apparatus (threading, derive-only mode, stamp distrust)
  disappears. The two axes then read two records: currency reads the
  *resolved-value* record (observations), grounding reads the *request*
  record.

## Scope

- **One serve-or-fail decision point**: task-output demands extend the
  executor's demand path; data/handle reads get an executor-provided
  read gate in the engine (the previously ungated channel — exactly
  where the gate has bite). Both consume a combined session op that
  evaluates currency + grounding under one lock hold and returns the
  value/results from that same snapshot, or raises.
- **Self-vouching requests**: a running generation's request is evaluated
  as if its edge were already recorded — for task-output subjects,
  `wanted` holds via the requester's `would_start` (the pending-reader
  parameter on `cell_grounding`, landing here with its consumer).
  User demands do NOT self-vouch (nothing durable justifies them) —
  the asymmetry is the point; the wrapper's auto-target default
  (chunk 4) is the ergonomic complement.
- **Policy knob**: ``strict`` (default — fault on ungrounded) /
  ``off`` (lint only), a session/bootstrap-level setting for v0.
- **Fault taxonomy**: grounding faults are fault-class (`CellFaultError`
  flavor → interrupted) with diagnosis chains naming the roots ("d's
  writer c is unwanted: no target, no current reader"; "grid g
  unanchored: rule references missing grid g'").
- **Structural mint gate**: grant minting against missing/incompatible
  `grid_dep` references faults immediately with the grounding-style
  diagnosis (this *is* mint failure, made immediate and well-diagnosed;
  always on, not a knob).
- User reads follow the same gate (they already follow the demand policy
  for currency); `inspect_cell` remains the labeled escape (its report
  fields land in chunk 4).

## Tests

- The design exhibits as scenarios: the cleared-driver window (strict
  faults a read of the still-fresh side-written value; ``off`` serves),
  and re-demand-after-settle convergence.
- Orphan reads faulting under strict, handle-sourcing
  revocation faulting new connects, the sub-workflow cascade running
  clean under strict (self-vouching), the emergent
  launch-without-target rejection (``target=False`` demand of a
  never-run unwanted cell faults at the gate), mint-gate diagnosis on
  deleted / config-drifted deps, the knob switching behavior with no
  state change.
- Safety-pass request-edge regressions: a request faults/aborts on the
  first try and the resumed run *skips* it yet completes → the safety
  check rejects the generation (replay failure, `NondeterminismError`
  evidence); the passing twin — the resume re-requests at the frontier,
  the edge set converges to the observations, the pass is clean.

## RESUME STATE (2026-07-07, end of session; audited + resolved 2026-07-09)

**Where things stand.** Chunk 3 is implemented as **option (A)** (the
pending-chain form) in the *uncommitted working copy*, sitting on top of
the committed chunk 4 ("fxtr expt phase 2 chunk 4: the grounding user
surface"). `jj diff` shows the whole of it; the package is
pyright-clean. The suite is **red on purpose**: 20 strict-gate-intended
failures (test_executor 13, test_executor_steps 5, test_coord 2 — their
harnesses seed grids/handle cells at session level with no sourcing
records or targets, so the run-prologue handle read correctly faults
orphaned).

**The interrupted subagent — AUDITED 2026-07-09.** An Opus test agent
was mid-task and was KILLED before finishing; the audit found it got
further than expected. (i) **Done**: the 20 strict-gate failures are
fixed the intended way — `test_executor.Env` (the de-facto shared
harness: `test_executor_steps.py` and `test_coord.py` import it) gained
`_source_grid` / `write_data` (write + sourcing upsert) / `target` /
an auto-targeting `demand` wrapper mirroring the `Workflow` default,
with every call site converted; `tests/boundary.py` correctly untouched
(it serves the bare-engine suites, ungated by design). Suite green (191
passed), pyright clean. (iii) The semantic tripwire passes:
`test_transitive_demand_runs_upstream_first` targets only the
downstream cell and completes — no vouching bug. (ii) **Not started**:
`tests/test_gates.py` (seven scenarios: cleared-driver exhibit, handle
revocation faulting the prologue, three-deep only-top-targeted cascade,
serve-time re-check after input revocation, the emergent
launch-without-target rejection, mint diagnosis, the off knob — the off
knob exercised by reopening the same state db via
`open_local(grounding_policy=...)`: behavior flips, stored
state/verdicts don't). Also still unverified: `typed_wire_call`
re-raises `UngroundedSubjectError` typed across the wire (should be
automatic — `errors._transit_types` walks the WorkflowError tree), and
the `inspect_cell`/CellReport field check. The main agent wraps these
up directly (no re-delegation).

**The design decision — option (B), CONFIRMED by Daniel 2026-07-09.**
See the worked example above for the problem and both options. The
rationale for (B) *request edges recorded at commitment*: `wanted`
becomes derivable from durable state at every moment (the record
contains *why* a cascade member is running); the executor threading,
the derive-only evaluation mode, and the negative-stamp-distrust rule
all disappear; lint/UI can see in-flight demand; request=observation
already declares arrival irrevocable, so nothing ever needs retraction
(aborted requests' surviving edges are correct — the resume re-requests
at the frontier; edges cascade at turnover); miss-only insertion keeps
the frozen safety pass write-free; and the axes split cleanly (currency
reads the resolved-value record, grounding reads the request record).

**Correction found on resume (2026-07-09): the safety pass DOES need a
new comparison.** The earlier claim ("for complete generations requests
⇔ resolved observations, so no new comparison") was circular — that
equivalence is a *determinism property across resumes*, exactly what
the pass exists to verify, never to assume. The failing case: a request
aborts unresolved (edge row, no observation row — e.g. the process
crashes mid-request), and the resumed run nondeterministically *skips*
that request and completes; the leftover edge is in none of the pass's
compared sets (observations ∪ checkpoints, assertions, outputs) and
survives into a complete, safety-passed generation as a phantom
``wanted`` edge. The governing invariant (to be stated in
`notes/execution_semantics.md`): **every durable per-generation fact is
compared by the safety pass at completion** — request edges included.
The fix is cheap and write-free: the pass already establishes
``visited == recorded observations``, so the new check is a pure
DB-side comparison — the generation's ``request_edges`` set must equal
the read-class projection of its observation record (at the cell
granularity edges are stored at), either direction failing as a
``NondeterminismError`` (edge-without-observation is the dangling
aborted request; observation-without-edge is a missed insert, a system
bug caught by the same comparison). Crash symmetry rides along: edges
are durable at commitment, so task-level cancellation and whole-process
crashes leave identical rows (a further advantage over (A), whose
call-stack chain a crash simply lost).

**If (B) is confirmed, the rollback/replace scope** (all uncommitted —
option A was never committed, so this is working-copy surgery, not
history):

- *Remove/replace*: `grounding.PendingEdge` + the `pending` param and
  `trust_negative` plumbing in `load_component` / the four `_load_*`
  loaders / `_expand_gen_reads` + `evaluate_component(write_stamps=)`;
  the executor's chain threading (`demand(pending=)`,
  `_run_once(pending)`, the `child_demand` closure's chain extension,
  the read-gate closure's pending arg); the `pending` params on
  `WorkflowSession.serve_check` / `gated_read_cell`; the "pending-edge
  evaluations are derive-only" bullet in `grounding.py` (d) and the
  transitive-chain paragraph in `notes/grounding.md`'s self-vouching
  bullet (both get *simpler*: "requests are recorded at commitment, so
  a cascade's demand structure is always durable"); the chunk-3
  discussion-log entry's sharpening paragraph (rewrite for B).
- *Keep unchanged*: the policy knob (`Executor` / `open_local`
  ``grounding_policy``), the serve-or-fail ops themselves
  (`serve_check` / `gated_read_cell`, minus pending), the engine's
  `ReadGateHook` seam (`read_gate=None` = ungated bare engines),
  `UngroundedSubjectError`, the mint-failure grounding diagnosis, the
  `_serve_gate` complete-and-about-to-serve rule.
- *Add for B*: schema v6 — ``request_edges(exec_id → task_execs
  CASCADE, grid_path, coords, output-ness/kind flag, PK(exec_id,
  location))`` covering BOTH task-output demands and data/handle read
  requests (wanted reads the task-output rows; `grounded_task_cell`'s
  input conjuncts read all rows — the *requested* set, a superset of
  resolved reads only for non-complete generations, and the more
  faithful one); insert at record-**miss** time before going live
  (insert-or-ignore; a hit implies the edge exists from the original
  miss; frozen engines never miss, staying write-free), with a
  WANTED(subject) grounding mark on task-output inserts; the component
  loader's reader queries switch from the observations join to
  `request_edges`; `grounding.py`'s I-G1 table + `notes/grounding.md`'s
  fact table (read_out / read_data become request-record facts) updated
  accordingly.
- *Add for B (the safety-pass correction above)*: the completion-time
  request-edge comparison in `Executor._safety_pass` (DB-side,
  write-free: ``request_edges`` == the read-class cell projection of
  the observation record, both directions, `NondeterminismError` on
  mismatch); **regression tests** for the dangling-request case — an
  observation faults/aborts on the first try, the resumed run *skips*
  that request and completes, and the safety check must reject the
  generation as a replay failure (plus the passing twin: the resume
  re-requests at the frontier and the edge set converges); and a brief
  note in `notes/execution_semantics.md`'s safety-pass section stating
  the general invariant (every durable per-generation fact — record
  rows, checkpoints, assertions, request edges — is compared at
  completion; requests ⇔ resolved observations is *enforced* for
  complete generations, not assumed).
- *Unaffected*: the interrupted agent's test work — harness
  sourcing/targets and all seven gate scenarios assert behavior
  identical under (A) and (B), including the only-top-targeted cascade.

**Resume order** ((B) confirmed; audit done): the (B) swap incl. the
safety-pass comparison → suite re-green (the harness work is
mechanism-agnostic and carries over) → `tests/test_gates.py` + the
safety-pass regression tests + the two deferred verifications → docs
per above (+ the execution-semantics invariant note) → `jj pre` →
commit chunk 3 in one piece.

## LANDED (2026-07-09) — final state and deviations

Everything above executed as planned: schema v6 ``request_edges`` (+
location index; `storage.py` docstring section "Requests are recorded at
commitment"), the loaders' reader queries moved to the request record,
the (A) apparatus removed wholesale, the safety pass gained the
request-edge comparison (with the regression pair in
`tests/test_executor.py`), `tests/test_gates.py` carries the seven
scenarios, and the docs updated (`notes/grounding.md` fact/gate/trigger
sections + status; `notes/execution_semantics.md` invariant note;
`grounding.py` I-G1/(c)/(d); CODE_OVERVIEW; the discussion-log entry
rewritten for (B)). Verified along the way: `typed_wire_call` re-raises
`UngroundedSubjectError` typed (exercised by the gate scenarios driving
the `Workflow` wrapper), and `inspect_cell`/`CellReport` serve the
grounding verdict + reason from one snapshot.

Deviations / ride-alongs found in implementation:

- **User data/handle reads are now actually gated** (`user_service.py`
  → `gated_read_cell` under the executor's policy): the scope bullet
  "user reads follow the same gate" was in the plan but the (A) working
  copy had not wired it. One pre-existing sourcing-surface test updated
  to the doctrine (read of a revoked-sourcing cell faults; `inspect` is
  the labeled escape).
- **Mint-gate enrichment catches `ViewAxisError` too** (`executor.py`):
  structural mint failures against a config-drifted dep raise
  `ViewAxisError`, not `CellFaultError`; when the grid is ungrounded the
  enriched fault-class form is raised instead (world drift is a
  not-ready dependency, not task error).
- **Stamped negative verdicts serve their stored diagnosis**: the
  grounding loaders carry ``grounding_reason`` so reports no longer say
  "(stamped)" when a verdict is served from its stamp.
- The white-box `tests/test_grounding.py` seeding helpers record request
  edges alongside observations (faithful to what a resolved read leaves
  behind).
