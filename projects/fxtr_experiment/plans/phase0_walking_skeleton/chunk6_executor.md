# Chunk 6 — executor: deterministic-only tasks end to end

Scope: the generation lifecycle, for tasks that do pure computation over
their reads (no steps yet — that's chunk 7).

Design inputs: `notes/execution_semantics.md` (updated 2026-07-04 with the
currency model, request = observation, failure records, and the run
prologue) and the two chunk-6 design rounds in `notes/discussion_log.md`.
A first executor attempt predating those rounds was abandoned (kept as a
conflicted WIP commit for grounding); its main flaws were classifying
durable status from wire-crossed `IRException` categories, an `on_results`
callback threaded through the run function instead of explicit scope
ownership, and rule-reconstruction logic living in the executor. Its
useful shapes — the recursive currency evaluation and the grants-from-rule
reconstruction — return in relocated, reworked form.

## Pieces

- **Storage**: per-generation `failures` table (engine-written at raise
  time; *plural* — concurrent branches may each fault/error), replacing
  `task_execs.reason`; `errors.TaskRaisedError` is the executor-recorded
  class for exceptions escaping a run with no engine-recorded failure.
  State-store accessors + tests.
- **Engine**: failure insertion at raise time (the raising side; nothing
  is ever classified from errors that crossed the wire); request
  *obligations* — op arrival is the commitment point, task-side
  cancellation cannot retract it, and the run-completion gate is
  "obligation set drained + failure set empty".
- **Spec layer**: grants-from-rule translation (stored rules are already
  holder-frame specs) moved out of the executor into
  `view_spec.py`/`spec_registry.py`, including the internals spec and
  received-axis kind reconstruction (proper `isinstance` checks).
- **Executor**: the demand path per the policy matrix (not-yet-run →
  execute; running → await + currency-check on arrival; interrupted+fresh
  → resume; errored+fresh → retry once; stale/unsettled → fault with
  diagnosis); run orchestration *owning the per-run scope* (spawn the task
  fn via the binding registry, run prologue reads the task-fn cell through
  the engine, persist results from inside the run scope before it closes,
  terminal status derived from the failure set); generation turnover on
  clear. The executor's demand method doubles as the engine's demand hook
  (transitive demands recurse).
- **Currency evaluation**: memoized three-valued recursion (fresh / stale
  / unsettled; the in-progress marker *is* unsettled, so cycles need no
  special case), per-output, derived on demand.
- **The safety pass**: replay-to-end with all-hits + set-equality +
  output-match, always-on; the run prologue makes the fn-cell observation
  replay like every other op (no exemptions).

## Tests

First real end-to-end (input cell → task → output) through the binding
registry; currency (overwrite → fault; clear → fresh generation; revert →
resurrection); unsettled propagation through a 2-task pipeline (stale
input → dependent unsettled, resolving both ways as the upstream rerun
reproduces or changes the value); interrupted cascades (one root cause, a
tree of interrupted dependents); poisoning survives task-side catching
(catch-and-return-normally still fails the generation);
early-return-with-pending-demand ends interrupted (request = observation);
safety pass catches an intentionally nondeterministic task.

Notes/log:

- **6.1 (failures storage)** done: `failures` table + `FAILURE_*` int
  enums, `task_execs.reason` dropped (schema v2, no migration), the
  startup flip records a fault-class ``RunnerCrash`` failure per flipped
  generation, `record_failure`/`load_failures`/`clear_failures` accessors.
  Failures are evidence about the *last* attempt: the executor clears them
  at the start of each new run (a resumed/retried run must be able to
  complete cleanly); history-keeping, if wanted later, is the event log's
  job.
- **6.2 (rule → grants)** done: pure translations in `view_spec.py` (with
  a section explainer decomposing the canonicalizer relationship into
  reversible type translation vs one-way frame translation),
  `mint_rule_grants` in `spec_registry.py` over a ``ConfigLookup``
  callable (keeps the spec layer free of a state-store dependency),
  `WorkflowSession.load_config`. The WIP's ``type(t).__name__`` check was
  just a missing import; proper ``isinstance`` now.
- **6.3 (engine poisoning + obligations)** done: `_effectful_op` wraps
  every public engine op — obligation registered as the first
  *synchronous* statement (anyio cancellation only lands at awaits, so
  the commitment point is atomic without locks or shields; this matters
  in-process, where dispatch runs in the caller's task and cancellation
  propagates — RPC drops it anyway), workflow errors persisted at raise
  time + mirrored on ``run_failures``, obligation discharged in
  ``finally``. Gate for the executor: ``wait_obligations_drained()`` +
  ``run_failures``. A committed op cancelled before resolution records a
  fault-class ``RequestAbortedError`` failure and re-raises immediately —
  cancellation never blocks on in-flight work (design round 3, replacing
  a first version that shielded the whole op body); the only shield is
  the narrow one around the failure-record write, which may run with a
  cancellation already pending. Executor note: a run cancelled *between*
  ops leaves no failure rows — classify cancellation exceptions as
  interrupted directly, don't fall through to escape-means-errored.
- **6.4 (executor core)** done: `executor.py` + the first real end-to-end
  test (`tests/test_executor.py`). Findings:
  - The "errored → retry once" row is per *demand*: a demand of an
    errored cell IS the retry (one fresh run; fault if it fails again) —
    the first cut looped twice per demand, which misread the policy.
  - Generation minting reads the fn cell via the session purely to
    populate ``current_fn``/``executed_fn`` (bookkeeping); the prologue's
    engine read is the one recorded observation, and a mismatch between
    the two (concurrent fn-cell edit mid-start) is caught by a safety
    check → fault-class ``TaskFnChanged`` failure, interrupted. On
    resume the check cannot fire (the prologue hits the record).
  - Bindings with a pre-selected task output (``SliceTemplate.output``)
    surfaced a translation bug: the first cut granted them as task-kind
    shadows, inventing "a task view with a selected output" — a shape the
    user-facing calculus deliberately has no such thing as
    (``TaskView.output()`` yields a Data(Frame)View). Fixed at the mint
    chokepoint: a selected-output task spec presents as its output's
    *value* kind (data / dataframe shadow, schema from the declaration,
    no outputs map) while the true spec keeps ``kind="task"`` + output;
    the engine's task-read checks now consult the *sliced spec's* output
    (covering both the wire-selection path and the pre-selected path).
    An interim ``select_output`` idempotence band-aid was reverted —
    strict double-select is correct again since a spec can never
    legitimately be re-selected.
  - ``run_task_over_engine`` dissolved into the executor;
    ``tests/boundary.py`` keeps a minimal one-run helper for the
    boundary-focused tests (what they assert — adapter behavior, wire
    transit form, raise-time poisoning — is product code; the helper is
    scaffolding).
  - ``resolve_templates`` extracted from the engine's ``_resolve_cell``
    (module-level, shared with the executor's mint-time fn resolution);
    ``get_generation`` now also returns ``executed_fn``.
- **6.5 (currency)** done: `currency.py` (memoized three-valued recursion
  per the currency section of the semantics note; ``CurrencyReport``
  carries the first-cause diagnosis) + the demand policy's currency rows
  in the executor (``_currency_gate``: strict — stale/unsettled fault
  whatever the status; applied before serving, after awaiting an
  in-flight run, and after a run completes, which is the stale-on-arrival
  check). Notes:
  - Stale short-circuits; unsettled keeps scanning (a later definite
    mismatch upgrades the verdict) — stale is settled evidence, unsettled
    is absence of evidence, and support verdicts only ever dampen to
    unsettled, never condemn.
  - A completed generation missing the observed output name counts as
    settled-on-a-different-value (stale), same as a value mismatch.
  - Tests: executor battery (overwrite→fault with nothing discarded;
    revert→resurrection; clear→fresh generation; unsettled pipeline
    resolving both ways; grid-delete staleness + path-identity
    resurrection via identical recreate; mid-run edit → coherent result
    lands but arrives stale) + unit tri-state over hand-built records
    incl. the observation cycle parking at unsettled.
- **6.7 (safety pass)** done — chunk complete: the engine gains a
  ``frozen`` mode (a record-miss effectful op raises
  ``NondeterminismError`` instead of going live, enforcing "every op is a
  hit" *by construction* — a failing pass performs no stray effects,
  rather than detecting them after the fact), and the executor replays
  the outer level against a frozen engine + refusing demand hook, then
  compares visited-vs-recorded op sets and replayed-vs-recorded outputs.
  The run prologue replays like any other op, so no subject is exempt
  from the set comparison. Violations record NondeterminismError
  failures (raise-side when an engine op tripped; executor-side for the
  comparisons) → errored. Consequence baked into the tests: every
  completed generation invokes its task fn twice (run + pass). Tests
  cover all three violation modes (divergent reads via the frozen
  frontier, omitted ops via the set comparison, divergent outputs).
- **6.6 (value-pinned currency + epoch memo)** done, per design round 4:
  single-snapshot sync-body evaluation (`run_readonly`; the writer lock
  is the snapshot mechanism in v0), reports carry the validated
  ``(generation, status, results)``, `Executor.demand` returns that
  results map, the `DemandHook` contract returns it, and the engine
  serves + records from exactly it (also closing the pre-existing hole
  where already-complete upstreams were served status-only without a
  currency check). ``change_epoch`` + `CurrencyEvaluator` cache verdicts
  across queries within an epoch. The post-v0 materialization plan
  (dirty bits + lazy re-verification in the db) is a TODO in
  `currency.py` and a followup in `design_overview.md`.
