"""Improved version of dag_traversal.py designed to be more efficient.

Key properties enabling a speedup:
- The cost of merging an edge only depends on the two nodes it connects.
- We can check whether nodes are mergeable by only doing a bounded-depth walk.
- If an edge A->C is not mergeable due to another path (e.g., A->B->C), we know A->C will naturally
  become mergeable when B gets merged with either A or C. So we don't need to track blocking paths -
  just mark the edge as blocked and it will become unblocked when intermediate nodes merge.

Improvements:
- Start by walking the graph and assigning a rank to each node, where the rank is always at least
  1 more than the rank of any upstream nodes.
- Track edge priority in an indexed priority queue, and iteratively pop the edge with the lowest
  cost. Use `pqdict`.
- When we pop an edge A->B:
    - Check that it is mergeable by doing a bounded-depth search (based on rank) to see if there's
      an alternate path from A->B.
    - If blocked, mark edge.blocked = True and skip it (it stays in edges_by_source/edges_by_target
      for cost calculations, but not in the priority queue).
    - If mergeable, perform the merge.
- When merging creates new edges (by redirecting edges to/from the merged nodes):
    - If any of the source edges being merged is unblocked, the result is unblocked.
    - Only add unblocked edges to the priority queue.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pqdict

type NodeID = str
type MergeID = int
type EdgeType = Any


@dataclasses.dataclass(frozen=True)
class OriginalEdge:
    """An edge in the original DAG."""

    source: NodeID
    target: NodeID
    type: EdgeType
    weight: float


@dataclasses.dataclass(frozen=True)
class LeafNode:
    """A leaf node in the DAG."""

    node_id: NodeID
    height: int = 1


class MergedNode:
    """A merged node in the DAG."""

    merge_id: MergeID
    top: MergedOrLeafNode
    bottom: MergedOrLeafNode
    height: int
    top_to_bottom_edges: list[OriginalEdge]


type MergedOrLeafNode = MergedNode | LeafNode


def _all_nodes_in(mergeable: MergedOrLeafNode) -> set[NodeID]:
    """Get all nodes in the mergeable."""
    if isinstance(mergeable, LeafNode):
        return {mergeable.node_id}
    elif isinstance(mergeable, MergedNode):
        return _all_nodes_in(mergeable.top) | _all_nodes_in(mergeable.bottom)
    else:
        raise ValueError(f"Unknown mergeable type: {type(mergeable)}")


@dataclasses.dataclass
class MergingEdge:
    """An edge in the DAG during the merging process."""

    merge_source: MergeID
    merge_target: MergeID
    inner_pairs: list[OriginalEdge]
    blocked: bool = False  # Whether this edge is blocked by an alternate path


@dataclasses.dataclass
class MergingDagState:
    """State of a DAG during the merging process."""

    mergeables: dict[MergeID, MergedNode | LeafNode]
    edges_by_source: dict[MergeID, dict[MergeID, MergingEdge]]
    edges_by_target: dict[MergeID, dict[MergeID, MergingEdge]]
    ranks: dict[MergeID, int]  # Topological rank of each node
    edge_priority_queue: pqdict.pqdict  # Maps (source, target) to cost
    ownerships: dict[NodeID, NodeColumnOwnership]  # Column ownership by node


def _compute_effective_weight(
    edges: list[OriginalEdge],
    ownerships: dict[NodeID, NodeColumnOwnership],
) -> float:
    """Compute the effective weight of a list of edges.

    The effective weight of an edge is based on the ownership of the nodes it connects:
    - if the source node owns the space below it, we get to take the maximum of the edge weights
      of all edges that connect to it below, instead of adding.
    - if the target node owns the space above it, we get to take the maximum of the edge weights
      of all edges that connect to it above, instead of adding.
    - all unowned edges have their weight added to this.
    """
    if not edges:
        return 0.0

    # Track cost attributed to each source/target, and unattributed cost
    cost_by_source: dict[NodeID, float] = {}
    cost_by_target: dict[NodeID, float] = {}
    unattributed_cost = 0.0

    for edge in edges:
        source_owns_below = ownerships.get(
            edge.source, NodeColumnOwnership(False, False)
        ).owns_below
        target_owns_above = ownerships.get(
            edge.target, NodeColumnOwnership(False, False)
        ).owns_above

        if source_owns_below and target_owns_above:
            # Both own - attribute to whichever increases total cost the least
            current_source_cost = cost_by_source.get(edge.source, 0.0)
            current_target_cost = cost_by_target.get(edge.target, 0.0)

            new_source_cost = max(current_source_cost, edge.weight)
            new_target_cost = max(current_target_cost, edge.weight)

            # Attribute to whichever has smaller increase (or source if equal)
            if (new_source_cost - current_source_cost) <= (new_target_cost - current_target_cost):
                cost_by_source[edge.source] = new_source_cost
            else:
                cost_by_target[edge.target] = new_target_cost
        elif source_owns_below:
            # Only source owns - attribute to source
            cost_by_source[edge.source] = max(cost_by_source.get(edge.source, 0.0), edge.weight)
        elif target_owns_above:
            # Only target owns - attribute to target
            cost_by_target[edge.target] = max(cost_by_target.get(edge.target, 0.0), edge.weight)
        else:
            # Neither owns - add to unattributed
            unattributed_cost += edge.weight

    # Total is sum of max costs per source/target plus all unattributed
    return sum(cost_by_source.values()) + sum(cost_by_target.values()) + unattributed_cost


def _compute_ranks(state: MergingDagState) -> dict[MergeID, int]:
    """Compute topological ranks for all nodes.

    The rank of a node is the length of the longest path from any root node to that node.
    Root nodes (with no incoming edges) have rank 0.
    """
    # Find all nodes
    all_nodes = set(state.mergeables.keys())

    # Compute in-degree for each node
    in_degree = {node: 0 for node in all_nodes}
    for source_dict in state.edges_by_source.values():
        for target in source_dict.keys():
            in_degree[target] = in_degree.get(target, 0) + 1

    # Start with nodes that have no incoming edges (rank 0)
    queue = [node for node in all_nodes if in_degree[node] == 0]
    ranks = {node: 0 for node in queue}

    # Process nodes in topological order
    while queue:
        current = queue.pop(0)
        current_rank = ranks[current]

        # Update ranks of downstream nodes
        for target in state.edges_by_source.get(current, {}).keys():
            ranks[target] = max(ranks.get(target, 0), current_rank + 1)
            in_degree[target] -= 1
            if in_degree[target] == 0:
                queue.append(target)

    return ranks


def _find_alternate_path(
    source: MergeID, target: MergeID, state: MergingDagState
) -> set[MergeID] | None:
    """Find an alternate path from source to target, excluding the direct edge.

    Returns the set of nodes on the alternate path if found, None otherwise.
    Uses ranks to bound the search - only follows edges to nodes with ranks
    between source and target.
    """
    source_rank = state.ranks[source]
    target_rank = state.ranks[target]

    # BFS from source to target, avoiding direct edge
    visited = set()
    # Each item in queue is (current_node, set of nodes visited on this path)
    queue = [(source, {source})]
    visited.add(source)

    while queue:
        current, path_nodes = queue.pop(0)

        for next_node in state.edges_by_source.get(current, {}).keys():
            # Skip direct edge from source to target
            if current == source and next_node == target:
                continue

            # Found target via alternate path
            if next_node == target:
                return path_nodes | {target}

            # Only follow edges to unvisited nodes with appropriate rank
            if next_node not in visited:
                next_rank = state.ranks[next_node]
                if source_rank < next_rank < target_rank:
                    visited.add(next_node)
                    queue.append((next_node, path_nodes | {next_node}))

    return None


def _cost_to_merge(edge: MergingEdge, state: MergingDagState) -> float:
    """Compute the cost of merging the two nodes at the ends of the edge.

    The cost of merging two nodes is computed based on how much edges have to be extended to get
    them past the nodes on the other "side" of the merge.

    For example, if merging A and B in the following arrangement:

            │    │    │  │
        ┌───┼────┼────┼──┼───┐
        │   │    │   y│ z│   │
        │   ▼    ▼    │  │   │
        │ ┌─────────┐ │  │   │
        │ │    A    │ │  │   │
        │ └─────────┘ │  │   │
        │    x│  │ │  │  │   │
        │     │  ▼ ▼  ▼  ▼   │
        │     │ ┌─────────┐  │
        │     │ │    B    │  │
        │     │ └─┬──┬──┬─┘  │
        │     │   │  │  │    │
        └─────┼───┼──┼──┼────┘
              ▼   ▼  ▼  ▼

    Then the cost is `x.weight * B.height + (y.weight + z.weight) * A.height`.

    Args:
        edge: The edge to compute the cost of merging.
        state: The current state of the DAG.

    Returns:
        The cost of merging the two nodes at the ends of the edge.
    """
    source_id = edge.merge_source  # A (top)
    target_id = edge.merge_target  # B (bottom)

    source_node = state.mergeables[source_id]
    target_node = state.mergeables[target_id]

    # Edges leaving source (A) that go to nodes other than target (B)
    # These need to pass through B's height
    edges_from_source = state.edges_by_source.get(source_id, {})
    edges_leaving_source_elsewhere = [
        orig_edge
        for tid, e in edges_from_source.items()
        if tid != target_id
        for orig_edge in e.inner_pairs
    ]
    weight_leaving_source_elsewhere = _compute_effective_weight(
        edges_leaving_source_elsewhere, state.ownerships
    )

    # Edges entering target (B) that come from nodes other than source (A)
    # These need to pass through A's height
    edges_to_target = state.edges_by_target.get(target_id, {})
    edges_entering_target_elsewhere = [
        orig_edge
        for sid, e in edges_to_target.items()
        if sid != source_id
        for orig_edge in e.inner_pairs
    ]
    weight_entering_target_elsewhere = _compute_effective_weight(
        edges_entering_target_elsewhere, state.ownerships
    )

    cost = (
        weight_leaving_source_elsewhere * target_node.height
        + weight_entering_target_elsewhere * source_node.height
    )
    return cost


@dataclasses.dataclass
class NodeColumnOwnership:
    """Ownership of columns by nodes."""

    owns_above: bool
    owns_below: bool


def compute_node_ownerships_heuristic(
    nodes: list[NodeID], edges: list[OriginalEdge]
) -> dict[NodeID, NodeColumnOwnership]:
    """Heuristically compute node column ownerships based on degree.

    Algorithm:
    1. Compute in-degree and out-degree for each node
    2. Put (node, direction) pairs in a priority queue sorted by degree
    3. Pop the lowest degree (node, direction) and give it ownership
    4. Reduce the effective degree of counterparties in the opposite direction
    5. Continue until every edge has at least one owner

    Args:
        nodes: The nodes in the DAG.
        edges: The edges in the DAG.

    Returns:
        A dictionary mapping each node to its ownership specification.
    """
    # Track edges by source and target for quick lookup
    edges_by_source: dict[NodeID, list[OriginalEdge]] = {}
    edges_by_target: dict[NodeID, list[OriginalEdge]] = {}

    for edge in edges:
        edges_by_source.setdefault(edge.source, []).append(edge)
        edges_by_target.setdefault(edge.target, []).append(edge)

    # Track which edges are already owned
    edges_owned: set[tuple[NodeID, NodeID]] = set()

    # Track ownership decisions
    ownerships: dict[NodeID, NodeColumnOwnership] = {
        node: NodeColumnOwnership(owns_above=False, owns_below=False) for node in nodes
    }

    # Compute initial degrees
    # "Effective" degree is the number of unowned edges in that direction
    effective_in_degree: dict[NodeID, int] = {}
    effective_out_degree: dict[NodeID, int] = {}

    for node in nodes:
        effective_in_degree[node] = len(edges_by_target.get(node, []))
        effective_out_degree[node] = len(edges_by_source.get(node, []))

    # Priority queue: (degree, node, direction)
    # direction: "in" for owns_above, "out" for owns_below
    pq = pqdict.pqdict(reverse=True)

    IN = 0
    OUT = 1

    for node in nodes:
        if effective_in_degree[node] > 0:
            pq[(node, IN)] = (effective_in_degree[node], IN)
        if effective_out_degree[node] > 0:
            pq[(node, OUT)] = (effective_out_degree[node], OUT)

    # Process until all edges are owned
    while pq and len(edges_owned) < len(edges):
        # Pop the (node, direction) with largest degree
        (node, direction), degree = pq.popitem()

        # Skip if degree is now 0 (all edges already owned)
        if degree == 0:
            continue

        # Grant ownership
        if direction == IN:
            ownerships[node].owns_above = True
            print(f"Node {node} -> ABOVE")
            # This node now owns all incoming edges
            for edge in edges_by_target.get(node, []):
                edge_key = (edge.source, edge.target)
                if edge_key not in edges_owned:
                    edges_owned.add(edge_key)

                    # Reduce the effective out-degree of the source node
                    source = edge.source
                    if effective_out_degree[source] > 0:
                        effective_out_degree[source] -= 1
                        # Update priority if still in queue
                        if (source, OUT) in pq:
                            pq[(source, OUT)] = (effective_out_degree[source], OUT)
        else:  # direction == "out"
            print(f"Node {node} -> BELOW")
            ownerships[node].owns_below = True
            # This node now owns all outgoing edges
            for edge in edges_by_source.get(node, []):
                edge_key = (edge.source, edge.target)
                if edge_key not in edges_owned:
                    edges_owned.add(edge_key)

                    # Reduce the effective in-degree of the target node
                    target = edge.target
                    if effective_in_degree[target] > 0:
                        effective_in_degree[target] -= 1
                        # Update priority if still in queue
                        if (target, IN) in pq:
                            pq[(target, IN)] = (effective_in_degree[target], IN)

    return ownerships


def iteratively_merge(
    nodes: list[NodeID], edges: list[OriginalEdge], ownerships: dict[NodeID, NodeColumnOwnership]
) -> list[MergedOrLeafNode]:
    """Iteratively merge nodes in the DAG by minimum cost, until all nodes are merged.

    This optimized version uses:
    - Topological ranks to bound the search for alternate paths
    - A priority queue to efficiently select the lowest-cost edge
    - Blocking path tracking to avoid redundant checks

    Args:
        nodes: The nodes in the DAG.
        edges: The edges in the DAG.
        ownerships: The ownership of columns by nodes.

    Returns:
        The merged DAG, as a list of separate connected components, each of which has been fully
        merged.
    """
    # Counter for generating unique MergeIDs
    next_merge_id = 0

    def get_next_merge_id() -> MergeID:
        nonlocal next_merge_id
        mid = next_merge_id
        next_merge_id += 1
        return mid

    # Create LeafNodes and assign MergeIDs
    node_id_to_merge_id: dict[NodeID, MergeID] = {}
    mergeables: dict[MergeID, MergedNode | LeafNode] = {}

    for node_id in nodes:
        merge_id = get_next_merge_id()
        node_id_to_merge_id[node_id] = merge_id
        mergeables[merge_id] = LeafNode(node_id=node_id, height=1)

    print(f"Created {len(nodes)} leaf nodes")

    # Build initial edges, grouping by (source, target) pair
    edges_by_source: dict[MergeID, dict[MergeID, MergingEdge]] = {}
    edges_by_target: dict[MergeID, dict[MergeID, MergingEdge]] = {}

    for orig_edge in edges:
        source_mid = node_id_to_merge_id[orig_edge.source]
        target_mid = node_id_to_merge_id[orig_edge.target]

        # Check if we already have an edge between these merge nodes
        if source_mid in edges_by_source and target_mid in edges_by_source[source_mid]:
            # Add to existing edge
            existing = edges_by_source[source_mid][target_mid]
            existing.inner_pairs.append(orig_edge)
        else:
            # Create new MergingEdge
            merging_edge = MergingEdge(
                merge_source=source_mid,
                merge_target=target_mid,
                inner_pairs=[orig_edge],
            )
            edges_by_source.setdefault(source_mid, {})[target_mid] = merging_edge
            edges_by_target.setdefault(target_mid, {})[source_mid] = merging_edge

    print(f"Created {sum(len(d) for d in edges_by_source.values())} initial edges")

    # Initialize state
    state = MergingDagState(
        mergeables=mergeables,
        edges_by_source=edges_by_source,
        edges_by_target=edges_by_target,
        ranks={},
        edge_priority_queue=pqdict.pqdict(),
        ownerships=ownerships,
    )

    # Compute initial ranks
    state.ranks = _compute_ranks(state)

    # Initialize priority queue with all unblocked edges
    for source_dict in edges_by_source.values():
        for edge in source_dict.values():
            if not edge.blocked:
                cost = _cost_to_merge(edge, state)
                state.edge_priority_queue[(edge.merge_source, edge.merge_target)] = cost

    print(f"Initialized priority queue with {len(state.edge_priority_queue)} edges")

    # Main merging loop
    iteration = 0
    while state.edge_priority_queue:
        iteration += 1

        # Pop edge with minimum cost
        (source_id, target_id), min_cost = state.edge_priority_queue.popitem()

        # Check if edge still exists (may have been removed by another merge)
        if (
            source_id not in state.edges_by_source
            or target_id not in state.edges_by_source[source_id]
        ):
            continue

        edge = state.edges_by_source[source_id][target_id]
        source_node = state.mergeables[source_id]
        target_node = state.mergeables[target_id]

        # Check if edge is mergeable (no alternate path exists)
        alternate_path = _find_alternate_path(source_id, target_id, state)

        if alternate_path is not None:
            # Edge is blocked by an alternate path - mark it as blocked
            # It will naturally become unblocked when intermediate nodes get merged
            print(
                f"Iteration {iteration}: Edge {source_id}->{target_id} blocked by "
                f"alternate path with {len(alternate_path)} nodes"
            )
            edge.blocked = True
            continue

        # Edge is mergeable - perform the merge
        print(
            f"Iteration {iteration}: Merging {source_id} ({_all_nodes_in(source_node)}) and"
            f" {target_id} ({_all_nodes_in(target_node)}) (cost={min_cost:.2f},"
            f" heights={source_node.height}+{target_node.height}), edges={len(edge.inner_pairs)}"
        )

        # Create the new merged node
        new_merge_id = get_next_merge_id()
        merged_node = MergedNode()
        merged_node.merge_id = new_merge_id
        merged_node.top = source_node
        merged_node.bottom = target_node
        merged_node.height = source_node.height + target_node.height
        merged_node.top_to_bottom_edges = edge.inner_pairs

        # Remove old nodes
        del state.mergeables[source_id]
        del state.mergeables[target_id]

        # Remove the edge between source and target
        del state.edges_by_source[source_id][target_id]
        if not state.edges_by_source[source_id]:
            del state.edges_by_source[source_id]
        del state.edges_by_target[target_id][source_id]
        if not state.edges_by_target[target_id]:
            del state.edges_by_target[target_id]

        # Collect all edges that need to be redirected
        edges_to_redirect: list[MergingEdge] = []

        # Edges from source to others
        if source_id in state.edges_by_source:
            edges_to_redirect.extend(state.edges_by_source[source_id].values())
        # Edges from target to others
        if target_id in state.edges_by_source:
            edges_to_redirect.extend(state.edges_by_source[target_id].values())
        # Edges to source from others
        if source_id in state.edges_by_target:
            edges_to_redirect.extend(state.edges_by_target[source_id].values())
        # Edges to target from others
        if target_id in state.edges_by_target:
            edges_to_redirect.extend(state.edges_by_target[target_id].values())

        # Remove all these edges from the state
        for e in edges_to_redirect:
            src, tgt = e.merge_source, e.merge_target

            # Remove from edges_by_source/target
            if src in state.edges_by_source and tgt in state.edges_by_source[src]:
                del state.edges_by_source[src][tgt]
                if not state.edges_by_source[src]:
                    del state.edges_by_source[src]
            if tgt in state.edges_by_target and src in state.edges_by_target[tgt]:
                del state.edges_by_target[tgt][src]
                if not state.edges_by_target[tgt]:
                    del state.edges_by_target[tgt]

            # Remove from priority queue if present
            if (src, tgt) in state.edge_priority_queue:
                del state.edge_priority_queue[(src, tgt)]

        # Re-add edges with updated source/target, merging edges that now have the same endpoints
        new_edges: dict[tuple[MergeID, MergeID], MergingEdge] = {}

        for e in edges_to_redirect:
            new_source = e.merge_source
            new_target = e.merge_target

            # Redirect source if it was one of the merged nodes
            if new_source == source_id or new_source == target_id:
                new_source = new_merge_id
            # Redirect target if it was one of the merged nodes
            if new_target == source_id or new_target == target_id:
                new_target = new_merge_id

            # Skip self-loops
            if new_source == new_target:
                continue

            key = (new_source, new_target)
            if key in new_edges:
                # Merge with existing edge
                existing = new_edges[key]
                if existing.blocked and not e.blocked:
                    print(
                        f"- Newly unblocked edges: {existing.inner_pairs} (unblocked by"
                        f" {e.inner_pairs})"
                    )
                if e.blocked and not existing.blocked:
                    print(
                        f"- Newly unblocked edges: {e.inner_pairs} (unblocked by"
                        f" {existing.inner_pairs})"
                    )
                existing.inner_pairs.extend(e.inner_pairs)
                # If any source edge is unblocked, the merged edge is unblocked
                if not e.blocked:
                    existing.blocked = False
            else:
                # Create new edge, preserving blocked status
                new_edges[key] = MergingEdge(
                    merge_source=new_source,
                    merge_target=new_target,
                    inner_pairs=list(e.inner_pairs),
                    blocked=e.blocked,
                )

        # Add the new edges back to the state
        for (src, tgt), e in new_edges.items():
            state.edges_by_source.setdefault(src, {})[tgt] = e
            state.edges_by_target.setdefault(tgt, {})[src] = e

        # Add the new merged node
        state.mergeables[new_merge_id] = merged_node

        # Update ranks - the new node takes the rank of the source (top) node
        state.ranks[new_merge_id] = state.ranks[source_id]
        del state.ranks[source_id]
        del state.ranks[target_id]

        # Add new edges to priority queue (only if not blocked)
        for (src, tgt), e in new_edges.items():
            if not e.blocked:
                cost = _cost_to_merge(e, state)
                state.edge_priority_queue[(src, tgt)] = cost

        print(
            f"  -> Created merged node {new_merge_id}, {len(state.edge_priority_queue)} edges in"
            " queue"
        )

    # Return all remaining mergeables (each is a fully merged connected component)
    result = list(state.mergeables.values())
    print(f"Final result: {len(result)} connected component(s)")
    return result


@dataclasses.dataclass
class NodeColumnAssignment:
    """An assignment of a column to a node in the DAG.

    Attributes:
        column: The column index of the node.
        from_row: The row index up to which the node's column is reserved.
        to_row: The row index down to which the node's column is reserved.
        reserved_above: Whether the node reserves space in its column above it, for edges it depends
          on.
        reserved_below: Whether the node reserves space in its column below it, for edges that
          depend on it.
    """

    column: int
    from_row: int
    to_row: int | None
    reserved_above: bool
    reserved_below: bool


@dataclasses.dataclass
class EdgeColumnAssignment:
    """Column locations of edges."""

    column: int


@dataclasses.dataclass
class DagGridLayout:
    """A layout of a DAG."""

    rows: list[NodeID]
    node_column_assignments: dict[NodeID, NodeColumnAssignment]
    edge_column_assignments: dict[tuple[NodeID, NodeID], EdgeColumnAssignment]


def assign_columns_to_nodes(
    root: MergedOrLeafNode,
    original_nodes: list[NodeID],
    original_edges: list[OriginalEdge],
    ownerships: dict[NodeID, NodeColumnOwnership],
    left_to_right_constraints: list[tuple[NodeID, NodeID]],
) -> DagGridLayout:
    """Assign non-overlapping columns to nodes in the DAG, based on merged traversal.

    We use the sequence corresponding to the merged DAG in order to determine row ordering.

    For the purpose of this algorithm we collapse together all edges that have the same source and
    target.

    Pseudocode:
    - Track the rightmost occupied column and a list of free columns, also track a frontier of which
      nodes currently have column reservations and what they are.
    - Subroutine to place a node:
        - First place any other nodes that need to be placed to the left of it.
        - Only then, pick the leftmost free column and assign it to the node.
    - Iterate over the nodes in order. When visiting a node:
        - If this node has not been placed:
            - If this node is the _last_ unvisited child of any upstream node, where that upstream also
              owns the column space below it, and if allowed by left-to-right constraints, place
              this node in that upstream's column, and immediately place that edge as well. Prioritize
              the farthest-away upstream node if there are multiple. End the other node's column
              reservation (but the column should still be occupied, now by this node).
        - For all incoming edges that have NOT yet been placed:
            - Assert that the upstream node owns the column space below it.
            - Place the edge that connects them, using the column assignment of the upstream node.
            - If this is the last edge from that upstream node, stop the upstream node's column
              reservation; that column should now be free.
        - If this node owns the column space below it, continue.
        - If this node does NOT own the column space below it,
            - Iterate over downstream nodes from this one, prioritizing those with the _longest_ edges
              according to the node ordering.
            - For each:
                - Make sure that downstream node is placed.
                    - As a special case, one of the downstream nodes we place may be placed in this
                      node's column (when allowed by left-to-right constraints).
                - Place the edge that connects them, using that node's column assignment.
            - Stop this node's column reservation here. If we did not place another node in this
              node's column, it should now be free.
    """
    # Build edges_by_source and edges_by_target for quick lookup
    edges_by_source: dict[NodeID, list[tuple[NodeID, OriginalEdge]]] = {}
    edges_by_target: dict[NodeID, list[tuple[NodeID, OriginalEdge]]] = {}

    for edge in original_edges:
        edges_by_source.setdefault(edge.source, []).append((edge.target, edge))
        edges_by_target.setdefault(edge.target, []).append((edge.source, edge))

    # Build row ordering by walking the merged tree
    rows: list[NodeID] = []

    def walk_tree(node: MergedOrLeafNode) -> None:
        if isinstance(node, LeafNode):
            rows.append(node.node_id)
        else:
            walk_tree(node.top)
            walk_tree(node.bottom)

    walk_tree(root)

    # Build row index lookup
    row_index: dict[NodeID, int] = {node: i for i, node in enumerate(rows)}

    # Build constraint graph: left_nodes[node] = set of nodes that must be to the left of node
    must_be_left_of: dict[NodeID, set[NodeID]] = {node: set() for node in original_nodes}
    for left, right in left_to_right_constraints:
        must_be_left_of[right].add(left)

    # State tracking
    node_column_assignments: dict[NodeID, NodeColumnAssignment] = {}
    edge_column_assignments: dict[tuple[NodeID, NodeID], EdgeColumnAssignment] = {}
    free_columns: list[int] = []
    next_column = 0

    # Track which nodes have column reservations and where
    active_reservations: dict[NodeID, int] = {}  # node -> column it reserves

    # Track placed edges
    placed_edges: set[tuple[NodeID, NodeID]] = set()

    # Track remaining outgoing edges for each node
    remaining_outgoing: dict[NodeID, set[NodeID]] = {}
    for source, targets in edges_by_source.items():
        remaining_outgoing[source] = {target for target, _ in targets}

    def can_place_in_column(node: NodeID, column: int) -> bool:
        """Check if node can be placed in column given left-to-right constraints."""
        # All nodes that must be to the left should have strictly smaller columns
        for left_node in must_be_left_of[node]:
            if left_node in node_column_assignments:
                if node_column_assignments[left_node].column >= column:
                    return False
            else:
                raise RuntimeError(f"Cannot do column check until placing nodes that must be left")
        # Also check the reverse: no node that this node must be left of can be in this column or to the left
        for other_node, constraints in must_be_left_of.items():
            if node in constraints and other_node in node_column_assignments:
                if node_column_assignments[other_node].column <= column:
                    return False
        return True

    def allocate_column(node: NodeID, current_row: int) -> int:
        """Allocate a column for a node, respecting constraints.

        This function always creates a NodeColumnAssignment for the node with
        from_row=current_row and to_row=None. The caller should update to_row
        when the node's reservation ends.

        Args:
            node: The node to allocate a column for.
            current_row: The current row index (used for from_row in the assignment).

        Returns:
            The allocated column number.
        """
        # First, place any nodes that must be to the left
        for left_node in must_be_left_of[node]:
            if left_node not in node_column_assignments:
                allocate_column(left_node, current_row)
                assert left_node in node_column_assignments

        # Find the leftmost free column that satisfies constraints
        candidate_column = None

        # Try free columns first
        free_columns.sort()
        for col in free_columns:
            if can_place_in_column(node, col):
                candidate_column = col
                free_columns.remove(col)
                break

        # If no free column works, use next_column
        if candidate_column is None:
            nonlocal next_column
            candidate_column = next_column
            next_column += 1

        # Create the reservation for this node
        node_column_assignments[node] = NodeColumnAssignment(
            column=candidate_column,
            from_row=current_row,
            to_row=None,
            reserved_above=ownerships.get(node, NodeColumnOwnership(False, False)).owns_above,
            reserved_below=ownerships.get(node, NodeColumnOwnership(False, False)).owns_below,
        )

        return candidate_column

    # Process nodes in row order
    for current_row_idx, node in enumerate(rows):
        # Check if node has not been placed yet
        if node not in node_column_assignments:

            # Allocate must-be-left-of nodes
            for left_node in must_be_left_of[node]:
                if left_node not in node_column_assignments:
                    allocate_column(left_node, current_row_idx)

            # Check if this node is the last unvisited child of any upstream node
            upstream_candidates = []
            for source, edge in edges_by_target.get(node, []):
                if source in node_column_assignments:
                    ownership = ownerships.get(source, NodeColumnOwnership(False, False))
                    if ownership.owns_below:
                        # Check if this is the last unvisited child
                        remaining = remaining_outgoing.get(source, set())
                        if node in remaining and len(remaining) == 1:
                            # This is the last one
                            upstream_candidates.append((row_index[source], source))

            # Sort by farthest away (smallest row index)
            upstream_candidates.sort()

            # Try to reuse an upstream's column
            placed_in_reused_column = False
            for _, source in upstream_candidates:
                source_col = node_column_assignments[source].column
                if can_place_in_column(node, source_col):
                    # Place node in same column
                    node_column_assignments[node] = NodeColumnAssignment(
                        column=source_col,
                        from_row=current_row_idx,
                        to_row=current_row_idx,
                        reserved_above=ownerships.get(
                            node, NodeColumnOwnership(False, False)
                        ).owns_above,
                        reserved_below=ownerships.get(
                            node, NodeColumnOwnership(False, False)
                        ).owns_below,
                    )

                    # Place the edge
                    edge_column_assignments[(source, node)] = EdgeColumnAssignment(
                        column=source_col
                    )
                    placed_edges.add((source, node))

                    # End the source's reservation
                    if source in active_reservations:
                        del active_reservations[source]

                    # Remove from remaining
                    if source in remaining_outgoing:
                        remaining_outgoing[source].discard(node)

                    placed_in_reused_column = True
                    break

            # If we didn't reuse a column, allocate a new one
            if not placed_in_reused_column:
                allocate_column(node, current_row_idx)

        # Update the from_row/to_row for this node
        assignment = node_column_assignments[node]
        node_column_assignments[node] = NodeColumnAssignment(
            column=assignment.column,
            from_row=assignment.from_row,
            to_row=current_row_idx,
            reserved_above=assignment.reserved_above,
            reserved_below=assignment.reserved_below,
        )

        # Place all incoming edges that haven't been placed
        for source, edge in edges_by_target.get(node, []):
            if (source, node) not in placed_edges:
                # Source should own space below
                assert source in node_column_assignments
                source_col = node_column_assignments[source].column

                edge_column_assignments[(source, node)] = EdgeColumnAssignment(column=source_col)
                placed_edges.add((source, node))

                # Remove from remaining
                if source in remaining_outgoing:
                    remaining_outgoing[source].discard(node)

                    # If this was the last edge, free the column
                    if len(remaining_outgoing[source]) == 0 and source in active_reservations:
                        freed_col = active_reservations[source]
                        del active_reservations[source]
                        free_columns.append(freed_col)

        # Handle outgoing edges
        ownership = ownerships.get(node, NodeColumnOwnership(False, False))

        if ownership.owns_below:
            # Mark this node as having a reservation
            active_reservations[node] = assignment.column

            # However, we can immediately place edges to targets that already have
            # reservations and own their above - this lets us potentially end our
            # reservation sooner once all children are visited.
            for target, edge in edges_by_source.get(node, []):
                if (node, target) in placed_edges:
                    continue
                # Check if target already has a reservation and owns above
                if target in node_column_assignments:
                    target_ownership = ownerships.get(target, NodeColumnOwnership(False, False))
                    if target_ownership.owns_above:
                        # Place this edge using the target's column
                        target_col = node_column_assignments[target].column
                        edge_column_assignments[(node, target)] = EdgeColumnAssignment(
                            column=target_col
                        )
                        placed_edges.add((node, target))

                        # Remove from remaining
                        if node in remaining_outgoing:
                            remaining_outgoing[node].discard(target)
        else:
            # Place all outgoing edges
            targets = edges_by_source.get(node, [])

            # Sort by distance (longest first)
            target_distances = []
            for target, edge in targets:
                if target in row_index:
                    distance = row_index[target] - current_row_idx
                    target_distances.append((distance, target, edge))
            target_distances.sort(reverse=True)

            # Place each target and its edge
            placed_in_own_column = False
            for distance, target, edge in target_distances:
                if target not in node_column_assignments:
                    # Check if we can place it in our column
                    if not placed_in_own_column:
                        for left_node in must_be_left_of[target]:
                            if left_node not in node_column_assignments:
                                allocate_column(left_node, current_row_idx)
                        if can_place_in_column(target, assignment.column):
                            # Reuse our column for this target
                            target_col = assignment.column
                            placed_in_own_column = True
                            node_column_assignments[target] = NodeColumnAssignment(
                                column=target_col,
                                from_row=row_index[target],
                                to_row=None,
                                reserved_above=ownerships.get(
                                    target, NodeColumnOwnership(False, False)
                                ).owns_above,
                                reserved_below=ownerships.get(
                                    target, NodeColumnOwnership(False, False)
                                ).owns_below,
                            )
                    if target not in node_column_assignments:
                        # Allocate a new column (this also creates the assignment)
                        allocate_column(target, row_index[target])

                # Place the edge if not already placed
                if (node, target) not in placed_edges:
                    target_col = node_column_assignments[target].column
                    edge_column_assignments[(node, target)] = EdgeColumnAssignment(
                        column=target_col
                    )
                    placed_edges.add((node, target))

            # Free our column if we didn't place anything in it
            if not placed_in_own_column:
                free_columns.append(assignment.column)

    return DagGridLayout(
        rows=rows,
        node_column_assignments=node_column_assignments,
        edge_column_assignments=edge_column_assignments,
    )


def pretty_print_dag(
    layout: DagGridLayout,
    original_edges: list[OriginalEdge],
    edge_type_colors: dict[EdgeType, str] | None = None,
) -> None:
    """Pretty-print a DAG layout using rich console.

    Shows the graph with nodes and edges on the left, and node identifiers on the right.
    Edges use horizontal and vertical lines, with diagonals for merges/splits.

    Each node gets display rows with routing rows before and after it.

    Args:
        layout: The DAG layout to print.
        original_edges: The original edges in the DAG.
        edge_type_colors: Optional map from edge type to color. If provided, edges with types
            found in this map will be drawn in the specified color. When edges overlap,
            an arbitrary color will be chosen from the overlapping edges.
    """
    from rich.console import Console
    from rich.text import Text

    # Build mapping from (source, target) to edge type (take first if multiple)
    edge_type_map: dict[tuple[NodeID, NodeID], EdgeType] = {}
    if edge_type_colors is not None:
        for edge in original_edges:
            key = (edge.source, edge.target)
            if key not in edge_type_map:
                edge_type_map[key] = edge.type

    def get_edge_color(source: NodeID, target: NodeID) -> str:
        """Get the color for an edge, or default to green."""
        if edge_type_colors is None:
            return "green"
        key = (source, target)
        edge_type = edge_type_map.get(key)
        if edge_type is not None and edge_type in edge_type_colors:
            return edge_type_colors[edge_type]
        return "green"

    def get_column_color(col: int, active_edges_in_col: list[tuple[NodeID, NodeID]]) -> str:
        """Get color for a column with potentially overlapping edges.

        When edges overlap, returns an arbitrary color (first found).
        """
        if edge_type_colors is None or not active_edges_in_col:
            return "green"
        # Pick first edge's color
        for source, target in active_edges_in_col:
            color = get_edge_color(source, target)
            if color != "green":  # Prefer non-default colors
                return color
        return "green"

    # Box-drawing character selection based on connected directions
    def get_box_char(up: bool, down: bool, left: bool, right: bool) -> str:
        """Get the appropriate box-drawing character for the given directions."""
        if up and down and left and right:
            return "┼"
        elif up and down and left:
            return "┤"
        elif up and down and right:
            return "├"
        elif up and left and right:
            return "┴"
        elif down and left and right:
            return "┬"
        elif up and down:
            return "│"
        elif left and right:
            return "─"
        elif up and right:
            return "╰"
        elif up and left:
            return "╯"
        elif down and right:
            return "╭"
        elif down and left:
            return "╮"
        elif up:
            return "│"
        elif down:
            return "│"
        elif left or right:
            return "─"
        else:
            return " "

    console = Console()

    # Find the maximum column to determine grid width
    max_col = max(assignment.column for assignment in layout.node_column_assignments.values())
    grid_width = max_col + 1

    # Build edges_by_source and edges_by_target for edge tracking
    edges_by_source: dict[NodeID, list[NodeID]] = {}
    edges_by_target: dict[NodeID, list[NodeID]] = {}
    for edge in original_edges:
        edges_by_source.setdefault(edge.source, []).append(edge.target)
        edges_by_target.setdefault(edge.target, []).append(edge.source)

    # Track active edges (edges that are currently being drawn vertically)
    # Maps column -> list of (source, target) tuples (multiple edges can share a column)
    active_edges: dict[int, list[tuple[NodeID, NodeID]]] = {}

    # Process each row
    for row_idx, node in enumerate(layout.rows):
        node_assignment = layout.node_column_assignments[node]
        node_col = node_assignment.column

        # Get incoming and outgoing edges for this node
        incoming_edges = []
        for source in edges_by_target.get(node, []):
            if (source, node) in layout.edge_column_assignments:
                edge_col = layout.edge_column_assignments[(source, node)].column
                incoming_edges.append((source, edge_col))

        outgoing_edges = []
        for target in edges_by_source.get(node, []):
            if (node, target) in layout.edge_column_assignments:
                edge_col = layout.edge_column_assignments[(node, target)].column
                outgoing_edges.append((target, edge_col))

        # --- ROW BEFORE NODE: Route incoming edges into the node's column ---
        if incoming_edges:
            # Check if any edges need routing
            needs_routing = any(edge_col != node_col for _, edge_col in incoming_edges)

            if needs_routing:
                # Single routing line that goes from edge columns to node column
                routing_line = Text()
                last_non_continuing_color = "green"  # Track color of last non-continuing edge
                for col in range(grid_width):
                    # Determine which directions have connections
                    has_up = False
                    has_down = False
                    has_left = False
                    has_right = False

                    # Track edges for color calculation
                    edges_for_color = []

                    # Check for edges in this column
                    edges_continuing_only = False
                    if col in active_edges:
                        edges_here = active_edges[col]
                        edges_for_color.extend(edges_here)
                        edges_to_node = [e for e in edges_here if e[1] == node]
                        edges_continuing = [e for e in edges_here if e[1] != node]

                        if edges_to_node:
                            # Edge from above routing to this node
                            has_up = True
                            # Determine horizontal direction
                            if col < node_col:
                                has_right = True
                            elif col > node_col:
                                has_left = True
                            else:
                                # col == node_col, edge goes straight down
                                has_down = True

                        if edges_continuing:
                            # Edge continues vertically
                            has_up = True
                            has_down = True
                            # If ONLY continuing (not participating in routing), force │
                            if not edges_to_node:
                                edges_continuing_only = True

                    # Check for horizontal routing lines to/from this position
                    if not edges_continuing_only:
                        for source, edge_col in incoming_edges:
                            min_col = min(edge_col, node_col)
                            max_col = max(edge_col, node_col)
                            if min_col <= col <= max_col:
                                # Edge routes through this column
                                edges_continuing_only = False
                                # Track this as a non-continuing edge for horizontal routing
                                last_non_continuing_color = get_edge_color(source, node)
                                # Add to edges_for_color
                                edges_for_color.append((source, node))
                                # Determine horizontal direction
                                if min_col < col < max_col:
                                    # Horizontal line passes through here
                                    has_left = True
                                    has_right = True
                                elif col == node_col and (edge_col < node_col):
                                    # Arriving at node from left
                                    has_left = True
                                    has_down = True
                                elif col == node_col and (edge_col > node_col):
                                    # Arriving at node from right
                                    has_right = True
                                    has_down = True

                    # Determine color for the current column
                    color_here = (
                        get_column_color(col, edges_for_color) if edges_for_color else "green"
                    )

                    char = (
                        "│"
                        if edges_continuing_only
                        else get_box_char(has_up, has_down, has_left, has_right)
                    )
                    routing_line.append(char, style=color_here)

                    # Add spacing - draw horizontal lines where edges pass through
                    if col < grid_width - 1:
                        space_has_horizontal = False
                        for _, edge_col in incoming_edges:
                            if edge_col != node_col:
                                min_col = min(edge_col, node_col)
                                max_col = max(edge_col, node_col)
                                if min_col <= col < max_col:
                                    space_has_horizontal = True
                                    break
                        # Use last_non_continuing_color for horizontal routing lines
                        routing_line.append(
                            "─" if space_has_horizontal else " ", style=last_non_continuing_color
                        )

                console.print(routing_line)

        # --- NODE ROW ---
        node_line = Text()

        for col in range(grid_width):
            if col == node_col:
                node_line.append("█", style="bold cyan")
            elif col in active_edges:
                # Show edge if any edge in this column doesn't end at this node
                edges_here = active_edges[col]
                continuing_edges = [e for e in edges_here if e[1] != node]
                if continuing_edges:
                    color = get_column_color(col, continuing_edges)
                    node_line.append("│", style=color)
                else:
                    node_line.append(" ")
            else:
                node_line.append(" ")

            if col < grid_width - 1:
                node_line.append(" ")

        node_line.append(f"  {node}")
        console.print(node_line)

        # Update active edges: remove edges that end here, add edges that start here
        for source, edge_col in incoming_edges:
            if edge_col in active_edges:
                # Remove this specific edge from the list
                active_edges[edge_col] = [e for e in active_edges[edge_col] if e != (source, node)]
                if not active_edges[edge_col]:
                    del active_edges[edge_col]

        for target, edge_col in outgoing_edges:
            if edge_col not in active_edges:
                active_edges[edge_col] = []
            active_edges[edge_col].append((node, target))

        # --- ROW AFTER NODE: Route outgoing edges from the node's column ---
        if outgoing_edges:
            needs_routing = any(edge_col != node_col for _, edge_col in outgoing_edges)

            if needs_routing:
                # Show routing start line
                routing_line = Text()
                last_non_continuing_color = "green"  # Track color of last non-continuing edge
                for col in range(grid_width):
                    # Determine which directions have connections
                    has_up = False
                    has_down = False
                    has_left = False
                    has_right = False

                    # Track edges for color calculation
                    edges_for_color = []

                    edges_continuing_only = False
                    if col in active_edges:
                        edges_here = active_edges[col]
                        edges_for_color.extend(edges_here)
                        edges_from_node = [e for e in edges_here if e[0] == node]
                        edges_continuing = [e for e in edges_here if e[0] != node]

                        if edges_from_node:
                            # Edge starting from node, routing to this column
                            has_down = True
                            # Determine horizontal direction - edge arrives from node_col
                            if col < node_col:
                                has_right = True  # Edge arrives from the right
                            elif col > node_col:
                                has_left = True  # Edge arrives from the left
                            else:
                                # col == node_col, edge comes from above
                                has_up = True

                        if edges_continuing:
                            # Edge continues vertically
                            has_up = True
                            has_down = True
                            # If ONLY continuing (not participating in routing), force │
                            if not edges_from_node:
                                edges_continuing_only = True

                    # Check for horizontal routing lines
                    if not edges_continuing_only:
                        for target, edge_col in outgoing_edges:
                            min_col = min(edge_col, node_col)
                            max_col = max(edge_col, node_col)
                            if min_col <= col <= max_col:
                                # Edge routes through this column
                                edges_continuing_only = False
                                # Add to edges_for_color
                                edges_for_color.append((node, target))
                                # Track this as a non-continuing edge for horizontal routing
                                last_non_continuing_color = get_edge_color(node, target)
                                # Determine horizontal direction
                                if min_col < col < max_col:
                                    # Horizontal line passes through here
                                    has_left = True
                                    has_right = True
                                elif col == node_col and (edge_col < node_col):
                                    # Departing from node to left
                                    has_up = True
                                    has_left = True
                                elif col == node_col and (edge_col > node_col):
                                    # Departing from node to right
                                    has_up = True
                                    has_right = True

                    # Determine color for the current column
                    color_here = (
                        get_column_color(col, edges_for_color) if edges_for_color else "green"
                    )

                    char = (
                        "│"
                        if edges_continuing_only
                        else get_box_char(has_up, has_down, has_left, has_right)
                    )
                    routing_line.append(char, style=color_here)

                    # Add spacing - draw horizontal lines where edges pass through
                    if col < grid_width - 1:
                        space_has_horizontal = False
                        for _, edge_col in outgoing_edges:
                            if edge_col != node_col:
                                min_col = min(edge_col, node_col)
                                max_col = max(edge_col, node_col)
                                if min_col <= col < max_col:
                                    space_has_horizontal = True
                                    break
                        # Use last_non_continuing_color for horizontal routing lines
                        routing_line.append(
                            "─" if space_has_horizontal else " ", style=last_non_continuing_color
                        )

                console.print(routing_line)

                # Show spacing after routing
                if row_idx < len(layout.rows) - 1:
                    spacing_line = Text()
                    for col in range(grid_width):
                        char = " "
                        if col in active_edges:
                            char = "│"
                            color = get_column_color(col, active_edges[col])
                        else:
                            color = "green"
                        spacing_line.append(char, style=color)
                        if col < grid_width - 1:
                            spacing_line.append(" ")
                    console.print(spacing_line)
            elif row_idx < len(layout.rows) - 1:
                # No routing, but still show spacing
                spacing_line = Text()
                for col in range(grid_width):
                    char = " "
                    if col in active_edges:
                        char = "│"
                        color = get_column_color(col, active_edges[col])
                    else:
                        color = "green"
                    spacing_line.append(char, style=color)
                    if col < grid_width - 1:
                        spacing_line.append(" ")
                console.print(spacing_line)
        elif row_idx < len(layout.rows) - 1:
            # No outgoing edges, but still show spacing
            spacing_line = Text()
            for col in range(grid_width):
                char = " "
                if col in active_edges:
                    char = "│"
                    color = get_column_color(col, active_edges[col])
                else:
                    color = "green"
                spacing_line.append(char, style=color)
                if col < grid_width - 1:
                    spacing_line.append(" ")
            console.print(spacing_line)
