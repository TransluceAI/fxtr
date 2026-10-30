# Fxtr (v1) Code Overview

This document describes the organization of the v1 `fxtr` library. For the next-generation system see the `v2` folder and its [v2/CODE_OVERVIEW.md](../../v2/CODE_OVERVIEW.md).

## What is Fxtr v1?

Fxtr v1 is a library for content-addressed, provenance-tracked data and task execution. Its main moving parts are:

- **Entities** — Linked immutable objects forming a content-hash-addressed DAG (IPLD/DAG-CBOR). Values are typed via TypeSpecs; entities are self-describing and identified by CIDs.
- **Call targets / impls** — Callable objects that implement some interface (`FxtrCallTarget`). Interfaces are defined with `interface_id` and `namespace_id`; implementations are produced from plans.
- **Plans** — Entities that declaratively specify how to construct a call target at runtime (e.g. `ExternalServiceIdentifier`, `ConfiguredPlan`, `ComputedPlan`). Plans can depend on other plans.
- **Providers** — Take plans and produce implementations (impls). Each provider supports a subset of plan types; the session manager routes plans to the right provider.
- **Tasks** — Entities that specify how to produce some output data, possibly taking other task outputs as input, in a content-addressed cache. Tasks depend on plans and use their impls at runtime via `TaskFunction`/`WorkflowFunction`.
- **Runs** — Records of running a task at a particular point in time. **Provenance sets** record how plans were resolved to impls for a run.
- **Collections** — Groups of entities (often including plans or tasks), with module-scoped tracking. Somewhat deprecated due to being “too magical.”
- **Session manager** — The recommended way to interact with the system: instantiate plans and run tasks. It manages plan workers, active runs, runtime env, and materialization.

## Terminology

- **Entity** — Immutable value with a content-addressed CID and an associated TypeSpec. Subclasses of `EntityBase`.
- **CID** — Content identifier (IPLD); used to refer to entities in the DAG.
- **TypeSpec** — Describes a set of values and how to serialize/deserialize them to/from IPLD.
- **Plan** — Entity describing how to build an implementation of an interface (`FxtrCallTarget`).
- **Impl / call target** — Concrete callable implementing an interface.
- **Provider** — Builds an impl from a plan (and dependency impls).
- **Task / Workflow** — Entity whose “function” is a plan; running it produces a **Run** with provenance.
- **RunSet** — Persistent set of runs (e.g. SQLite-backed) used by the session manager.
- **Locator** — Low-level object that can retrieve entity data (DAG-CBOR) for a set of CIDs and delegate to child locators.
- **Collection** — Group of entities with optional module-based tracking; used for discovery and CLI.

## File Structure

```
fxtr/
├── __init__.py
├── logging_config.py              # Logging configuration for the CLI/tools
├── object_model/                  # Core entity DAG, types, serialization
│   ├── core.py                    # EntityBase, TypeSpecs, interfaces, tasks, runs (see core.py.overview.md)
│   ├── core.py.overview.md        # Detailed line-range overview of core.py
│   ├── locator.py                 # Locator protocol and implementations (entity data retrieval)
│   ├── serialization.py           # SerializedWithFallbackLocator, SimpleJsonSerializer
│   ├── cbor_util.py               # DAG-CBOR encode/decode helpers (CID tag 42, cbrrr/cbor2)
│   ├── dag_util.py                # IPLDSubgraph, RecursiveSubgraphCollector, DAG traversal
│   ├── entity_util.py             # EntityAccessPath, EntityChildCollector, walk_ipld_path
│   ├── modulescoping.py           # Module-level collection scope tracking (traceback-based)
│   ├── collection_tracking.py     # Collections, blueprints, entity keys, extract_collection_from_module
│   └── collection_tracking_alternative.py  # Alternative collection design
├── impl_plans/                    # Plan types and helpers for building plans/tasks
│   ├── py_impl_plan.py            # PyImplPlan[T] — plan for Python impl of an interface
│   ├── plan_helpers.py            # build_plan, task_function, workflow_function, make_task_from, make_plan_from, bind_impl, bind_storage
│   └── plan_dep_util.py           # plan_deps (direct/recursive plan dependency extraction)
├── impls/                         # Concrete impl and RPC machinery
│   ├── impl_remote.py             # RemoteImpl — FxtrCallTarget that proxies to a remote impl
│   ├── implserver.py              # ImplServer — registry and server for impls
│   └── rpc.py                     # FxtrRPCConnection, sendable value↔CBOR, RemoteSubtargetImpl
├── providers/                     # Providers: plan → impl
│   ├── provider_base.py          # ImplProvider, SimpleCallableProvider, provider exceptions
│   ├── in_process_py_provider.py  # InProcessPyProvider — load Python impl from entrypoint in-process
│   ├── uv_py_provider.py          # UVManagedPyImplProvider — UV-managed env + subprocess impls
│   ├── uv_py_provider_entrypoint.py  # UVSpawnerEntrypoint — entrypoint for UV-spawned impl process
│   └── externals_provider.py     # SecretsProvider, TaskResultHandleProvider, ConstantProvider, RuntimeEnvConfigProvider, RuntimeCoordinatorBinding provider
├── api/                           # Session manager, CLI, backend runner
│   ├── session_manager.py        # Plan workers, active runs, materialization, SessionManager (see session_manager.py.overview.md)
│   ├── session_manager.py.overview.md  # Detailed region overview of session_manager.py
│   ├── backend_runner.py         # BackendRunner, TaskWaiter, launch tasks/workflows, run cache
│   ├── api_lib.py                # load_collection_from_entrypoint, collection/runset helpers
│   ├── fxtr_cli.py               # Typer CLI (fxtr ls, run, etc.)
│   ├── fxtrconfig.py             # Config loading (e.g. fxtr.toml)
│   └── task_launch_view/         # Optional TS/React task launch UI (package.json, task_launch_view.tsx)
├── builtin_util/                  # Built-in interfaces and helpers
│   └── watchable.py              # Watchable interface, _WatchablePublisher (for reactive polling)
├── debug_visualizer/              # Debug UI for entity graphs
│   ├── spec.md                    # Spec for the visualizer (stages 1–9)
│   ├── locator_server.py          # FxtrLocatorDebugServer — serves blocks and view routes
│   └── ts/                        # React/TypeScript frontend (main.tsx, HubValueView, EntityPage, etc.)
└── playground/                    # Ad-hoc tests, benchmarks, protocol experiments (not part of core API)
```

