# Workflows Local Code Overview

A durable, trace-based task/workflow system on top of fxtr2. Tasks (observably
pure, possibly-nondeterministic computations bound to args) are run into named
**slots** of a **realization map**, executed by **task functions** which are
stateless services spawned from content-addressed **service handle** entities.

Design docs: `plans/` (`overview.md`, the two
`design/` docs, `open_questions.md`, `followups.md`, and the `partN_*.md`
plans).

## Current state

Implemented: the task-facing type surface (protocols + errors), the external-only
stateless service provider, the realization-map / op-log **entities**, the
**cache-key canonicalization**, and the **single-level runner** (`runner.py`):
run groups, slot capabilities, and task **execution** — `run` spawns the task
fn, runs it once per slot in its own scope (joining concurrent callers),
projects + pins the result as a `WorkflowTaskComplete`, and records per-request
op-log events. **Nesting** works too: a task fn can run children via
`step_ctx.subslot` and via `TaskStorage` slots passed to it as args, with child
tracking, `depends_on`, and poisoning (a child failure poisons the parent). A
slot arg is recognized by handle identity, cache-keyed by its root-relative path,
and a run into a slot already in the request chain raises `WorkflowCycleError`.
Still **not implemented**: **slot deletion** — see the plans in
`plans/`.

## File overview

