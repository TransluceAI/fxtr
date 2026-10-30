# session_manager.py Overview

This file (~2720 lines) implements the session management system for fxtr, handling plan instantiation, task execution, and run lifecycle management.

## File Structure (by region)

| Region | Lines | Purpose |
|--------|-------|---------|
| Plan worker management | 38-335 | Manage plan lifecycles and implementations |
| Active run management | 341-1296 | Track runs, reservations, and dependencies |
| Environment management | 1300-1506 | Handle secrets, env vars, coordinator bindings |
| Materialization | 1510-2133 | Execute tasks and workflows |
| UV python management | 2137-2234 | Set up UV-based Python providers |
| Session manager API | 2238-2266 | Abstract interface |
| Session manager impl | 2268-2720 | Main implementation |

---

## Plan Worker Management (lines 38-335)

### `PlanWorker[T]` (line 38)
Abstract base class for workers that manage a single plan's lifecycle.

**Lifecycle states:** `pending` → `setup` → `ready` → `down`

**Key attributes:**
- `plan` - The plan being managed
- `dependency_workers` - Workers for dependency plans
- `is_current` - False when stale (new users should get a new worker)
- `user_count` - Reference count; worker shuts down when 0
- `impl` / `provenance` - The built implementation and its provenance

**Key methods:**
- `run_lifecycle()` - Main async task that builds the implementation
- `retrieve()` - Wait for and return the implementation
- `increment_user_count()` / `decrement_user_count()` - Reference counting
- `mark_stale()` - Mark this worker (and dependents) as stale

### `PlanManager` (line 229)
Manages plan workers and their lifecycles.

**Key methods:**
- `request_plan(plan)` - Get or create a worker for a plan (increments user count)
- `_run_worker(worker)` - Run a worker's lifecycle in the task group

### `ProviderBasedPlanWorker` (line 319)
Concrete worker that uses a `provider` to build its implementation via `provider.provide()`.

---

## Active Run Management (lines 341-1296)

### `RunReservation` (line 344)
Dataclass for a reserved slot for a run that hasn't completed yet.
- `task` - The task/workflow being reserved
- `fulfilled_or_cancelled_event` - Set when reservation resolves

### `RunNode` (line 367)
Node in the active run graph tracking state of a task/workflow run.
- `task_cid` - CID of the task/workflow
- `run_or_reservation` - Either a completed `Run` or a `RunReservation`
- `dependencies` / `dependents` - Graph edges
- `workflow_contains` / `workflow_next` - Workflow step tracking

### `WorkflowCommitmentNode` (line 406)
Tracks a promise to run a workflow to completion.
- `workflow_cid` - The workflow being committed to
- `advanced_or_cancelled_event` - Set when workflow advances or is cancelled

### Helper functions
- `_walk_all_tasks_in_object(target)` (line 442) - Find all tasks/workflows in an IPLD object
- `_topologically_sort_tasks(tasks)` (line 454) - Sort tasks by dependency order

### `TaskHandleWorker` (line 507)
Worker for task/workflow result handles. Attaches to `RunNode`s and waits for their completion.

### `ActiveRunManager` (line 634)
Central manager maintaining the active runset with consistency invariants.

**Key methods:**
- `_load_runset()` - Load existing runset from disk on init
- `add_completed_run(task, run)` - Add a completed run (validates dependencies)
- `create_reservation(task)` - Reserve a slot for a future run
- `fulfill_reservation(reservation, run)` - Complete a reservation with a run
- `cancel_reservation(reservation)` - Cancel a pending reservation
- `create_workflow_commitment(workflow)` - Promise to complete a workflow
- `fulfill_workflow_commitment(commitment)` / `cancel_workflow_commitment(commitment)`
- `drop_run_for_task(task_cid, ...)` - Remove a run from the active set
- `drop_all()` - Clear all runs
- `save_runset()` / `build_runset()` - Persist current state

---

## Environment Management (lines 1300-1506)

### `CoordinatorBindingWorker` (line 1304)
Worker that wraps a bound plan and provides its implementation with provenance that includes all transitive dependencies.

### `RuntimeEnvironmentManager` (line 1347)
Manages runtime environment configuration.

