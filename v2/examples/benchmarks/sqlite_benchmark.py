"""Benchmark comparing sqlite_arena (old) vs sqlite_store (new) implementations.

This benchmark measures performance across three scenarios:
A) Copy large entity from memory to SQLite namespace
B) Copy large entity from SQLite to fresh memory store
C) Copy large entity between namespaces in same SQLite store

Usage (from v2 directory):
    uv run python examples/benchmarks/sqlite_benchmark.py [--skip-old]

    --skip-old: Only benchmark the new implementation (useful for comparing
                changes to sqlite_store without re-running slow old implementation)
"""

from __future__ import annotations

import argparse
import random
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

# Import implementations
from fxtr2.data.deprecated.sqlite_arena import SQLiteFxtrSession as OldSQLiteFxtrSession
from fxtr2.data.entity_serdes import ChildRef, DataclassEntity
from fxtr2.data.fxtr_scope import FxtrScope
from fxtr2.data.memory_arena import MemoryStore
from fxtr2.data.sqlite_store import SQLiteFxtrSession as NewSQLiteFxtrSession
from fxtr2.data.system_entities import define_type

# =============================================================================
# Test Entity: Binary Tree Node
# =============================================================================

TREE_NAMESPACE = uuid.UUID("4a5e8c3d-1f2a-4b6c-9d8e-7f3a2b1c0d9e")


@dataclass(frozen=True)
class TreeNode(DataclassEntity, fxtr_type=define_type(TREE_NAMESPACE, "tree_node")):
    """A binary tree node with 1KB random data payload."""

    value: int
    left: ChildRef[TreeNode] | None
    right: ChildRef[TreeNode] | None
    data: bytes  # 1KB random payload


def create_binary_tree(depth: int, value_offset: int = 0) -> TreeNode:
    """Create a complete binary tree of given depth.

    Args:
        depth: Tree depth (depth 10 creates ~1024 nodes)
        value_offset: Starting value for node numbering

    Returns:
        Root node of the tree
    """
    if depth == 0:
        # Leaf node
        return TreeNode(
            value=value_offset,
            left=None,
            right=None,
            data=random.randbytes(1024),  # 1KB random data
        )

    # Internal node - create left and right subtrees
    left_child = create_binary_tree(depth - 1, value_offset * 2 + 1)
    right_child = create_binary_tree(depth - 1, value_offset * 2 + 2)

    left_ref = left_child.store()
    right_ref = right_child.store()

    return TreeNode(
        value=value_offset,
        left=left_ref,
        right=right_ref,
        data=random.randbytes(1024),
    )


# =============================================================================
# Benchmark Scenarios
# =============================================================================


async def scenario_a_memory_to_sqlite(
    db_path: str, tree_depth: int, use_old: bool
) -> tuple[float, int]:
    """Scenario A: Copy large entity from memory to SQLite namespace.

    Returns:
        (elapsed_time, entity_count)
    """
    # Create tree in memory
    async with FxtrScope() as scope:
        tree_root = create_binary_tree(tree_depth)
        root_ref = tree_root.store()

        # Get entity count
        entity_count = 2**tree_depth - 1  # Complete binary tree formula

        # Time the copy operation
        start = time.perf_counter()

        if use_old:
            # Old implementation uses SQLAlchemy URL
            db_url = f"sqlite+aiosqlite:///{db_path}"
            async with OldSQLiteFxtrSession.open(db_url) as session:
                ns = await session.create_or_retrieve_user_namespace("test_ns")
                # Pin copies the entity to the namespace
                await ns.pin_entity_from_arena("tree_root", root_ref.ref, scope._main_arena)
        else:
            # New implementation uses plain path
            async with NewSQLiteFxtrSession.open(db_path) as session:
                ns = await session.create_or_retrieve_user_namespace("test_ns")
                await ns.pin_entity_from_arena("tree_root", root_ref.ref, scope._main_arena)

        elapsed = time.perf_counter() - start

    return elapsed, entity_count