## Module Summary

### object_model/

**core.py** — The heart of v1. Defines:

- **Entity base**: `EntityBase`, `AnyFrozenIPLDable`, `freeze()`
- **TypeSpecs**: Primitives, collections (tuples, frozendicts, frozensets), unions, optionals, `TypeSpec_DataEntity`, `TypeSpec_Plan`, etc.
- **Data entities**: `DataEntityBase`, `DataEntitySchema`, `define_entity_type()`
- **Interfaces and call targets**: `InterfaceDefn`, `InterfaceSpec`, `FxtrCallTarget`, `ExternalServiceIdentifier`, `spec_of()`, `implements_interface()`
- **Plans**: `TypeSpec_Plan` (ExternalServiceIdentifier, ConfiguredPlan, ComputedPlan)
- **Tasks and runs**: `Task`, `TaskFunction`, `Workflow`, `WorkflowFunction`, `Run`, `RunSet`, handles
- **Runtime config**: `RuntimeEnvRetriever`, `SecretRetriever`, `RuntimeCoordinatorBinding`
- **Access paths**: `AccessPathStep_*` for traversing entity structures
- **Type registry**: `HUB_TYPE_SPEC_REGISTRY`, `ipld_to_type_spec()`, `ipld_entity_data_to_entity()`

See **object_model/core.py.overview.md** for line ranges and search tips.

**locator.py** — Protocol for locating entity data by CID without necessarily deserializing to Python. Defines `Locator` (e.g. `has_entity`, `retrieve`, `try_retrieve`), implementations such as `EmptyLocator`, and `CachingEntityLoader` for loading entities from a locator with weak-ref caching.

**serialization.py** — `SerializedWithFallbackLocator` (combines a serialized blob loader with a fallback locator), `SimpleJsonSerializer` for human-readable JSON serialization of entity graphs.

**cbor_util.py** — Helpers for encoding/decoding DAG-CBOR with CID support (tag 42), bridging cbrrr and cbor2.

**dag_util.py** — `IPLDSubgraph` (named_roots, sequence_roots, nodes), `RecursiveSubgraphCollector` visitor, and utilities for building/escaping subgraphs and converting to/from JSON for the debug visualizer.

**entity_util.py** — `EntityAccessPath`, `EntityChildCollector` visitor, `walk_ipld_path` for following an access path in IPLD.

**modulescoping.py** — Tracks “active” collection scope during module execution using stack inspection (e.g. for `extract_collection_from_module` and CLI discovery).

**collection_tracking.py** — Collections, blueprints, `LocalEntityKey`/`ImportedEntityKey`, conversion of Python objects to collection-owned entities, `extract_collection_from_module`. Used by the CLI and collection-based workflows. Somewhat deprecated.

### impl_plans/

**py_impl_plan.py** — `PyImplPlan[T]` — plan entity that describes a Python implementation of an interface (module, entrypoint, requirements).

