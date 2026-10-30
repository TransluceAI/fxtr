# Migrating from v1 SessionManager to TransitionalSessionManager

This guide describes how to migrate from the v1 `SessionManager` (in `fxtr.api.session_manager`) to the v2 `TransitionalSessionManager` (in `fxtr2.legacy_compat.transitional_session_manager`).

## Overview of Changes

The TransitionalSessionManager runs v1-style tasks but uses the v2 storage system. Key differences:

1. **Imports from both fxtr and fxtr2** - You'll import v1 types (tasks, plans, config) from `fxtr` and the new session manager infrastructure from `fxtr2`.

2. **Explicit namespace management** - You directly create and manage `FxtrNamespace` instances for storage, rather than relying on collections and the session manager's internal storage.

3. **Task upgrading** - v1 `Task` objects must be "upgraded" to `TaskWithPredecessors` before they can be invoked.

4. **Different invocation methods** - Instead of `materialize()`, you use `invoke_task()` or `materialize_all_tasks()`.

## Setup: Namespaces and Session Manager

### Required Namespaces

You need at least two namespaces:

1. **Run cache namespace** - Stores completed runs, keyed by task ID. This is the primary storage for your experiment results.

2. **Upgrade cache namespace** - Caches the v1→v2 entity translations. This enables looking up upgraded tasks by their original v1 CID.

Optionally, you may want a third namespace for **user-controlled storage** where you can pin entities under friendly names.

### Recommended: Git-backed Namespaces

For version-controlled storage, use `GitFxtrNamespace`. Each namespace points to a separate `fxtr_store.toml` file, but they can live in the same git repository:

```
my_project/
├── fxtr.toml                    # v1 fxtr config (for task definitions)
├── storage/
│   ├── run_cache/
│   │   └── fxtr_store.toml      # Run cache namespace
│   ├── upgrade_cache/
│   │   └── fxtr_store.toml      # Upgrade cache namespace
│   └── user_storage/
│       └── fxtr_store.toml      # Optional: friendly-named storage
```

To initialize a new git namespace (one-time setup):

```python
from fxtr2.data.fxtr_context_api import GitFxtrNamespace

# Initialize each store (only needed once)
await GitFxtrNamespace(Path("storage/run_cache")).create(name="run_cache")
await GitFxtrNamespace(Path("storage/upgrade_cache")).create(name="upgrade_cache")
```

The `create()` method automatically generates:
- `fxtr_store.toml` - the main config file (version-controlled)
- `.gitignore` - ignores local config files, sync locks, and dirty mark directories

You don't need to create these files manually.

### Context Manager Pattern

The namespaces and session manager are all async context managers that should be nested:

```python
from pathlib import Path

from fxtr.api import fxtrconfig
from fxtr2.data.fxtr_context_api import GitFxtrNamespace
from fxtr2.legacy_compat.data import UpgradeCache
from fxtr2.legacy_compat.transitional_session_manager import TransitionalSessionManager

config = fxtrconfig.parse_fxtr_config(Path("fxtr.toml"))

async with (
    GitFxtrNamespace(Path("storage/run_cache")) as run_cache_ns,
    GitFxtrNamespace(Path("storage/upgrade_cache")) as upgrade_cache_ns,
    TransitionalSessionManager.create(
        config=config,
        run_cache_namespace=run_cache_ns,
        upgrade_cache=UpgradeCache(upgrade_cache_ns),
        in_process_python=True,  # or False for subprocess execution
    ) as mgr,
):
    # Work with mgr here
    ...
```

The scopes nest automatically—no need to manually create a combined `FxtrScope`.

## Upgrading v1 Tasks

Before you can invoke a task, you must upgrade it from a v1 `Task` to a `TaskWithPredecessors`.

### Basic Upgrade

```python
from fxtr2.legacy_compat.transitional_rundata import TaskWithPredecessors

# v1_task is a fxtr.object_model.core.Task
v2_task = await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)
```

### Dependency Order Requirement

**Important:** Tasks must be upgraded in dependency order. If task B depends on task A's result, you must upgrade task A before upgrading task B.

This is because `upgrade_from` looks up predecessors from the upgrade cache. If a predecessor hasn't been upgraded yet, the lookup will fail.

(Aside: the reason this is required is that the v2 system enforces stronger consistency guarantees than v2. In the v1 system, tasks could refer to previous tasks by content-addressed ID, but it was NOT guaranteed that the previous tasks would actually be stored somewhere reachable, and there was no way to actually look up a task by its unique ID. In the v2 system, unless directly opted out of, any reference to a previous object by its ID comes with a promise that the referenced object can be loaded. This means that, in order to construct an idiomatic v2 task, we need its predecessors to be available somewhere; in this case that is the upgrade cache.)

### Safe to Upgrade Multiple Times

It's safe to call `upgrade_from` on the same task multiple times—subsequent calls will just recompute the same cached result (since the v2 version of any given v1 task is deterministic). If you know you have already upgraded it, you can also just look it up in the cache. This means you can upgrade tasks immediately after creating them, even if you don't store the returned value:

```python
# These two patterns are equivalent:

# Pattern 1: Upgrade and keep reference
v2_task = await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)
# ... later use v2_task

# Pattern 2: Upgrade now, look up later
await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)
# ... later, retrieve from cache:
from fxtr.object_model import core as v1_core
v1_task_cid = v1_core.entity_cid(v1_task)
v2_task_ref = await upgrade_cache.lookup_upgraded(v1_task_cid, TaskWithPredecessors)
v2_task = await v2_task_ref.load()
```

