"""Demo server for testing the embed view with triggerable widgets.

This demo creates widgets that:
- Are triggerable via receive() loop
- Accumulate messages in their data
- Have a custom renderer that uses args from postMessage
- Update live via streaming or regular ticks

Usage:
    python demo.py

Then open http://localhost:8765/debug-embed to test the embed view.

The renderers are loaded from .tsx files in this directory.
Typecheck with: deno check *.tsx
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from thingwatcher import ThingState, Watcher
from thingwatcher.api import (
    AnchorCapturer,
    StatelessSnapshotter,
    register_anchor_capturer,
    register_renderer,
    register_snapshotter,
)
from thingwatcher.registry import configure_rendering

# Directory containing this script (and the .tsx renderer files)
SCRIPT_DIR = Path(__file__).parent


def load_renderer(name: str) -> str:
    """Load a renderer TSX file from the same directory as this script."""
    path = SCRIPT_DIR / f"{name}.tsx"
    return path.read_text()


@dataclass
class Widget:
    """A demo widget that collects messages."""

    id: str
    title: str
    messages: list[str] = field(default_factory=list)


class WidgetSnapshotter(StatelessSnapshotter):
    """Snapshotter for Widget objects."""

    def snapshot(self, obj: Widget) -> ThingState:
        return ThingState(
            key={"widget": obj.id},
            thing_type="Widget",
            thing_data={
                "title": obj.title,
                "messages": obj.messages,
                "messageCount": len(obj.messages),
            },
            links=[],
        )


class WidgetAnchorCapturer(AnchorCapturer):
    """Anchor capturer for Widget objects - allows using widget as anchor for receive()."""

    def handle(self, anchor: Widget) -> ThingState:
        # Return the widget's state directly (widget is its own anchor)
        return ThingState(
            key={"widget": anchor.id},
            thing_type="Widget",
            thing_data={
                "title": anchor.title,
                "messages": anchor.messages,
                "messageCount": len(anchor.messages),
            },
            links=[],
        )


async def widget_receive_loop(watcher: Watcher, widget: Widget):
    """Loop that waits for messages and adds them to the widget."""
    from thingwatcher import receive, tick

    while True:
        # Wait for a trigger with receive()
        # This makes the widget triggerable in the UI
        payload = await receive(widget, Widget)

        # Extract message from payload
        if payload and isinstance(payload, dict) and "message" in payload:
            message = str(payload["message"])
            widget.messages.append(message)
            print(f"[{widget.id}] Received: {message}")

        # Tick to save the new state
        tick("info", f"Widget {widget.id} received message")


async def main(port: int = 8765):
    """Run the demo server."""
    # Register snapshotters and capturers
    register_snapshotter(Widget, WidgetSnapshotter())
    register_anchor_capturer(Widget, WidgetAnchorCapturer())

    # Configure rendering
    configure_rendering("Widget", color="#4a9")

    # Register custom renderer from .tsx file
    register_renderer("Widget", load_renderer("Widget"))

    # Create watcher
    watcher = Watcher(db_path=":memory:", port=port)

    async with watcher.watching_and_serving():
        # Create widgets
        alpha = Widget("alpha", "Alpha Widget")
        beta = Widget("beta", "Beta Widget")

        # Track widgets
        watcher.track(alpha)
        watcher.track(beta)

        # Add as roots for easy access
        watcher.add_root("alpha", alpha, Widget)
        watcher.add_root("beta", beta, Widget)

        # Initial tick
        watcher.tick("info", "Widgets created")

        print()
        print("=" * 60)
        print("Embed Demo Server")
        print("=" * 60)
        print()
        print(f"Server running at http://localhost:{port}")
        print()
        print("Routes:")
        print(f"  Main app:    http://localhost:{port}/")
        print(f"  Embed view:  http://localhost:{port}/embed")
        print(f"  Debug UI:    http://localhost:{port}/debug-embed")
        print()
        print("Try these steps in the Debug UI:")
        print('  1. Enter key (keyJSON): {"widget": "alpha"}')
        print('  2. Enter args: {"highlight": "title", "message": "Hello!"}')
        print("  3. Click 'Send to Embed'")
        print("  4. Type a message in the widget and click Send")
        print("  5. Watch messages accumulate live!")
        print()
        print("Available presets match the demo widgets.")
        print()
        print(f"Renderers loaded from: {SCRIPT_DIR}")
        print("Typecheck with: deno check *.tsx")
        print()
        print("Press Ctrl+C to stop.")
        print()

        # Run receive loops for both widgets
        async with anyio.create_task_group() as tg:
            tg.start_soon(widget_receive_loop, watcher, alpha)
            tg.start_soon(widget_receive_loop, watcher, beta)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embed demo server")
    parser.add_argument("--port", type=int, default=8765, help="Port to run on")
    args = parser.parse_args()

    try:
        anyio.run(main, args.port)
    except KeyboardInterrupt:
        print("\nShutting down...")
