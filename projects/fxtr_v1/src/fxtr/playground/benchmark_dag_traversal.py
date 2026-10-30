"""Benchmark comparing original and optimized DAG traversal algorithms."""

import time

from dag_traversal import iteratively_merge as iteratively_merge_v1
from dag_traversal_v2 import OriginalEdge
from dag_traversal_v2 import iteratively_merge as iteratively_merge_v2


def generate_large_dag(n_nodes: int) -> tuple[list[str], list[OriginalEdge]]:
    """Generate a large DAG for benchmarking.

    Creates a layered structure with some bypass edges to test blocking path logic.
    """
    nodes = [f"N{i}" for i in range(n_nodes)]
    edges = []

    # Create a chain with some branching
    for i in range(n_nodes - 1):
        # Add some bypass edges every 5 nodes
        if i % 5 == 0 and i + 3 < n_nodes:
            edges.append(OriginalEdge(source=f"N{i}", target=f"N{i+3}", type="bypass", weight=10.0))

        # Main chain
        edges.append(OriginalEdge(source=f"N{i}", target=f"N{i+1}", type="chain", weight=1.0))

    return nodes, edges


def benchmark(name: str, func, nodes, edges, warmup: bool = False):
    """Benchmark a function."""
    if warmup:
        # Silence output during warmup
        import io
        import sys

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        func(nodes, edges)
        sys.stdout = old_stdout
        return None

    start = time.time()
    result = func(nodes, edges)
    end = time.time()
    return end - start, result


def main():
    print("=" * 70)
    print("DAG Traversal Algorithm Benchmark")
    print("=" * 70)

    # test_sizes = [10, 20, 30, 50]
    test_sizes = [50]

    for size in test_sizes:
        print(f"\n--- Testing with {size} nodes ---")
        nodes, edges = generate_large_dag(size)
        print(f"Generated {len(nodes)} nodes and {len(edges)} edges")

        # Warmup
        benchmark("V1 warmup", iteratively_merge_v1, nodes, edges, warmup=True)
        benchmark("V2 warmup", iteratively_merge_v2, nodes, edges, warmup=True)

        print("\nV1 (Original - BFS for each edge):")
        time_v1, result_v1 = benchmark("V1", iteratively_merge_v1, nodes, edges)

        print("\nV2 (Optimized - Rank-bounded DFS + blocking paths):")
        time_v2, result_v2 = benchmark("V2", iteratively_merge_v2, nodes, edges)

        print(f"\n{'='*50}")
        print(f"Results for {size} nodes:")
        print(f"  V1 time: {time_v1:.4f}s")
        print(f"  V2 time: {time_v2:.4f}s")
        print(f"  Speedup: {time_v1/time_v2:.2f}x")
        print(f"  Both produced {len(result_v1)} component(s)")
        print(f"{'='*50}")


if __name__ == "__main__":
    main()
