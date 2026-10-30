"""Run the demo_09_15 experiment using the v1 SessionManager."""

import asyncio
from pathlib import Path

import demo_09_15_user

from fxtr.api import fxtrconfig, session_manager
from fxtr.object_model import core


async def main():
    # Load config
    config_path = Path(__file__).parents[4] / "examples/demo_09_15/user/fxtr.toml"
    config = fxtrconfig.parse_fxtr_config(config_path)

    # Fetch the experiment
    experiment = demo_09_15_user.experiment

    # Extract all tasks from the experiment explicitly
    all_tasks: list[core.Task] = []
    all_tasks.append(experiment.idea_gen_task)
    for sample_group in experiment.sample_tasks:
        all_tasks.extend(sample_group)
    for judge_group in experiment.judge_tasks:
        all_tasks.extend(judge_group)
    all_tasks.append(experiment.collect_task)

    print(f"Found {len(all_tasks)} tasks to materialize")

    # Run session
    async with session_manager.SessionManager.create(config, in_process_python=False) as mgr:
        await mgr.activate_collection("default")
        # await mgr.drop_all_runs()

        print("Materializing tasks...")
        record = await mgr.materialize(core.freeze(all_tasks), message="Demo materialization")

        # Print stats
        print(f"\n=== Materialization Results ===")
        print(f"Completed: {len(record.completed)}")
        print(f"Cached: {len(record.cached)}")
        print(f"Failed: {len(record.failed)}")
        print(f"Cancelled: {len(record.cancelled)}")

        # Retrieve and print result
        if not record.failed:
            retriever = await mgr.instantiate(experiment.collect_task.result_handle)
            result = await retriever.retrieve()

            print(f"\n=== WeirdBehaviorDemoResult ===")
            print(f"Behavior: {result.weird_behavior.description[:100]}...")
            print(f"Conditions evaluated: {len(result.conditions)}")
            for i, cond in enumerate(result.conditions):
                print(f"\n  Prompt {i+1}: {cond.user_message.text[:60]}...")
                for cat, count in cond.judgement_counts.items():
                    print(f"    {cat}: {count}")
        else:
            print("\n=== Failures ===")
            for task_cid, msg in record.failed.items():
                print(f"  {task_cid}: {msg}")


if __name__ == "__main__":
    asyncio.run(main())
