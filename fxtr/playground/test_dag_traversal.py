"""Test script for DAG merging algorithm."""

from dag_traversal_v2 import LeafNode, MergedNode, OriginalEdge, iteratively_merge


def print_merged_tree(node: MergedNode | LeafNode, indent: int = 0) -> None:
    """Pretty-print the merged tree structure."""
    prefix = "  " * indent
    if isinstance(node, LeafNode):
        print(f"{prefix}Leaf({node.node_id})")
    else:
        print(f"{prefix}Merged(id={node.merge_id}, height={node.height}):")
        print(f"{prefix}  top:")
        print_merged_tree(node.top, indent + 2)
        print(f"{prefix}  bottom:")
        print_merged_tree(node.bottom, indent + 2)
        if node.top_to_bottom_edges:
            edge_strs = [f"{e.source}->{e.target}" for e in node.top_to_bottom_edges]
            print(f"{prefix}  internal_edges: {edge_strs}")


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

    result = iteratively_merge(nodes, edges)
    print("\nFinal structure:")
    for component in result:
        print_merged_tree(component)


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

    result = iteratively_merge(nodes, edges)
    print("\nFinal structure:")
    for component in result:
        print_merged_tree(component)


def test_weighted_preference():
    """Test that higher-weight edges are preferred for merging."""
    print("\n" + "=" * 60)
    print("TEST: Weighted preference - A -> B (weight 10), B -> C (weight 1)")
    print("Expected: A-B should merge first due to lower cross-edge cost")
    print("=" * 60)

    nodes = ["A", "B", "C"]
    edges = [
        OriginalEdge(source="A", target="B", type="heavy", weight=10.0),
        OriginalEdge(source="B", target="C", type="light", weight=1.0),
    ]

    result = iteratively_merge(nodes, edges)
    print("\nFinal structure:")
    for component in result:
        print_merged_tree(component)


def test_disconnected_components():
    """Test with two disconnected components."""
    print("\n" + "=" * 60)
    print("TEST: Two disconnected components: (A -> B) and (C -> D)")
    print("=" * 60)

    nodes = ["A", "B", "C", "D"]
    edges = [
        OriginalEdge(source="A", target="B", type="comp1", weight=1.0),
        OriginalEdge(source="C", target="D", type="comp2", weight=1.0),
    ]

    result = iteratively_merge(nodes, edges)
    print("\nFinal structure:")
    for component in result:
        print_merged_tree(component)


def test_bypass_edges():
    """Test with bypass edges that skip intermediate nodes.

    A -> B -> C, but also A -> C

    This tests the cost calculation where merging B-C has cost from A->C bypassing B.
    """
    print("\n" + "=" * 60)
    print("TEST: Bypass edge: A -> B -> C, and also A -> C")
    print("=" * 60)

    nodes = ["A", "B", "C"]
    edges = [
        OriginalEdge(source="A", target="B", type="direct", weight=1.0),
        OriginalEdge(source="B", target="C", type="direct", weight=1.0),
        OriginalEdge(source="A", target="C", type="bypass", weight=5.0),
    ]

    result = iteratively_merge(nodes, edges)
    print("\nFinal structure:")
    for component in result:
        print_merged_tree(component)


if __name__ == "__main__":
    test_simple_chain()
    test_diamond()
    test_weighted_preference()
    test_disconnected_components()
    test_bypass_edges()
    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)
