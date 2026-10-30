"""Demo server for testing streaming updates and multi-chunk data rendering.

This demo shows:
1. Streaming updates - updating individual objects without full ticks
2. De-synced streams - counter and logger update at different rates
3. ThingDelta with append mode for accumulating log entries
4. How to use streaming_update() for real-time UI updates
5. Async owning snapshotters - immediate pending state, then async completion

Key concepts demonstrated:
1. streaming_update() updates a single object in a "stream" tick
2. Multiple streaming updates can happen to the same stream tick
3. triggerTick mutation captures a history checkpoint
4. ThingDelta append mode for chunked data accumulation
5. AsyncThingUpdate for async work in snapshotters

The renderers are loaded from .tsx files in this directory.
Typecheck with: deno check *.tsx
"""

from __future__ import annotations

import argparse
import asyncio
import random
import signal
import time
import uuid
from pathlib import Path
from typing import Optional

import anyio
from anyio import open_signal_receiver
from thingwatcher import (
    AsyncThingUpdate,
    ThingDelta,
    ThingRef,
    ThingState,
    Watcher,
    register_snapshotter,
)
from thingwatcher.api import (
    AnchorCapturer,
    StatefulOwningSnapshotter,
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


# =============================================================================
# Domain objects
# =============================================================================


class LogAccumulator:
    """A thing that accumulates log entries over time.

    Each snapshot after the first emits a ThingDelta with append mode,
    creating a new data chunk that extends the previous one.
    """

    def __init__(self, name: str):
        self.name = name
        self.entries: list[dict] = []

    def add_entry(self, level: str, message: str):
        """Add a new log entry."""
        self.entries.append(
            {
                "timestamp": time.time(),
                "level": level,
                "message": message,
            }
        )


class Counter:
    """A simple counter to show contrast with single-chunk things.

    Uses ThingState (replace mode) so there's always just one chunk.
    """

    def __init__(self, name: str):
        self.name = name
        self.value = 0

    def increment(self, amount: int = 1):
        self.value += amount


class AsyncSnapshottedThing:
    """Demonstrates async snapshot completion.

    When snapshotted, immediately returns a "pending" state,
    then after a delay, an async result with "completed" status.

    The object has an async getter that simulates slow async work
    (e.g., fetching from an API). The snapshotter uses this to
    get the delayed state.
    """

    def __init__(self, name: str, delay: float = 5.0):
        self.name = name
        self.delay = delay

    async def get_delayed_state(self) -> dict:
        """Simulate fetching state from a slow async source.

        Returns a random value after a delay, without modifying
        the object itself.
        """
        await anyio.sleep(self.delay)
        return {
            "fetched_value": random.randint(1, 1000),
            "fetched_at": time.time(),
        }


# =============================================================================
# Snapshotters
# =============================================================================


class LogAccumulatorSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that tracks state and emits deltas for new entries.

    First snapshot: emits ThingState with all current entries
    Subsequent snapshots: emits ThingDelta with only new entries (append mode)
    """

    def __init__(self):
        # Track last seen entry count per object (by id)
        self._last_count: dict[int, int] = {}

    def snapshot_owned(self, obj: LogAccumulator) -> ThingState | ThingDelta:
        obj_id = id(obj)
        last_count = self._last_count.get(obj_id, 0)
        current_count = len(obj.entries)

        if last_count == 0:
            # First snapshot: emit full ThingState
            self._last_count[obj_id] = current_count
            return ThingState(
                key=("log", obj.name),
                thing_type="LogAccumulator",
                thing_data={"name": obj.name, "entries": list(obj.entries)},
                links=[],
            )
        elif current_count > last_count:
            # New entries: emit ThingDelta with append mode
            new_entries = obj.entries[last_count:]
            self._last_count[obj_id] = current_count
            return ThingDelta(
                key=("log", obj.name),
                push_chunk={"entries": new_entries},  # Only new entries
            )
        else:
            # No changes
            return ThingDelta(
                key=("log", obj.name),
                # Default pop_chunks=0, push_chunk=NO_PUSHED_CHUNK means no data change
            )

    def stateless_snapshot(self, obj: LogAccumulator) -> ThingState:
        """Return full state for hard recapture."""
        return ThingState(
            key=("log", obj.name),
            thing_type="LogAccumulator",
            thing_data={"name": obj.name, "entries": list(obj.entries)},
            links=[],
        )


class CounterSnapshotter(StatefulOwningSnapshotter):
    """Owning snapshotter that emits full state (required for streaming_update)."""

    def snapshot_owned(self, obj: Counter) -> ThingState:
        return ThingState(
            key=("counter", obj.name),
            thing_type="Counter",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )

    def stateless_snapshot(self, obj: Counter) -> ThingState:
        """Return full state for hard recapture (same as snapshot_owned for this simple case)."""
        return self.snapshot_owned(obj)


class AsyncSnapshottedThingSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that demonstrates async updates.

    snapshot_owned returns an AsyncThingUpdate containing:
    - immediate_update: A "pending" state shown right away
    - async_updater: An async function that computes the final state

    The async_updater result appears in a subsequent streaming tick.
    If another tick/streaming_update happens before completion, the
    async_updater is cancelled and its result is discarded.
    """

    def snapshot_owned(self, obj: AsyncSnapshottedThing) -> AsyncThingUpdate:
        request_id = str(uuid.uuid4())[:8]

        immediate = ThingState(
            key=("async_thing", obj.name),
            thing_type="AsyncSnapshottedThing",
            thing_data={
                "name": obj.name,
                "status": "pending",
                "request_id": request_id,
            },
            links=[],
        )

        async def complete_async():
            result = await obj.get_delayed_state()
            return ThingState(
                key=("async_thing", obj.name),
                thing_type="AsyncSnapshottedThing",
                thing_data={
                    "name": obj.name,
                    "status": "completed",
                    "request_id": request_id,  # Same ID - shows continuity
                    **result,
                },
                links=[],
            )

        return AsyncThingUpdate(
            immediate_update=immediate,
            async_updater=complete_async,
        )

    def stateless_snapshot(self, obj: AsyncSnapshottedThing) -> ThingState:
        """For hard recapture - return current state as unknown."""
        return ThingState(
            key=("async_thing", obj.name),
            thing_type="AsyncSnapshottedThing",
            thing_data={
                "name": obj.name,
                "status": "unknown",
            },
            links=[],
        )


# =============================================================================
# Anchor capturers
# =============================================================================


class LogAccumulatorAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: LogAccumulator) -> ThingRef:
        return ThingRef(key=("log", anchor.name))


class CounterAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: Counter) -> ThingRef:
        return ThingRef(key=("counter", anchor.name))


class AsyncSnapshottedThingAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: AsyncSnapshottedThing) -> ThingRef:
        return ThingRef(key=("async_thing", anchor.name))


# =============================================================================
# Main demo
# =============================================================================


async def setup_demo(watcher: Watcher):
    """Set up the demo with log accumulator, counter, and async thing using streaming updates."""
    # Register snapshotters
    register_snapshotter(LogAccumulator, LogAccumulatorSnapshotter())
    register_snapshotter(Counter, CounterSnapshotter())
    register_snapshotter(AsyncSnapshottedThing, AsyncSnapshottedThingSnapshotter())

    # Register anchor capturers
    register_anchor_capturer(LogAccumulator, LogAccumulatorAnchorCapturer())
    register_anchor_capturer(Counter, CounterAnchorCapturer())
    register_anchor_capturer(AsyncSnapshottedThing, AsyncSnapshottedThingAnchorCapturer())

    # Configure rendering
    configure_rendering("LogAccumulator", "#4ecdc4")
    configure_rendering("Counter", "#ff6b6b")
    configure_rendering("AsyncSnapshottedThing", "#9b59b6")

    # Register custom renderers from .tsx files
    register_renderer("LogAccumulator", load_renderer("LogAccumulator"))
    register_renderer("Counter", load_renderer("Counter"))
    register_renderer("AsyncSnapshottedThing", load_renderer("AsyncSnapshottedThing"))

    print("\n=== Streaming Updates Demo on port 8766 ===\n")
    print("This demo shows streaming updates with de-synced objects:\n")
    print("1. LogAccumulator - streams at 3 second intervals")
    print("   Uses ThingDelta(append) to accumulate entries")
    print("2. Counter - streams at 1.5 second intervals (faster)")
    print("   Uses ThingState (replace) for single-chunk data")
    print("3. AsyncSnapshottedThing - demonstrates async updates")
    print("   Shows 'pending' immediately, then 'completed' after 5s")
    print("   Trigger a tick before completion to see cancellation")
    print()
    print("All update the same stream tick, which keeps getting modified.")
    print("Use triggerTick mutation in GraphQL to capture a history checkpoint.")
    print()
    print(f"Renderers loaded from: {SCRIPT_DIR}")
    print("Typecheck with: deno check *.tsx")
    print()

    # Create test objects
    log = LogAccumulator("app_log")
    watcher.track(log)
    watcher.add_root("log", log)

    counter = Counter("clicks")
    watcher.track(counter)
    watcher.add_root("counter", counter)

    async_thing = AsyncSnapshottedThing("slow_fetch", delay=5.0)
    watcher.track(async_thing)
    watcher.add_root("async_thing", async_thing)

    # Initial tick to establish the objects
    # This will trigger the async_thing's async_updater which completes after 5s
    log.add_entry("info", "Application started")
    watcher.tick("info", "Initial state")

    print("Initial state created. Now starting streaming updates...")
    print("Open http://localhost:8766 to view.")
    print()
    print("TIP: In GraphQL, run 'mutation { triggerTick { index } }' to capture a checkpoint.")
    print("     This also cancels any pending async updates and restarts them.")
    print("Press Ctrl+C to stop.\n")

    # Log messages for random selection
    log_messages = [
        ("info", "Processing request"),
        ("debug", "Cache hit for key xyz"),
        ("info", "User logged in"),
        ("warn", "High memory usage detected"),
        ("info", "Background job started"),
        ("error", "Connection timeout"),
        ("info", "Retry successful"),
        ("debug", "Query executed in 42ms"),
        ("info", "Data sync complete"),
        ("warn", "Rate limit approaching"),
    ]

    async def stream_logger():
        """Stream log entries at 3-second intervals."""
        log_count = 0
        while True:
            await anyio.sleep(3.0)
            level, message = random.choice(log_messages)
            log.add_entry(level, f"{message} (log #{log_count})")
            watcher.streaming_update(log)
            log_count += 1
            print(f"[Logger] Added entry #{log_count}: {level} - {message[:20]}...")

    async def stream_counter():
        """Stream counter updates at 1.5-second intervals (faster than logger)."""
        while True:
            await anyio.sleep(1.5)
            amount = random.randint(1, 5)
            counter.increment(amount)
            watcher.streaming_update(counter)
            print(f"[Counter] Incremented by {amount}, now {counter.value}")

    # Run all streams concurrently
    async with anyio.create_task_group() as tg:
        tg.start_soon(stream_logger)
        tg.start_soon(stream_counter)


async def main(db_path: Optional[str]):
    if db_path is None:
        import datetime

        db_path = f"/tmp/demo_multi_chunk_{datetime.datetime.now().isoformat()}.db"
        print(f"Using auto database file: {db_path}")

    watcher = Watcher(db_path=db_path, port=8766)
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
                        await setup_demo(watcher)
                except Exception as e:
                    print(f"Demo error: {e}")
                finally:
                    shutdown_event.set()
                print("Demo stopped, waiting for shutdown...")
                await shutdown_event.wait()
            signals_tg.cancel_scope.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo server for multi-chunk data rendering")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database file (default: auto-generated temp file)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
