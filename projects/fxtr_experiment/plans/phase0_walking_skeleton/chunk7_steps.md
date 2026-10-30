# Chunk 7 — steps and checkpoints join

Scope: the inner (nondeterministic) level, per the decided semantics.

- `open_checkpoint` (internals-only, already enforced by the engine) +
  `run_step` end to end: record-check on the checkpoint cell (invocation
  = step service type id + args fingerprint; mismatch →
  NondeterminismError), miss → per-step scope + mounted
  `StepCapabilityProto` → invoke step ServiceRef → persist-then-record →
  serve.
- Step capability: `connect` (authority = handle-grant reads in the
  generation's record; `HandleUnboundError` poisons the *step invocation*
  → task interrupted), `log` (stub ok).
- Step args + results as entity-encoded payloads (closure pinning).
- Author-side sugar: `@step_fn(type_id=...)` / `@task_fn` decorators over
  `__call__`-style fns.

Design (settled in the 2026-07-04 chunk 7 round; see the discussion log
for rationale):

- **Seam**: one op record conceptually, two entry shapes physically —
  checkpoints get their own in-memory map beside `_record` (they don't
  participate in currency), sharing the discipline surfaces: `visited`,
  `_guard_frontier`, the safety-pass set comparison over the union.
- **Host-style**: the engine takes a `StepHost` (parent scope + binding
  registry — injected authority, no logic) and owns the whole miss path,
  persisting args/value entities from inside the per-step scope before
  recording. The safety pass's frozen engine gets a refusing host.
- **Policies**: one `run_step` call per checkpoint cell per run — a
  second call, concurrent or sequential, is `DuplicateStepError`
  (errored; dedup belongs in task logic; also closes the safety pass's
  set-comparison blindness to call multiplicity); `ServiceRef`s rejected
  in step args and results (not durable; handles cross as keys —
  `StepPayloadError`); errors escaping the step fn → `StepRaisedError`
  (errored); connect poisoning via a per-invocation poisoned flag
  consulted after the step returns (no checkpoint entry even if caught).

Pieces:

- [x] 7.1 — encoding/storage plumbing: `StepPayloadEntity` (service-data
      ↔ entity round-trip with contained-id refs; ServiceRef rejection),
      checkpoints-by-exec accessor, new error classes.
- [x] 7.2 — engine: checkpoint entries join the loaded record, full
      `run_step` (record-check + invocation comparison + frontier +
      StepHost miss path + duplicate-call error + poisoning),
      `StepCapabilityImpl` (connect authority via the record + bindings;
      log → event log); direct-engine tests with a trivial host.
- [x] 7.3 — task side: `CheckpointView.run_step` through the wire
      (mounting plain step fns), the `@step_fn` / `@task_fn` decorators.
