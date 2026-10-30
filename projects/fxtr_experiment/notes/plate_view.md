# Plate view — design note

Standing design note for the **plate view**: a second visualization of a
workflow's grids in the dashboard, drawing the grids as a *plate-notation
graph* — a node per grid, plates for replication axes, and typed edges for
the relationships between grids. It complements the tree/detail view
(`notes/monitoring_ui.md`), which is the filesystem-metaphor browser; this
is the structural map. Terms used without definition (grids, cells, axes,
flattened frames, subtrees, task internals, generations, observations,
currency, grounding, targets, sourcing) are defined in `view_spec.py`,
`model.py`, `notes/execution_semantics.md`, and `notes/grounding.md`.

The view rests on the same architecture as the rest of the dashboard: the
renderer drives the user capability directly, and live state arrives as a
watch subscription (`notes/event_system.md`).

> Status: built (`plans/phase4_monitoring_ui/chunk8_plate_view.md`),
> and nix-bundled with the rest of the renderer module — elkjs is
> bundled into the repo lib's slice
> (`nix/lib/experimentRenderers.nix`; landing trail:
> `plans/phase4_monitoring_ui/renderer_consolidation.md`).

## Why plate notation

Plate notation is the standard way to draw a large set of random variables
with replication structure and dependencies: a node is a variable, a plate
(a labeled rectangle) means "replicated over this index", and edges are
dependencies. A workflow's task grid maps onto this almost exactly — a task
cell is effectively a random variable, each grid is replicated over its
axes, and the wiring between grids is a dependency graph. The plate view
makes that structure legible at a glance, in a way the tree cannot: which
grids share an index, and how data flows between them.

## What is a plate here: axes and the flattened frame

A grid's cells are addressed by its **flattened frame** — the enclosing
subtrees' own axes (outer→inner, in path order) followed by the grid's own
axes (`view_spec.py`, `model.Param`). Every grid therefore already sits
inside a stack of replication indices, one per axis in that frame, and that
stack is exactly the key-path nesting. Two facts follow, and they are the
whole basis of the view:

- **Containment is a plate structure, and it is a tree.** A subtree grid's
  own axes are the plate over all grids nested under it; drawing the
  children inside the subtree's box, plated over its axes, is faithful to
  storage, not a stylistic choice. Because key-paths form a tree, the
  containment plates are always laminar and always drawable as clean nested
  rectangles. This is the backbone of every layout.
- **Grids in the same subtree share the enclosing plate by identity** (same
  flattened positions) — genuine shared-axis semantics, for free.

Task internals are the same shape one level down: a task cell's private
keyspace is a subtree of further grids (checkpoints included), plated over
the task's own axes. They are drawn inside the task node like any nested
subtree.

## Merging plates across the tree: grant-matching

The interesting question is when two grids in *different* subtrees should
share a plate. Axis *names* are canonical per grid but are not a
cross-grid identity (they are local vocabulary — `view_spec.py`), so
matching by name alone is a heuristic that can be wrong. Instead the view
merges by **grant**, which is provable.

A task grid's rule binds each input's target axes over the task's flattened
frame (`model.SliceTemplate` / `model.TaskRule`). Each target flattened
position gets one of three templates, and each means something different
for plates:

- **`Param(i)`** — the target's axis is vmap-bound to position `i` of the
  task's frame. This is a *provable 1:1 index identity*: the task cell at
  frame-position-`i` = `k` reads exactly the target cell at that axis = `k`.
  **→ a co-plate merge.** The correspondence is positional, so it holds
  regardless of whether the two axes share a name.
- **`KeepAxis`** — the target axis stays open; the task fn receives a view
  *across* it and iterates internally. The task's cells are not indexed by
  it. **→ not a plate merge; an edge annotation** ("reads batched over this
  axis").
- **`LiteralCoord`** — the slice pins a constant coordinate. **→ a
  projection**, not a replication.

So: **merge on `Param` bindings only.** The transitive closure of the
`Param` correspondences over all rules partitions `(grid, own-axis)` pairs
into **grant-classes** — each a provably-consistent replication index. A
class chains along the dataflow (a data grid's axis, consumed into a task's
own axis, consumed again downstream, all unify), and a task consuming two
inputs over one of its own axes unifies both inputs' axes *through* the
task. Merged grids therefore tend to be dataflow-adjacent, which also helps
the layout place them together.

Properties, by construction:

- Every merge is sound — a class is drawn confidently, never hedged.
- A class may span heterogeneous canonical names (`model` on one grid,
  `subject` on another, wired together by a binding). All names are shown
  on the plate; the disagreement is information.
- Domain cells (`notes/design_overview.md`, open questions) are orthogonal:
  a domain cell would give a plate's *extent* (its cardinality and an
  enumeration for joins), not its *identity*. Grant-matching answers
  identity; the two compose but neither implies the other.

