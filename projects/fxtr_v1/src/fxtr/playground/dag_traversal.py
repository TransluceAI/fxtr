"""DAG merging and traversal."""

from __future__ import annotations

import dataclasses
from typing import Any

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
    top: LeafNode | MergedNode
    bottom: LeafNode | MergedNode
    height: int
    top_to_bottom_edges: list[OriginalEdge]


@dataclasses.dataclass
class MergingEdge:
    """An edge in the DAG during the merging process."""

    merge_source: MergeID
    merge_target: MergeID
    inner_pairs: list[OriginalEdge]
    total_weight: float


@dataclasses.dataclass
class MergingDagState:
    """State of a DAG during the merging process."""

    mergeables: dict[MergeID, MergedNode | LeafNode]
    edges_by_source: dict[MergeID, dict[MergeID, MergingEdge]]
    edges_by_target: dict[MergeID, dict[MergeID, MergingEdge]]
    # possibly other stuff


def _get_total_edge_weight(edges: list[OriginalEdge]) -> float:
    """Get the total weight of a list of edges.

    Currently a naive sum, but could be modified to e.g. group together the scores for edges that
    share nodes in common.
    """
    return sum(edge.weight for edge in edges)


def _is_mergeable(edge: MergingEdge, state: MergingDagState) -> bool:
    """Check if an edge can be merged without creating a cycle.

    An edge S→T is mergeable only if there's no other path from S to T in the current graph.
    If there's an alternate path (S→...→T), merging would collapse nodes that have intermediate
    nodes between them, violating the DAG structure.

    This performs a BFS from the source, excluding the direct edge, to check reachability.
    """
    source_id = edge.merge_source
    target_id = edge.merge_target

    # BFS from source to see if we can reach target without using the direct edge
    visited: set[MergeID] = set()
    queue: list[MergeID] = [source_id]
    visited.add(source_id)

    while queue:
        current = queue.pop(0)
        # Get all outgoing edges from current node
        for next_id in state.edges_by_source.get(current, {}).keys():
            # Skip the direct edge we're testing
            if current == source_id and next_id == target_id:
                continue
            if next_id == target_id:
                # Found alternate path to target
                return False
            if next_id not in visited:
                visited.add(next_id)
                queue.append(next_id)

    # No alternate path found - edge is mergeable
    return True


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
    weight_leaving_source_elsewhere = sum(
        e.total_weight for tid, e in edges_from_source.items() if tid != target_id
    )

    # Edges entering target (B) that come from nodes other than source (A)
    # These need to pass through A's height
    edges_to_target = state.edges_by_target.get(target_id, {})
    weight_entering_target_elsewhere = sum(
        e.total_weight for sid, e in edges_to_target.items() if sid != source_id
    )

    cost = (
        weight_leaving_source_elsewhere * target_node.height
        + weight_entering_target_elsewhere * source_node.height
    )
    return cost


