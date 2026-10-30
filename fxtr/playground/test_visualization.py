"""Test script for DAG visualization."""

import random

from dag_traversal_v2 import (
    OriginalEdge,
    assign_columns_to_nodes,
    compute_node_ownerships_heuristic,
    iteratively_merge,
    pretty_print_dag,
)


def test_simple_chain():
    """Test a simple chain: A -> B -> C"""
    print("\n" + "=" * 60)
    print("TEST: Simple chain A -> B -> C")
    print("=" * 60)

    nodes = ["A", "B", "C"]
    edges = [
        OriginalEdge(source="A", target="B", type="chain", weight=1.0),
        OriginalEdge(source="B", target="C", type="chain", weight=1.0),
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_diamond():
    """Test a diamond pattern: A -> B, A -> C, B -> D, C -> D"""
    print("\n" + "=" * 60)
    print("TEST: Diamond A -> B,C -> D")
    print("=" * 60)

    nodes = ["A", "B", "C", "D"]
    edges = [
        OriginalEdge(source="A", target="B", type="left", weight=1.0),
        OriginalEdge(source="A", target="C", type="right", weight=1.0),
        OriginalEdge(source="B", target="D", type="left", weight=1.0),
        OriginalEdge(source="C", target="D", type="right", weight=1.0),
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_fork():
    """Test a fork pattern: A -> B, A -> C, A -> D"""
    print("\n" + "=" * 60)
    print("TEST: Fork A -> B, C, D")
    print("=" * 60)

    nodes = ["A", "B", "C", "D"]
    edges = [
        OriginalEdge(source="A", target="B", type="fork", weight=1.0),
        OriginalEdge(source="A", target="C", type="fork", weight=1.0),
        OriginalEdge(source="A", target="D", type="fork", weight=1.0),
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_join():
    """Test a join pattern: A -> D, B -> D, C -> D"""
    print("\n" + "=" * 60)
    print("TEST: Join A, B, C -> D")
    print("=" * 60)

    nodes = ["A", "B", "C", "D"]
    edges = [
        OriginalEdge(source="A", target="D", type="join", weight=1.0),
        OriginalEdge(source="B", target="D", type="join", weight=1.0),
        OriginalEdge(source="C", target="D", type="join", weight=1.0),
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_complex():
    """Test a more complex pattern."""
    print("\n" + "=" * 60)
    print("TEST: Complex pattern")
    print("=" * 60)

    nodes = ["A", "B", "C", "D", "E", "F"]
    edges = [
        OriginalEdge(source="A", target="B", type="edge", weight=1.0),
        OriginalEdge(source="A", target="C", type="edge", weight=1.0),
        OriginalEdge(source="B", target="D", type="edge", weight=1.0),
        OriginalEdge(source="C", target="D", type="edge", weight=1.0),
        OriginalEdge(source="C", target="E", type="edge", weight=1.0),
        OriginalEdge(source="D", target="F", type="edge", weight=1.0),
        OriginalEdge(source="E", target="F", type="edge", weight=1.0),
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_larger_graph():
    """Test a larger, more complex graph."""
    print("\n" + "=" * 60)
    print("TEST: Larger graph with multiple patterns")
    print("=" * 60)

    nodes = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
    edges = [
        # Initial fork from A
        OriginalEdge(source="A", target="B", type="edge", weight=1.0),
        OriginalEdge(source="A", target="C", type="edge", weight=1.0),
        # B splits to D and E
        OriginalEdge(source="B", target="D", type="edge", weight=1.0),
        OriginalEdge(source="B", target="E", type="edge", weight=1.0),
        # C goes to F
        OriginalEdge(source="C", target="F", type="edge", weight=1.0),
        # D and E converge to G
        OriginalEdge(source="D", target="G", type="edge", weight=1.0),
        OriginalEdge(source="E", target="G", type="edge", weight=1.0),
        # F also goes to G
        OriginalEdge(source="F", target="G", type="edge", weight=1.0),
        # G splits to H and I
        OriginalEdge(source="G", target="H", type="edge", weight=1.0),
        OriginalEdge(source="G", target="I", type="edge", weight=1.0),
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_random_tree():
    """Test a pseudorandomly generated tree with randomly directed edges."""
    print("\n" + "=" * 60)
    print("TEST: Random tree (seed=42)")
    print("=" * 60)

    # Build tree pseudorandomly with fixed seed
    random.seed(42)
    # num_nodes = 20
    num_nodes = 10
    # Note: with 10 nodes and seed 42, the algorithm currently merges some nodes in a slightly
    # suboptimal manner, precommitting to an ordering that isn't a tree traversal. Could be due
    # to a flaw in the merge cost heuristic. Seems to occur fairly rarely in practice.

    # Start with the first node
    nodes = ["0"]
    edges = []

    # For each new node, attach it to a randomly selected existing node
    for i in range(1, num_nodes):
        new_node = str(i)
        # Randomly select an existing node to attach to
        existing_node = random.choice(nodes)

        # Randomly decide the direction of the edge
        if random.random() < 0.5:
            # new_node -> existing_node
            source, target = new_node, existing_node
        else:
            # existing_node -> new_node
            source, target = existing_node, new_node

        edges.append(OriginalEdge(source=source, target=target, type="tree", weight=1.0))

        nodes.append(new_node)

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    # random.shuffle(edges)
    # random.shuffle(nodes)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_random_tree_biased():
    """Test a pseudorandomly generated tree with randomly directed edges."""
    print("\n" + "=" * 60)
    print("TEST: Random tree (seed=42, biased)")
    print("=" * 60)

    # Build tree pseudorandomly with fixed seed
    random.seed(42)
    # num_nodes = 20
    num_nodes = 15

    # Start with the first node
    nodes = ["0"]
    edges = []

    # For each new node, attach it to a randomly selected existing node
    for i in range(1, num_nodes):
        new_node = str(i)
        # Randomly select an existing node to attach to
        j = len(nodes) - 1
        while random.random() < 0.5:
            j = (j - 1 + len(nodes)) % len(nodes)
        existing_node = nodes[j]

        # Randomly decide the direction of the edge
        if random.random() < 0.5:
            # new_node -> existing_node
            source, target = new_node, existing_node
        else:
            # existing_node -> new_node
            source, target = existing_node, new_node

        edges.append(OriginalEdge(source=source, target=target, type="tree", weight=1.0))

        nodes.append(new_node)

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    random.shuffle(edges)
    random.shuffle(nodes)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_grid_layers():
    """Test a grid structure with source, sink, and multiple layers of parallel chains."""
    print("\n" + "=" * 60)
    print("TEST: Grid with layers (source -> layer items -> sink)")
    print("=" * 60)

    source = "source"
    sink = "sink"
    num_layers = 4
    items_per_layer = 3

    nodes = [source, sink]
    edges = []

    # Create layers
    for layer_idx in range(num_layers):
        layer_items = []
        # Create items for this layer
        for item_idx in range(items_per_layer):
            item_name = f"layer{layer_idx}_item{item_idx}"
            layer_items.append(item_name)
            nodes.append(item_name)

        # Connect source -> item0 -> item1 -> ... -> itemK -> sink
        # source -> first item
        edges.append(OriginalEdge(source=source, target=layer_items[0], type="grid", weight=1.0))

        # item_i -> item_{i+1} for all items in the chain
        for i in range(len(layer_items) - 1):
            edges.append(
                OriginalEdge(
                    source=layer_items[i], target=layer_items[i + 1], type="grid", weight=1.0
                )
            )

        # last item -> sink
        edges.append(OriginalEdge(source=layer_items[-1], target=sink, type="grid", weight=1.0))

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_bipartite_zigzag():
    """Test a bipartite zig-zag structure with K sources and K-1 sinks, where each sink depends on two adjacent sources."""
    print("\n" + "=" * 60)
    print("TEST: Bipartite zig-zag (K sources, K-1 sinks)")
    print("=" * 60)

    K = 8  # Number of sources

    nodes = []
    edges = []

    # Create K sources
    sources = [f"source{i}" for i in range(K)]
    nodes.extend(sources)

    # Create K-1 sinks, each depending on two adjacent sources
    sinks = [f"sink{i}" for i in range(K - 1)]
    nodes.extend(sinks)

    # Connect each sink to its two adjacent sources
    for i in range(K - 1):
        # sink i depends on source i and source i+1
        edges.append(OriginalEdge(source=sources[i], target=sinks[i], type="zigzag", weight=1.0))
        edges.append(
            OriginalEdge(source=sources[i + 1], target=sinks[i], type="zigzag", weight=1.0)
        )

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)

    if len(result) != 1:
        print(f"Error: Expected 1 component, got {len(result)}")
        return

    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])
    print("\nVisualization:")
    pretty_print_dag(layout, edges)


def test_example():
    """Test an example graph from the mockup."""

    nodes = []
    edges = []

    t0 = "user: hello how are you"
    nodes.append(t0)
    t1 = "asst: How can I assist you today?"
    nodes.append(t1)
    edges.append(OriginalEdge(source=t0, target=t1, type="prec", weight=1.0))
    t2 = "user: i'd like to get an opinion..."
    nodes.append(t2)
    edges.append(OriginalEdge(source=t1, target=t2, type="prec", weight=1.0))
    t3 = "asst: I'd be happy to help you with..."
    nodes.append(t3)
    edges.append(OriginalEdge(source=t2, target=t3, type="prec", weight=1.0))
    t4 = "user: i'd like advice about my..."
    nodes.append(t4)
    edges.append(OriginalEdge(source=t3, target=t4, type="prec", weight=1.0))
    t5 = "asst: That sounds like a tricky..."
    nodes.append(t5)
    edges.append(OriginalEdge(source=t4, target=t5, type="prec", weight=1.0))
    t6 = "user: hm that's a good point"
    nodes.append(t6)
    edges.append(OriginalEdge(source=t5, target=t6, type="prec", weight=1.0))
    t7 = "asst: Glad that I could help!"
    nodes.append(t7)
    edges.append(OriginalEdge(source=t6, target=t7, type="prec", weight=1.0))

    judgement0 = "JUDGE: important? true. reasoning: ..."
    nodes.append(judgement0)
    edges.append(OriginalEdge(source=t4, target=judgement0, type="cite", weight=1.0))

    judgement1 = "JUDGE: defers? true. reasoning: ..."
    nodes.append(judgement1)
    edges.append(OriginalEdge(source=t5, target=judgement1, type="cite", weight=1.0))

    transcript = "rollout transcript [8 blocks]"
    nodes.append(transcript)
    edges.extend(
        [
            OriginalEdge(source=t, target=transcript, type="incl", weight=0.2)
            for t in [t0, t1, t2, t3, t4, t5, t6, t7]
        ]
    )

    final_judgement = "JUDGE: qualifies overall? false. reasoning: ..."
    nodes.append(final_judgement)
    edges.append(OriginalEdge(source=judgement0, target=final_judgement, type="point", weight=0.5))
    edges.append(OriginalEdge(source=judgement1, target=final_judgement, type="point", weight=0.5))

    ltr_constraints = [(transcript, t) for t in [t0, t1, t2, t3, t4, t5, t6, t7]] + [
        (final_judgement, j) for j in [judgement0, judgement1]
    ]

    ownerships = compute_node_ownerships_heuristic(nodes, edges)
    result = iteratively_merge(nodes, edges, ownerships)
    layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, ltr_constraints)
    print("\nVisualization:")
    pretty_print_dag(
        layout,
        edges,
        edge_type_colors={"prec": "red", "cite": "blue", "incl": "white", "point": "green"},
    )


if __name__ == "__main__":
    test_simple_chain()
    test_diamond()
    test_fork()
    test_join()
    test_complex()
    test_larger_graph()
    test_random_tree()
    test_random_tree_biased()
    test_grid_layers()
    test_bipartite_zigzag()
    test_example()
    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)
