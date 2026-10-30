# Part 3 — task nesting (TaskStorage args + step_ctx.subslot)

Adds **nesting**: a running task can run sub-tasks — into subslots of its own
slot (`step_ctx.subslot`) and into `TaskStorage` slots passed to it as args. This
unlocks the full coherence surface part 2 deferred: the requester-task layer,
cycle detection, child tracking + `depends_on`, waiting for children, and
poisoning.

Builds on the part-2 runner. Design: [design/workflow_runner.md](design/workflow_runner.md).

## Core mechanism: per-job re-minting of slot capabilities

The central problem: **when a task fn calls `run` on a slot, how does the runner
know which job is calling?** The task fn is a separate service (in-process now,
sandboxed later), so its call back to the runner carries no runner-side context.

Answer: **when the runner invokes a task fn for job `J`, it re-mints every slot
capability `J` receives as a `J`-bound slot service** — `step_ctx` and every
`TaskStorage` arg. A `J`-bound `_SlotImpl` records `job=J` (alongside its path +
run group). So when `run`/`subslot` is called on one, the runner knows it is `J`
calling, at a known path. Everything else derives from this:

- **Run-group attribution** — a `J`-bound slot carries `J`'s run group, so a
  nested run is attributed to that group; the whole nested tree shares the
  top-level launcher's group.
- **Child tracking** — a `J`-bound slot's `run` registers the child under `J`
  (for `depends_on` / poisoning).
- **Cycle detection** — only needed with slot *args* (3b): `J` carries a `chain`
  (tuple of slot paths) and a `run` into `P_C` errors if `P_C ∈ J.chain`. Not
  needed in 3a — `step_ctx.subslot` only ever descends (`P/k`), so paths strictly
  deepen and can never repeat.

`_SlotImpl` gains `job: _Job | None`: `None` for top-level slots (from
`get_storage`), `J` for re-minted slots handed to a task fn. `_request_run`
gains a `parent_job` param threaded from the slot's `job`.

## Slot registry (path) — NQ-2

The runner keeps a registry `dict[ServiceKey, str]` (keyed on
`ServiceKey(handle, service_type_id)`) mapping every `_SlotImpl` it mounts to its
root-relative path. To recognize a `TaskStorage` arg, compute the arg
`ServiceRef`'s `ServiceKey` and look it up. **Confirmed by spike:** an in-process
`ServiceRef` passed as an arg and decoded keeps the *same handle object* (cap-IR
bounce-back), so its `ServiceKey` matches the original mount's — no
`_internal_id()` fallback needed. (Bounce-back should preserve this across the
sandbox RPC boundary too; verify in part 5.) A `ServiceRef` arg not in the
registry is a foreign live service → **rejected** (as today).

The registry stores **path only** — no run group (see the next section for why
group is unneeded). Note it grows for the runner's lifetime and retains the slot
handles (and thus their now-closed scopes); a bounded / scope-close-driven
registry is a tracked followup.

## Run-group consistency (decision 5) — enforced structurally

A run's target slot and **all** its `TaskStorage` args must share one run group;
a cross-group arg is forbidden. This needs **no explicit runner check**: sibling
run-group scopes are isolated, so a slot mounted on group A's scope simply isn't
reachable to *encode* as an arg in a group-B `run` call — the service layer
rejects it ("ServiceKey not registered on home scope") before any runner code
runs. (For nested runs the property holds by construction anyway — a task fn only
holds `J`-bound slots, all in `J`'s group.) So the registry need not record each
slot's group, and there is no group comparison in `run`.

## Cache keying of slot args (placeholder entity)

A `TaskStorage` arg contributes its **root-relative slot path** to the cache key
(not the live ref — rebasing is a followup; task logic can't read its slot's
path). Encoding: a private entity

```python
class WorkflowTaskStorageSlotPlaceholder(DataclassEntity):
    path: str
```

The runner replaces each slot arg with a stored placeholder entity ref before
building the invocation; `canonicalize` then sees an ordinary `EntityRef` and
handles it as usual. Using an entity (not a reserved dict key) avoids accidental
collision and is introspectable (load it, read `path`). The arg the *task fn*
receives is the re-minted live slot (below), not the placeholder. (Followup:
reject a *forged* placeholder passed as a real arg — tracked in followups.md.)

Two derived forms of the args, both walked with the runner's slot resolver:
- **invocation (cache key):** slot arg → placeholder entity ref; entity refs →
  bare ids; non-slot `ServiceRef` → rejected.
- **task-fn args:** slot arg → a path marker carried through; entity refs → bare;
  non-slot `ServiceRef` → rejected. `_run_job` re-mints each path marker as a
  `J`-bound slot before invoking the task fn.

## Requester chain + cycle detection (3b only)

Cycles are impossible with subslots alone (they only descend), so this lands
with slot args, which can point up or across the tree. `_request_run(parent_job,
run_group_ref, target_path, ...)`:
- `chain = parent_job.chain if parent_job else ()`.
- If `target_path ∈ chain` → `WorkflowCycleError` (covers self-request and
  transitive cycles — a slot whose subtree would re-enter a running ancestor).
- The (fresh) child job is created with `chain = chain + (target_path,)`.

`_Job` gains `chain` in 3b. (3a tracks `children` but no `chain`.)

## Child tracking, waiting, depends_on, poisoning

A job `J` accumulates the children it requested. For a nested run (parent_job =
`J`), `_request_run` registers the child in `J.children[child_path]` **before
awaiting** (so a child stays tracked even if the task fn's await is cancelled or
the task fn fails mid-way), and records the observed outcome when it resolves.

The two completion paths are asymmetric:

- **Task fn raises (failure):** finalize `J` *immediately* — set `outcome=failed`,
  remove `J` from `_jobs`, set `done` (all under the lock, eagerly — not in a
  `finally` — so a retry into the slot doesn't join the dead failed job) — and
  **do not wait for children**. There's no coherence obligation (a failed `J`
  pins no `Complete`), and the children's own jobs persist their own results
  independently; we just stop waiting. This gives fast op-log recording +
  propagation to `J`'s awaiter.
- **Task fn returns (success):** wait for all of `J`'s launched children to
  finish, then:
  1. **Poison** `J` if any child's outcome is a failure (`failed` / `conflict` /
     `child_incomplete` / `child_conflict` / `interrupted`): `J`'s outcome becomes
     `child_incomplete` (or `child_conflict`), *regardless of whether the task fn
     caught the error* — a completed slot can never point at a failed
     computation.
  2. Otherwise build `depends_on = {child_path: child's WorkflowTaskComplete}` for
     each **direct** child observed, and persist `J`'s `WorkflowTaskComplete` with
     that `depends_on`. (Coherence is transitive: each child's own `Complete`
     carries its own `depends_on`.)

