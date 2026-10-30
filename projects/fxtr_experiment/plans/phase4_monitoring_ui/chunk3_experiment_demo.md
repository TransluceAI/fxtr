# Chunk 3 — examples/fxtr_experiment_demo

**Status: complete** (one xfailed test tracking the fxtr2
teardown-hardening followup below; the crash/revive arc itself works
live and is browser-verified).

*(Swapped with the dashboard renderer in the chunk order, 2026-07-10:
the demo lands first, dashboardless, so the renderer gets built against
a live backend instead of blind. Until chunk 4 the capability renders
through the shell's fallback panels; the demo is fully exercisable via
the driver, and the demo doubles as the renderer chunk's manual-e2e
harness and bit-rot guard.)*

## Scope

- **Orchestrator** (patterned on the workstation demo's
  `run_demo_workstation.py`): persistence + `WorkstationServer` +
  mounts + Vite frontend; sandbox environment **flag-gated** (off by
  default — when on, the sandbox spawner is exposed exactly as in the
  workstation demo, for agent-driven task-fn spawns over MCP).
- **The runner mount**: a host-side service CM using the
  mount-lifespan pattern for per-mount state — `WorkflowSession` (two
  sqlite files under the demo workdir's data dir; **state survives
  relaunches**), runner scope, binding registry, executor, and the
  mounted user capability — registered as external name
  `experiment-workflow`. Relaunch demonstrates the startup
  running→interrupted flip and record-check resume (headlessly /
  through fallback panels until chunk 4 adds the dashboard).
- **The demo driver** (`experiment-demo-helper`, `demo_helper`
  pattern): a host-mounted service whose scenario methods drive the
  *same capability programmatically* — bind host-side task fns
  (connectors), create grids, write inputs, add targets, demand.
  Scenario set (exercising every status in the inventory):
    - a small `data → task → downstream task` pipeline (fresh /
      complete / wanted / grounded);
    - a slow multi-step **checkpointed** task (running; kill/relaunch
      → interrupted; re-demand → checkpoint replay);
    - a **flaky** task (errored; "Retry" exercises the demand
      policy's retry);
    - an **input overwrite** (stale + unsettled propagation, then
      resurrection on revert);
    - a **handle grid** with a bindable/releasable binding (boundness,
      `HandleUnboundError` → interrupted → bind → re-demand).
  Each scenario method returns the touched paths/records so the
  operator knows where to look.
- **The workdir preset**: the demo workdir flake with renderers for
  the demo's own entity types (at least the pipeline's data entities);
  driver panel renderer if useful (the workstation demo's
  scenario-picker shape). The dashboard slice is added by chunk 4 when
  the renderer exists.
- **Operator docs**: README covering launch, the driver panel, the
  relaunch/resume arc, and the sandbox flag (the dashboard panel joins
  in chunk 4).

## Tests

Kept light, matching the workstation demo's precedent (its pytest
suite covers task/scheduler *impls*, not scenario arcs — the demo is
itself essentially a test, driven from the browser):

- Unit tests for the demo task fns with real logic (the checkpointed
  multi-step fn, the flaky counter fn).
- One runner-mount smoke: mount opens over temp files, auto-binds the
  standard fns, one pipeline demand runs end-to-end.
- Interactive verification through the browser is the point of the
  chunk and is documented, not automated.

## Settled in the pre-chunk alignment round (2026-07-10)

- **Lean orchestrator**: WorkstationServer + the nix renderer
  dispatcher over the scaffolded workdir + the runner mount + the
  driver + the flag-gated sandbox env. No report builder, no docent,
  no LLM authorizers, no entity seeding; the agent-MCP persistence
  namespace only under the sandbox flag.
- **The workflow sqlite files live under the scaffolded workdir's data
  dir** and share its lifetime: `--auto-workdir` re-scaffolds (wipes)
  by design — for durable state, scaffold a workdir yourself and point
  `--workdir` at it; relaunching against the same workdir is the
  resume arc.
- **The runner mount auto-binds the standard demo task fns at mount
  time** (idempotent; handle key entities are content-addressed, so
  the ids are stable across relaunches and re-demand-after-relaunch
  works out of the box). The boundness scenario uses a dedicated extra
  handle outside the auto-bind set, with driver bind/release methods.