- [x] 7.3 adjustment (discussed, then landed as the hybrid): collapse the
      adapter into the decorator so that the decorated function _is_ the
      TypedServiceImpl object we mount, with `__call__` on the impl acting
      like the undecorated fn locally (not exposed as a service method).
      Hybrid shape: `TaskFn`/`StepFn` renamed ``run`` → ``__call__`` (a
      task/step fn is any async ``(ctx, /, **args)`` callable — plain fns,
      class impls, and the adapters all satisfy one protocol); the
      decorators construct the adapters directly (``@task_fn`` ≡
      ``TaskFnAdapter(fn)``, ``@step_fn(type_id=...)`` ≡
      ``StepFnAdapter(fn, type_id)``; the ``_Wrapped*`` middle layer is
      gone; manual wrapping is preserved and identical); decorators moved
      next to the impls (task_service.py / view_runtime.py — task_api
      stays pure protocols; author-import ux deferred). Bonus: step-fn
      mounts are memoized per execution scope
      (``StepFnAdapter.mount_on``, weak-keyed by scope — fxtr2 sanctions
      one impl mounted into many scopes, per-mount state rides each
      mount's lifespan).
- [x] 7.4 — executor + safety pass over the union + the property tests.
      Detailed spec (written before pausing; everything here is settled):

      **Executor wiring.** The main run's engine gets
      ``step_host=StepHost(scope=run_scope, bindings=self._bindings)`` —
      per-step scopes are children of the *run* scope (decided scope
      tree). Implementation gotcha: ``_run_once`` currently loads the
      engine *before* opening the run scope; restructure so the run scope
      opens first (engine load inside it, or host injected after — prefer
      opening the scope first, it changes nothing else). The executor's
      module docstring loses its ``TODO(chunk 7)`` and the "tasks that do
      pure computation" phrasing (steps are supported once this lands).

      **Safety pass.** The frozen replay engine keeps ``step_host=None``:
      a checkpoint miss cannot legitimately happen (``_guard_frontier``
      fires first), and if a frontier hole ever let one through, the
      hostless miss raises ``InternalRunnerError`` — the same
      bug-not-user-code layering as the refusing demand hook. The set
      comparison extends over the union: ``recorded`` = observation
      subjects (``load_record``) ∪ checkpoint subjects
      (``load_checkpoints`` → ``(grid_path, coords, "")``); ``visited``
      already contains checkpoint subjects (run_step adds them).
      Invocation equality on replayed hits is automatic (hit-mismatch →
      NondeterminismError, recorded by the op wrapper). The replay
      re-mounts task-side StepFnAdapters (harmless — never invoked) and
      re-stages args payload entities per call (unpersisted staging;
      also harmless).

      **Property tests** (through the executor):
      - Flagship, cancel-based: a 3-step task (three checkpoint keys or
        coords) with gate events; cancel the demand mid-step-2 →
        RequestAborted → interrupted (step 2's checkpoint unwritten);
        re-demand → step 1 HIT (its fn ran exactly once overall), steps
        2–3 run, task completes; step 2's fn ran twice (aborted + real).
        Mind the safety pass doubling task-fn invocation counts (not
        step counts — the pass replays steps as hits).
      - Flagship, crash-simulated: after an interrupted attempt with
        step 1's checkpoint persisted, manually ``set_status(exec_id,
        "running")``, close + reopen the session (startup flip →
        interrupted + a ``RunnerCrash`` fault row), re-demand → resume
        serves step 1 from the durable checkpoint.
      - Poisoning through the executor: unbound-handle step
        (caught-and-swallowed variant) → demand faults, cell
        ``interrupted``, ``HandleUnboundError`` fault row; then bind the
        handle + re-demand → resumes and completes (the poisoned
        invocation wrote no checkpoint, so the step re-runs).
        Connect-authority violation → ``errored``.
      - Safety pass catches a nondeterministic *invocation*: a task
        computing different step args on the replay → hit-mismatch →
        ``NondeterminismError`` → errored.

      **Explicitly not in 7.4**: currency × checkpoints (none — entries
      don't feed currency, decided); engine-level step behavior (7.2's
      direct-engine tests own it); typed re-raise ergonomics (the
      cross-cutting item noted under 7.3).

Tests: checkpoint replay across runs (the flagship, both ways: cancel
mid-task after step 1 of 3 + crash-simulated session reopen; resume,
assert step 1 served + steps 2–3 run), invocation mismatch detection,
unbound-handle step poisoning (caught-and-swallowed still poisons),
connect-authority rejection for an unread key, duplicate same-cell
run_step error (sequential and concurrent), ServiceRef-in-args rejection.

Notes/log:

- **7.1** done (payload entity, accessors, error classes; the pinning
  test proves embedded ids' closures survive a session reopen).
- **7.2** done. Notes:
  - Connect authority is enforced via an in-memory per-run set populated
    by ``read_handle_key`` (hit or miss): program order guarantees a
    resumed run replays its handle reads before any step connects the
    key, so the set is exactly "keys this generation read from handle
    grants" without needing grid kinds in the loaded record.
  - Poisoning responsibility (refined in review, replacing a first cut
    that re-raised the original exception with identity dedup):
    **registered exactly once, at the raise site that originates the
    error** — ``connect`` records its own failures at raise time (the
    poisoned-but-running state is durable even if the step then hangs),
    and a poisoned invocation surfaces to the demanding task as a fresh
    ``StepInvocationFailedError`` — a *signal*, not a second poisoning:
    chained ``from`` the cause (or from what escaped the step), naming
    the cause class in its message, and by type contract never recorded
    by the op wrapper. Two events, two exceptions, one failure row.
  - The step ref arrives through the op's dispatch scope and is bridged
    into the per-step scope (dest nested inside the in-flight source
    call — the sanctioned ``independent_lease`` arrangement).
  - ``record_checkpoint`` (record-only session accessor) exists because
    the two payloads persist from *different* scopes (args: dispatch;
    value: the step's return arena, which dies with the step scope) — a
    single combined persist-then-record helper couldn't express that.
  - ``log`` writes to the passive event log (kind ``step_log``).
  - ``InternalRunnerError`` moved to ``errors.py`` (the engine needs it
    for hostless misses; executor imports it from there).
- **7.3** done. Notes:
  - ``StepFnAdapter`` lives in ``view_runtime.py`` (not ``task_service``,
    which imports view_runtime — the runtime is where the checkpoint view
    mounts it anyway). Its service type id IS the step's declared
    identity; the adapter is mounted on the task fn's execution context
    (the user-boundary active scope), which outlives the call.
  - An undeclared plain fn is a ``TypeError`` at the view (task-side
    misuse → escapes → ``TaskRaisedError``/errored at the run level).
  - ``_StepContext.connect`` retypes to ``OverrideThisTypeBeforeUsing``
    per the task_api contract; ``open_channel`` stays unplumbed (the
    contract tracks it; string ``log`` is the implemented path).
  - Typed re-raise of workflow errors task-side (the runner_api docstring
    promises user code the documented exception types; today transit
    ``IRException``\ s pass through the views) remains an unimplemented
    cross-cutting ergonomics item — noted for a later pass, not step-
    specific.
- **7.3 adjustment** done (see the checked item above for the full shape;
  rationale in the discussion log's 2026-07-05 round).
- **7.4** done, per the spec above (run scope opens first, engine loaded
  inside it with the StepHost; frozen replay stays hostless; ``recorded``
  extends over checkpoint subjects; all five property tests through the
  executor, in ``tests/test_executor_steps.py`` reusing
  ``test_executor.Env``). Notes:
  - With ``engine`` now bound inside the try, the executor distinguishes
    an engine-*load* failure (``InternalRunnerError``, errored — the fn
    never ran) from the task-raised path; cancellation during the load
    records ``RunCancelled`` as before.
  - **Real bug found by the crash-simulated flagship**: served ids were
    coverable only by in-process residue. An engine read (hit or miss)
    returns a state-db-recorded id, and the service return packet must
    lift its node from the dispatch scope's ancestors — within the
    writing process that works because the original persist left the
    closure tracked, but a *resumed session* has no tracked node and the
    lift fails (``Node ... was not found in any parent scope``). Fix:
    ``WorkflowSession.ensure_loadable`` (untyped ``namespace.retrieve``
    into the entity scope — lazy, idempotent, copy-free re-attach; the
    liveness invariant guarantees the pin exists), called by the engine's
    ``_read`` on every served id, hit and miss alike. One test
    (``test_read_coherence_and_resume``) simulated a user edit through
    the *raw* db helper, bypassing the pin — updated to use the real
    ``write_cell`` (which is what an actual privileged edit does).
