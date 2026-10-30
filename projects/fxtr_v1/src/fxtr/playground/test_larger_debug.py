"""Debug script for larger graph visualization."""

from dag_traversal_v2 import (
    OriginalEdge,
    assign_columns_to_nodes,
    compute_node_ownerships_heuristic,
    iteratively_merge,
)

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

layout = assign_columns_to_nodes(result[0], nodes, edges, ownerships, [])

print("Row ordering:", layout.rows)
print("\nNode column assignments:")
for node in layout.rows:
    assignment = layout.node_column_assignments[node]
    print(f"  {node}: column {assignment.column}")

print("\nEdge column assignments:")
for (src, tgt), assignment in layout.edge_column_assignments.items():
    print(f"  {src}->{tgt}: column {assignment.column}")