- **The driver is operated through a minimal scenario-picker panel
  renderer in the demo workdir** (the workstation demo's demo_helper
  renderer shape: one button per scenario, touched paths as results).
- **Demo entity types + the driver proto live as marker-style packages
  under `agent_workdir/packages/`** (host-importable via the sys.path
  convention; renderer slices reference their types via
  `mkEntityFromPython`); connectors, task fns, and the driver impl are
  host-side modules next to the orchestrator.

Notes/log:

- **Host core landed first, headless-green** (runner mount + task fns +
  driver + the two smoke tests): pipeline / staleness+resurrection /
  flaky-retry / boundness arcs, and the relaunch test proving the
  persistence + auto-bind promise (second mount over the same data dir
  serves everything — the fresh flaky instance's planted failure never
  fires, so nothing re-ran).
- **Scope-topology learnings** (all now documented in
  `experiment_runner.py`'s module docstring, found as real hangs/faults
  while wiring the mount):
  1. The host scope (task-fn adapter + connector mounts) must
     *enclose* the runner scope — the binding registry holds
     independent leases on the connectors, and an independent lease's
     source must close after its holder; nesting the host inside the
     runner deadlocks the host's drain (the keep-alive contract of
     `v2/notes/scope_service_lifetimes.md`, empirically confirmed).
  2. The host scope must be **non-isolated** (the test suite's plain
     `test-host` shape): capability-proxy arg lowering for connector
     ServiceRefs fails the registration walk against an isolated
     host's mounts.
  3. The driver mounts on a scope that **includes the runner scope**:
     scenario returns carry ids living there (read results, keys), and
     the driver's call scopes must lift them for the return packet.
  4. Entities passed as capability-call args store into the **runner
     scope** (the proxy's home scope) — the explicit-scope version of
     what the test suite's ambient-active-scope stores do.
- Per-test timeout guard: the demo's conftest adopts
  v2/tests/conftest.py's signal.alarm fixture (30s) — the first
  topology bug above surfaced as a silent hang without it.
- **Wire-level verification** (beyond the pytest smoke): a peer-RPC
  probe drove every scenario arc against the running demo through the
  real workstation boundary (the same one the browser crosses) —
  pipeline / stale+revert / flaky+retry / unbound(interrupted)+bind+
  rerun — and the crash arc end-to-end: SIGKILL mid-slow-run →
  relaunch shows the startup flip (interrupted) → re-demand resumes →
  complete. The probes caught two bugs the in-process tests missed
  (a flattened renderers/builtin dir breaking the preset build; the
  helper calling a nonexistent `Workflow.clear_cells` inside a
  `suppress(Exception)`).
- **Graceful shutdown drains in-flight runs** (pre-supervisor
  behavior, kept for the record): Ctrl-C/SIGTERM waited for a running
  demand to complete before exiting (the runner scope's drain).
  Superseded once the supervisor landed — orchestrator shutdown now
  crashes the runner (prompt interruption); the hard `kill -9` +
  relaunch variant remains the way to exercise the *startup flip*
  (nothing written at kill time; the reopen repairs `running` →
  `interrupted`).
- **The soft crash/revive arc** (added in a mid-chunk round with
  Daniel): the runner's lifetime moved onto a `RunnerSupervisor`
  (`experiment_runner.py`) — each runner is one supervised task whose
  cancel scope wraps the WHOLE mount, so "crash" is plain task
  cancellation and "revive" starts a fresh mount over the same files.
  The driver became **stable across crashes** (holds the supervisor,
  looks the runner up per call, exports returned ids into its call
  scopes since it no longer shares runner-scope ancestry, invalidates
  per-runner caches by supervisor epoch) and gained `crash_runner` /
  `revive_runner` buttons; `experiment-workflow` is registered
  per-revival via `expose_service` over a loopback peer-RPC session
  (which also exercises chunk 4's stub-is-a-re-derivable-cache
  contract). Semantics confirmed: a crashed run records the
  fault-class `RunnerCrash` failure → **interrupted**, and revive +
  re-demand resumes the same generation through its checkpoints — the
  executor's cancellation path handles all of it with zero engine
  changes. (Two rejected detours, for the record: force-closing the
  runner scope breaks steps as bare scope-closed RuntimeErrors →
  misclassified *errored*; cancelling only the demand *callers* never
  reaches the runs, since dispatch decouples work from callers.)

## Blocked-on followup: fxtr2 teardown is not cancellation-safe

Found implementing the soft-crash arc; needs fixing in **v2/fxtr2**
before the crash/revive feature can land (everything else in the demo
is complete and green around it).

**Symptom / reproducer.** With the supervisor code in this chunk's
working tree, `examples/fxtr_experiment_demo`'s
`tests/test_demo_smoke.py::test_crash_revive_resume` — crash a runner
mid-slow-run via task cancellation, revive over the same files,
re-demand. The *arc itself succeeds* (cell reads `interrupted`,
`reason=RequestAbortedError: request run_step cancelled before
resolution; RunnerCrash: runner crashed or was shut down mid-run`;
revive reopens the same sqlite files fine; the resume completes) — and
then the process's **final orderly teardown hangs forever** (the test
dies on the conftest 30s alarm).

**Diagnosis.** An `asyncio.all_tasks()` stack dump at hang time (a
watchdog task that sleeps, prints `task.print_stack()` for every task,
then `os._exit`s — a technique worth reusing) shows the crashed
runner's world leaving abandoned finalizer tasks parked forever: many
`FxtrScope._run_lifespan`s, sqlite `ConnectionWorkerPool.
run_worker_thread_forever` workers, `SQLiteFxtrSession`'s lifecycle
handler + `_cleanup_worker`, dispatch bodies
(`service_cap_common._dispatch_body_factory.<locals>.run`), and the
capability's `_EventBroadcaster._worker`. Cause: when the mount stack
unwinds **under a pending cancellation**, each `__aexit__`'s
finalization awaits get cancelled partway, abandoning these workers;
the parked tasks keep holding resources (pool refcounts, session
leases), and the eventual clean shutdown wedges on them.

**Fix direction.** fxtr2 already has the convention for exactly this —
**bounded shielded finalization**, `anyio.fail_after(10.0,
shield=True)`, used at `sqlite_namespace.py:484` and `:883` — it just
isn't applied on the paths this flow exercises. Sites to audit:

- `sqlite_namespace.SQLiteFxtrNamespace.get_pool_group`'s `finally`:
  the `await grp.aclose()` (skipped entirely under cancellation). The
  *registry bookkeeping* half of that finally is already fixed in this
  chunk's tree (try/finally so the `_pool_group_evts` /`_refcounts`
  cleanup can't be skipped — before that fix, a cancelled unwind
  poisoned the (path, mode) key for the whole process and any reopen
  died with "Connection pool … not found, likely failed during setup
  in a different task").
- `SQLiteFxtrSession`'s close path (the lifecycle handler + cleanup
  worker are among the parked tasks).