Name equality with no grant backing draws **nothing** — such grids simply
carry the same axis name, and a reader can eyeball a possible relationship
without the view asserting a structural one.

## Drawing plates: always rectangles, split when non-laminar

**Every replication axis is a plate, and every plate is drawn as a
rectangle** — the notation stays uniform. But a grant-class's member set
can overlap other plates and the containment tree non-laminarly (a class
spanning two subtrees typically cannot be one rectangle without cutting
across an unrelated box). So a plate is allowed to be drawn as *more than
one* rectangle:

- A grant-class is rendered as the **minimum set of rectangles** such that
  each rectangle is laminar with containment and the plates already placed,
  together covering every member. When the layout places the class
  contiguously, that is one rectangle; when containment forces members
  apart, it is several.
- **All rectangles of one class share a color and a badge** (the axis
  name(s)). A hybrid is normal:
  members `A,B` in rectangle 1, member `C` (which could not join laminarly)
  in rectangle 2, both tagged identically.
- **A rail ties the split rectangles of a class**: each split class gets a
  small synthetic **rail node** — labeled and colored like the class's
  plates — that participates in the ELK layout like any other node, with a
  dotted tie edge routed from the node into the top of each of the class's
  rectangles. Because the rail and its ties are real layout objects,
  ELK keeps rails clear of each other and of the content (no overlay
  geometry to collide) — the "same index, one shared anchor" reading.

Which rectangles a class splits into is a function of the *layout*, not
decided up front: the layout runs first (containment as hard nesting, class
membership as soft grouping hints), then maximal contiguous same-class
regions are boxed. Contiguity is therefore best-effort — a class may split
into more rectangles than strictly necessary if the layout interleaves a
non-member; the result stays correct (same color/badge/rail), just busier.

**Every own axis gets its own rectangle** — for a uniform visual language
and so axis labels have a home separate from the node's grid/task info. A
*shared* axis is one plate spanning its grant-class members; a *solo* axis
(shared with no other grid) is a **singleton plate** around its one grid.
So a grid with own axes always sits inside a stack of nested plates (one
per own axis, outermost = the outermost own-axis position), and the node
itself carries only kind / key / counts — the axis names ride the plates.
A grid with no own axes is a bare node. (Ancestor-subtree axes are carried
by the enclosing subtree containers, as ever.)

## The nodes

A node is a grid. Its identity, kind, own axes, containment, and live
status counts (populated / running / complete / errored / interrupted /
stale) all come from the **workflow summary mirror** the dashboard already
holds (`user_api.GridSummary`, keyed by path — the same rows the tree
folds). The plate view builds every node, the containment tree, and the
own-axis plates from that mirror alone; it joins the graph overlay (below)
for edges and cross-tree merges. Node chrome: **fill by kind** (data /
dataframe / handle / task / checkpoint / subtree each a distinct tint), a
kind glyph, the status counts, and a **border encoding intent** — targeted
(blue) wins, then a grounding problem (orphaned red / unanchored amber),
else neutral.

Clicking a node navigates to that grid in the tree/detail view (the plate
view is a tab in the one dashboard panel; navigation is shared panel
state).

## The edges