def iteratively_merge(
    nodes: list[NodeID], edges: list[OriginalEdge]
) -> list[MergedNode | LeafNode]:
    """Iteratively merge nodes in the DAG by minimum cost, until all nodes are merged.

    This function should create merged versions of the nodes and edges, and then iteratively merge
    across the edge whose merge cost is lowest. At each step, the edges adjacent to the unmerged
    nodes must be updated to instead be edges that point to/from the merged node. Edges that are
    entirely inside a merged node can be absorbed into it instead.

    Note that it is only allowed to merge nodes that are connected by an edge.

    Args:
        nodes: The nodes in the DAG.
        edges: The edges in the DAG.

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
            existing.total_weight += orig_edge.weight
        else:
            # Create new MergingEdge
            merging_edge = MergingEdge(
                merge_source=source_mid,
                merge_target=target_mid,
                inner_pairs=[orig_edge],
                total_weight=orig_edge.weight,
            )
            edges_by_source.setdefault(source_mid, {})[target_mid] = merging_edge
            edges_by_target.setdefault(target_mid, {})[source_mid] = merging_edge

    state = MergingDagState(
        mergeables=mergeables,
        edges_by_source=edges_by_source,
        edges_by_target=edges_by_target,
    )

    print(f"Created {sum(len(d) for d in edges_by_source.values())} initial edges")

    # Helper to get all edges in the state
    def all_edges() -> list[MergingEdge]:
        result = []
        for target_dict in state.edges_by_source.values():
            result.extend(target_dict.values())
        return result

    # Iteratively merge until no edges remain
    iteration = 0
    while True:
        current_edges = all_edges()
        if not current_edges:
            print("No more edges to merge")
            break

        iteration += 1

        # Filter to only mergeable edges (no alternate path exists)
        mergeable_edges = [e for e in current_edges if _is_mergeable(e, state)]

        if not mergeable_edges:
            # All remaining edges are bypass edges - remove them and continue
            print(
                f"Iteration {iteration}: No mergeable edges, removing {len(current_edges)} bypass"
                " edge(s)"
            )
            for edge in current_edges:
                src, tgt = edge.merge_source, edge.merge_target
                del state.edges_by_source[src][tgt]
                if not state.edges_by_source[src]:
                    del state.edges_by_source[src]
                del state.edges_by_target[tgt][src]
                if not state.edges_by_target[tgt]:
                    del state.edges_by_target[tgt]
            continue

        # Find edge with minimum merge cost among mergeable edges
        min_cost = float("inf")
        best_edge: MergingEdge | None = None
        for edge in mergeable_edges:
            cost = _cost_to_merge(edge, state)
            if cost < min_cost:
                min_cost = cost
                best_edge = edge

        assert best_edge is not None

        source_id = best_edge.merge_source
        target_id = best_edge.merge_target
        source_node = state.mergeables[source_id]
        target_node = state.mergeables[target_id]

        print(
            f"Iteration {iteration}: Merging {source_id} and {target_id} "
            f"(cost={min_cost:.2f}, heights={source_node.height}+{target_node.height})"
        )

        # Create the new merged node
        new_merge_id = get_next_merge_id()
        merged_node = MergedNode()
        merged_node.merge_id = new_merge_id
        merged_node.top = source_node
        merged_node.bottom = target_node
        merged_node.height = source_node.height + target_node.height
        merged_node.top_to_bottom_edges = best_edge.inner_pairs

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
        # Edges FROM source (other than to target) -> now from new_merge_id
        # Edges FROM target -> now from new_merge_id
        # Edges TO source -> now to new_merge_id
        # Edges TO target (other than from source) -> now to new_merge_id

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
        for edge in edges_to_redirect:
            src, tgt = edge.merge_source, edge.merge_target
            if src in state.edges_by_source and tgt in state.edges_by_source[src]:
                del state.edges_by_source[src][tgt]
                if not state.edges_by_source[src]:
                    del state.edges_by_source[src]
            if tgt in state.edges_by_target and src in state.edges_by_target[tgt]:
                del state.edges_by_target[tgt][src]
                if not state.edges_by_target[tgt]:
                    del state.edges_by_target[tgt]

        # Re-add edges with updated source/target, merging edges that now have the same endpoints
        new_edges: dict[tuple[MergeID, MergeID], MergingEdge] = {}

        for edge in edges_to_redirect:
            new_source = edge.merge_source
            new_target = edge.merge_target

            # Redirect source if it was one of the merged nodes
            if new_source == source_id or new_source == target_id:
                new_source = new_merge_id
            # Redirect target if it was one of the merged nodes
            if new_target == source_id or new_target == target_id:
                new_target = new_merge_id

            # Skip self-loops (shouldn't happen in a DAG, but just in case)
            if new_source == new_target:
                continue

            key = (new_source, new_target)
            if key in new_edges:
                # Merge with existing edge
                existing = new_edges[key]
                existing.inner_pairs.extend(edge.inner_pairs)
                existing.total_weight += edge.total_weight
            else:
                # Create new edge
                new_edges[key] = MergingEdge(
                    merge_source=new_source,
                    merge_target=new_target,
                    inner_pairs=list(edge.inner_pairs),
                    total_weight=edge.total_weight,
                )

        # Add the new edges back to the state
        for (src, tgt), edge in new_edges.items():
            state.edges_by_source.setdefault(src, {})[tgt] = edge
            state.edges_by_target.setdefault(tgt, {})[src] = edge

        # Add the new merged node
        state.mergeables[new_merge_id] = merged_node

        print(f"  -> Created merged node {new_merge_id}, {len(new_edges)} edges remaining")

    # Return all remaining mergeables (each is a fully merged connected component)
    result = list(state.mergeables.values())
    print(f"Final result: {len(result)} connected component(s)")
    return result