Children are independent jobs in the runner task group — never cancelled because
the parent failed. Note: child slots are re-minted on `J`'s job scope, so a still
in-flight child `run` (an unawaited/detached child when `J` fails) keeps `J`'s
job-*scope* teardown lingering in the background until that child finishes —
harmless (no deadlock; `J`'s awaiter is already unblocked by the early `done`;
runner teardown waits for everything). The common case (task fn awaited its
children, or failed *because* a child failed) leaves nothing in-flight.

`WorkflowTaskComplete.depends_on` is already the right shape (part 2 leaves it
empty); part 3 populates it.

## `step_ctx.subslot`

`step_ctx` is the `J`-bound step context. `subslot(key)` mounts a `J`-bound
`_SlotImpl` at `job_path + "/" + key` on the **job scope** (so it is freed when
the job ends), records it in the registry (group = `J`'s group), and returns its
ref — which reaches the task fn through the normal service-return bridge (no
scope-inheritance needed). Running into it is a nested run attributed to `J`.

## Op log under nesting

Unchanged shape: nested runs emit the per-request `RunStarted` / `RunCompleted`
pair (part 2d), attributed to the run group the slot carries. A poisoned parent
records `RunCompleted(outcome="child_incomplete" | "child_conflict")`.

## Error types

Add `WorkflowCycleError(WorkflowError)`. Poison surfaces to the parent's awaiter
as `TaskFailedError` (kind `child_incomplete` / `child_conflict`).

## Staging (flexible)

- **3a — own-subtree nesting** ✅: `step_ctx.subslot` + the per-job slot binding
  (`_SlotImpl.job`, `_StepContextImpl` mounts job-bound subslots on the job
  scope) + child tracking (`_ChildObs` in `job.children`) / `depends_on` /
  success-waits-failure-returns / poisoning. No chain / cycle detection
  (descending-only subslots can't cycle). Tests:
  `tests/test_workflows/test_runner_nesting.py`.
- **3b — `TaskStorage` slot args** ✅: the registry recognition (NQ-2) +
  placeholder cache-keying + re-minting of *incoming* slot refs (cross-subtree) +
  the **chain / cycle detection** (now reachable, since args can point
  up/across). Run-group consistency (decision 5) is enforced structurally by
  scope isolation, not an explicit check (see above). Builds on 3a. Tests:
  `tests/test_workflows/test_runner_storage_args.py`.

(The split can shift if the code decomposes more naturally another way.)

## Tests (sketch)

3a: a task fn runs a subslot and incorporates its result; cache hit on the parent
also reuses the child; child fails → parent poisoned (`child_incomplete`) even
when the task fn catches it; parent's own failure records immediately without
waiting; `depends_on` snapshot correctness. 3b: pass a slot arg and run into it;
same child slot from two parents dedups (one job); a slot arg pointing at an
ancestor / its own path → `WorkflowCycleError`; cross-group arg → error; slot-arg
cache keying (same path → same key) via the placeholder; foreign `ServiceRef`
arg → rejected.

## Followups (add to followups.md)

- Reject a forged `WorkflowTaskStorageSlotPlaceholder` passed as a real arg
  (so a task can't inject an arbitrary slot path into a peer's cache key).
- The deferred requester-task layer is implemented here (was deferral (a) from
  part 2).
