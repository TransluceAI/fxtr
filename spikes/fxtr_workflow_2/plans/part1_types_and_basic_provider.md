# Part 1 — type-only modules + basic stateless service provider

> **Status: ✅ complete.** All steps below done and tested
> (`tests/test_workflows/test_basic_provider.py`, 6 tests passing; full
> `tests/test_services` suite still green). Scoping adjustment applied: the
> realization-map / op-log **entities** moved to part 2 (see step 4 note). One
> shared-module line added to `fxtr2/services/typed_service.py` to support
> self-referential `@service_protocol` return types (see step 4 / the
> implementation notes at the bottom).

Lands the **entire stable API surface** of the workflow system as type-only
modules (protocols, entities, errors) plus the one concrete impl that does not
need the runner: the external-only `BasicStatelessServiceProvider`. After this
part you can register external services and spawn them by handle; the runner
itself is part 2.

See [design/workflow_runner.md](design/workflow_runner.md) and
[design/stateless_service_provider.md](design/stateless_service_provider.md)
for the design these implement.

## Scope

- All protocol definitions (`@service_protocol`) for both the provider and the
  task-facing surface.
- All entity types (handles, slot states, invocation, op-log events) and error
  types.
- `BasicStatelessServiceProvider` + helpers + tests.
- **Not** in this part: the `WorkflowRunner` (part 2), any nesting, any nix /
  sandbox impl (part 5). Protocols that only the runner implements are *defined*
  here but have no impl yet.

## Steps

### 1. Package scaffold

Create `v2/fxtr2/workflows/__init__.py` and
`v2/fxtr2/workflows/stateless_service_provider/__init__.py`. Pick a namespace
UUID for workflow entity/type ids (one module-level `WORKFLOWS_NS`, used by
`define_type`). Re-export the public names from the package `__init__`s.

### 2. `stateless_service_provider/handles.py` — handle entities

- `ServiceHandle[T](Entity, abc.ABC)` — empty marker base ("I identify a
  stateless service of protocol `T`"). Subclassing gives type inference a way
  to say "handle for `T`"; carries no fields itself.
- `ExternalServiceHandle(DataclassEntity, ServiceHandle[UnknownService])` with
  one field `key: EntityRef[UnknownEntity]`. Its FxtrID (a function of the key
  id) is the cache identity.
- Round-trip test target: store/load both, confirm stable ids.

(The nix handle entity types `NixWasmStatelessServiceHandle` /
`NixContainerStatelessServiceHandle` are part 5, not here.)

### 3. `stateless_service_provider/protocols.py` — provider protocols

Define three `@service_protocol`s (untyped on the wire, typed in the impl):

- `StatelessServiceSpawnerProto.spawn(handle: EntityRef[UnknownEntity]) ->
  ServiceRef[UnknownService]` — the capability the runner consumes.
- `StatelessServiceProviderProto` (registration surface): `register_external(key,
  connector) -> EntityRef[ExternalServiceHandle]` and `spawner() ->
  ServiceRef[StatelessServiceSpawnerProto]`.
- `ExternalConnectorProto.connect() -> ServiceRef[UnknownService]`.

Plus the python helper `spawn_resolved(spawner, handle_ref:
EntityRef[ServiceHandle[T]]) -> T`: calls `spawner.spawn(...)`, retypes the
result via `replace_service_type(T)` (T recovered from the handle annotation),
and `.resolve()`s it. (Tracked followup: make `spawn` itself typed and drop
this helper.)

### 4. `task_types.py` — task-facing protocols + errors

All type-only here; impls are part 2.

> Scoping note (adjusted during impl): the realization-map + op-log **entity**
> definitions (`RunGroup`, `WorkflowTaskInvocation`, `WorkflowTaskComplete`, the
> op-log events) are **deferred to part 2**. Their `args`/`kwargs` /
> `depends_on` representations are coupled to how the runner *builds* them
> (e.g. `FrozenFxtrCbor` uses raw CBOR-wrapper types, not a clean
> `DataclassEntity` field), so defining them here would mean shipping
> unexercised entities. Nothing in part 1 references them. Part 1 ships only the
> service protocols + errors below.

**Service protocols:**
- `TaskSlotProto`: `run(step_fn: EntityRef[UnknownEntity], *args:
  FxtrServiceData, **kwargs: FxtrServiceData) -> EntityRef[UnknownEntity]`;
  `subslot(key: str) -> ServiceRef[TaskSlotProto]`.
- `TaskStepContextProto`: `subslot(key: str) -> ServiceRef[TaskSlotProto]`;
  `log(message: str) -> None`.
- `TaskFnProto`: `run(step_ctx: ServiceRef[TaskStepContextProto], *args, **kwargs)
  -> EntityRef[UnknownEntity]`.
- `RunGroupProto`: `get_storage(path: str) -> ServiceRef[TaskSlotProto]`.
- `WorkflowRunnerProto`: `start_run_group(name, description) ->
  ServiceRef[RunGroupProto]` — defined for completeness / future RPC, not
  mounted in v0.

**Errors:** `WorkflowError` base; `TaskConflictError`, `TaskFailedError`
(carries slot path + kind + reason), `SlotDeletionBlockedError`.

(Entities — `RunGroup`, `WorkflowTaskInvocation`, `WorkflowTaskComplete`,
op-log events — are defined in part 2, see scoping note above.)

### 5. `stateless_service_provider/basic_provider.py`

- `BasicStatelessServiceProvider(TypedServiceImpl[StatelessServiceProviderProto])`.
  All internal state (the `key-id -> connector ref` registration map, the
  mounted spawner sub-impl, sub-mount bookkeeping) lives in its
  `_fxtr_mount_lifespan` handler — deliberately exercising the impl
  mount-lifespan system rather than an external CM.
- `register_external(key, connector)`: build the `ExternalServiceHandle(key)`,
  record `handle.key.id -> connector` (bridge the connector ref into the
  provider's scope so it outlives the call), return the handle ref.
- `spawner()`: mint/return the spawner sub-service. **Capability split**: the
  spawner is a *separate* mounted sub-impl that holds only a read-capability
  into the registration map — holding the spawner ref cannot reach
  `register_external`. (Sub-impl is a small `TypedServiceImpl[
  StatelessServiceSpawnerProto]` closed over the shared registration state.)
- spawner `spawn(handle_ref)`: load the handle; for `ExternalServiceHandle`,
  look up the connector by `handle.key.id` (unknown → error), `connect()`,
  return the ref. (Dispatch on handle type is a single case here; part 5 adds
  the nix cases.)
- `wrap_singleton(scope, ref) -> ServiceRef[ExternalConnectorProto]` helper:
  mounts a connector whose `connect()` returns a known-stateless singleton ref.

### 6. Tests (`tests/test_workflows/test_basic_provider.py`)

In-process, SQLite/ memory scopes. Cover: register + spawn round-trip;
`spawn_resolved` typing; unknown-key spawn error; capability split (a resolved
spawner ref cannot call `register_external`); `wrap_singleton`; handle entity
id stability; provider mount-lifespan setup/teardown (state lives in the
lifespan, nothing leaks after unmount).

### 7. Update overviews

Add `fxtr2/workflows/` to `v2/CODE_OVERVIEW.md` (new top-level subpackage) and
create `fxtr2/workflows/LOCAL_CODE_OVERVIEW.md` describing the module layout and
the type-only-vs-impl split. (Last step; also add an intermediate overview
touch after step 5 since the provider is a feature worth recording.)

## Done when

External services can be registered and spawned by handle through the provider
+ spawner capability split, all protocols/entities/errors are defined, and the
provider tests pass. The runner can be built against this in part 2.
