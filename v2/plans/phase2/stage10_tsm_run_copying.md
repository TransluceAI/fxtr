# Stage 10: TSM Run Copying

This plan covers adding run copying functionality to the TransitionalSessionManager's RunCacheManager, including:
1. Copying runs between run cache namespaces
2. Converting legacy v1 runsets to the new run cache format
3. Demo updates

## Background

The `RunCacheManager` manages runs stored in an `FxtrNamespace`, keyed by task ID. Each run is a `RunWithPredecessors` which references:
- `task: EntityRef[TaskWithPredecessors]` - the task that was run
- `previous_runs: frozenset[EntityRef[RunWithPredecessors]]` - predecessor runs

The `UpgradeCache` maps legacy CIDs to FxtrIDs, allowing lookup of v2 entities from v1 CIDs.

## Part 1: `RunCacheManager.copy_runs_from`

### API

```python
async def copy_runs_from(
    self,
    other: RunCacheManager,
    selection: Sequence[RunWithPredecessors | TaskWithPredecessors],
    include_predecessors: bool,
) -> int:
    """Copy runs from another run cache namespace into this one.

    Args:
        other: The source RunCacheManager to copy from.
        selection: The runs (or tasks whose runs should be copied) to copy.
            TaskWithPredecessors are converted to their runs by lookup in `other`.
        include_predecessors: If True, also copy all predecessor runs recursively.

    Returns:
        The number of runs copied (excluding skipped duplicates).

    Raises:
        RuntimeError: If the UpgradeCaches don't match (different objects).
        RuntimeError: If a selected task has no run in the source cache.
        RuntimeError: If a predecessor run is missing from the source (consistency error).
        RuntimeError: If a run already exists in this cache with a different value (conflict).
    """
```

### Implementation

1. **Check UpgradeCache equality**: `self._upgrade_cache == other._upgrade_cache` (UpgradeCaches compare equal if they have the same underlying namespace)

2. **Convert tasks to runs**: For each item in selection:
   - If `RunWithPredecessors`, use directly
   - If `TaskWithPredecessors`, look up its run via `other.get_run_for_task()`
   - Error if task has no run

3. **Expand predecessors** (if `include_predecessors=True`):
   - Use a worklist algorithm similar to `materialize_all_tasks`
   - Track seen runs by FxtrID to avoid duplicates
   - For each run, add all `run.previous_runs` to the worklist

4. **Topological sort**: Order runs so predecessors come before dependents
   - Build dependency graph from `run.previous_runs`
   - Use Kahn's algorithm or similar

5. **Copy each run in order** (with tqdm progress bar):
   ```python
   copied_count = 0
   for run, run_ref in tqdm(sorted_runs, desc="Copying runs"):
       # Check if target already has a run for this task
       existing_ref = await self.get_run_ref_for_task(run.task)
       if existing_ref is not None:
           if existing_ref.ref != run_ref.ref:
               raise RuntimeError(
                   f"Conflict: task {run.task.ref} already has a different run"
               )
           continue  # Skip, same run already exists

       # Verify predecessor consistency (same check as save_run())
       # This ensures the "closed under predecessors" invariant
       for pred_ref in run.previous_runs:
           pred_existing = await self.get_run_ref_for_task(pred_ref)
           if pred_existing is None:
               raise RuntimeError(
                   f"Cannot copy run: predecessor {pred_ref.ref} not in target cache. "
                   "Use include_predecessors=True or copy predecessors first."
               )
           if pred_existing.ref != pred_ref.ref:
               raise RuntimeError(
                   f"Cannot copy run: predecessor has different run in target cache"
               )

       # Copy the pin from source namespace to target namespace
       pin_key = self._get_pin_key(run.task.ref)
       await self._run_cache_namespace.pin(
           pin_key,
           run_ref,
           scope=other._run_cache_namespace.scope  # Copy from source scope
       )
       copied_count += 1
   ```
   - The namespace's `pin_entity_from_arena` will recursively copy all referenced entities
   - The predecessor check ensures run caches are always "closed" under predecessors

