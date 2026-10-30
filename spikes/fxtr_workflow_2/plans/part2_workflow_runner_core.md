# Part 2 — workflow runner core (no nesting)

Lands the `WorkflowRunner`: two namespaces, run groups, the slot / step-context
impls, the job + requester machinery, cache keying, result persistence, and
op-log events — **without** nesting. `TaskStorage`-as-arg and
`step_ctx.subslot` are wired enough to exist but raise `NotImplementedError`;
`step_ctx.log` works. No deletion (part 4).

Because there is no nesting, there are no child jobs: `depends_on` is always
empty, poisoning never fires, and the only failure kinds are `failed` /
`interrupted` / `cancelled` / conflicts. This keeps part 2 focused on the
single-level slot lifecycle.

Builds on the protocols/entities from part 1 and the
`BasicStatelessServiceProvider` (the test spawner). Design:
[design/workflow_runner.md](design/workflow_runner.md).

## Chunking (self-contained review slices)

Part 2 is split into three sequential, independently-reviewable chunks:

- **2a — entities + cache-key canonicalization** ✅ *complete.* `entities.py`
  (`RunGroup`, `WorkflowTaskInvocation`, `WorkflowTaskComplete`, op-log events)
  + `cache_key.py` (`canonicalize` / `build_invocation` / `strip_provenance`),
  with pure unit tests (`tests/test_workflows/test_invocation.py`). No runner.
- **2b — runner skeleton**: lifecycle (two namespaces, child scope, job task
  group, op-log append helper) + run groups (`start_run_group` /
  `RunGroupProto.get_storage` + open/close events) + slot / step-context
  mounting (`subslot`, `log`); `run` not yet executing. Structural; no hard
  concurrency.
- **2c — job/requester execution core + persistence** ✅: dedup, cache
  hit/miss/conflict, callers parked on shared jobs, completion +
  result-provenance projection + `Complete` persistence + `ResultRecorded` /
  failure events. Each job runs in its own isolated scope (scope discipline).
  Tests: `tests/test_workflows/test_runner_exec.py`.

- **2d — execution-core completeness** ✅: closed the gaps left by 2c (see
  below). The unifying move: **op-log emission is the *requester's* concern; the
  realization map is the *job's* concern.** Done — symmetric per-request
  `RunStarted`/`RunCompleted` (dropped `ObservedResult`); job returns a
  structured outcome (no throw-catch-rethrow); `"conflict"` outcome +
  `TaskConflictError`; full-runner-cancel writes `RunCompleted(interrupted)`;
  spawner resolved in the job scope; per-run-group scope for slot mounts.
  Tests in `test_runner_exec.py`. **Part 2 complete.**

### Chunk 2d

The single insight: relocate op-log emission from the job to the **requester**
(the `run` dispatch, attributed to its run group). The job becomes pure
execution + realization-map persistence and *returns a structured outcome*; the
requester records the op-log and maps the outcome to one raised exception.

1. **Symmetric per-request events.** Every `run` call emits its own `RunStarted`
   (before it waits) and `RunCompleted` (when its wait resolves), attributed to
   its run group — whether it started the physical job, joined an in-flight one,
   or cache-hit. Each run group's log slice is self-contained and robust to the
   shared job failing/cancelling. `RunStarted` carries `launched_job: bool` (did
   *this* request start the physical job, vs join/observe). **Rename**
   `JobLaunched` → `RunStarted`, `ResultRecorded` → `RunCompleted`; **drop**
   `ObservedResult` (folds into `RunCompleted`). No execute-vs-observe marker on
   `RunCompleted` — results are symmetric (any waiter could have produced it).
2. **Job returns an outcome; clean `_run_job`.** `_run_job` runs the task fn +
   pins the `Complete` and produces a structured outcome
   (`success(result_id)` | `failed(reason)` | `conflict(reason)`) — never
   raising for expected outcomes (fixes the throw-catch-rethrow). The requester
   maps the outcome to a single `RunCompleted` event and a single raised
   exception (`TaskConflictError` for conflict, `TaskFailedError` for failed).
   Add `"conflict"` to `WorkflowOutcome`. This is the clean `_run_job` /
   requester split (job = realization map; requester = op log + error shaping).
3. **Cancellation.** A requester whose wait is cancelled emits
   `RunCompleted(outcome="interrupted")` best-effort (shielded; op log still
   open), then re-raises. Full-runner cancellation thus records interrupted
   completions per run group. (Per-run-group cancellation machinery stays
   part-3+.)
