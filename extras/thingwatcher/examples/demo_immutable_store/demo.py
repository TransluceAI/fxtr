"""Demo for immutable object stores feature.

This demo showcases:
1. A 4-level binary tree of immutable things in an ImmutableStore
2. Active snapshot things that link into the middle of the tree
3. Override scenario: snapshot thing temporarily overrides an immutable thing
4. Ephemeral snapshot thing that appears/disappears (can be pinned)
5. Backlinks from immutable store things

The demo creates several ticks to show different states:
- Ticks 0-1: Active things pointing into tree, no override
- Ticks 2-3: Override appears (snapshot with same key as immutable thing, different data)
- Ticks 4-5: Override disappears, ephemeral snapshot thing appears
- Tick 6+: Ephemeral thing disappears
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from anyio import open_signal_receiver
from thingwatcher import ThingLink, ThingRef, ThingState, Watcher, register_snapshotter
from thingwatcher.api import (
    AnchorCapturer,
    BacklinkInfo,
    StatelessSnapshotter,
    register_anchor_capturer,
    register_immutable_store,
    register_renderer,
)
from thingwatcher.registry import configure_rendering

# Directory containing this script (and the .tsx renderer files)
SCRIPT_DIR = Path(__file__).parent

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Binary Tree Immutable Store
# =============================================================================


def build_tree_path(index: int) -> str:
    """Convert a 0-based index to a tree path string.

    Index 0 = root
    Index 1, 2 = L, R (children of root)
    Index 3, 4, 5, 6 = LL, LR, RL, RR
    Index 7-14 = LLL, LLR, LRL, LRR, RLL, RLR, RRL, RRR
    """
    if index == 0:
        return "root"

    # Find level and position within level
    # Level 0: 1 node (index 0)
    # Level 1: 2 nodes (indices 1-2)
    # Level 2: 4 nodes (indices 3-6)
    # Level 3: 8 nodes (indices 7-14)
    level = 0
    level_start = 0
    level_size = 1
    while index >= level_start + level_size:
        level_start += level_size
        level_size *= 2
        level += 1

    position = index - level_start

    # Build path from position: each bit determines L or R
    path = ""
    for i in range(level - 1, -1, -1):
        if (position >> i) & 1:
            path += "R"
        else:
            path += "L"

    return path


def get_parent_path(path: str) -> str | None:
    """Get the parent's path, or None if root."""
    if path == "root":
        return None
    if len(path) == 1:
        return "root"
    return path[:-1]


def get_children_paths(path: str, max_level: int = 3) -> tuple[str | None, str | None]:
    """Get the paths of left and right children, or None if at max level."""
    if path == "root":
        current_level = 0
    else:
        current_level = len(path)

    if current_level >= max_level:
        return None, None

    prefix = "" if path == "root" else path
    return (prefix + "L", prefix + "R")


@dataclass
class BinaryTreeStore:
    """An immutable store containing a binary tree of nodes.

    Each node has:
    - A value (the node's path string)
    - Links to its children (if not a leaf)
    - Backlinks index for efficient reverse lookups
    """

    num_levels: int = 4  # 4 levels = 15 nodes
    _backlinks: dict[tuple, list[BacklinkInfo]] = field(default_factory=dict)

    def __post_init__(self):
        # Build the backlinks index
        self._build_backlinks_index()

    def _build_backlinks_index(self):
        """Build reverse index: for each node, which nodes link to it."""
        num_nodes = (1 << self.num_levels) - 1  # 2^levels - 1

        for i in range(num_nodes):
            path = build_tree_path(i)
            key = ("tree", path)
            left_path, right_path = get_children_paths(path, self.num_levels - 1)

            if left_path:
                left_key = ("tree", left_path)
                if left_key not in self._backlinks:
                    self._backlinks[left_key] = []
                self._backlinks[left_key].append(
                    BacklinkInfo(
                        from_thing_key=key,
                        link_type="left",
                        link_key="left",
                    )
                )

            if right_path:
                right_key = ("tree", right_path)
                if right_key not in self._backlinks:
                    self._backlinks[right_key] = []
                self._backlinks[right_key].append(
                    BacklinkInfo(
                        from_thing_key=key,
                        link_type="right",
                        link_key="right",
                    )
                )

    def _path_from_key(self, key: Any) -> str | None:
        """Extract path from a tree key, or None if not a tree key."""
        if not isinstance(key, (tuple, list)) or len(key) != 2:
            return None
        if key[0] != "tree":
            return None
        return key[1]

    def _is_valid_path(self, path: str) -> bool:
        """Check if a path exists in this tree."""
        if path == "root":
            return True
        if not path:
            return False
        max_depth = self.num_levels - 1
        if len(path) > max_depth:
            return False
        return all(c in "LR" for c in path)

    async def get_thing_state(self, key: Any) -> ThingState | None:
        """Look up a tree node by its key."""
        path = self._path_from_key(key)
        if path is None or not self._is_valid_path(path):
            return None

        # Build links to children
        links = []
        left_path, right_path = get_children_paths(path, self.num_levels - 1)

        if left_path:
            links.append(
                ThingLink(
                    link_type="left",
                    link_key="left",
                    to_thing_key=("tree", left_path),
                )
            )
        if right_path:
            links.append(
                ThingLink(
                    link_type="right",
                    link_key="right",
                    to_thing_key=("tree", right_path),
                )
            )

        return ThingState(
            key=key,
            thing_type="TreeNode",
            thing_data={
                "path": path,
                "level": 0 if path == "root" else len(path),
                "is_leaf": left_path is None,
                "description": f"Binary tree node at path '{path}'",
            },
            links=links,
        )

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:
        """Get backlinks to this node from parent nodes."""
        path = self._path_from_key(key)
        if path is None or not self._is_valid_path(path):
            return []

        # Convert to tuple for dict lookup (backlinks index uses tuple keys)
        key_tuple = tuple(key) if isinstance(key, list) else key
        return self._backlinks.get(key_tuple, [])