6. **Return count** of actually copied runs (excluding skipped)

### Key Details

- The pin key is computed via `_get_pin_key(task_id)` - same sharding scheme
- Pins are copied from `other._run_cache_namespace` to `self._run_cache_namespace`
- The `pin` method handles copying entity data between scopes/arenas internally
- Scope handling: we pass `scope=other._run_cache_namespace.scope` to `pin()` so it reads from the source namespace's scope - no special scope nesting required from the caller

## Part 2: Legacy Runset Conversion

### New File: `v2/fxtr2/legacy_compat/runset_conversion.py`

### API

```python
async def convert_legacy_runset_to_run_cache(
    config: fxtrconfig.FxtrConfig,
    session_manager: TransitionalSessionManager,
) -> int:
    """Convert a v1 runset to the v2 run cache format.

    Loads all runs from the config's active runset and converts them to
    RunWithPredecessors entities in the session manager's run cache.

    Args:
        config: The fxtr config containing paths to the v1 data stores.
        session_manager: The TransitionalSessionManager with the target run cache.

    Returns:
        The number of runs converted.

    Raises:
        RuntimeError: If the runset references a predecessor not in the runset.
    """
```

### Implementation

1. **Load the legacy runset**:
   ```python
   runset = api_lib.load_active_runset(config)
   if not runset.runs:
       return 0
   ```

2. **Create the run data loader**:
   ```python
   run_data_locator = locator.ClosedBinaryCBOROnDiskLocator(
       fxtrconfig.get_active_entity_store(config)
   )
   run_data_loader = locator.CachingEntityLoader.from_locator(run_data_locator)
   ```

3. **Load all v1 entities**:
   - Load each Run by CID via `run_data_loader(run_cid)`
   - Load each Task via `run_data_loader(run.task)`
   - Build mapping: `task_cid -> (task, run)`

4. **Topological sort by task dependencies**:
   - Parse task dependencies via `plan_dep_util.plan_deps(task.function, recursive=True)`
   - Find predecessor tasks (via `TaskResultHandle.task_ref`)
   - Order so predecessors come first

5. **Convert in dependency order with progress bar**:
   ```python
   from tqdm import tqdm

   run_cache_manager = session_manager._run_cache_manager
   upgrade_cache = run_cache_manager._upgrade_cache

   for task_cid, (v1_task, v1_run) in tqdm(sorted_items, desc="Converting runs"):
       # Check if already converted
       existing_run = await run_cache_manager.get_run_for_task_by_legacy_cid(task_cid)
       if existing_run is not None:
           # Verify consistency
           if existing_run.legacy_run_entity.legacy_cid != v1_core.entity_cid(v1_run):
               raise RuntimeError(f"Conflict: task {task_cid} has different run")
           continue  # Skip, already converted

       # Upgrade the task (if not already)
       v2_task = await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)

       # Upgrade run data and provenance
       v1_provenance = run_data_loader(v1_run.provenance)
       v2_provenance = await ImplProvenanceSet.upgrade_from(v1_provenance, upgrade_cache)

       v1_run_data = run_data_loader(v1_run.run_data)
       await upgrade_cache.save_upgraded(LegacyCompat(v1_run_data))

       # Build previous_runs from task predecessors
       previous_runs = set()
       for dep in plan_dep_util.plan_deps(v1_task.function, recursive=True):
           if isinstance(dep, v1_core.TaskResultHandle):
               pred_task_cid = dep.task_ref
               if pred_task_cid not in runset.runs:
                   raise RuntimeError(
                       f"Runset consistency error: task {task_cid} depends on "
                       f"{pred_task_cid} which is not in the runset"
                   )
               pred_run = await run_cache_manager.get_run_for_task_by_legacy_cid(pred_task_cid)
               if pred_run is None:
                   raise RuntimeError(
                       f"Predecessor run for {pred_task_cid} not yet converted. "
                       "This indicates a topological sort error."
                   )
               previous_runs.add(pred_run.store())

       # Create the RunWithPredecessors
       run_with_predecessors = RunWithPredecessors(
           task=v2_task.store(),
           provenance=v2_provenance.store(),
           run_data=LegacyCompat(v1_run_data).store(),
           run_info=LegacyCompat(v1_run.run_info),
           previous_runs=frozenset(previous_runs),
           legacy_run_entity=LegacyCompat(v1_run),
       )

       # Save to upgrade cache and run cache
       await upgrade_cache.save_upgraded(run_with_predecessors)
       await run_cache_manager.save_run(run_with_predecessors)
   ```

