"""Demo server for testing drag and drop with custom drop regions.

The renderers are loaded from .tsx files in this directory.
Typecheck with: deno check *.tsx

This demo creates:
- Fruit things (Apple, Orange, Banana) that can be dragged
- DropRegion things that accept drops and can be triggered

The DropRegions demonstrate:
- "any" filter - accepts any thing
- "type:Apple" filter - only accepts Apple things
- "key:{specific}" filter - only accepts a specific thing
"""

import argparse
import asyncio
import base64
import signal
from pathlib import Path

import anyio
import cbor2
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


def renderer_path(name: str) -> Path:
    """Get path to a renderer TSX file in the same directory as this script."""
    return SCRIPT_DIR / f"{name}.tsx"


def decode_key_b64(key_b64: str) -> tuple:
    """Decode a base64-encoded CBOR key to a Python tuple."""
    key_bytes = base64.b64decode(key_b64)
    return cbor2.loads(key_bytes)


# =============================================================================
# Fruit - Simple draggable things
# =============================================================================


class Fruit:
    """A fruit that can be dragged."""

    def __init__(self, name: str, fruit_type: str):
        self.name = name
        self.fruit_type = fruit_type  # "Apple", "Orange", or "Banana"


class FruitSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: Fruit) -> ThingState:
        return ThingState(
            key=("fruit", obj.name),
            thing_type=obj.fruit_type,  # Use fruit type as thing type for filtering
            thing_data={"name": obj.name},
            links=[],
        )


class FruitCapturer(AnchorCapturer):
    """Anchor capturer for Fruit - returns a ThingRef to the tracked fruit."""

    def handle(self, anchor: Fruit) -> ThingRef:
        return ThingRef(key=("fruit", anchor.name))


# =============================================================================
# DropRegion - Triggerable drop zone
# =============================================================================


class DropRegion:
    """A drop region that can receive dropped things and be triggered."""

    def __init__(self, name: str, filter_mode: str = "any"):
        self.name = name
        self.filter_mode = filter_mode  # "any", "type:Apple", or "key:{base64key}"
        self.confirmed_links: list[tuple[str, str]] = []  # (thing_key, thing_type) pairs


class DropRegionCapturer(AnchorCapturer):
    """Anchor capturer for DropRegion - creates a triggerable thing."""

    def handle(self, anchor: DropRegion) -> ThingState:
        # Convert confirmed links to actual ThingLink objects
        links = []
        for i, (key_b64, _) in enumerate(anchor.confirmed_links):
            try:
                decoded_key = decode_key_b64(key_b64)
                links.append(
                    ThingLink(
                        link_type="confirmed",
                        link_key=f"link-{i}",
                        to_thing_key=decoded_key,
                    )
                )
            except Exception as e:
                print(f"  Warning: Could not decode key {key_b64[:20]}...: {e}")

        return ThingState(
            key=("drop_region", anchor.name),
            thing_type="DropRegion",
            thing_data={
                "name": anchor.name,
                "filter_mode": anchor.filter_mode,
            },
            links=links,
        )


# =============================================================================
# Demo Logic
# =============================================================================


async def drop_region_handler(watcher: Watcher, region: DropRegion):
    """Handle triggers for a drop region - creates links to dropped things."""
    print(f"  [{region.name}] Waiting for drops...")

    while True:
        # Wait for a trigger with a thing key payload
        payload = await watcher.receive(region)

        if payload is None:
            print(f"  [{region.name}] Received empty trigger, ignoring")
            continue

        thing_key = payload.get("thingKey")
        thing_type = payload.get("thingType", "Unknown")

        if thing_key:
            print(f"  [{region.name}] Confirmed drop: {thing_type} (key: {thing_key[:20]}...)")
            region.confirmed_links.append((thing_key, thing_type))
            watcher.tick("info", f"{region.name} confirmed {thing_type}")
        else:
            print(f"  [{region.name}] Invalid payload: {payload}")


