# DAG-view Spec

This document describes the desired behavior of the fxtr DAG view.

## Description of the object model

The kind of data we are trying to visualize is essentially objects expressable in the IPLD data model. There are some top-level objects called "entities", which have a DAG-CBOR representation and a corresponding CID (content ID, a hash of their serialized canonical form). Entities can reference other entities.

Using some additional metadata in the DAG-CBOR representation, we distinguish between two types of links:
- Containment links: Entity A logically contains entity B. When loading entity A we should load entity B also. Giving access to A also gives access to B.
- Reference links: Entity A logically "refers to" entity B without including it. When loading entity B we should not load entity B. Giving access to A does not give access to B.

This allows us to express logical relationships and also express containment and grouping with separate concepts.

The object model is primarily used to contain and organize information about language model transcripts, analyses of them, and comments about them. For instance:

- There may be a top level entity `transcript` that contains a list of turns `transcript.turns[i]`.
    - Each of these turns will _refer_ to the previous turn.
- There may be another top level entity `judgements` that contains a dict of judgements.
    - Each judgement may _refer_ to a particular turn in the transcripts.
- There may be another top level entity `comments` that contains some free-form comments.
    - Comments may _refer_ to any judgement, or any transcript turn, or to the whole transcript, or to the whole set of judgements.

There is a DAG relationship, due to the constraint that all of these objects are immutable and content-addressed: in order to refer or contain an object, that other object must have been created first.

## Description of the overall view

At a high level, the DAG view is a frontend for viewing collections of DAG nodes and their relationships with each other, where those DAG nodes themselves are stored in various collections. Conceptually, it should have four panes:

1. Roots pane: A view of a set of named objects. These objects are the top level objects. They may be entities, or lists/dicts of entities.
    - You can navigate the roots pane by folding or unfolding lists, dicts, or entities. Entities show up in a sort of inspector style view, where things that are collapsed are hidden, and things that expand are shown as like pretty-printed lines.
    - Any of these lists, dicts, or entities can be "enabled" by clicking a button to the right of it.
    - Enabling one of these moves it into an "active root set" as an "active root".
        - Within a given container, active sets must be disjoint. So, if you have marked something active, it subsumes and removes anything inside it from the active root set, since now they are active at this higher level.
2. DAG minimap pane: A view of all of the active nodes and their relationships.
    - Every node in the active set, as well as all nodes contained by those nodes, appear here.
    - Overall the view looks like a GitKraken view, a 2D grid of nodes connected by non-conflicting horizontal and vertical connectors
    - Each node is on its own line, and nodes are topologically sorted so that if A -> B (or A contains B) then A comes after B.
        - The left section of the pane shows the connections between the nodes, described in more detail below.
        - The right section of the pane shows a one-line text summary of each node.
    - Nodes are logically "contained to the left": if A contains B, then A is to the left of B, and appears on a "shelf" that extends rightward. Additionally, top-level nodes that are in the active set have a "root-level shelf" that extends all the way from the left of the panel.
    - Nodes can be "collapsed", which conceptually hides their children (but does not hide the node itself)
        - You collapse or expand a node by clicking a little button that appears on the far left of the node's row.
        - If a node is reachable by some chain of containment relations, starting from an active root, then it is fully visible. It gets its own row. (Nodes are _also_ fully visible if they are selected, described below)
        - If a node is not fully visible according to the above, but it is _pointed to_ by something fully visible, then it is partially visible, and gets annotated as a small half-marker near where it is pointed to (described more below)
        - If a node is not reachable in either of these ways it simply does not appear
        - Nodes that are known to exist, but do NOT appear in the active root set, can still become visible if they are selected, or partially-visible if pointed to. Effectively, nodes that are not in the active root set are considered to be collapsed.
    - Nodes can be "selected", which makes their row highlighted blue.
        - An arbitrary set of nodes can be selected. You can select them by shift-clicking or alt-clicking following standard conventions