6. **Return count**

### Helper Method

Add to `RunCacheManager`:

```python
async def get_run_for_task_by_legacy_cid(
    self, task_cid: cbrrr.CID
) -> RunWithPredecessors | None:
    """Get a run by the legacy task CID.

    Looks up the task in the upgrade cache, then finds its run.
    """
    try:
        task_ref = await self._upgrade_cache.lookup_upgraded(task_cid, TaskWithPredecessors)
    except KeyError:
        return None
    return await self.get_run_for_task(task_ref)
```

## Part 3: Demo Updates

### Changes to `v2/examples/session_manager_porting/v2_version/run_demo.py`

1. **Add CLI flag for conversion**:
   ```python
   import argparse

   parser = argparse.ArgumentParser()
   parser.add_argument("--convert-v1-runset", action="store_true",
                       help="Convert the v1 active runset before running")
   args = parser.parse_args()
   ```

2. **Call conversion if flag is set**:
   ```python
   if args.convert_v1_runset:
       from fxtr2.legacy_compat.runset_conversion import convert_legacy_runset_to_run_cache
       count = await convert_legacy_runset_to_run_cache(config, mgr)
       print(f"Converted {count} runs from v1 runset")
   ```

3. **Add small copy demo**:
   ```python
   # Initialize the small copy namespace
   small_copy_path = storage_dir / "run_cache_small_copy"
   await ensure_git_namespace_initialized(
       small_copy_path,
       "run_cache_small_copy",
       "Demo: small subset of runs copied from main cache"
   )

   # After running tasks, copy one judge run with predecessors
   async with GitFxtrNamespace(small_copy_path) as small_copy_ns:
       small_copy_manager = RunCacheManager(
           run_cache_namespace=small_copy_ns,
           upgrade_cache=upgrade_cache,  # Same upgrade cache!
       )

       # Pick one judge task (e.g., the first one)
       judge_task = v2_tasks[...]  # Index of a judge task

       print(f"\n=== Demo: Copy judge run with predecessors ===")
       copied = await small_copy_manager.copy_runs_from(
           other=mgr._run_cache_manager,
           selection=[judge_task],
           include_predecessors=True,
       )
       print(f"Copied {copied} runs to small_copy namespace")

       # Verify by listing pins
       pins = await small_copy_ns.list_pins()
       print(f"Small copy has {len(pins)} pins")
   ```

## Implementation Steps

- [x] Add `get_run_for_task_by_legacy_cid` helper to `RunCacheManager`
- [x] Implement `copy_runs_from` on `RunCacheManager`
- [x] Create `runset_conversion.py` with `convert_legacy_runset_to_run_cache`
- [x] Update `__init__.py` to export new function
- [x] Update demo to support `--convert-v1-runset` flag
- [x] Update demo to copy a judge run to small copy namespace
- [ ] Test the conversion with the demo_09_15 experiment (manual test required)
- [x] Update CODE_OVERVIEW.md

## Notes

- The `pin` method on `FxtrNamespace` handles the actual entity data copying between arenas
- We rely on the namespace's copy-on-pin behavior to recursively copy all referenced entities
- The UpgradeCache identity check ensures CID→FxtrID mappings are consistent
- Idempotency allows safe re-runs of conversion
- **Workflow limitation**: The current `TaskWithPredecessors` and `RunWithPredecessors` types do not support workflows - they raise `ValueError` for `WorkflowStepResultHandle` and `WorkflowResultHandle` dependencies. This is a known limitation of the transitional types.