async def run_demo(watcher: Watcher):
    """Main demo logic."""
    # Register snapshotters and capturers
    register_snapshotter(Fruit, FruitSnapshotter())
    register_anchor_capturer(Fruit, FruitCapturer())
    register_anchor_capturer(DropRegion, DropRegionCapturer())

    # Configure rendering colors
    configure_rendering("Apple", "#e74c3c")  # Red
    configure_rendering("Orange", "#f39c12")  # Orange
    configure_rendering("Banana", "#f1c40f")  # Yellow
    configure_rendering("DropRegion", "#3498db")  # Blue

    # Register custom renderers
    register_renderer("Apple", renderer_path("Fruit"))
    register_renderer("Orange", renderer_path("Fruit"))
    register_renderer("Banana", renderer_path("Fruit"))
    register_renderer("DropRegion", renderer_path("DropRegion"))

    print("\n=== Drag and Drop Demo on port 8765 ===\n")
    print("This demo shows drag-and-drop with custom drop regions.")
    print(f"Renderers loaded from: {SCRIPT_DIR}")
    print("Typecheck with: deno check *.tsx")
    print()

    # Create some fruits
    apple1 = Fruit("apple1", "Apple")
    apple2 = Fruit("apple2", "Apple")
    orange1 = Fruit("orange1", "Orange")
    banana1 = Fruit("banana1", "Banana")

    watcher.track(apple1)
    watcher.track(apple2)
    watcher.track(orange1)
    watcher.track(banana1)

    # Compute the base64 key for apple1 to use in the "specific key" filter
    apple1_key = ("fruit", "apple1")
    apple1_key_b64 = base64.b64encode(cbor2.dumps(apple1_key)).decode("ascii")

    # Create drop regions with different filters
    region_any = DropRegion("any-drop", "any")
    region_apples = DropRegion("apples-only", "type:Apple")
    region_apple1_only = DropRegion("apple1-only", f"key:{apple1_key_b64}")  # Only accepts apple1

    # Add roots so they're easy to find
    watcher.add_root("Any Drop Zone", region_any)
    watcher.add_root("Apples Only", region_apples)
    watcher.add_root("Apple1 Only", region_apple1_only)
    watcher.add_root("apple1", apple1)
    watcher.add_root("apple2", apple2)
    watcher.add_root("orange1", orange1)
    watcher.add_root("banana1", banana1)

    watcher.tick("info", "Demo started - drag fruits onto drop zones!")

    print("\nInstructions:")
    print("1. Open http://localhost:8765 in your browser")
    print("2. Drag fruits from the Roots panel or graph onto drop regions")
    print("3. Drop regions will highlight when valid drops are detected")
    print("4. Click 'Confirm' to trigger and create a link")
    print()
    print("Filter modes:")
    print("  - 'any-drop': Accepts any fruit")
    print("  - 'apples-only': Only accepts Apple things")
    print(f"  - 'apple1-only': Only accepts apple1 specifically (key:{apple1_key_b64[:20]}...)")
    print()
    print("Press Ctrl+C to stop the server.")
    print("-" * 50)

    # Run handlers for each drop region
    async with anyio.create_task_group() as tg:
        tg.start_soon(drop_region_handler, watcher, region_any)
        tg.start_soon(drop_region_handler, watcher, region_apples)
        tg.start_soon(drop_region_handler, watcher, region_apple1_only)

        # Keep running until cancelled
        try:
            while True:
                await anyio.sleep(1)
        except anyio.get_cancelled_exc_class():
            tg.cancel_scope.cancel()


async def main(db_path: str | None):
    if db_path is None:
        import datetime

        db_path = f"/tmp/demo_drag_drop_{datetime.datetime.now().isoformat()}.db"
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
                        await run_demo(watcher)
                    print("Demo task group exited")
                    await shutdown_event.wait()
                finally:
                    shutdown_event.set()
                print("Main loop ready to exit, just waiting on watcher")
            signals_tg.cancel_scope.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo server for drag and drop testing")
    parser.add_argument(
        "--db", type=str, default=None, help="Path to SQLite database file (default: auto)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