3. DAG selection pane: A detailed view of the selected nodes
    - All selected nodes show, in the same order as they appeared in the minimap pane.
    - Each node shows as a little card, with text or other content. This content depends on the specifics of the data type, in principle you can think of it as "a card-shaped react component".
        - The card takes up the whole row, expanding to fill the pane widthwise.
        - Edges that end at or emerge from the node attach directly to it.
        - Edges that go "through" this row have a little "ducking" effect where they truncate a bit before the node and then reappear a bit after the node, kind of like how people draw knot diagrams.
    - Most non-selected nodes are skipped. However, nodes that are pointed-to by a selected node, OR nodes that point to a selected node, get shown as a single row (similar to how they appear in the minimap). The idea is to show the "local network" of the selected nodes.
        - You can click them to add them to the selection, which also expands them.
        - (stretch feature, maybe add later) You can also select nodes that aren't even in the minimap currently due to not being in the active set. This selects them, which _also_ brings them into the minimap due to being selected. If they get
    - Clicking any node in this pane scrolls the minimap pane so that it is visible.
    - There's a little "X" button on the node that, if you click it, removes it from the selection.
4. Metadata pane: A small pane that shows details about a single node.
    - This pane shows debug information about a single node in the selection pane, e.g. raw data, ID, anything else we have.
    - Clicking on a selected node in the DAG selection pane makes that the node that is being shown in this pane.
    - This is mostly for debugging and can be collapsed if not useful.

## Details on the DAG minimap pane

- Nodes are in a 2D grid. Each point in the grid has at most one node on it.
- References bewteen nodes are shown as edges. Edges always connect nodes either through a straight upward arrow, or an elbow arrow that bends exactly once, in one of these configurations:
    - (Type A) starting going up, then going left or right to the node
    - (Type B) starting going left or right, then going up to the node
    - (possibly, but only if this makes the routing algorithm significantly easier to implement, type A+B) starting going up, then left or right, then up to the node
- Containment is shown by having a "pole" that extends upward from the node, and then has a "shelf / paddle" that sticks out to the right of the "pole", where the other node sits on top of this.
    - In very rare cases where a node is contained by multiple other nodes, the paddles all overlap.
- Only a few kinds of vertical component are allowed to overlap in the same vertical column of the grid:
    - Type A edges that all start from the same node can overlap with each other (because they all go up from the same place), and can also overlap with containment poles from that same node
    - Type B edges that all end at the same node can overlap with each other (because they all must end up at the same place)
    - Additionally, no edges or containment poles can pass through any other node that is not their source or destination.
- Effectively, this means that each node can "reserves" space in its column for some interval of rows including it but possibly extending above and below, such that
    - no two nodes have overlapping reservations in the same column
    - if A contains B, then A's reservation extends up to at least B's row, and A's reservation is to the left of B's
    - if A points to B, then either A's reservation extends up to at least B's row, or B's reservation extends down to at least A's row
- Subject to this restriction, we try to arrange nodes to take up as little width as possible.
    - This roughly means that we should reduce the number of column reservations that are active at a given time
    - Technically it is also valuable to arrange them such that they can actually be arranged together into a small number of columns, but perhaps this is not necessary and a heuristic would suffice.
    - This is essentially trying to pick a valid DAG topological sort such that the width of the frontier is small, although the frontier definition is a bit different here due to column reservation constraints
- When nodes are collapsed, they are hidden as much as possible.
    - Non-visible nodes are simply removed
    - Partially-visible nodes effectively lose their row and column reservations, but still appear "locally": a copy of it appears as a small dot in a half-row immediately above each node that points to it.
        - A small half-height "shelf" or series of shelves still extends to it from the right (e.g. to the left of the half node), corresponding to the sequence of containments that it has. These half-height shelves extend out to the pole of the outermost collapsed node that is still fully visible (e.g. the node that was actively collapsed by the user), visually showing that this node was contained by that node.
    - Collapsing nodes only affects the layout locally.
        - The rows simply disappear.
        - For efficiency, column reservations don't globally recompute. Instead, if there is any other column to the _right_ of this one that contains a set of contiguous column reservations that would fit into a collapsed node's reservation, they get pulled left.
            - So, basically, each column reservation tries to "float left" as far as it can without overlapping a previous reservation.
            - This might be a fine heuristic algorithm for the layout anyway; maybe each column reservation just gets its row span without being assigned a specific column, and they also get put in some order, and then the actual column it gets is just the first column that doesn't overlap any of the previous ones, skipping hidden reservations
    - Note that the outer collapsed node itself still keeps its own column reservation, since only its children are hidden.