4. **Resolve inward + per-run-group scope.** Resolve the spawner in the **job
   scope** (where it's used), not the runner scope — drop `_spawner_proxy`. Give
   each run group its **own scope** (child of the runner scope); mount its slots
   there and close it on `run_group` CM exit, bounding slot lifetime to the group
   (callers resolve slot refs against `RunGroupHandle.scope`). (Note for part 3:
   how a nested task fn — whose job scope descends from the runner scope, not a
   group scope — resolves a slot arg is a part-3 concern.)
5. **Tests:** symmetric cross-group attribution (two groups racing one slot each
   get started+completed), `launched_job` flag, conflict outcome + error type,
   full-runner-cancel writes interrupted.

**Part 2 will be complete after 2d** (single-level workflows, with a faithful op
log). Part 3 adds nesting (incl. the requester-task layer — deferral (a)).

**Scope discipline** (`notes/scope_discipline.md`): the runner follows it — its
scope is `isolated=True` with explicit `include`s (the namespaces' scopes), no
implicit active-scope use, refs documented with membership rationales. 2c
extends this: **each job runs in its own isolated child scope** (include the
runner scope) so in-flight entities are isolated per job; the result is copied
out / pinned, and the job scope is torn down after.

## Steps

### 0. Entity definitions + cache-key canonicalization (chunk 2a) ✅

Done in `entities.py` + `cache_key.py`. The invocation's `args`/`kwargs` are
stored as `FrozenFxtrCbor` (refs ride as bare `RawWeakFxtrID` leaves — the
`SandboxProvenanceContext.config_snapshot` pattern), so the cache key is
deterministic + provenance-blind while arg closures travel and stay loadable.
`canonicalize` rejects live `ServiceRef`s; slot-arg → path-marker conversion is
done upstream by the runner (part 3), not in the canonicalizer.

### Chunk 2b status ✅ (steps 1–3 below)

Done: `runner.py` with `WorkflowRunner` (async CM owning a child scope + job
task group + the two namespaces; **not a service**) + `RunGroupHandle`
(plain-python). Run groups are the `run_group(name, desc)` **async CM** (records
`RunGroupOpened`/`Closed` on enter/exit, both authorized — no unauthorized
mount-lifespan-teardown pin problem); `RunGroupHandle.get_storage(path)` +
`TaskSlotProto.subslot` mount slots; the `{ts_us}-{seq}` op-log append helper
stamps `owner` on every event (default `{host}:{pid}:{nonce}`). `RunGroupProto`
/ `WorkflowRunnerProto` are kept only as the eventual RPC surface (unmounted).
`TaskSlotProto.run` raises `NotImplementedError` (chunk 2c). Tests:
`tests/test_workflows/test_runner_skeleton.py`.

Resolved deferrals: `on_existing_failure` is **dropped** (no persisted failure
state → always retry). `owner` is added now and stamped on events. Run groups
close on CM exit (clean per-group lifecycle). Recommended storage: back
`realization_map` + `op_log` by the **same SQLite db file** so shared entity
data isn't duplicated (tests do this). Slot services mount lazily inside the
`get_storage`/`subslot` dispatch and accumulate on the runner scope until
teardown (fine for v0).

### 1. `WorkflowRunner` construction + lifecycle (`runner.py`)

`async with WorkflowRunner(spawner, realization_map, op_log, owner=None) as
runner:` (the run-group / get_storage shape below was revised — see the chunk-2b
status note above for what shipped).

- Owns: the two namespaces (passed in; runner does not create them), a child
  `FxtrScope` (mount point for slot/group/ctx services + home for in-flight
  result entities), and an anyio task group for jobs.
- `owner` defaults to `"{host}:{pid}:{short-nonce}"`, stamped on op-log events.
- On enter: open the scope + job task group, init in-memory state (`jobs:
  dict[slot_path, _Job]`, the op-log sequence counter, the run-group registry).
- On exit: **clean teardown waits** for in-flight jobs to finish and record
  results (does not cancel them). If the surrounding cancel scope fires
  (full-runner cancellation), each job tries to append a `cancelled` op-log
  event on the way out. Confidence: the exact "wait vs cancel" structuring
  against anyio teardown is the fiddly bit — get the boundary right (a clean
  `__aexit__` drains jobs; an injected cancellation writes events) even if the
  internal mechanics get refined during impl.

Helper: a small `_append_op_log(event)` that pins `event` in the op-log
namespace under key `f"{ts}-{seq}"` (monotonic per-runner `seq`).

### 2. Run groups

- `runner.start_run_group(name, description)` (plain async python method in v0):
  build the `RunGroup(name, description, created_at)` entity, mount a
  `RunGroupProto` impl for it, append a `RunGroupOpened` event, and return the
  resolved `RunGroupProto` proxy. The mount lifespan appends `RunGroupClosed`
  on teardown.
- `RunGroupProto.get_storage(path)` (service method): validate `path` nonempty
  + each segment valid; mount a `TaskSlotProto` impl rooted at `path`,
  associated with this run group; return its `ServiceRef`. Caller must
  `.resolve()` it before calling `run`/`subslot`.

The run group is the unit the runner uses to attribute op-log events; it does
not partition the realization-map slot namespace (all groups share it).

### 3. Slot + step-context impls

- `TaskSlotProto` impl, rooted at `(run_group, base_path)`:
  - `subslot(key)`: validate key, mount a new `TaskSlotProto` rooted at
    `base_path + "/" + key` (same run group), return its ref. Each subslot is a
    genuine new mount.
  - `run(step_fn, *args, **kwargs)`: entry point into the job/requester
    machinery (step 4), attributed to this slot's run group.
- `TaskStepContextProto` impl, one per job, knows the job's slot path + run
  group:
  - `log(message)`: forward to python logging, prefixed with the slot path.
  - `subslot(key)`: in part 2, raise `NotImplementedError` (the method exists so
    the API is stable; nesting is part 3).

Capability validation: the runner only honors calls through slot/ctx services
it mounted; a slot service is permanently rooted at its base path.

### 4. Job + requester machinery (the core)

This is the heart of part 2. Model (design doc "Job + requester model"):

- `_Job` holds: slot path, invocation id (cache key), the anyio task running
  the fn, a completion event/result box, the set of waiting requesters, and (for
  later parts) child-slot + observation accumulators + a `poisoned` flag. In
  part 2 the latter stay empty.
- A `run` call spawns a **requester task** that: canonicalizes args → builds the
  `WorkflowTaskInvocation` → cache key `K` (step 5); ensures the job at `path`
  is running; parks on its completion; returns the result (or raises). For part
  2 the "requester chain / cycle check" is trivial (no nesting) — include the
  hook but it can be a no-op.
- Ensure-job logic at `path`:
  - in-memory `_Job` exists, key == K → join (park on its event); key != K →
    `TaskConflictError`.
  - else read the realization-map pin: `Complete` key match → cache hit (return
    result); `Complete` key differ → `TaskConflictError`; empty → start a job:
    register `_Job`, append `JobLaunched`, spawn the job task.
- **Job task**: `spawn_resolved(spawner, step_fn_handle)` retyped to
  `TaskFnProto`; mount this job's `TaskStepContextProto`; invoke `run(step_ctx,
  *args, **kwargs)` over the service layer; on return/raise go to completion
  (step 6). One job per slot, owns the slot until it exits.

Confidence: the precise requester↔job task wiring (which task owns the event,
how a joiner parks, how results/exceptions propagate back through anyio) is the
part most likely to be refined in code — keep the *roles* clear (requester
parks; job runs once; many requesters share one job; cancelling a requester's
await never cancels the job) and leave the exact primitive choice (event vs
memory-stream vs future box) to implementation.

### 5. Cache-key canonicalization

Runner-side, before dispatch, walk the args/kwargs tree and build the
`FrozenFxtrCbor` for the invocation entity:

- `EntityRef` / `FxtrID` leaves → keep as full `EntityRef`s (so closures pin
  into the map + stay loadable) but strip `_provenance_parent` (provenance-blind
  key). Mechanism: build the invocation entity through a strip-mode scope so
  layered children reduce to data ids while the data closures still travel.
- slot-service `ServiceRef` leaves → **part 2 raises `NotImplementedError`**
  (slot args are part 3).
- any other `ServiceRef` leaf → reject (no stable identity).
- else → canonical CBOR.

The invocation entity's FxtrID is `K`. Store the invocation as a strong child
of any `Complete` pin so it stays inspectable.

### 6. Completion + persistence

When the job's `run` returns or raises (part 2: no children to wait on):

- raised → append `ResultRecorded(outcome="failed", reason=...)`; drop the
  in-memory job; notify joined requesters with `TaskFailedError`.
- returned `EntityRef` `e` → project to a provenance-layer node (design "Result
  provenance": layered → extract prov half; bare `d` or bare prov-tag-no-parent
  → `u`-wrap), bridge into the runner scope, pin `WorkflowTaskComplete`
  (`invocation` strong child, `result` strong child so the closure travels,
  `depends_on={}`) into the realization map; append
  `ResultRecorded(outcome="success", result=...)`; notify requesters with the
  result.
- conflicting save (slot already holds a `Complete` with a different key, e.g.
  a second runner) → `TaskConflictError`.

Re-running a previously-failed slot: since failures aren't persisted, the slot
is empty, so a re-run simply starts a fresh job (always retry). There is no
`on_existing_failure` knob — it was dropped in the two-namespace redesign.

### 7. Tests (`tests/test_workflows/test_runner_core.py`)

In-process, two SQLite namespaces in one session, task fns registered as
external services on a `BasicStatelessServiceProvider`. Cover: single
run/cache-hit/cache-miss; cache-key conflict; N concurrent identical requests
joining one job (fn runs once); op-log event sequence + ordering + run-group
attribution (incl. a cross-group cache-hit producing `ObservedResult`); a second
runner instance over the same map detecting a conflicting save; failure →
`TaskFailedError` + `ResultRecorded(failed)` + no map pin + retry-on-rerun;
result-provenance projection (layered result vs bare `d` vs bare prov-tag);
`step_ctx.log`; `step_ctx.subslot` / slot-arg → `NotImplementedError`; clean
teardown waits for an in-flight job; injected cancellation writes `cancelled`
events.

### 8. Update overviews

Update `v2/CODE_OVERVIEW.md` + `fxtr2/workflows/LOCAL_CODE_OVERVIEW.md` for the
runner (do this before writing the tests, per repo convention).

## Done when

A single-level workflow runs end-to-end against the basic provider: tasks
dispatch, dedup, cache, persist successful results to the realization map, and
record the full operation history to the op log; failures surface and are
logged but not cached; nesting paths raise `NotImplementedError`.
