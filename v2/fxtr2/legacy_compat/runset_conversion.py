"""Convert legacy v1 runsets to the v2 run cache format.

This module provides functionality to migrate runs from fxtr v1's active runset
(stored in SqliteDict) to the v2 TransitionalSessionManager's run cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tqdm import tqdm

from fxtr2.legacy_compat.data import LegacyCompat
from fxtr2.legacy_compat.transitional_rundata import (
    ImplProvenanceSet,
    RunWithPredecessors,
    TaskWithPredecessors,
)
from fxtr.api import api_lib, fxtrconfig
from fxtr.impl_plans import plan_dep_util
from fxtr.object_model import core as v1_core
from fxtr.object_model import locator

if TYPE_CHECKING:
    from fxtr2.legacy_compat.transitional_session_manager import TransitionalSessionManager


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

    Note:
        This function does not support workflows - only tasks are converted.
        Workflow dependencies will raise ValueError.
    """
    # 1. Load the legacy runset
    runset = api_lib.load_active_runset(config)
    if not runset.runs:
        return 0

    # 2. Create the run data loader
    run_data_locator = locator.ClosedBinaryCBOROnDiskLocator(
        fxtrconfig.get_active_entity_store(config)
    )
    run_data_loader = locator.CachingEntityLoader.from_locator(run_data_locator)

    # 3. Load all v1 entities
    task_cid_to_data: dict[
        v1_core.CID_Of[v1_core.Task[Any]], tuple[v1_core.Task[Any], v1_core.Run]
    ] = {}

    print(f"Loading {len(runset.runs)} runs from legacy runset...")
    for task_cid, run_cid in tqdm(runset.runs.items(), desc="Loading v1 entities"):
        try:
            v1_run = run_data_loader(run_cid)
            assert isinstance(v1_run, v1_core.Run)
            v1_task = run_data_loader(v1_run.task)
            assert isinstance(v1_task, v1_core.Task)
            task_cid_to_data[task_cid] = (v1_task, v1_run)
        except Exception as e:
            raise RuntimeError(f"Failed to load run {run_cid} or task {task_cid}: {e}") from e

    # 4. Topological sort by task dependencies
    # Build dependency graph
    task_cids = set(task_cid_to_data.keys())
    in_degree: dict[v1_core.CID_Of[v1_core.Task[Any]], int] = {cid: 0 for cid in task_cids}
    dependents: dict[v1_core.CID_Of[v1_core.Task[Any]], list[v1_core.CID_Of[v1_core.Task[Any]]]] = {
        cid: [] for cid in task_cids
    }

    for task_cid, (v1_task, _) in task_cid_to_data.items():
        # Find predecessor tasks
        deps = plan_dep_util.plan_deps(v1_task.function, recursive=True)
        for dep in deps:
            match dep:
                case v1_core.TaskResultHandle(task_ref=pred_cid):
                    if pred_cid in task_cids:
                        in_degree[task_cid] += 1
                        dependents[pred_cid].append(task_cid)
                case v1_core.WorkflowStepResultHandle() | v1_core.WorkflowResultHandle():
                    raise ValueError(
                        f"Workflow dependency {dep} not supported in runset conversion"
                    )

    # Kahn's algorithm
    sorted_task_cids: list[v1_core.CID_Of[v1_core.Task[Any]]] = []
    ready = [cid for cid, deg in in_degree.items() if deg == 0]
    while ready:
        cid = ready.pop()
        sorted_task_cids.append(cid)
        for dep_cid in dependents[cid]:
            in_degree[dep_cid] -= 1
            if in_degree[dep_cid] == 0:
                ready.append(dep_cid)

    if len(sorted_task_cids) != len(task_cids):
        raise RuntimeError("Cycle detected in task dependencies - this should not happen")

    # 5. Convert in dependency order
    run_cache_manager = session_manager._run_cache_manager
    upgrade_cache = run_cache_manager._upgrade_cache
    converted_count = 0

    for task_cid in tqdm(sorted_task_cids, desc="Converting runs"):
        v1_task, v1_run = task_cid_to_data[task_cid]

        # Check if already converted
        existing_run = await run_cache_manager.get_run_for_task_by_legacy_cid(task_cid)
        if existing_run is not None:
            # Verify consistency
            if existing_run.legacy_run_entity.legacy_cid != v1_core.entity_cid(v1_run):
                raise RuntimeError(
                    f"Conflict: task {task_cid} already has a different run in cache "
                    f"(existing: {existing_run.legacy_run_entity.legacy_cid}, "
                    f"converting: {v1_core.entity_cid(v1_run)})"
                )
            continue  # Skip, already converted

        # Upgrade the task (if not already)
        v2_task = await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)

        # Upgrade run data and provenance
        v1_provenance = run_data_loader(v1_run.provenance)
        assert isinstance(v1_provenance, v1_core.ImplProvenanceSet)
        v2_provenance = await ImplProvenanceSet.upgrade_from(v1_provenance, upgrade_cache)

        v1_run_data = run_data_loader(v1_run.run_data)
        await upgrade_cache.save_upgraded(LegacyCompat(v1_run_data))

        # Build previous_runs from task predecessors
        previous_runs = set()
        deps = plan_dep_util.plan_deps(v1_task.function, recursive=True)
        for dep in deps:
            match dep:
                case v1_core.TaskResultHandle(task_ref=pred_task_cid):
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
        converted_count += 1

    return converted_count