@dataclass
class OverridableThingStore:
    """A store with a single thing that can be overridden by a snapshot.

    This demonstrates the override behavior: when a snapshot thing has the
    same key as a store thing, the snapshot wins.
    """

    async def get_thing_state(self, key: Any) -> ThingState | None:
        """Return the overridable thing if requested."""
        # CBOR decodes arrays as lists, so convert to tuple for comparison
        key_tuple = tuple(key) if isinstance(key, list) else key
        if key_tuple == ("overridable", "shared_key"):
            return ThingState(
                key=key,
                thing_type="OverridableThing",
                thing_data={
                    "source": "immutable_store",
                    "message": "I am the ORIGINAL from the immutable store",
                    "color": "blue",
                },
                links=[],
            )
        return None

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:  # noqa: ARG002
        """No backlinks in this store."""
        del key  # Unused
        return []


# =============================================================================
# Domain objects for snapshot things
# =============================================================================


@dataclass
class TreePointer:
    """An active thing that points into the binary tree."""

    name: str
    target_path: str  # Path in the tree, e.g., "LR"


@dataclass
class OverrideSnapshot:
    """A snapshot thing that overrides an immutable thing."""

    # This will have the same key as the overridable immutable thing


@dataclass
class EphemeralThing:
    """A snapshot thing that appears temporarily and can be pinned."""

    name: str
    message: str


# =============================================================================
# Snapshotters
# =============================================================================


class TreePointerSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: TreePointer) -> ThingState:
        return ThingState(
            key=("pointer", obj.name),
            thing_type="TreePointer",
            thing_data={
                "name": obj.name,
                "target_path": obj.target_path,
                "description": f"Points to tree node '{obj.target_path}'",
            },
            links=[
                ThingLink(
                    link_type="target",
                    link_key="tree_target",
                    to_thing_key=("tree", obj.target_path),
                ),
            ],
        )


class OverrideSnapshotSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: OverrideSnapshot) -> ThingState:
        del obj  # Unused - all instances produce the same key
        # Use the SAME KEY as the immutable store thing
        return ThingState(
            key=("overridable", "shared_key"),
            thing_type="OverridableThing",  # Same type
            thing_data={
                "source": "snapshot",
                "message": "I am the OVERRIDE from the snapshot!",
                "color": "red",
            },
            links=[],
        )


class EphemeralThingSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: EphemeralThing) -> ThingState:
        return ThingState(
            key=("ephemeral", obj.name),
            thing_type="EphemeralThing",
            thing_data={
                "name": obj.name,
                "message": obj.message,
            },
            links=[],
        )


# =============================================================================
# Anchor capturers
# =============================================================================


class TreePointerAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: TreePointer) -> ThingRef:
        return ThingRef(key=("pointer", anchor.name))


class OverrideSnapshotAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: OverrideSnapshot) -> ThingRef:
        del anchor  # Unused - all instances produce the same key
        return ThingRef(key=("overridable", "shared_key"))


class EphemeralThingAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: EphemeralThing) -> ThingRef:
        return ThingRef(key=("ephemeral", anchor.name))


# =============================================================================
# Main demo
# =============================================================================