**plan_helpers.py** — Helpers for building plans and task/workflow functions: `build_plan`, `task_function`, `workflow_function`, `make_task_from`, `make_plan_from`, `bind_impl`, `bind_storage`. Handles runtime plan dependencies (`DependencyAtRuntime`) and placeholder replacement.

**plan_dep_util.py** — `plan_deps(plan)` for direct and recursive plan dependency sets (used by session manager and providers).

### impls/

**impl_remote.py** — `RemoteImpl` — `FxtrCallTarget` that forwards calls to a remote process via RPC.

**implserver.py** — `ImplServer` — in-process registry of impls by plan CID and HTTP server for remote access.

**rpc.py** — `FxtrRPCConnection` for RPC over a stream, CBOR conversion for sendable values, `RemoteSubtargetImpl` for per-subtarget RPC proxies.

### providers/

**provider_base.py** — `ImplProvider` (async `provide(plan, dependencies, provenance_lock) -> (impl, provenance)`), `SimpleCallableProvider`. Exceptions: `ProviderUnsupportedPlanError`, `ProviderUnsupportedProvenanceLockError`, `InvalidPlanError`.

**in_process_py_provider.py** — `InProcessPyProvider` — loads the Python callable from a `PyImplPlan` entrypoint in the same process.

**uv_py_provider.py** — `UVManagedPyImplProvider` — creates UV-managed virtualenvs and runs impls in a subprocess; communicates via spawner/register protocol. Uses `UVSpawnerEntrypoint` from `uv_py_provider_entrypoint.py`.

**externals_provider.py** — Providers for “external” plans: secrets, task result handles, constants, runtime env config, and runtime coordinator bindings.

### api/

**session_manager.py** — Session management and task execution:

- **Plan workers**: `PlanWorker`, `PlanManager`, `ProviderBasedPlanWorker` — lifecycle (pending → setup → ready → down), staleness, user counts.
- **Active runs**: `RunReservation`, `RunNode`, `WorkflowCommitmentNode`, `ActiveRunManager` — runset invariants, reservations, workflow commitments.
- **Environment**: `RuntimeEnvironmentManager`, `CoordinatorBindingWorker` — secrets, env assignments, coordinator bindings.
- **Materialization**: `MaterializationTracker`, `MaterializationWorkerManager` — scheduling and running tasks/workflows, saving runs and materialization records.
- **Session manager**: `SessionManager` — `create()`, `instantiate(plan)`, `materialize(task_container)`, `activate_collection`, `drop_all_runs` / `drop_runs`.

See **api/session_manager.py.overview.md** for line regions and concepts.

**backend_runner.py** — `BackendRunner` — long-lived runner that can launch tasks/workflows (e.g. via `TaskWaiter`), manage run cache, and integrate with the session manager. Defines entities like `WorkflowStepLaunch` and protocols used for remote execution.

**api_lib.py** — `load_collection_from_entrypoint()`, helpers for loading collections and runset state (e.g. for CLI and tools).

**fxtr_cli.py** — Typer app: commands for listing collections, running tasks, config (e.g. `fxtr.toml`), and integration with debug visualizer and backend runner.

**fxtrconfig.py** — Configuration loading and validation for the CLI/backend.

### builtin_util/

**watchable.py** — `Watchable` interface (e.g. for polling) and `_WatchablePublisher` implementation used by backend/runner-style code.

### debug_visualizer/

Interactive debugger for the entity graph: browse entities by CID, view module/collection structure, entity definitions, and type-aware rendering. **locator_server.py** implements the server; **ts/** contains the React/TypeScript app. See **debug_visualizer/spec.md** for the full spec and stages.

### playground/

Experimental scripts, benchmarks, and protocol spikes (e.g. `dag_traversal`, `protocol_playground_*`, `json_entities`). Not part of the stable API.

## Where to Start

- **Entities and types**: `object_model/core.py` and **object_model/core.py.overview.md**
- **Plans and tasks**: `object_model/core.py` (TypeSpec_Plan, Task, Workflow, Run) and `impl_plans/plan_helpers.py`
- **Running tasks**: `api/session_manager.py` and **api/session_manager.py.overview.md**, then `api/backend_runner.py`
- **Providers**: `providers/provider_base.py`, then `in_process_py_provider` or `uv_py_provider`
- **Collections and CLI**: `api/api_lib.py`, `object_model/collection_tracking.py`, `api/fxtr_cli.py`
- **Locators and storage**: `object_model/locator.py`, `object_model/serialization.py`, `object_model/dag_util.py`

## Relation to v2

The **v2** package (`fxtr2` in the `v2` folder) is a redesign with a different data model (FxtrID, scopes, namespaces, segment-store SQLite, etc.). v2 includes a legacy compatibility layer for storing and running v1 entities. Eventually v2 is intended to subsume v1; for now v1 remains the main entrypoint for “session manager + tasks + plans + providers” workflows.
