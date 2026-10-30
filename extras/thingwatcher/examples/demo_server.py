#!/usr/bin/env python3
"""Demo server for testing graphqurl interaction with debug pauses."""

import asyncio
import signal
import sys

# Add thingwatcher to path
sys.path.insert(0, "/Users/daniel/fxtr/extras/thingwatcher")

from thingwatcher import ThingLink, ThingState, Watcher, register_snapshotter, tick, track
from thingwatcher.api import StatelessSnapshotter
from thingwatcher.registry import configure_rendering


class Task:
    """A simulated task."""

    def __init__(self, name: str, status: str = "pending"):
        self.name = name
        self.status = status
        self.parent = None


class TaskSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: Task) -> ThingState:
        links = []
        if obj.parent:
            links.append(
                ThingLink(
                    link_type="parent",
                    link_key="parent",
                    to_thing_key=("task", obj.parent.name),
                )
            )
        return ThingState(
            key=("task", obj.name),
            thing_type="Task",
            thing_data={"name": obj.name, "status": obj.status},
            links=links,
        )


class Message:
    """A message between tasks."""

    def __init__(self, id: int, content: str, sender: Task, receiver: Task):
        self.id = id
        self.content = content
        self.sender = sender
        self.receiver = receiver


class MessageSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: Message) -> ThingState:
        return ThingState(
            key=("message", obj.id),
            thing_type="Message",
            thing_data={"id": obj.id, "content": obj.content},
            links=[
                ThingLink("from", "sender", ("task", obj.sender.name)),
                ThingLink("to", "receiver", ("task", obj.receiver.name)),
            ],
        )


async def simulate_work(watcher: Watcher):
    """Simulate some work with tasks and messages, pausing for inspection."""
    # Register snapshotters
    register_snapshotter(Task, TaskSnapshotter())
    register_snapshotter(Message, MessageSnapshotter())
    configure_rendering("Task", "#4a90d9")
    configure_rendering("Message", "#e5a445", link_colors={"from": "#88cc88", "to": "#cc8888"})

    print("\n=== Demo server running on port 8765 ===\n")

    # Phase 1: Create main task
    main_task = Task("main", "running")
    track(main_task)
    tick("info", "Main task started")

    print("Phase 1: Main task created. Inspect with:")
    print("  graphqurl http://localhost:8765/graphql -q '{ latestTick }'")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ thingsAtTick(tick: 1) { key thingType } }'"
    )
    print("\nContinue with: curl -X POST http://localhost:8765/debug_trigger/continue")
    await watcher.debug_receive("continue", timeout=300)

    # Phase 2: Spawn workers
    worker1 = Task("worker-1", "running")
    worker1.parent = main_task
    worker2 = Task("worker-2", "pending")
    worker2.parent = main_task
    track(worker1)
    track(worker2)
    tick("debug", "Spawned worker tasks")

    print("\n--- Phase 2: Workers spawned ---")
    print("Now at tick 2. See the new tasks and their links:")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ thingsAtTick(tick: 2) { key thingType links { linkType toThingKey } } }'"
    )
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ changedThings(fromTick: 1, toTick: 2) { kind thing { key thingType } } }'"
    )
    print("\nContinue with: curl -X POST http://localhost:8765/debug_trigger/continue")
    await watcher.debug_receive("continue", timeout=300)

    # Phase 3: Send a message
    msg1 = Message(1, "Hello from worker-1", worker1, worker2)
    track(msg1)
    worker2.status = "running"
    tick("info", "Message sent, worker-2 activated")

    print("\n--- Phase 3: Message sent ---")
    print("Now at tick 3. See the message and updated task status:")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ thingsAtTick(tick: 3) { key thingType links { linkType toThingKey } } }'"
    )
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ thingData(tick: 3, key: \"gaNkYXNrZHdvcmtlci0y\") { thingType dataChunks { data } } }'"
    )
    print("  # (The key above is base64 CBOR for ('task', 'worker-2'))")
    print("\nContinue with: curl -X POST http://localhost:8765/debug_trigger/continue")
    await watcher.debug_receive("continue", timeout=300)

    # Phase 4: Worker-1 completes
    worker1.status = "completed"
    tick("info", "Worker-1 completed")

    print("\n--- Phase 4: Worker-1 completed ---")
    print("Now at tick 4. Check what changed:")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ changedThings(fromTick: 3, toTick: 4) { kind thing { key } } }'"
    )
    print("\nContinue with: curl -X POST http://localhost:8765/debug_trigger/continue")
    await watcher.debug_receive("continue", timeout=300)

    # Phase 5: Reply message and completion
    msg2 = Message(2, "Reply from worker-2", worker2, main_task)
    track(msg2)
    worker2.status = "completed"
    main_task.status = "completed"
    tick("info", "All tasks completed")

    print("\n--- Phase 5: All done ---")
    print("Final state at tick 5:")
    print("  graphqurl http://localhost:8765/graphql -q '{ ticks { tick level data } }'")
    print(
        "  graphqurl http://localhost:8765/graphql -q '{ thingsAtTick(tick: 5) { key thingType } }'"
    )
    print("\nDemo complete! Press Ctrl+C to stop the server.")

    # Keep running
    while True:
        await asyncio.sleep(1)


async def main():
    watcher = Watcher(db_path=":memory:", port=8765)

    shutdown_event = asyncio.Event()

    def handle_signal(sig, frame):
        print("\nShutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def run_demo():
        try:
            await simulate_work(watcher)
        except asyncio.CancelledError:
            pass
        await shutdown_event.wait()

    await watcher.watch_and_serve_until(run_demo)


if __name__ == "__main__":
    asyncio.run(main())
