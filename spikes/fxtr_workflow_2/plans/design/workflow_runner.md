# Design: workflow runner (slot manager)

The workflow runner manages a durable, trace-based task/workflow over two
fxtr2 namespaces it owns:

- **Realization map** — a queryable namespace whose pins are *successful task
  results only*. Conceptually mutable, written and updated pointwise (one pin
  per slot). This is the cache / lockfile: you version-control it (eventually
  exported to TSV) but don't hand-edit it.
- **Op log** — an append-only namespace recording the *history of operations*
  on the realization map: run groups opening/closing, jobs launched, results
  recorded (success **or** failure), interruptions, and (part 4) deletions.

In v0 both are `SQLiteFxtrNamespace`s, typically two named namespaces inside
one SQLite session / backing db (so entity data shared between them is not
physically duplicated). The op log is "just an event log" for now; a future
event-sourcing redesign could derive the realization map *from* the op log
(tracked in followups).

The runner accepts task-run requests, deduplicates them by slot + cache key,
resolves task functions through a stateless service provider, persists
successful results as typed pins in the realization map, appends events to the
op log, and maintains the coherence invariant between dependent slots.

## Two storage namespaces — division of labor

| | Realization map | Op log |
|---|---|---|
| Shape | pin per slot path | append-only events, timestamp-keyed |
| Contents | `WorkflowTaskComplete` only (or empty) | every operation, incl. failures |
| Mutability | mutable pointwise (overwrite/unpin) | append-only |
| Claims / incomplete | **not stored** (in-memory only) | recorded as events |
| Purpose | the cache + coherence lockfile | the audit/history + failure record |

The realization map holds *no* `Claimed` or `Incomplete` pins. A slot is
either empty (no pin) or a single `WorkflowTaskComplete`. Claims and
in-progress / failed state live only in the runner's memory; the durable
trace of "what was attempted and what failed" lives in the op log.

## Public surface

The runner is service-first: its functionality is reachable through service
protocols so it can be threaded through a workstation or across RPC, while
construction is a local async context manager that owns the two namespaces, a
child `FxtrScope`, and a job task group.

```python
async with WorkflowRunner(
    spawner=...,                 # ServiceRef[StatelessServiceSpawnerProto]
    realization_map=...,         # FxtrQueryableNamespace the runner owns
    op_log=...,                  # FxtrNamespace the runner owns (append-only use)
    owner=None,                  # owner string for events; default "{host}:{pid}:{nonce}"
) as runner:
    async with runner.run_group("nightly-evals", "kick off the eval suite") as group:
        # get_storage / subslot return ServiceRefs; you must .resolve() before
        # calling methods, so real call sites are a bit more verbose:
        storage_ref = await group.get_storage("evals/run1")        # ServiceRef[TaskSlotProto]
        storage = await storage_ref.resolve(runner.scope)          # TaskSlotProto proxy
        ref = await storage.run(step_fn_handle, arg, k=2)          # EntityRef[...]
```

Throughout: a `ServiceRef[T]` is resolved via `await ref.resolve(scope)` (against
`runner.scope`) to get the typed proxy, and `ref = ref.replace_service_type(T)`
retypes an `UnknownService`-typed wire ref. So every `get_storage` / `subslot`
hop is resolve-then-call.

