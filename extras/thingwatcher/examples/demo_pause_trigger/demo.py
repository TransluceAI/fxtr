"""Demo server for testing pause/trigger with multiple concurrent workflows.

The renderers are loaded from .tsx files in this directory.
Typecheck with: deno check *.tsx
"""

import argparse
import asyncio
import signal
from pathlib import Path

# Need anyio for task groups
import anyio
from anyio import open_signal_receiver
from thingwatcher import ThingLink, ThingRef, ThingState, Watcher, register_snapshotter
from thingwatcher.api import (
    AnchorCapturer,
    StatelessSnapshotter,
    register_anchor_capturer,
    register_renderer,
)
from thingwatcher.registry import configure_rendering

# Directory containing this script (and the .tsx renderer files)
SCRIPT_DIR = Path(__file__).parent


def load_renderer(name: str) -> str:
    """Load a renderer TSX file from the same directory as this script."""
    path = SCRIPT_DIR / f"{name}.tsx"
    return path.read_text()


class Worker:
    """A simulated worker task."""

    def __init__(self, name: str, job: str = "idle"):
        self.name = name
        self.job = job
        self.progress = 0


class WorkerSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: Worker) -> ThingState:
        return ThingState(
            key=("worker", obj.name),
            thing_type="Worker",
            thing_data={"name": obj.name, "job": obj.job, "progress": obj.progress},
            links=[],
        )


class WorkerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for Worker objects - returns a ref to the tracked worker."""

    def handle(self, anchor: Worker) -> ThingRef:
        return ThingRef(key=("worker", anchor.name))


class WaitPoint:
    """An anchor for pausing execution at a wait point."""

    def __init__(self, worker: Worker, description: str):
        self.worker = worker
        self.description = description


class WaitPointCapturer(AnchorCapturer):
    def handle(self, anchor: WaitPoint) -> ThingState:
        return ThingState(
            key=("waitpoint", anchor.worker.name, anchor.description),
            thing_type="WaitPoint",
            thing_data={
                "worker": anchor.worker.name,
                "description": anchor.description,
                "worker_job": anchor.worker.job,
                "worker_progress": anchor.worker.progress,
            },
            links=[
                ThingLink(
                    link_type="worker",
                    link_key="worker",
                    to_thing_key=("worker", anchor.worker.name),
                )
            ],
        )


async def worker_task(watcher: Watcher, name: str, steps: list[str]):
    """A worker that goes through multiple steps, pausing between each."""
    worker = Worker(name, "starting")
    watcher.track(worker)
    # Add a root for this worker so it's easily accessible in the UI
    watcher.add_root(name, worker)
    watcher.tick("info", f"{name} starting")

    for i, step in enumerate(steps):
        worker.job = step
        worker.progress = int((i / len(steps)) * 100)
        watcher.tick("debug", f"{name}: {step}")

        # Create a wait point and pause
        wait_point = WaitPoint(worker, f"after_{step}")
        payload = await watcher.receive(wait_point)

        # Check if we got a skip command
        if payload and payload.get("action") == "skip":
            print(f"  [{name}] Skipping remaining steps")
            break
        elif payload and payload.get("message"):
            print(f"  [{name}] Received message: {payload['message']}")

    worker.job = "completed"
    worker.progress = 100
    # Remove the root when worker is done
    watcher.remove_root(name)
    watcher.tick("info", f"{name} completed")


async def simulate_multiple_workflows(watcher: Watcher):
    """Simulate multiple concurrent workers with pause points."""
    register_snapshotter(Worker, WorkerSnapshotter())
    register_anchor_capturer(Worker, WorkerAnchorCapturer())
    register_anchor_capturer(WaitPoint, WaitPointCapturer())
    configure_rendering("Worker", "#4a90d9")
    configure_rendering("WaitPoint", "#e5a445", link_colors={"worker": "#88cc88"})

    # Register custom renderers from .tsx files
    register_renderer("Worker", load_renderer("Worker"))
    register_renderer("WaitPoint", load_renderer("WaitPoint"))

    print("\n=== Pause/Trigger Demo on port 8765 ===\n")
    print("This demo spawns multiple workers that pause between steps.")
    print("Use GraphQL to inspect and trigger them.\n")
    print(f"Renderers loaded from: {SCRIPT_DIR}")
    print("Typecheck with: deno check *.tsx")
    print()

    print("Useful commands:")
    print("  # See current tick")
    print("  graphqurl http://localhost:8765/graphql -q '{ latestTick { index } }'")
    print()
    print("  # See all triggerable things (with shortIds and data!)")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ latestTick { things(triggerable: true) {"
        " shortId thingType data } } }'"
    )
    print()
    print("  # See all things at latest tick")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ latestTick { things { shortId thingType"
        " triggerable data } } }'"
    )
    print()
    print("  # Trigger by shortId (easy!)")
    print(
        "  graphqurl http://localhost:8765/graphql -q 'mutation { triggerPause(shortId: 1) { ok"
        " error } }'"
    )
    print()
    print("  # Trigger with a payload (use heredoc for JSON)")
    print(
        """  graphqurl http://localhost:8765/graphql \\
    -q 'mutation T($shortId: Int!, $payload: JSON) { triggerPause(shortId: $shortId, payload: $payload) { ok } }' \\
    --variablesJSON "$(cat <<'EOF'
{"shortId":1,"payload":{"message":"hello"}}
EOF
)"""
    )
    print()
    print("  # Skip a worker's remaining steps")
    print(
        """  graphqurl http://localhost:8765/graphql \\
    -q 'mutation T($shortId: Int!, $payload: JSON) { triggerPause(shortId: $shortId, payload: $payload) { ok } }' \\
    --variablesJSON "$(cat <<'EOF'
{"shortId":1,"payload":{"action":"skip"}}
EOF
)"""
    )
    print()

    # Define different workflows for each worker
    workflows = {
        "alice": ["fetch_data", "process", "validate", "save"],
        "bob": ["init", "compute", "output"],
        "carol": ["read", "transform", "write", "cleanup"],
    }

    print("Starting workers:", list(workflows.keys()))
    print("-" * 50)

    async with anyio.create_task_group() as tg:
        for name, steps in workflows.items():
            tg.start_soon(worker_task, watcher, name, steps)

    print("\n--- All workers completed ---")
    print("Press Ctrl+C to stop the server.")


async def main(db_path: str | None):
    if db_path is None:
        import datetime

        db_path = f"/tmp/demo_pause_trigger_{datetime.datetime.now().isoformat()}.db"
        print(f"Using auto database file: {db_path}")
    watcher = Watcher(db_path=db_path, port=8765)

    shutdown_event = asyncio.Event()

    async def signal_handler():
        with open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for signum in signals:
                if shutdown_event.is_set():
                    print("Already shutting down, cancelling")
                    outer_cancel_scope.cancel()
                    return
                else:
                    if signum == signal.SIGINT:
                        print("\nShutting down gracefully...")
                    else:
                        print("\nTerminated! Shutting down gracefully...")
                    shutdown_event.set()
                    workflow_cancel_scope.cancel()

    with anyio.CancelScope() as outer_cancel_scope:
        async with anyio.create_task_group() as signals_tg:
            signals_tg.start_soon(signal_handler)
            async with watcher.watching_and_serving():
                try:
                    with anyio.CancelScope() as workflow_cancel_scope:
                        await simulate_multiple_workflows(watcher)
                    print("Inner workflow task group exited")
                    await shutdown_event.wait()
                finally:
                    shutdown_event.set()
                print("Main loop ready to exit, just waiting on watcher")
            signals_tg.cancel_scope.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo server for pause/trigger testing")
    parser.add_argument(
        "--db", type=str, default=None, help="Path to SQLite database file (default: in-memory)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