| File | Purpose |
|---|---|
| `_ns.py` | `WORKFLOWS_NS` — shared namespace UUID for all workflow entity / service type ids (leaf module, avoids import cycles). |
| `task_types.py` | Task-facing `@service_protocol`s — `TaskSlotProto` (`run`, `subslot`), `TaskStepContextProto` (`subslot`, `log`), `TaskFnProto` (`run`), `RunGroupProto` (`get_storage`), `WorkflowRunnerProto` (`start_run_group`) — plus error types (`WorkflowError`, `TaskConflictError`, `TaskFailedError`, `SlotDeletionBlockedError`). Protocols + errors only; the implementing runner lives elsewhere. |
| `entities.py` | Realization-map / op-log entities: `RunGroup`; `WorkflowTaskInvocation` (cache-key entity; `args`/`kwargs` as `FrozenFxtrCbor`) + `WorkflowTaskComplete` (slot result + `depends_on`); `WorkflowTaskStorageSlotPlaceholder` (cache-key stand-in for a `TaskStorage` slot arg, holding its root-relative `path`); the op-log event union (`RunGroupOpened`/`Closed`; `RunStarted` (per-request, with `launched_job`) / `RunCompleted` (per-request, with `outcome`); `SlotsDeleted`) + the `WorkflowOutcome` literal (incl. `"conflict"`). |
| `cache_key.py` | `canonicalize` (one `FxtrServiceData` value → `FrozenFxtrCbor`, refs → bare `RawWeakFxtrID` leaves) + `bare_value` (provenance-stripped args forwarded to the task fn) + `build_invocation` (→ a `WorkflowTaskInvocation` whose stored id is the deterministic, provenance-blind cache key) + `strip_provenance`. An optional `slot_resolver` recognizes a task-storage slot `ServiceRef` arg and returns its path: `canonicalize` encodes it as a stored `WorkflowTaskStorageSlotPlaceholder` (so the key depends on the path), `bare_value` as a `SlotArgMarker` (re-minted by the runner before the task fn runs); without a resolver every `ServiceRef` is rejected. |
| `runner.py` | `WorkflowRunner` (async CM owning a child scope + job task group + the two namespaces; not a service), `RunGroupHandle` (plain-python; owns the group's scope), the internal `_SlotImpl` / `_StepContextImpl` services, and the `_Job` / `_ChildObs` records. Run groups are the `run_group(name, desc)` async CM (records `RunGroupOpened`/`Closed`); each group has its own scope where `get_storage` / `subslot` mount `TaskSlotProto` slots (freed on group close; resolve against `RunGroupHandle.scope`). All slot mounts route through `_mount_slot`, which records each slot in the `_slots` registry (`dict[ServiceKey, str]` → root-relative path) so a slot passed back as an arg is recognized by handle identity (`_make_slot_resolver`); `_materialize_arg` re-mints each `SlotArgMarker` as a job-bound slot before the task fn runs. **Responsibility split:** the *job* (`_run_job`) runs the task fn once in an isolated job scope (spawner resolved *there*) and *returns a structured outcome* — no op log, no raising. The *requester* (`_request_run`, per `run` call) owns the op log (`RunStarted`/`RunCompleted` per request, attributed to its run group) and maps the outcome to one raised exception; it also runs the cycle check (`path` in the parent job's `chain` → `WorkflowCycleError`). **Nesting:** a `_SlotImpl` carries `job` (the job it's bound to, `None` for top-level); `step_ctx.subslot` mounts a job-bound subslot on the job scope, and an incoming `TaskStorage` slot arg is re-minted job-bound; a run on a job-bound slot is tracked under that job (`_ChildObs` in `job.children`). On success `_complete_success` waits for children, poisons (`child_incomplete`/`child_conflict`) if any failed, else pins `WorkflowTaskComplete` with `depends_on`; on failure `_run_job` finalizes immediately (no waiting). |
| `stateless_service_provider/handles.py` | `ServiceHandle[T]` — abstract generic `Entity` marker ("I identify a stateless service of protocol `T`"; same pattern as `StreamTag`); `ExternalServiceHandle(key)` concrete handle for promised-stateless external services. |
| `stateless_service_provider/protocols.py` | The **shared/base** surface: `StatelessServiceSpawnerProto` (`spawn`, the only capability the runner consumes) and `ExternalConnectorProto` (`connect`, the external-service contract shared by any provider supporting external services). Plus `service_handle_target_type` (recover `T` from a `ServiceHandle[T]` subclass) and the `spawn_resolved` helper. **Registration surfaces are provider-specific and do not live here.** |
| `stateless_service_provider/basic_provider.py` | `BasicStatelessServiceProviderProto` (the external-only provider's registration surface: `register_external` + `spawner`), `BasicStatelessServiceProvider` (the impl, `TypedServiceImpl`) + its `_SpawnerImpl` sub-service (capability split) + `_SingletonConnector` / `wrap_singleton` helper + `UnknownHandleError`. |

## Key concepts

- **Service handle** — a content-addressed entity identifying a stateless
  service. `ServiceHandle[T]` is an abstract marker (adds no fields, no
  `_fxtr_store_impl`); concrete handles subclass `DataclassEntity` +
  `ServiceHandle[<proto>]`. The handle id is the cache/registration identity
  (provenance-blind `FxtrID`).
- **Base interface vs provider-specific registration** — the only capability
  the workflow runner (and the shared base) needs is the spawner
  (`StatelessServiceSpawnerProto`). *Registration* surfaces are
  provider-specific: the basic provider exposes
  `BasicStatelessServiceProviderProto` (`register_external` + `spawner`); a
  planned sandboxed provider (in `projects/fxtr_sandboxing`) would expose its
  own (nix flake registration). So the shared `protocols` module holds only the
  spawner + external-connector contract, and each provider defines its
  registration protocol next to its impl.
- **Capability split** — a provider mounts a separate `_SpawnerImpl`
  sub-service (`StatelessServiceSpawnerProto`) sharing the registration map but
  exposing only `spawn`. Holding the spawner ref cannot register anything.
- **Mount lifespan** — the basic provider keeps all per-mount state (the
  registration map, the mounted spawner ref) in the value yielded by
  `_fxtr_mount_lifespan` rather than on the instance, so the same impl mounted
  twice stays isolated. The spawner sub-service is mounted from inside the
  lifespan setup (sanctioned: setup runs authorized + with the scope active).
- **External registration** — `register_external(key, connector)` builds an
  `ExternalServiceHandle(key)`, keeps the connector resolvable for the
  provider's lifetime (bridges it from the call's input scope into the mount
  scope via `bridge_service_from_scope(..., keepalive_via="independent_lease")`,
  unless it already lives on the mount scope), and records it keyed by handle
  id. `spawn` looks the connector up by the handle's id and calls `connect()`.
- **What is / isn't a service** — the runner and run groups are plain-python
  async context managers, *not* services (`RunGroupProto` / `WorkflowRunnerProto`
  exist only as the eventual RPC surface, unmounted in v0). The first service a
  caller touches is the `TaskSlotProto` returned by `get_storage`. Slot
  capabilities are bare `ServiceRef`s (no wrapper classes); `subslot` is async
  (mints a new runner-side mount); callers `.resolve()` a returned ref against
  `WorkflowRunner.scope` before calling it.
- **Op log is per-request; realization map is per-job.** Op-log emission lives
  in the *requester* (each `run` call), not the physical job: every request
  emits its own `RunStarted` + `RunCompleted` pair attributed to its run group,
  whether it started the job, joined an in-flight one, or cache-hit. So
  concurrent run groups racing one slot each get a self-contained started/
  completed pair, robust to the shared job failing or being cancelled. The job
  only touches the realization map (one `Complete` per slot) and returns a
  structured outcome. `RunStarted.launched_job` marks the one request that
  actually started the physical job.
- **Op log + owner** — every op-log event carries `owner` (which runner
  recorded it; default `{host}:{pid}:{nonce}`), stamped in `_append_oplog`.
  Recommended deployment backs the realization map + op log by the **same SQLite
  db file** (two namespaces over one content-addressed store → no entity-data
  duplication when events reference results).
- **Cache key** — a task's cache key is the content-addressed id of its
  `WorkflowTaskInvocation`. `cache_key.build_invocation` canonicalizes the
  `FxtrServiceData` arg tree into `FrozenFxtrCbor` so the key is *deterministic*
  and *provenance-blind* (refs become bare `RawWeakFxtrID` leaves; `step_fn`
  provenance is stripped too) while the arg closures still travel with the
  invocation and stay loadable (the same `FrozenFxtrCbor`-with-`Raw*FxtrID`-leaves
  pattern as `SandboxProvenanceContext.config_snapshot`). Live `ServiceRef` args
  are rejected unless they are one of the runner's own **task-storage slots**: a
  slot arg contributes its root-relative path to the key (encoded as a stored
  `WorkflowTaskStorageSlotPlaceholder` entity), so two calls passing "the slot at
  this path" cache identically. Any other handle entity is spawned inside the
  task as usual.
- **Slot-arg recognition + cycle detection** — the runner recognizes a
  `TaskStorage` slot passed back as a `ServiceRef` arg by handle identity: every
  slot it mounts is recorded in `_slots` (`dict[ServiceKey, str]` → path; relies
  on cap-IR bounce-back preserving handle identity, NQ-2), and an arg whose
  `ServiceKey` isn't registered is a foreign live service → rejected. Each job
  carries a `chain` of slot paths from the root; running into a slot already in
  the chain raises `WorkflowCycleError` (covers self- and transitive cycles).
  Run-group consistency (a run's slot args share its group) needs no explicit
  check — sibling group scopes are isolated, so a cross-group slot isn't reachable
  to encode as an arg. (The `_slots` registry grows for the runner's lifetime — a
  tracked followup.)

## Note on a shared-module dependency

`TaskSlotProto.subslot` returns `ServiceRef[TaskSlotProto]` — a self-referential
return type. This works because `_resolve_method_hints` in
`fxtr2/services/typed_service.py` injects the owning protocol class into
`localns` by name, so a `@service_protocol` method may reference its *own* class
in its annotations (the decorator resolves hints eagerly, before the class name
is bound in module globals).

## Tests

`tests/test_basic_provider.py` — register + spawn round-trip,
`spawn_resolved` typing, unknown-handle error, capability split, handle-id
stability, independent providers, connector bridging from a separate
scope/task.

`tests/test_invocation.py` — `canonicalize` over
primitives/refs/nesting, ServiceRef rejection, provenance-blindness;
`build_invocation` cache-key determinism / provenance-blindness / distinctness /
arg-closure travel; realization-map + op-log entity round-trips.

`tests/test_runner_skeleton.py` — run-group open/close op-log
events (owner-stamped), `get_storage` + `subslot` mounting, path/key validation,
over two namespaces on one shared db file.

`tests/test_runner_exec.py` — task execution against a task fn
registered as an external service: execute + cache hit (fn runs once),
cache-key conflict, task failure + retry (failures not pinned), concurrent runs
joining one job, symmetric cross-group `RunStarted`/`RunCompleted` +
`launched_job`, the `"conflict"` outcome, and full-runner-cancel writing
`RunCompleted(interrupted)`.

`tests/test_runner_nesting.py` — `step_ctx.subslot` nesting: a
parent runs a child and records `depends_on`, whole-tree cache reuse, and a child
failure poisoning the parent (even when caught).

`tests/test_runner_storage_args.py` — passing a `TaskStorage` slot
as a task arg: run into the passed slot + `depends_on`, path-based cache keying,
cross-group rejection (structural, via scope isolation), cycle detection, and
rejection of a foreign (non-slot) `ServiceRef`.
