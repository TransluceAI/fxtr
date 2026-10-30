# Implementation Questions and Uncertainties

## assign_columns_to_nodes

### BUG FOUND: Edge column assignment
- **Issue**: Edges are being assigned to the wrong column in some cases
- **Root cause**: When placing incoming edges (line 894), we're using `source_col` but the edge should use the column of whichever node owns that space
- **Fix needed**: Check ownership - if source owns space below, use source column; if target owns space above, use target column
- **Current behavior**: All incoming edges use source column, which is incorrect

### Left-to-right constraints interpretation
- **Assumption Made**: I interpreted `left_to_right_constraints: list[tuple[NodeID, NodeID]]` as meaning the first node in each tuple must be placed to the left of (or in the same column as) the second node.
- **Uncertainty**: The exact semantics weren't specified - should same-column placement be allowed, or must the first strictly be to the left?
- **Current Implementation**: Allows same column (uses `>=` comparison).

### "Longest edges" priority
- **Assumption Made**: When the pseudocode says to prioritize downstream nodes with "longest edges", I interpreted this as the distance in rows between source and target in the final layout (i.e., `row_index[target] - row_index[source]`).
- **Uncertainty**: Could also mean by original edge weight, by number of intermediate nodes, or some other metric.
- **Current Implementation**: Uses row distance in the final layout ordering.

### Edge placement assertions
- **Assumption Made**: The pseudocode says "Assert that the upstream node owns the column space below it" when placing incoming edges. I implemented this as an actual assertion.
- **Uncertainty**: In production code, this might need better error handling rather than assertions.
- **Current Implementation**: Uses Python `assert` statement.

### Column reuse heuristic
- **Assumption Made**: When checking if a node is the "last unvisited child" of an upstream node, I track remaining outgoing edges and check if only one target remains unvisited.
- **Uncertainty**: The exact bookkeeping for "last unvisited" wasn't fully specified.
- **Current Implementation**: Maintains `remaining_outgoing` dict that gets updated as nodes are visited.

## pretty_print_dag

### Box-drawing character selection (FIXED)
- **Solution**: Implemented `get_box_char(up, down, left, right)` function that selects appropriate characters based on directional connections
- **Implementation**: Tracks edges as boolean directions (up, down, left, right) and selects the proper box-drawing character:
  - `├` for edges from left+up continuing down
  - `┤` for edges from up+down going left
  - `┼` for crossroads (all 4 directions)
  - `╭`, `╮`, `╰`, `╯` for proper corner directions
- **Key fix**: Outgoing edges use `has_down = True` (not `has_up`) since they depart downward from nodes

### Edge routing visualization
- **Simplification**: The current visualization uses a simple grid-based approach where edges are either vertical (│) or bend with diagonals and horizontal lines (─).
- **Limitation**: When multiple edges need to cross or overlap in complex ways, the visualization may not perfectly represent all edge paths. Multiple edges in the same column are now tracked but character selection could be improved.
- **Current Implementation**: Tracks multiple active edges per column, which handles overlapping edges better.

### Diagonal placement
- **Simplification**: Diagonals are only placed in the connector line between two node rows when an edge needs to bend from one column to another.
- **Limitation**: Very wide bends (spanning many columns) may be visually ambiguous about which edges connect to which nodes.
- **Current Implementation**: Shows the diagonal at the source column with horizontal lines extending toward the target.
