
# Session manager for transitional state: Initial sketch

Main goals:
- Migrate from v1 storage system to v2 storage system to enable pushing-to-hub
- Start testing v2 storage system with real data, to prepare for future
- Fix issue regarding runset loading being absurdly slow (which is, in turn, due to complex and unnecessary reservation system)

Approach:
- Copy plan system from existing session manager; this part works fairly well
- Simplify how run state management is done:
    - Append-only runset namespaces: Each runset is a fxtr2 namespace. Runset managers always add new runs to an existing namespace, there is no way to "delete" a run from a runset
    - No reservations for pending task handles: It is NOT allowed to request a task result handle for a task that hasn't completed. Caller is responsible for sequencing their own tasks. Auto-sequencing can be added at a separate layer.
    - Manual run workers: Tasks are NOT run in separate workers owned by the manager. Instead they are owned by the user. Tasks fail at completion time if they cannot save into the run cache. Run cache is the source of truth.

How to do common things under this system:
- To revert a runset to an earlier state: don't drop runs imperatively but instead just revert in source control
- To reduce to a subset of runs: don't drop runs imperatively but instead copy the relevant runs out into a new runset
- To run tasks: manually order the tasks and invoke them in the order that they are needed

---

# Detailed Design Notes

## Key Components

### RunCacheManager

The `RunCacheManager` is responsible for storing and retrieving completed runs. It wraps a `FxtrNamespace` and stores `RunWithPredecessors` entities, keyed by the `FxtrID` of their corresponding `TaskWithPredecessors`.

Key operations:
- `save_run(run)`: Save a completed run. Validates that all direct predecessor runs are already in the cache before saving.
- `get_run_for_task(task)`: Look up a run by its task. Returns `None` if not found.
- `check_consistency()`: Exhaustive validation that all runs and their transitive predecessors are properly stored.
- `make_task_handle_worker(plan)`: Create a `TaskHandleFromRunCacheWorker` for serving a `TaskResultHandle` plan from the cache.

The manager also holds a reference to the `UpgradeCache`, which is needed to map v1 Task CIDs to their canonical `TaskWithPredecessors` entities.

### TaskWithPredecessors Identity

There is exactly one canonical `TaskWithPredecessors` for each v1 `Task`. The predecessors are deterministically computed from the task's function plan (by finding all `TaskResultHandle` dependencies). The `UpgradeCache` tracks the mapping from v1 CID to the canonical `TaskWithPredecessors`.

This means:
- The cache key is the `FxtrID` of the `TaskWithPredecessors`, not the v1 CID
- To look up a run for a v1 `TaskResultHandle`, you first look up the `TaskWithPredecessors` via `UpgradeCache`, then look up the run via `RunCacheManager`

### TaskHandleFromRunCacheWorker

A `PlanWorker` that serves `TaskResultHandle` plans by loading the corresponding run from the cache. If the run is not in the cache, it raises an error (the task must be invoked first).

The `_build_impl` method:
1. Gets the v1 Task CID from the `TaskResultHandle`
2. Looks up the `TaskWithPredecessors` via `UpgradeCache`
3. Looks up the `RunWithPredecessors` via `RunCacheManager`
4. Returns the run data and a v1 `RunProvenance` entity

### TransitionalSessionManager

The main orchestrator. Combines:
- A `PlanManager` for building and caching plan workers
- A `RunCacheManager` for storing completed runs
- An `UpgradeCache` for v1→v2 entity translation
- Runtime environment management (same as v1)

## The "No Reservations" Model

In v1, the session manager used a reservation system that allowed requesting `TaskResultHandle` plans before the task had completed. The system would track pending reservations and fulfill them when tasks completed.

The transitional model removes this complexity:

**Invariant**: You cannot `instantiate()` a plan that depends on a `TaskResultHandle` until that task has been `invoke_task()`'d and completed.

This simplifies the implementation significantly:
- No need to track reservation state across multiple calls
- No need for complex dependency resolution between pending tasks
- The run cache is the single source of truth

The tradeoff is that callers must manually sequence their task invocations.

## Entry Points and Their Interactions

### `invoke_task(task, cache_behavior)`

Runs a single task and saves the result to the cache.

- `cache_behavior="require_cached"`: Just load from cache, error if not found
- `cache_behavior="require_uncached"`: Must run the task, error if already cached
- `cache_behavior="auto"`: Load from cache if available, otherwise run