async def scenario_b_sqlite_to_memory(
    db_path: str, tree_depth: int, use_old: bool
) -> tuple[float, int]:
    """Scenario B: Copy large entity from SQLite to fresh memory store.

    Assumes db_path contains a pinned tree from scenario A.

    Returns:
        (elapsed_time, entity_count)
    """
    entity_count = 2**tree_depth - 1

    # Create fresh memory store and arena
    memory_store = MemoryStore()
    memory_arena = memory_store.new_empty_arena()

    start = time.perf_counter()

    if use_old:
        db_url = f"sqlite+aiosqlite:///{db_path}"
        async with OldSQLiteFxtrSession.open(db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            # Retrieve creates an arena with the entity
            root_id, sqlite_arena = await ns.retrieve_pinned("tree_root")
            # Copy to memory arena
            await memory_arena.copy_from_arena(root_id, sqlite_arena)
    else:
        async with NewSQLiteFxtrSession.open(db_path) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            root_id, sqlite_arena = await ns.retrieve_pinned("tree_root")
            await memory_arena.copy_from_arena(root_id, sqlite_arena)

    elapsed = time.perf_counter() - start

    return elapsed, entity_count


async def scenario_c_cross_namespace(
    db_path: str, tree_depth: int, use_old: bool
) -> tuple[float, int]:
    """Scenario C: Copy large entity between namespaces in same SQLite store.

    Assumes db_path contains a pinned tree in namespace "test_ns".
    Copies to a fresh namespace "test_ns_2".

    Returns:
        (elapsed_time, entity_count)
    """
    entity_count = 2**tree_depth - 1

    start = time.perf_counter()

    if use_old:
        db_url = f"sqlite+aiosqlite:///{db_path}"
        async with OldSQLiteFxtrSession.open(db_url) as session:
            ns1 = await session.create_or_retrieve_user_namespace("test_ns")
            ns2 = await session.create_or_retrieve_user_namespace("test_ns_2")

            # Retrieve from ns1
            root_id, arena = await ns1.retrieve_pinned("tree_root")

            # Pin to ns2 (this copies to the new namespace)
            await ns2.pin_entity_from_arena("tree_root", root_id, arena)
    else:
        async with NewSQLiteFxtrSession.open(db_path) as session:
            ns1 = await session.create_or_retrieve_user_namespace("test_ns")
            ns2 = await session.create_or_retrieve_user_namespace("test_ns_2")

            root_id, arena = await ns1.retrieve_pinned("tree_root")
            await ns2.pin_entity_from_arena("tree_root", root_id, arena)

    elapsed = time.perf_counter() - start

    return elapsed, entity_count


# =============================================================================
# Benchmark Runner
# =============================================================================


async def run_benchmarks(tree_depth: int, skip_old: bool) -> dict[str, Any]:
    """Run all benchmark scenarios and return results.

    Args:
        tree_depth: Depth of binary tree (10 = ~1024 nodes)
        skip_old: If True, only run new implementation

    Returns:
        Dictionary with benchmark results
    """
    results: dict[str, Any] = {
        "tree_depth": tree_depth,
        "entity_count": 2**tree_depth - 1,
        "scenarios": {},
    }

    print(f"\n{'='*70}")
    print(f"Benchmark Configuration")
    print(f"{'='*70}")
    print(f"Tree depth: {tree_depth}")
    print(f"Entity count: {results['entity_count']}")
    print(f"Skip old implementation: {skip_old}")
    print()

    # Create temporary databases
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Scenario A: Memory → SQLite
        print(f"{'='*70}")
        print("Scenario A: Copy from Memory to SQLite")
        print(f"{'='*70}")

        scenario_a_results = {}

        if not skip_old and True:
            old_db = str(tmpdir_path / "scenario_a_old.sqlite")
            old_time, entity_count = await scenario_a_memory_to_sqlite(
                old_db, tree_depth, use_old=True
            )
            scenario_a_results["old"] = old_time
            print(f"Old implementation: {old_time:.3f}s ({entity_count} entities)")

        new_db = str(tmpdir_path / "scenario_a_new.sqlite")
        new_time, entity_count = await scenario_a_memory_to_sqlite(
            new_db, tree_depth, use_old=False
        )
        scenario_a_results["new"] = new_time
        print(f"New implementation: {new_time:.3f}s ({entity_count} entities)")

        if "old" in scenario_a_results:
            speedup = scenario_a_results["old"] / scenario_a_results["new"]
            print(f"Speedup: {speedup:.2f}x")

        results["scenarios"]["A_memory_to_sqlite"] = scenario_a_results
        print()

        # Scenario B: SQLite → Memory
        print(f"{'='*70}")
        print("Scenario B: Copy from SQLite to Memory")
        print(f"{'='*70}")

        scenario_b_results = {}

        if not skip_old and True:
            old_time, entity_count = await scenario_b_sqlite_to_memory(
                old_db, tree_depth, use_old=True
            )
            scenario_b_results["old"] = old_time
            print(f"Old implementation: {old_time:.3f}s ({entity_count} entities)")

        new_time, entity_count = await scenario_b_sqlite_to_memory(
            new_db, tree_depth, use_old=False
        )
        scenario_b_results["new"] = new_time
        print(f"New implementation: {new_time:.3f}s ({entity_count} entities)")

        if "old" in scenario_b_results:
            speedup = scenario_b_results["old"] / scenario_b_results["new"]
            print(f"Speedup: {speedup:.2f}x")

        results["scenarios"]["B_sqlite_to_memory"] = scenario_b_results
        print()

        # Scenario C: Cross-namespace copy
        print(f"{'='*70}")
        print("Scenario C: Copy Between Namespaces (Same SQLite Store)")
        print(f"{'='*70}")

        scenario_c_results = {}

        if not skip_old and True:
            old_time, entity_count = await scenario_c_cross_namespace(
                old_db, tree_depth, use_old=True
            )
            scenario_c_results["old"] = old_time
            print(f"Old implementation: {old_time:.3f}s ({entity_count} entities)")

        new_time, entity_count = await scenario_c_cross_namespace(new_db, tree_depth, use_old=False)
        scenario_c_results["new"] = new_time
        print(f"New implementation: {new_time:.3f}s ({entity_count} entities)")

        if "old" in scenario_c_results:
            speedup = scenario_c_results["old"] / scenario_c_results["new"]
            print(f"Speedup: {speedup:.2f}x")

        results["scenarios"]["C_cross_namespace"] = scenario_c_results
        print()

    return results


def print_summary(results: dict[str, Any]) -> None:
    """Print a summary table of benchmark results."""
    print(f"\n{'='*70}")
    print("Summary")
    print(f"{'='*70}")
    print(f"Entity count: {results['entity_count']} nodes")
    print(f"Payload size: 1KB per node")
    print(f"Total data: ~{results['entity_count'] / 1024:.1f} MB")
    print()

    print(f"{'Scenario':<40} {'Old (s)':<12} {'New (s)':<12} {'Speedup':>8}")
    print("-" * 70)

    for scenario_name, scenario_results in results["scenarios"].items():
        # Format scenario name
        if scenario_name == "A_memory_to_sqlite":
            display_name = "A: Memory → SQLite"
        elif scenario_name == "B_sqlite_to_memory":
            display_name = "B: SQLite → Memory"
        elif scenario_name == "C_cross_namespace":
            display_name = "C: Cross-namespace"
        else:
            display_name = scenario_name

        old_time = scenario_results.get("old")
        new_time = scenario_results["new"]

        if old_time is not None:
            speedup = old_time / new_time
            print(f"{display_name:<40} {old_time:>10.3f}  {new_time:>10.3f}  {speedup:>7.2f}x")
        else:
            print(f"{display_name:<40} {'N/A':<12} {new_time:>10.3f}  {'N/A':>8}")

    print()


async def main() -> None:
    """Main entry point for benchmark."""
    parser = argparse.ArgumentParser(
        description="Benchmark sqlite_arena vs sqlite_store implementations"
    )
    parser.add_argument(
        "--skip-old",
        action="store_true",
        help="Skip old implementation (only benchmark new sqlite_store)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=10,
        help="Binary tree depth (default: 10, creates ~1024 entities)",
    )

    args = parser.parse_args()

    if args.skip_old:
        print("Note: Skipping old implementation (--skip-old flag)")
    elif not True:
        print("Warning: Old implementation not available (sqlite_arena not in deprecated/)")
        print("Only benchmarking new implementation")
        args.skip_old = True

    # Run benchmarks
    results = await run_benchmarks(tree_depth=args.depth, skip_old=args.skip_old)

    # Print summary
    print_summary(results)


if __name__ == "__main__":
    anyio.run(main)