**Handles three types of bindings:**
- Secret declarations (`SecretFromEnvVarDeclaration`)
- Runtime env assignments (`RuntimeEnvConfigAssignment`)
- Runtime coordinator bindings (`RuntimeCoordinatorBinding`)

**Key methods:**
- `set_runtime_bindings(...)` - Update all bindings (invalidates stale workers)
- `make_runtime_env_worker(plan)` - Create a worker for an `ExternalServiceIdentifier`

---

## Materialization (lines 1510-2133)

### `MaterializationTracker` (line 1515)
Mutable tracker for a materialization request.
- `root` - The object being materialized
- `cached` / `completed` / `failed` / `cancelled` - Task outcome maps
- `scheduled_tasks` / `scheduled_commitments` - Pending work
- `done` - Event set when all work completes

### `MaterializationRecord` (line 1552)
Immutable entity recording a completed materialization (saved to disk).

### `_save_materialization_record(config, record)` (line 1569)
Saves a `MaterializationRecord` to the entity store and state directory.

### `MaterializationWorkerManager` (line 1604)
Manages workers that execute tasks and workflows.

**Key methods:**
- `materialize_task_soon(task, tracker)` - Schedule a single task
- `materialize_workflow_step_soon(workflow_step, tracker)` - Schedule one workflow step
- `materialize_full_workflow_soon(workflow, tracker)` - Schedule entire workflow to completion
- `materialize_all_soon(task_container, tracker)` - Materialize all tasks/workflows in a container

**Internal workers:**
- `_worker_for_materializing_task(...)` (line 1626) - Executes a single task
- `_worker_for_waiting_on_cached_reservation(...)` (line 1805) - Waits for existing reservation
- `_worker_for_materializing_full_workflow(...)` (line 1949) - Manages full workflow execution

---

## UV Python Management (lines 2137-2234)

### `_extract_uv_source_from_package(package_name)` (line 2141)
Extract UV source configuration from package metadata (handles editable/git installs).

### `_setup_uv_provider(config)` (line 2195)
Async context manager that sets up a `UVManagedPyImplProvider`.

---

## Session Manager (lines 2238-2720)

### `AbstractSessionManager` (line 2243)
Abstract base with `instantiate(plan)` method.

### `SessionManager` (line 2288)
Main implementation orchestrating all managers.

**Components:**
- `_plan_manager` - `PlanManager` instance
- `_active_run_manager` - `ActiveRunManager` instance
- `_runtime_env_manager` - `RuntimeEnvironmentManager` instance
- `_materialization_worker_manager` - `MaterializationWorkerManager` instance
- `_python_provider` - UV or in-process Python provider

**Key methods:**
- `create(config)` (line 2336) - Async context manager factory
- `instantiate(plan)` (line 2566) - Get implementation for a plan
- `instantiate_with_provenance(plan)` (line 2570) - Also returns transitive provenance
- `materialize(task_container, ...)` (line 2409) - Execute tasks and wait for completion
- `_materialize_soon(...)` (line 2368) - Schedule without waiting
- `drop_all_runs()` / `drop_runs(...)` - Clear runs from active set
- `activate_collection(collection)` (line 2593) - Load runtime bindings from a collection
- `_make_worker(plan)` (line 2478) - Factory method dispatching to appropriate worker type

### `spawn_session_manager_in_asyncio_task(config)` (line 2675)
Convenience function for interactive use (Jupyter, REPL). Returns `(SessionManager, Task)`.

---

## Key Concepts

**Plan Worker Lifecycle:**
Plans have workers that manage their setup, provide implementations, and track usage. Workers become "stale" when their dependencies change, causing dependents to also become stale.

**Active Runset:**
A consistent set of completed runs where every run's dependencies are also present. Maintains invariants for reproducibility.

**Reservations vs Runs:**
A `RunReservation` holds a slot before task execution; it's fulfilled with a `Run` when complete, or cancelled on failure.

**Workflow Commitments:**
A promise to run an entire workflow to completion, allowing downstream plans to optimistically wait for the final result.

**Materialization:**
The process of executing tasks/workflows and adding their runs to the active set. Tracked by `MaterializationTracker`, recorded in `MaterializationRecord`.