The session manager tracks `_currently_invoking: set[FxtrID]` to prevent concurrent invocations of the same task.

### `materialize_all_tasks(tasks, include_predecessors)`

Runs multiple tasks with automatic dependency ordering.

1. If `include_predecessors=True`, expand the task set to include all predecessors
2. Topologically sort by predecessor relationships
3. Launch tasks concurrently, but each task waits for its predecessors to complete before starting
4. If any task fails, cancel all remaining tasks (via anyio.TaskGroup)

This is the primary way to run a batch of tasks. Within a single `materialize_all_tasks` call, tasks can depend on each other. But you cannot call `materialize_all_tasks` for tasks that depend on tasks from a *different* `materialize_all_tasks` call that's still running.

### `instantiate(plan)` / `instantiate_with_provenance(plan)`

Build a plan and return its implementation.

Uses the `PlanManager` to resolve the plan. For `TaskResultHandle` plans, this creates a `TaskHandleFromRunCacheWorker` which loads from the cache. If the task hasn't been invoked yet, this errors.

Typical usage pattern:
```python
# First, invoke the tasks
await manager.materialize_all_tasks({task_a, task_b})

# Then, instantiate plans that depend on them
result = await manager.instantiate(some_plan_using_task_a_result)
```

### `activate_collection(collection)`

Extracts runtime environment configuration from a collection:
- Secret declarations
- Runtime env config assignments
- Runtime coordinator bindings

Validates there are no conflicts (e.g., multiple assignments for the same plan). Same logic as v1.

## Worker Builder Logic

The `_make_worker` method dispatches based on plan type:

| Plan Type | Worker |
|-----------|--------|
| `PyImplPlan` | `ProviderBasedPlanWorker` with python provider |
| `ExternalServiceIdentifier` | `RuntimeEnvironmentManager.make_runtime_env_worker()` |
| `ConstantHandle` | `ProviderBasedPlanWorker` with `ConstantProvider` |
| `PinnedResultHandle` | **Error** (not supported) |
| `TaskResultHandle` | `RunCacheManager.make_task_handle_worker()` |
| `WorkflowStepResultHandle` | **Error** (not supported) |
| `WorkflowResultHandle` | **Error** (not supported) |

## Concurrency Model

Within `materialize_all_tasks`:
- Tasks are launched concurrently in an `anyio.TaskGroup`
- Each task waits on completion events of its predecessor tasks before starting execution
- If any task fails, the TaskGroup cancels all other tasks

Outside of `materialize_all_tasks`:
- `invoke_task` for a single task is straightforward
- Concurrent `invoke_task` calls for *different* tasks are allowed
- Concurrent `invoke_task` calls for the *same* task error (checked via `_currently_invoking`)

## Predecessor Validation

`save_run()` validates that direct predecessor runs are in the cache at their expected locations. This is a fast check that catches the most common errors (forgetting to invoke a predecessor).

`check_consistency()` does an exhaustive check of the entire cache:
- Every pin value is a `RunWithPredecessors`
- The task's `FxtrID` matches the pin key
- All transitive predecessors are present and valid

## Namespace Ownership

The caller is responsible for:
- Creating the `run_cache_namespace` (a `FxtrNamespace`)
- Creating the `upgrade_cache` (wrapping another `FxtrNamespace`)
- Ensuring these stay open for the session's lifetime
- Closing them when done

The `TransitionalSessionManager.create()` context manager does NOT own these namespaces.

## Limitations

- **No workflow support**: Only `Task` is supported, not `Workflow`. `WorkflowStepResultHandle` and `WorkflowResultHandle` plans will error.
- **No PinnedResultHandle support**: `PinnedResultHandle` plans will error.
- **No cross-call task dependencies**: You cannot `instantiate()` a `TaskResultHandle` for a task that's currently being invoked in another call. The task must complete first.
- **No run deletion**: The cache is append-only. To "delete" runs, revert in source control or copy to a new namespace.

## Helper: spawn_session_manager_in_asyncio_task

A convenience function that:
1. Takes factory functions for creating namespaces
2. Enters the async contexts for both namespaces
3. Creates the session manager
4. Returns both the manager and the background task

This allows running the session manager in a background task while the caller does other work.