## Sketch of a possible formalization of the layout algorithm

- we maintain a few sets: "pending", "reserved-before" set, a "reserved-after" set, and "done" set, each of which contains some nodes and is disjoint. reserved-before means that, at the current row, we are "above" the node, but it has a column reservation. reserved-after means that, at the current row, we are "below" the node, but it still has its column reservation. when we encounter the node we move it bewteen these.
- let `parent(node)` and `children(node)` be sets that track containment relationships, and `upstream(node)` and `downstream(node)` be sets that track refers-to relationships (where A is in downstream(B) and B is in upstream(A) if A points to B).
- roughly we want to produce an iterative sequence of events, which are something like:
    - start a node's column reservation (pending -> reserved-before)
    - emit a node (reserved-before -> reserved-after)
        - requires that every upstream node and every child be in "reserved-after" or "done"
        - requires that its parent (/ all parents) be in "reserved-before"
        - conceptually this is where the node appears in its row
    - stop a node's reserved-after reservation (reserved-after -> done)
        - requires that every downstream node be in "reserved-before", "reserved-after" or "done"
    - replace a node X with a downstream node Y
        - only possible when node Y points to exactly one node (X), and node Y has no children
        - X moves from reserved-after -> done
        - Y simultaneously moves immediately from pending -> reserved-after
        - requires that Y's parent (/ all parents) be in "reserved-before"
- we use a variant of a greedy algorithm to do this, with the goal of minimizing the maximum size of `union(reserved-before, reserved-after)`
    - possibly we will need to do some sort of lookahead to resolve the relationship between starting and ending column reservations
    - maybe a heuristic is that, if a node has only one downstream node, and that downstream node has only one upstream node, we always replace. if a node has only one downstream, but that downstream has multiple upstream, we force the downstream to start its column reservation and then immediately end this nodes reserved-after reservation. otherwise if it has multiple downstream it keeps its reserved-after until no longer needed

## Inlined nodes

Some nodes are not intended to be full nodes but instead just appear as data storage inside other nodes. We maintain a set of these types. An identifying feature for them is that they have no one-line summary, and are never pointed to.

If a node is inlined in this way, we essentially treat it as if it was part of its parent, with a copy for each parent if there are multiple. This means that its children are treated as its parent's children, things it points to are treated as things its parent points to, it does not get its own row.

We could effectively handle this as a preprocessing step by inlining these children's properties into their parents while processing them.

## Efficiently adding new nodes

It should be possible to add new nodes to the active set easily, without forcing a global re-layout. Perhaps just a greedy method that adds one node to the active set by sticking it somewhere locally that satisfies the constraints.

We can use this to track data being

## Supporting data required and prototype implementation goals

We need to track (on the frontend, for simplicity):
- The root objects (nested lists or dicts of entities, essentially)
- All the entities, which each have
    - an ID
    - a type name (or none if inlined)
    - a text summary (or none if inlined)
    - a collection of their direct children's IDs
    - a collection of their upstream node IDs
    - a raw JSON-based representation, which may include special "link" leaves that refer to children or upstream nodes

For an initial pass, we can have all of these just be frontend-sourced. Perhaps there's a textbox where you write a json-encoded version of all this data. The text box should let you extend the root objects with a list of new root objects. Maybe two buttons, where one button adds new roots without them being active (such that enabling them will re-layout the minimap), and another button that adds the roots, immediately makes them active, and splices them into the minimap without causing a global re-layout.

For the minimap and selected-node panes, we can use d3 for drawing, or emit raw SVG if that is easier.