Three relationship families, each collapsed to the grid level. The
direction convention makes the whole graph read as data flow: **grants and
reads point into a task; writes and creations point out of it.** Two colour
tiers carry the primary distinction: **black = the declared rule wiring**
(input grants), **grey = everything merely observed at runtime** (an
incidental read the grants don't cover, plus write/creation provenance).

| family | source | grid-level meaning | rendering |
| --- | --- | --- | --- |
| input grant | `grid_refs` + decoded `TaskRule.inputs` | task is wired to read/write this grid (a capability, held whether or not used) | **black** filled arrow into the task — **solid** when a matching read makes it "used", **dashed** when held-unread |
| observation (read) | `observations` kind `cell_value` | some current generation in the task read some cell/output of the subject | **grey** filled arrow, subject → task (a runtime read the grants don't account for) |
| cell write | `observations` kind `wrote_cell` | some current generation in the task wrote some cell of the target | grey thin **hollow** arrow, task → target |
| grid creation | `grid_assertions` | some current generation in the task asserted (would-create) the target grid | grey line ending in a **circle**, task → target ("this task was the creator" — deliberately not an arrowhead, so it doesn't read as an input) |

`grid_config` observations (a task observing a grid's *config/shape*, e.g.
on open/attach) are **deliberately not surfaced** as edges: they are
redundant with the creation edge (for grids opened with write authority) or
with the actual cell reads/writes, so they would only add noise.

All edges are over **current** generations only: old generations' records
cascade away at turnover (`storage.py`), and the aggregation joins
observations / grid_assertions through `task_execs` (which holds the
current generation per cell), so this falls out automatically.

Coordinate inputs (`model.CoordBinding`) have no storage target and produce
no edge. `KeepAxis` inputs annotate their edge as "batched over <axis>"
rather than merging plates (above).

**Dedup grammar** (an input grant and a read between the same pair both
point subject/input → task, so they coincide):

- grant *and* a matching read → one **black solid** arrow (the grant is
  used).
- grant with no matching read → a **black dashed** arrow (held but not
  observed).
- read with no matching grant → a **grey** arrow to the specific grid,
  while the grant's black edge still points at the granted grid. The
  canonical case is a **subtree granted as an input while a child grid
  inside it is what is actually read**: the black edge lands on the subtree,
  the grey arrow on the child — "the subtree is the capability, its contents
  are the dependency".

A grant's arg-name (its "slot") is revealed **on hover** of the edge, on a
white chip just outside the target node at the arrowhead — kept off-canvas
by default so the graph stays uncluttered. (Fixed, always-visible input
*ports* on the node are a possible later refinement.)

Edge families should be individually toggleable: the input-grant + plate +
containment layer is always legible, but a dense observation graph is where
the view can get noisy. In the common case the dependency graph mirrors the
input grants closely, so the two dedup onto shared arrows.

## The graph overlay and its watch

The dashboard subscribes to the **`watch_graph`** family (delivery
contract: `notes/event_system.md`, shared with the other watch families)
while the plate tab is visible; dropping the tab drops the subscription. The mirror carries only
the *relational overlay* — nodes, containment, axes, and status stay the
workflow mirror's job:

- **axis correspondences**: the raw `Param` links, each resolved on both
  sides to `(grid path, own-axis name)`. The renderer computes grant-classes
  (transitive closure) and laminarity itself — layout is presentation
  logic.
- **the four edge families**, grid-level aggregated with counts.

The plate badge shows an axis's name(s) but no cardinality: a true "how
many values" count is not an observable fact (the open-map principle
forbids an "all cells" query; `notes/design_overview.md`). A future
addition could carry an explicitly *approximate* **observed extent** per
`(grid, axis)` — the distinct coordinate values seen among currently-
populated cells — rendered as a non-authoritative badge; it is not part of
the overlay today.

The overlay is **entity-id-free** (all paths / names / ints), so unlike the
other watch families it re-attaches nothing (`notes/boundary_discipline.md`
— no ids on the wire, no coverage obligation). It is slow-flagged like the
summary (it runs aggregation joins) — the same engine shape as the workflow
watch (`watch_engine.py`) — with its two halves dirtied independently:

- **Grants and axis correspondences** are a function of the *grid set*
  alone: a task grid's rule is fixed at creation (`model.TaskRule` —
  changing it means a new grid), so this half is derived purely from the
  grids' immutable configs and recomputes only on `grids` hints, never on
  generation turnover.
- **The edge families** recompute on `cells` hints — observations / writes
  / assertions turn over with generations.

Because the structural half derives from immutable, content-addressed
configs, decoding a config to its rule/axes is a deterministic function of
its id and safe to memoize by id without invalidation (an optimization
shared with the summary, which decodes the same configs for its axes).

## Layout

Layout is [elkjs](https://github.com/kieler/elkjs) (`elk.layered` with
`hierarchyHandling: INCLUDE_CHILDREN`): the one mainstream JS engine that
handles compound/nested containers, ports, and orthogonal edge routing
across container boundaries — precisely plate notation. The containment
tree is handed to ELK as hard compound nesting; grant-class membership as
soft grouping hints (to encourage contiguity); ports for input grants; and
split classes as rail nodes + tie edges. Plates and node/port chrome are
drawn by the renderer as an SVG overlay on ELK's coordinates. Getting good
class contiguity out of ELK's soft hints is the main layout-tuning loop
(worst case: more rectangles tied to the rail node, still correct).

## Deliberately out (this view, v0)

- **Semantic (name-only) axis merging** as anything drawn — deferred to a
  future domain-cell axis-identity notion; grant-matching is the sound
  merge.
- **Collapsing task internals to a button.** v0 shows internals inline
  (they are nested subtree grids). The collapse mode's rule is defined —
  edges to/from an internal grid re-anchor to the task node — but is a
  later refinement once the inline view proves noisy.
- **A configurable subset / focus** of grids (start with the whole
  namespace).
- **Keyed graph patches.** The overlay ships as a whole-value replace per
  structural change (it changes rarely); keyed patches are an optimization
  if graphs grow large.
- **Advanced non-laminar layout** (biasing the layout so one rectangle
  physically spans a gap) — the rail gets the same legibility for far less
  complexity.
