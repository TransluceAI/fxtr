"""Run the demo_09_15 experiment using the v2 TransitionalSessionManager.

This demonstrates migrating from the v1 SessionManager to the v2 TransitionalSessionManager.
See v2/notes/migrating_to_transitional_sm.md for a detailed migration guide.
"""

import logging
from pathlib import Path

import anyio
import demo_09_15_user
from fxtr.api import fxtrconfig
from fxtr.object_model import core as v1_core

from fxtr2.data.fxtr_context_api import GitFxtrNamespace
from fxtr2.legacy_compat.data import UpgradeCache
from fxtr2.legacy_compat.transitional_rundata import TaskToRunMap, TaskWithPredecessors
from fxtr2.legacy_compat.transitional_session_manager import TransitionalSessionManager


async def ensure_git_namespace_initialized(
    path: Path, name: str, description: str | None = None
) -> None:
    """Initialize a git namespace if it doesn't exist yet."""
    config_file = path / "fxtr_store.toml"
    if not config_file.exists():
        print(f"Initializing git namespace at {path}...")
        await GitFxtrNamespace(path).create(name=name, description=description)


async def main():

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s %(filename)s:%(lineno)d [%(threadName)s=%(thread)d]"
            " %(message)s"
        ),
    )
    # from fxtr2.data.sqlite_store import sqlite_util, sqlite_namespace
    # sqlite_namespace.logger.setLevel(logging.DEBUG)
    # sqlite_ops.logger.setLevel(logging.DEBUG)
    # sqlite_util.logger.setLevel(logging.DEBUG)

    # git_namespace.logger.setLevel(logging.DEBUG)
    # sync.logger.setLevel(logging.DEBUG)
    # dirty.logger.setLevel(logging.DEBUG)
    # Load config (same as v1)
    config_path = Path(__file__).parents[4] / "examples/demo_09_15/user/fxtr.toml"
    config = fxtrconfig.parse_fxtr_config(config_path)

    # Fetch the experiment (same as v1)
    experiment = demo_09_15_user.experiment

    # Extract all v1 tasks from the experiment in dependency order
    # (This is the same as v1, but order matters for upgrading)
    v1_tasks: list[v1_core.Task] = []
    v1_tasks.append(experiment.idea_gen_task)
    for sample_group in experiment.sample_tasks:
        v1_tasks.extend(sample_group)
    for judge_group in experiment.judge_tasks:
        v1_tasks.extend(judge_group)
    v1_tasks.append(experiment.collect_task)

    print(f"Found {len(v1_tasks)} v1 tasks")

    # Set up storage directories
    # Using GitFxtrNamespace for version-controlled storage
    # All three namespaces live in the same git repo (the v2_version folder)
    storage_dir = Path(__file__).parent / "storage"
    run_cache_path = storage_dir / "run_cache"
    upgrade_cache_path = storage_dir / "upgrade_cache"
    user_storage_path = storage_dir / "user_storage"

    # Initialize git namespaces if they don't exist
    await ensure_git_namespace_initialized(
        run_cache_path,
        "run_cache",
        "Stores completed task runs, keyed by task ID. Managed by TransitionalSessionManager.",
    )
    await ensure_git_namespace_initialized(
        upgrade_cache_path,
        "upgrade_cache",
        "Caches v1-to-v2 entity translations (Task -> TaskWithPredecessors, etc.).",
    )
    await ensure_git_namespace_initialized(
        user_storage_path,
        "user_storage",
        "User-controlled storage for pinning entities under friendly names.",
    )

    # Create namespaces and session manager
    # Note: scopes nest automatically through the async with blocks
    async with (
        GitFxtrNamespace(run_cache_path) as run_cache_ns,
        GitFxtrNamespace(upgrade_cache_path) as upgrade_cache_ns,
        GitFxtrNamespace(user_storage_path) as user_ns,
        TransitionalSessionManager.create(
            config=config,
            run_cache_namespace=run_cache_ns,
            upgrade_cache=UpgradeCache(upgrade_cache_ns),
            in_process_python=False,
        ) as mgr,
    ):
        # Activate collection (same as v1)
        await mgr.activate_collection("default")

        # === NEW: Upgrade v1 tasks to v2 TaskWithPredecessors ===
        # Tasks must be upgraded in dependency order because upgrade_from()
        # looks up predecessors from the upgrade cache.
        print("Upgrading tasks to v2 format...")
        upgrade_cache = UpgradeCache(upgrade_cache_ns)
        v2_tasks: list[TaskWithPredecessors] = []
        for v1_task in v1_tasks:
            v2_task = await TaskWithPredecessors.upgrade_from(v1_task, upgrade_cache)
            v2_tasks.append(v2_task)

        print(f"Upgraded {len(v2_tasks)} tasks to TaskWithPredecessors")

        # === NEW: Materialize using materialize_all_tasks() ===
        # Unlike v1's materialize(), this doesn't take a message parameter
        print("Materializing first tasks individually")
        for v2_task in v2_tasks[:3]:
            res = await mgr.invoke_task(v2_task)
            print(f"Task {v2_task.store().ref.summary()} result: {v1_core.entity_cid(res)}")

        print("Materializing all tasks (should cache)...")
        task_to_run_map = await mgr.materialize_all_tasks(v2_tasks)

        # Print stats
        # Note: v2 returns a TaskToRunMap, not a record with completed/failed/etc.
        # All tasks in the map completed successfully (failures raise exceptions)
        print(f"\n=== Materialization Results ===")
        print(f"Tasks completed: {len(task_to_run_map.runs)}")

        # === Retrieve results: Method 1 - via instantiate() (v1 style) ===
        print("\n=== Method 1: Retrieve via instantiate() ===")
        retriever = await mgr.instantiate(experiment.collect_task.result_handle)
        result_via_instantiate = await retriever.retrieve()

        print(f"Behavior: {result_via_instantiate.weird_behavior.description[:100]}...")
        print(f"Conditions evaluated: {len(result_via_instantiate.conditions)}")

        # === Retrieve results: Method 2 - via RunWithPredecessors (v2 style) ===
        print("\n=== Method 2: Retrieve via RunWithPredecessors ===")
        # Get the v2 task for collect_task
        v2_collect_task = v2_tasks[-1]  # Last task is collect_task
        v2_collect_task_ref = v2_collect_task.store()

        # Look up the run in the task-to-run map
        run_ref = task_to_run_map.runs[v2_collect_task_ref]
        run = await run_ref.load()

        # Load the run data (it's wrapped in LegacyCompat)
        run_data_compat = await run.run_data.load()
        result_via_run = run_data_compat.value

        print(f"Behavior: {result_via_run.weird_behavior.description[:100]}...")
        print(f"Conditions evaluated: {len(result_via_run.conditions)}")

        # === Demo: Look up v2 task from v1 task via cache ===
        print("\n=== Demo: Look up v2 task from v1 task via upgrade cache ===")
        # This shows you can always recover the v2 task from a v1 task
        v1_collect_task = experiment.collect_task
        v1_collect_task_cid = v1_core.entity_cid(v1_collect_task)
        v2_task_ref_from_cache = await upgrade_cache.lookup_upgraded(
            v1_collect_task_cid, TaskWithPredecessors
        )
        v2_task_from_cache = await v2_task_ref_from_cache.load()
        print(f"Original v1 task CID: {v1_collect_task_cid}")
        print(f"Looked up v2 task: {v2_task_ref_from_cache}")
        print(
            f"Same as our upgraded task? {v2_task_from_cache == v2_collect_task} (refs: {(v2_task_ref_from_cache == v2_collect_task_ref)=})"
        )

        # === Demo: Save results with friendly names in user storage ===
        print("\n=== Demo: Save results with friendly names ===")

        old_pins = await user_ns.list_pins()
        print(f"\nCleaining up old pins ({len(old_pins)} pins):")
        for key in sorted(old_pins.keys()):
            print(f"  - {key}")
            await user_ns.unpin(key)

        if old_pins:
            await user_ns._backend.flush()
            await anyio.sleep(0.5)

        # Save the final task under a friendly name
        await user_ns.pin("demo:final_task", v2_collect_task, overwrite=True)
        print("Pinned final task as 'demo:final_task'")

        # Save the entire task-to-run map
        await user_ns.pin("demo:latest_run_map", task_to_run_map, overwrite=True)
        print("Pinned task-to-run map as 'demo:latest_run_map'")

        # Save the final run
        await user_ns.pin("demo:final_run", run, overwrite=True)
        print("Pinned final run as 'demo:final_run'")

        # List what's in user storage
        pins = await user_ns.list_pins()
        print(f"\nUser storage contents ({len(pins)} pins):")
        for key in sorted(pins.keys()):
            print(f"  - {key}")

        # Demo: retrieve by friendly name
        print("\n=== Demo: Retrieve by friendly name ===")
        retrieved_map_ref = await user_ns.retrieve("demo:latest_run_map", TaskToRunMap)
        retrieved_map = await retrieved_map_ref.load()
        print(f"Retrieved run map has {len(retrieved_map.runs)} runs")

        # === Print detailed results ===
        print("\n=== WeirdBehaviorDemoResult Details ===")
        for i, cond in enumerate(result_via_instantiate.conditions):
            print(f"\n  Prompt {i+1}: {cond.user_message.text[:60]}...")
            for cat, count in cond.judgement_counts.items():
                print(f"    {cat}: {count}")


if __name__ == "__main__":
    anyio.run(main)