### Typical Workflow

If your experiment defines tasks in dependency order (which is natural since later tasks reference earlier ones), you can upgrade them in the same order:

```python
upgrade_cache = UpgradeCache(upgrade_cache_ns)

# Upgrade in dependency order
v2_tasks = []
for v1_task in [task_a, task_b, task_c]:  # already in dependency order
    v2_task = await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)
    v2_tasks.append(v2_task)
```

## Activating Collections

Just like v1, you need to activate a collection to load runtime bindings (secrets, coordinator configs, etc.):

```python
await mgr.activate_collection("default")
```

This is the same API as v1's `SessionManager.activate_collection()`.

## Invoking Tasks

### Single Task: `invoke_task()`

```python
result = await mgr.invoke_task(v2_task)
```

The `cache_behavior` parameter controls caching:
- `"auto"` (default): Return cached result if available, otherwise run the task
- `"require_cached"`: Only return cached result, error if not cached
- `"require_uncached"`: Always run the task, error if already cached

### Multiple Tasks: `materialize_all_tasks()`

For running multiple tasks with automatic dependency ordering:

```python
from fxtr2.legacy_compat.transitional_rundata import TaskToRunMap

task_to_run_map: TaskToRunMap = await mgr.materialize_all_tasks(
    v2_tasks,
    include_predecessors=False,  # Set True to auto-include predecessor tasks
)
```

This launches tasks concurrently while respecting dependencies—each task waits for its predecessors to complete before starting.

**Note:** Unlike v1's `materialize()`, this method does not take a `message` parameter. Run metadata is set at the task level.

## Retrieving Results

There are two ways to retrieve results after materialization:

### Method 1: Via `instantiate()` (v1 style)

This works like v1—you instantiate the task's result handle to get a retriever:

```python
# Get the v1 task's result handle
result_handle = v1_task.result_handle

# Instantiate it to get a retriever
retriever = await mgr.instantiate(result_handle)

# Retrieve the actual result
result = await retriever.retrieve()
```

### Method 2: Via `RunWithPredecessors` (v2 style)

Access the run directly from the task-to-run map:

```python
# Get the run for a specific task
v2_task_ref = v2_task.store()
run_ref = task_to_run_map.runs[v2_task_ref]
run = await run_ref.load()

# Load the run data (wrapped in LegacyCompat)
run_data_compat = await run.run_data.load()
result = run_data_compat.value
```

Or look up the run via the session manager:

```python
run = await mgr.get_run_for_task(v2_task)
if run is not None:
    run_data_compat = await run.run_data.load()
    result = run_data_compat.value
```

## Inspecting Task and Run Graphs

The v2 types make it easy to walk the dependency graph:

### Walking Task Predecessors

```python
async def print_task_tree(task: TaskWithPredecessors, indent: int = 0):
    v1_task = task.legacy_task.value
    print("  " * indent + f"Task: {v1_task}")
    for pred_ref in task.predecessors:
        pred = await pred_ref.load()
        await print_task_tree(pred, indent + 1)
```

### Walking Run Predecessors

```python
async def print_run_tree(run: RunWithPredecessors, indent: int = 0):
    task = await run.task.load()
    v1_task = task.legacy_task.value
    print("  " * indent + f"Run of: {v1_task}")
    for prev_run_ref in run.previous_runs:
        prev_run = await prev_run_ref.load()
        await print_run_tree(prev_run, indent + 1)
```

## Saving with Friendly Names

The run cache stores runs by task ID (not human-readable). To save entities under friendly names, use a separate namespace:

```python
async with GitFxtrNamespace(Path("storage/user_storage")) as user_ns:
    # Save a task under a friendly name
    await user_ns.pin("my_experiment:final_task", v2_task)

    # Save a run map
    await user_ns.pin("my_experiment:run_2024_01_15", task_to_run_map)

    # Later, retrieve by name
    retrieved_map = await user_ns.retrieve(
        "my_experiment:run_2024_01_15",
        TaskToRunMap
    )
```

## Complete Example

See `v2/examples/session_manager_porting/v2_version/run_demo.py` for a complete working example that demonstrates all of these concepts.

## Summary of API Changes

| v1 SessionManager | v2 TransitionalSessionManager |
|-------------------|-------------------------------|
| `SessionManager.create(config, ...)` | `TransitionalSessionManager.create(config, run_cache_namespace, upgrade_cache, ...)` |
| Use v1 `Task` directly | Upgrade to `TaskWithPredecessors` first |
| `materialize(tasks, message=...)` | `materialize_all_tasks(tasks)` or `invoke_task(task)` |
| `instantiate(plan)` | `instantiate(plan)` (same) |
| `activate_collection(name)` | `activate_collection(name)` (same) |
| Collections manage storage | You manage `FxtrNamespace` instances |
| `drop_all_runs()` | Not supported (storage is append-only; revert via git) |

## Limitations

The TransitionalSessionManager does not support:

- **Workflows** - Only `Task` is supported, not `Workflow`. `WorkflowStepResultHandle` and `WorkflowResultHandle` will error.
- **PinnedResultHandle** - Not supported.
- **Run deletion** - The cache is append-only. To "delete" runs, revert in git or copy desired runs to a new namespace.