- `FxtrScope.__aexit__` / lifespan join (`_run_lifespan` tasks are the
  bulk of the graveyard).

One caution: a first attempt that wrapped `get_pool_group`'s
`grp.aclose()` in an **unbounded** `CancelScope(shield=True)` made an
unrelated test fail with a leaked `CancelledError` — never fully
diagnosed (reverted in favor of the design questions of the moment).
Use the bounded `fail_after(…, shield=True)` form, and re-diagnose if
the leak reappears.

**Suggested v2 regression test**: open an `FxtrScope` + sqlite-backed
session, start work, cancel the owning task mid-flight; assert (a) no
lingering tasks remain after the unwind (compare `asyncio.all_tasks()`
before/after, or park-detect with a timeout), and (b) the same db path
reopens cleanly in the same process.

**State at chunk close**: the supervisor + stable driver + crash/revive
buttons + the peer-RPC registration all landed and are browser-verified
(crash → panel survives + scenarios fail clearly → revive → resume);
`test_crash_revive_resume` asserts the full arc and is
**xfail(run=False)** on this bug (run=False because the failure mode is
a 30s hang). The registry-consistency half was fixed and committed
separately in v2 ("fxtr2: make get_pool_group's registry cleanup
exception-safe", with `tests/test_sqlite_store/
test_pool_group_manager.py` as its regression test); when tackling the
remaining finalization-completion half, un-xfail the demo test — it is
the end-to-end regression test for this whole followup.