async def setup_demo(watcher: Watcher):
    """Set up the demo with immutable stores and snapshot things."""

    # Register snapshotters
    register_snapshotter(TreePointer, TreePointerSnapshotter())
    register_snapshotter(OverrideSnapshot, OverrideSnapshotSnapshotter())
    register_snapshotter(EphemeralThing, EphemeralThingSnapshotter())

    # Register anchor capturers
    register_anchor_capturer(TreePointer, TreePointerAnchorCapturer())
    register_anchor_capturer(OverrideSnapshot, OverrideSnapshotAnchorCapturer())
    register_anchor_capturer(EphemeralThing, EphemeralThingAnchorCapturer())

    # Configure rendering colors
    configure_rendering("TreeNode", "#8b5cf6")  # Purple for tree nodes
    configure_rendering("TreePointer", "#22c55e")  # Green for pointers
    configure_rendering("OverridableThing", "#f59e0b")  # Amber for overridable
    configure_rendering("EphemeralThing", "#06b6d4")  # Cyan for ephemeral

    # Register custom renderers
    register_renderer("TreeNode", (SCRIPT_DIR / "TreeNode.tsx").read_text())
    register_renderer("TreePointer", (SCRIPT_DIR / "TreePointer.tsx").read_text())
    register_renderer("OverridableThing", (SCRIPT_DIR / "OverridableThing.tsx").read_text())
    register_renderer("EphemeralThing", (SCRIPT_DIR / "EphemeralThing.tsx").read_text())

    # Register immutable stores
    tree_store = BinaryTreeStore(num_levels=4)
    register_immutable_store("binary_tree", tree_store)

    override_store = OverridableThingStore()
    register_immutable_store("override_store", override_store)

    print("\n=== Immutable Object Stores Demo on port 8765 ===\n")
    print("This demo showcases immutable object stores:\n")
    print("1. Binary tree store with 15 nodes (4 levels)")
    print("2. Active things pointing into the middle of the tree")
    print("3. Override scenario: snapshot overrides immutable thing")
    print("4. Ephemeral thing that appears/disappears (try pinning!)")
    print("5. Backlinks from tree nodes to their parents")
    print()

    # Create objects that will live across ticks
    pointer1 = TreePointer("mid_pointer", "LR")
    pointer2 = TreePointer("deep_pointer", "RLL")
    override_snapshot = OverrideSnapshot()
    ephemeral = EphemeralThing("temp_thing", "I only exist for a few ticks!")

    # --- Ticks 0-1: Active things pointing into tree, no override ---
    print("Creating ticks 0-1: Pointers to tree, no override...")

    watcher.track(pointer1)
    watcher.track(pointer2)
    watcher.add_root("pointers", pointer1)
    watcher.tick("info", "Tick 0: Pointers pointing into immutable tree")

    await anyio.sleep(0.1)  # Small delay between ticks
    watcher.tick("info", "Tick 1: Same state - explore the tree via links")

    # --- Ticks 2-3: Override appears ---
    print("Creating ticks 2-3: Override snapshot appears...")

    watcher.track(override_snapshot)
    watcher.add_root("override", override_snapshot)
    watcher.tick(
        "info", "Tick 2: Override snapshot now active - check ('overridable', 'shared_key')"
    )

    await anyio.sleep(0.1)
    watcher.tick("info", "Tick 3: Override still active - note 'overridden' badge on store source")

    # --- Ticks 4-5: Override disappears, ephemeral appears ---
    print("Creating ticks 4-5: Override gone, ephemeral appears...")

    watcher.untrack(override_snapshot)
    watcher.remove_root("override")
    watcher.track(ephemeral)
    watcher.add_root("ephemeral", ephemeral)
    watcher.tick("info", "Tick 4: Override gone (store version visible again), ephemeral appeared")

    await anyio.sleep(0.1)
    watcher.tick("info", "Tick 5: Try pinning the ephemeral thing before it disappears!")

    # --- Tick 6+: Ephemeral disappears ---
    print("Creating tick 6: Ephemeral gone...")

    watcher.untrack(ephemeral)
    watcher.remove_root("ephemeral")
    watcher.tick(
        "info", "Tick 6: Ephemeral gone - if you pinned it, it stays in the pinned section"
    )

    # Final tick
    watcher.tick("info", "Tick 7: Final state - pointers remain, explore the tree!")

    print("\nDemo setup complete!")
    print()
    print("Things to try:")
    print("  1. Click on a TreePointer to see it link into the immutable tree")
    print("  2. Follow links through the tree (left/right children, parent)")
    print("  3. Check backlinks on tree nodes to see what points to them")
    print("  4. Go to tick 2-3 to see the override scenario")
    print("  5. Go to tick 4-5, open ephemeral thing, PIN IT, then go to tick 6+")
    print("  6. Notice the pinned thing stays visible even when it doesn't exist")
    print()
    print("Open http://localhost:8765 to explore!")
    print("Press Ctrl+C to stop the server.")


async def main(db_path: str | None):
    if db_path is None:
        import datetime

        db_path = f"/tmp/demo_immutable_store_{datetime.datetime.now().isoformat()}.db"
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
                        await setup_demo(watcher)
                    print("Demo setup complete, waiting for shutdown signal...")
                    await shutdown_event.wait()
                finally:
                    shutdown_event.set()
                print("Main loop ready to exit, just waiting on watcher")
            signals_tg.cancel_scope.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo for immutable object stores feature")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database file (default: auto-generated temp file)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