**The runner and run groups are plain-python async context managers — not
services.** A run group is `runner.run_group(name, description)` (an async CM
yielding a `RunGroupHandle`); it records a `RunGroupOpened` op-log event on entry
and `RunGroupClosed` on exit (both authorized, while the op log is open). The
first *service* a caller touches is the `TaskSlotProto` returned by
`RunGroupHandle.get_storage(path)` (nonempty path → every storage is rooted at
least one slot deep, so there's no "run into the map root" case). `get_storage`
returns a `ServiceRef[TaskSlotProto]` to resolve against `runner.scope`.

`RunGroupProto` / `WorkflowRunnerProto` are defined only as the *eventual RPC
surface* (a future way to drive the runner remotely); neither is mounted in v0.

Storage: back `realization_map` and `op_log` by the **same SQLite db file** (two
namespaces over one content-addressed store) so entity data shared between them
(e.g. results referenced by op-log events) is not physically duplicated.

Runner-level python methods (not reachable from any task-granted capability,
so not grantable to sandboxed tasks):

- `runner.clear_slots(pattern)` — part 4: delete realization-map slots; errors
  if any non-deleted slot depends on a target (suggests clearing those first);
  appends deletion events to the op log.


## Run groups

A **run group** groups a set of runs that were launched together, purely for
op-log legibility. It does **not** affect the realization map: `get_storage`
roots its `TaskSlotProto` at the given `path` in the *shared* realization map,
so two run groups can request the same slot and cache-share freely.

- A run group is identified by a content-addressed entity
  `RunGroup(name, description, created_at)` (the timestamp makes otherwise
  identical groups distinct). It is recorded in the op log alongside every
  event for jobs launched under it, so the log can be filtered/grouped by run
  group.
- Lifecycle is the `run_group` **async CM**: a `RunGroupOpened` event is
  appended on entry and a `RunGroupClosed` event on exit (both run authorized,
  while the op log is open). Every op-log event also carries the runner's
  `owner` string.
- **Single-group attribution per top-level run.** A single top-level `run`
  call is attributed to exactly one run group, so all `TaskStorage`
  (`ServiceRef[TaskSlotProto]`) args passed *into a top-level run* must belong
  to the same run group, else the runner errors. Inside a task, subslots and
  child runs descend from one group automatically, so the check is only needed
  at the top level.
- **Cross-group joins.** When a job launched under group A is cache-hit /
  joined by a request from group B, group A owns the `job_launched` /
  `result_recorded` events; group B gets an `observed_result` event
  referencing A's slot. Each run group's slice of the log thus reflects what
  *it* caused or observed.

(Per-run-group cancellation is deferred to a late stage — see "Claims,
teardown & multi-runner posture" and followups. v0 has only full-runner
cancellation.)

## Task-facing capabilities — bare ServiceRefs

There are **no** local adapter/wrapper classes. `TaskStorage` *is* a
`ServiceRef[TaskSlotProto]` and `TaskStepContext` *is* a
`ServiceRef[TaskStepContextProto]`; they flow over the wire as ordinary
ServiceRefs (so there is no `$fxtr.workflow` marker-mapping encoding — slot
args are just ServiceRef leaves like any other). `subslot` is async (it mints
a new runner-side mount and returns its ref); we accept the per-subslot async
round-trip for now.

```python
@service_protocol
class TaskSlotProto(Protocol):
    """A capability over one slot subtree of the realization map.

    Downward-only: you can run into this slot or descend to a child slot,
    but there is no read API and no way to walk *up*. Holding a slot ref is
    a capability for that subtree only."""
    async def run(
        self, step_fn: EntityRef[UnknownEntity],
        *args: FxtrServiceData, **kwargs: FxtrServiceData,
    ) -> EntityRef[UnknownEntity]: ...
    async def subslot(self, key: str) -> ServiceRef[TaskSlotProto]: ...

@service_protocol
class TaskStepContextProto(Protocol):
    """Per-job capability available *inside* a running task. Distinct from
    TaskSlotProto so it can grow (checkpointing, richer output) without slot
    capabilities growing with it."""
    async def subslot(self, key: str) -> ServiceRef[TaskSlotProto]: ...
    async def log(self, message: str) -> None: ...

@service_protocol
class TaskFnProto(Protocol):
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto],
        *args: FxtrServiceData, **kwargs: FxtrServiceData,
    ) -> EntityRef[UnknownEntity]: ...
```

Notes:

- `subslot(key)` validates `key` (nonempty, no `/`, charset per pin-key rules)
  and mounts a fresh `TaskSlotProto` impl rooted at `parent_path + "/" + key`,
  returning its ref. Each subslot is a genuine new runner-side mount.
- `TaskStepContextProto.subslot` returns a slot under the *job's own* slot
  path; `log` forwards to python logging on the runner side (prefixed with the
  slot path) for now — persisting logs / output channels is a followup. The
  asymmetry (slot ops vs per-job extras live on different protocols) is locked
  in from day one.
- **Authoring task fns**: a task fn is authored as a normal `TypedServiceImpl`
  subclass with a typed `run(self, step_ctx, *args, **kwargs) -> EntityRef`,
  while the wire protocol stays untyped. Everything is untyped on the wire and
  typed in the impl; the runner treats positional args as opaque
  `FxtrServiceData`, canonicalizes them for the cache key, and ships them
  through — it never needs an arg schema. No dedicated authoring base class is
  required.

**Capability validation.** The runner only honors `run` / `subslot` calls
through slot services *it* mounted, and a slot service is permanently rooted at
its base path (a task can only ever address its own subtree).

## Realization-map slot model

A slot is a pin key in the realization map; `subslot` of `P` with key `k` pins
at `"P/k"`. The pin is in one of exactly two states:

```python
# (empty: no pin)

class WorkflowTaskComplete(DataclassEntity):
    invocation: ChildRef[WorkflowTaskInvocation]   # its id IS the cache key
    result: ChildFxtrID            # provenance-level node; see "Result provenance"
    depends_on: dict[str, EntityRef[WorkflowTaskComplete]]
                                   # root-relative slot path -> observed pin value
```

There is no separate `cache_key` field: the invocation entity is
content-addressed, so `invocation.entity_id` *is* the cache key, and
strong-ref'ing it keeps the invocation inspectable/diffable in the map.

**KEY INVARIANT** (coherence): as long as a `Complete` pin is in the map,
every entry in its `depends_on` must also be pinned, at the given key, with the
given value. Not checked during normal operation; enforced at deletion time
(part 4) and optionally checkable on request.

## Invocation entity & cache key

```python
class WorkflowTaskInvocation(DataclassEntity):
    step_fn: ChildRef[UnknownEntity]   # the service-handle entity (small closure)
    args: FrozenFxtrCbor               # canonicalized args tree
    kwargs: FrozenFxtrCbor
```

Canonicalization of the args tree (runner-side, before dispatch), tuned so the
cache key is provenance-blind but arg data stays loadable from the map:

- `EntityRef` / `FxtrID` leaves → kept as **full `EntityRef`s** (so their
  closures are pinned into the realization map and remain loadable for
  inspection / replay), but with their `_provenance_parent` **stripped** so the
  cache key is provenance-blind (decision 2; provenance plumbing is a
  followup). Building the invocation entity in a strip-mode scope achieves
  both: data closures travel, provenance lineage does not contribute to the
  key.
- Slot-service ServiceRef leaves (a `TaskStorage` passed as an arg) → the
  runner recognizes its own slot mounts and canonicalizes them to their
  **root-relative slot path** string. Rebasing rewrites these (followup).
  Recognition works because cap-IR bounce-back returns a round-tripped handle
  with its original identity, so the runner can look the ServiceRef up in its
  mount registry; if bounce-back identity proves insufficient, the fallback is
  a private undocumented `_internal_id()` (a UUID) on the slot handle that the
  runner keys its registry on.
- Any *other* `ServiceRef` leaf → **rejected**: live handles have no stable
  identity. Non-slot services must be passed as handle entities and spawned
  inside the task.
- Everything else: canonical CBOR (sorted sets, etc.).

The service-handle entity contributes via `step_fn` (strong child — handles
and their configs/bindings are small and worth keeping inspectable).

## Result provenance

The result slot **always holds a provenance-layer node**, mirroring
`PreserveProvPersistenceImpl` in
`projects/fxtr_workstation/src/fxtr_workstation/persistence.py`
(`_layer_for_storage` / `_restore_layered`). The decision keys on the presence
of a `_provenance_parent` **sidecar**, *not* on whether the returned node's
*type* tag is a provenance type — a task may legitimately return a
provenance-typed node (`n`/`c`/`p`/`u`) as its real data.

On store (`run` returns `EntityRef` `e`):

- `e` carries a `_provenance_parent` → store the bare prov half
  (`extract_provenance_parent`).
- `e` is a bare data node (`d`) → wrap it in a `u`-node (`create_u_node`) and
  store that, so the slot still holds a provenance-layer node.
- `e` is a bare provenance-tag node (`n`/`c`/`p`/`u`) with no
  `_provenance_parent` → treat it as **data** and `u`-wrap it, same as a bare
  `d`. A task returning e.g. a c-node as its genuine output is legitimate, and
  the prov *parent* sidecar is the only signal we key on. (Note:
  `PreserveProvPersistenceImpl._layer_for_storage` currently *rejects* this
  case as ambiguous — that's arguably a bug there; we do not copy that
  behavior.)

On retrieve: the low-level path returns the stored provenance node verbatim
(no stripping). A higher-level retrieval API can re-attach it as a provenance
parent of its unwrapped data half (`embed_as_provenance_parent`) so callers get
the layered `(data, prov)` form.

## Job + requester model

The runner separates **jobs** (one per active slot, owns the slot, runs the
task fn exactly once) from **requesters** (per-await tasks parked on a job's
completion). This makes "many callers awaiting one job" and "cancelling your
own await doesn't kill the shared job" fall out naturally.

In-memory state: `jobs: dict[slot_path, _Job]`, where `_Job` holds the
invocation id (cache key), the anyio task running the fn, a completion event,
the set of child slot paths it requested, its observations (`depends_on`
accumulator), and a `poisoned` flag. (No durable claim — the in-memory `_Job`
*is* the claim.)

Flow for a `run` call on slot `path` with cache key `K`, made from requester
context R (either a running job, or the top-level/external entrypoint):

1. **Requester task.** The `run` call spawns a *requester task* into R's
   job-owned task group (for a top-level call, the runner's own group). R's
   handler parks on that requester task completing. The requester task:
   - **Cycle check**: if `path` is R's own slot or any slot in R's requester
     chain → error immediately (would deadlock).
   - Canonicalize → invocation entity, cache key `K`.
   - Ensure the job at `path` is running, then park on it:
     - **In-memory job at `path` exists**: key == K → join it (park on its
       event). Key != K → `TaskConflictError`.
     - **Else read the realization-map pin**:
       - `Complete`, key matches → cache hit: the requester records
         `depends_on[path] = pin` into R and returns the result.
       - `Complete`, key differs → `TaskConflictError`.
       - empty → this requester *starts the job*: register the in-memory
         `_Job`, append a `job_launched` op-log event, and spawn the job task
         into the runner's job task group; then park on it.
   - When the job completes, the result (or failure) flows back to the
     requester, which flows it to R's handler via the await.
2. **Job task** (one per slot; owns the slot until it exits):
   - Resolve the step fn: `spawner.spawn(step_fn_handle)` → retype to
     `TaskFnProto` → resolve proxy. (Per-job spawn; caching spawns is the
     provider's concern, OQ-11.)
   - Mount this job's `TaskStepContextProto` impl (its slot ops route through
     a `TaskSlotProto` rooted at the job's slot).
   - Invoke `run(step_ctx, *args, **kwargs)` over the service layer.
3. **Completion.** When `run` returns or raises, first **wait for all child
   jobs this job requested** (children are independent jobs in the runner's
   task group — never cancelled because a parent failed; we wait so their
   results record coherently). Then:
   - `run` raised → record failure (see below).
   - any child failure observed (even if the task fn caught the exception) →
     job is *poisoned* → record failure — we never cache a result that observed
     a failure.
   - otherwise → persist: bridge the returned `EntityRef` into the runner
     scope, project to the provenance-layer node (Result provenance above),
     pin `WorkflowTaskComplete` in the realization map (result as strong child
     so the result closure travels; `depends_on` from recorded observations),
     and append a `result_recorded(success)` op-log event.
   - On failure: append a `result_recorded(failure, kind, reason)` op-log
     event. Nothing is written to the realization map. The in-memory job is
     dropped.
4. **Notify** all joined requesters: success → result ref; failure →
   `TaskFailedError` (each waiting job records poisoning).

`kind` of a failure ranges over `failed` (the fn raised),
`child_incomplete` / `child_conflict` (poisoned by a child), `interrupted` /
`cancelled` (see teardown).

## Failure & poisoning semantics

- A task fn that raises → its job records `failed` and notifies waiters with
  `TaskFailedError`.
- A job that *observed* any child failure/conflict (regardless of whether the
  fn caught it) is poisoned and records `child_incomplete` / `child_conflict`
  rather than a success — a completed slot can never point at a failed/partial
  computation.
- Every child job a task launched must complete before the parent job can
  exit. Even if one child fails, already-launched siblings are kept alive and
  awaited so their results record coherently.
- (Part 2 has no nesting, so `depends_on` is always empty and poisoning never
  fires — the only failure kinds there are `failed` / `interrupted` /
  conflicts.)

## Claims, teardown & multi-runner posture

Claims are **in-memory only** (decision 4, revised): the live `_Job` is the
claim. There is no durable `Claimed` pin and no `break_claim`.

- **Single-runner assumption.** Two runners over the same realization map are
  assumed not to target the same slot concurrently. If they do, the loser is
  detected when it tries to *save*: the slot already holds a `Complete` with a
  different cache key → `TaskConflictError`. Best-effort for v0; a
  compare-and-swap namespace primitive (followup) would close the window.
- **Clean teardown waits.** Normal teardown of the runner / a run group
  **waits for in-flight jobs to finish** and record their results. It does not
  cancel them.
- **Cancellation writes events.** v0 only supports **full-runner**
  cancellation: if the runner's job task group is cancelled (an enclosing anyio
  cancel scope fires), each cancelled job tries to append a `cancelled` op-log
  event on the way out — we react to cancellation as gracefully as we can.
  Cancelling your own *await* of a job does **not** cancel the job's logic (the
  job runs in its own task and may have other requesters).
- **Per-run-group cancellation is deferred** to a late stage (see open
  questions / followups): jobs are shared across run groups, so cancelling one
  group means cancelling its requester tasks and any now-orphaned jobs. v0 ships
  without it.

## Deletion & the dependency index (part 4)

The runner registers an indexer over `WorkflowTaskComplete` pins in the
realization map that emits one row per `depends_on` entry: columns
`(dep_path: str, dep_value_id)`. `clear_slots(pattern)`:

1. Resolve `pattern` to a target path set; error if any target has an
   in-flight job.
2. Query the index for rows where `dep_path ∈ targets` and the depending pin
   ∉ targets → error listing the dependents ("clear those first").
3. Unpin all targets; append deletion events to the op log.

This enforces the coherence invariant at delete time: you can never remove a
result some retained result observed. (Exact indexer path expression over dict
keys to be verified at impl time — open question; flag before any fallback.)

## Error types

`WorkflowError` base; `TaskConflictError` (cache-key mismatch on a live job or
saved `Complete`), `TaskFailedError` (awaited task failed/poisoned; carries
slot path + kind + reason), `SlotDeletionBlockedError` (part 4). All raised to
the awaiting caller; failures are also appended to the op log.

## Module layout (`v2/fxtr2/workflows/`)

```
fxtr2/workflows/
├── __init__.py          # re-exports
├── stateless_service_provider/   # see design/stateless_service_provider.md
│   ├── __init__.py
│   ├── handles.py       # ServiceHandle ABC, ExternalServiceHandle entities
│   ├── protocols.py     # StatelessServiceProviderProto / SpawnerProto /
│   │                    #   ExternalConnectorProto + spawn_resolved helper
│   └── basic_provider.py # BasicStatelessServiceProvider
├── task_types.py        # TaskSlotProto / TaskStepContextProto / TaskFnProto,
│                        #   WorkflowRunnerProto / RunGroupProto, error types
├── entities.py          # RunGroup, WorkflowTaskInvocation / WorkflowTaskComplete,
│                        #   op-log event entities + WorkflowOutcome
├── cache_key.py         # canonicalize() + build_invocation() (deterministic,
│                        #   provenance-blind invocation entity / cache key)
├── runner.py            # WorkflowRunner (+ RunGroup handle)
└── LOCAL_CODE_OVERVIEW.md
```

## Op-log event entities

A small union of content-addressed event entities, each carrying the run-group
ref, slot path (where applicable), invocation/result refs (full refs — back the
two namespaces by one db file so this does not physically duplicate data), the
runner's `owner` string, and a timestamp. Sketch (every event has `owner: str`):

```python
class RunGroupOpened / RunGroupClosed(...):    run_group: ChildRef[RunGroup]; owner; ts
class JobLaunched(...):    run_group, slot_path, invocation: ChildRef[...]; owner; ts
class ResultRecorded(...): run_group, slot_path, invocation,
                           outcome: Literal["success","failed",
                               "child_incomplete","child_conflict",
                               "interrupted","cancelled"],
                           result: ChildFxtrID | None, reason: str | None; owner; ts
class ObservedResult(...): run_group, slot_path, observed: EntityRef[WorkflowTaskComplete]; owner; ts
class SlotsDeleted(...):   run_group | None, paths: tuple[str, ...]; owner; ts   # part 4
```

Keyed `"{ts_us}-{seq}"` (microsecond timestamp + a monotonic per-runner sequence
counter), zero-padded for a deterministic lexical total order regardless of
clock resolution.

## Implementation staging

- **Part 2** (runner core): two namespaces, run groups, slot model,
  invocation/cache key, in-memory jobs/requesters, success/failure persistence
  + op-log events — *except* nesting. `TaskStorage` detected in args →
  `NotImplementedError`; `TaskStepContextProto.subslot` exists but raises
  `NotImplementedError` (`log` works). No deletion. Without nesting there are
  no child jobs, so `depends_on` is always empty and poisoning never fires.
- **Part 3**: `TaskStorage` args + `step_ctx.subslot`, waiting/joining,
  child tracking, poisoning, slot-path cache keying, slot-path recovery from
  ServiceRef args.
- **Part 4**: deletion + dependency index.

## Testing approach

Part 2/3 tests run entirely in-process: task fns are ordinary impls registered
as external services on a `BasicStatelessServiceProvider`; both namespaces are
SQLite-backed (two namespaces in one session). Key scenarios: cache
hit/miss/conflict, concurrent identical requests joining one job, op-log event
ordering + run-group attribution + cross-group joins, a second runner instance
detecting a conflicting save, cancellation writing interrupted/cancelled
events, clean teardown waiting for jobs, poisoning chains (child fails → parent
incomplete even when it catches), children kept alive after parent failure,
coherence of `depends_on` snapshots, deletion blocked by dependents.
