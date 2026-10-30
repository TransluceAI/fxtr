#!/usr/bin/env python3
"""Demo for fxtr2-thingwatcher integration.

This demo showcases:
1. Creating fxtr entities with cross-references
2. Pinning entities into a namespace
3. Visualizing the entity graph in thingwatcher
4. Navigating through entity links in the UI
5. Using the fallback renderer for generic entity visualization

Run from the thingwatcher directory with:
    uv run python examples/demo_fxtr/demo.py

Then open http://localhost:8765 to explore the entity graph.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import signal

import anyio
from anyio import open_signal_receiver
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_serdes import DataclassEntity, StrongLinkedEntityRef
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.fxtr_scope import FxtrScope
from thingwatcher import Watcher
from thingwatcher.fxtr import fxtr_namespace_view

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Demo Entity Types
# =============================================================================

# Define type IDs for our demo entities
PERSON_TYPE = FxtrID.builtin("demo.person")
PROJECT_TYPE = FxtrID.builtin("demo.project")
TASK_TYPE = FxtrID.builtin("demo.task")


@dataclasses.dataclass(frozen=True)
class Person(DataclassEntity, fxtr_type=PERSON_TYPE):
    """A person in our demo system."""

    name: str
    email: str
    role: str


@dataclasses.dataclass(frozen=True)
class Task(DataclassEntity, fxtr_type=TASK_TYPE):
    """A task with an assigned person."""

    title: str
    description: str
    status: str
    assignee: StrongLinkedEntityRef[Person]


@dataclasses.dataclass(frozen=True)
class Project(DataclassEntity, fxtr_type=PROJECT_TYPE):
    """A project with tasks and team members."""

    name: str
    description: str
    tasks: list[StrongLinkedEntityRef[Task]]
    lead: StrongLinkedEntityRef[Person]


# =============================================================================
# Demo Setup
# =============================================================================


def track_demo_types(scope: FxtrScope) -> None:
    """Track all builtin types used by demo entities."""
    scope.track_builtin(PERSON_TYPE)
    scope.track_builtin(PROJECT_TYPE)
    scope.track_builtin(TASK_TYPE)


async def create_demo_entities(ns: SQLiteFxtrNamespace) -> None:
    """Create and pin demo entities into the namespace."""
    # Create people
    alice = Person(name="Alice", email="alice@example.com", role="Tech Lead")
    bob = Person(name="Bob", email="bob@example.com", role="Developer")
    carol = Person(name="Carol", email="carol@example.com", role="Designer")

    alice_ref = alice.store()
    bob_ref = bob.store()
    carol_ref = carol.store()

    # Create tasks
    task1 = Task(
        title="Design user dashboard",
        description="Create mockups for the new user dashboard with charts and widgets",
        status="in_progress",
        assignee=carol_ref,
    )
    task2 = Task(
        title="Implement REST API",
        description="Build the backend API endpoints for dashboard data",
        status="todo",
        assignee=bob_ref,
    )
    task3 = Task(
        title="Code review",
        description="Review PRs from the team and provide feedback",
        status="done",
        assignee=alice_ref,
    )
    task4 = Task(
        title="Write documentation",
        description="Document the API endpoints and usage examples",
        status="todo",
        assignee=bob_ref,
    )

    task1_ref = task1.store()
    task2_ref = task2.store()
    task3_ref = task3.store()
    task4_ref = task4.store()

    # Create a project
    project = Project(
        name="Dashboard Redesign",
        description="Modernize the user dashboard with new visualizations",
        tasks=[task1_ref, task2_ref, task3_ref, task4_ref],
        lead=alice_ref,
    )
    project_ref = project.store()

    # Pin entities into the namespace
    # Pin the project as the main entry point
    await ns.pin("project:dashboard", project_ref)

    # Also pin individual people for direct access
    await ns.pin("person:alice", alice_ref)
    await ns.pin("person:bob", bob_ref)
    await ns.pin("person:carol", carol_ref)

    # Pin a task directly
    await ns.pin("task:api", task2_ref)


# =============================================================================
# Main Demo
# =============================================================================


async def main(db_path: str | None):
    if db_path is None:
        import datetime

        db_path = f"/tmp/fxtr_demo_{datetime.datetime.now().isoformat()}.db"
        print(f"Using database: {db_path}")

    db_url = f"sqlite+aiosqlite:///{db_path}"
    watcher_db = db_path.replace(".db", "_watcher.db")

    watcher = Watcher(db_path=watcher_db, port=8765)
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
                        async with SQLiteFxtrNamespace(db_url, "demo") as ns:
                            # Track our entity types in the scope
                            track_demo_types(ns.scope)

                            # Create demo entities
                            await create_demo_entities(ns)

                            # Mount namespace in thingwatcher
                            async with fxtr_namespace_view(ns, "demo", watcher):
                                # Create ticks
                                watcher.tick("info", "Initial state - explore the entity graph!")

                                print("\n=== fxtr2-thingwatcher Integration Demo ===\n")
                                print(
                                    "This demo showcases fxtr entity visualization in"
                                    " thingwatcher:\n"
                                )
                                print("1. Entity graph with cross-references")
                                print("2. Fallback renderer showing entity data as JSON")
                                print("3. Clickable links between entities")
                                print("4. Backlinks showing what references each entity")
                                print()
                                print("\nDemo setup complete!")
                                print()
                                print("Things to try:")
                                print("  1. Click on 'demo' root to see pinned entities")
                                print("  2. Click on the project to see its tasks and lead")
                                print("  3. Follow links from tasks to their assignees")
                                print("  4. Check backlinks on a Person to see what refs them")
                                print("  5. Explore the JSON data for each entity")
                                print()
                                print("Open http://localhost:8765 to explore!")
                                print("Press Ctrl+C to stop the server.")

                                await anyio.sleep_forever()
                except anyio.get_cancelled_exc_class():
                    pass  # Normal shutdown
                finally:
                    shutdown_event.set()
                print("Demo ready to exit, waiting on watcher...")
            signals_tg.cancel_scope.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="fxtr2-thingwatcher integration demo")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database file (default: auto-generated temp file)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
