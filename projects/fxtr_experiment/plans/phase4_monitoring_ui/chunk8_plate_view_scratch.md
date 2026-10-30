# chunk 8 scratch — working notes

Scratch for the plate view. Settled design is `notes/plate_view.md`; the
plan is `chunk8_plate_view.md`. This is the rationale trail + loose ends.

## Alternatives weighed

- **Axis merging: name-match vs grant-match.** Name-match (same canonical
  name ⇒ same plate) is trivial but a heuristic that can be wrong (two
  unrelated `idx` axes). Grant-match (merge on `Param` vmap bindings) is
  provable, positional (name-independent), and follows the dataflow. Chose
  grant-match as the drawn merge; name equality with no grant draws nothing
  (the shared name is visible, the reader infers). Grant-match needs the
  decoded binding structure, not just `grid_refs` reference paths — hence
  `collect_correspondences` decodes the rule.
- **`KeepAxis` / `LiteralCoord`.** Kept = the task iterates across the axis
  internally, its cells are not indexed by it ⇒ *not* a co-plate merge, just
  an edge "batched over" annotation. Literal = a projection. Only `Param`
  merges. This split is load-bearing; verified against `model.SliceTemplate`
  (path cell templates + target_axes, each an `AxisTemplate`).
- **Non-laminar plates: annotation vs multiple rectangles.** First sketch
  fell back to a bare color/rail annotation when a class could not nest.
  Rejected in favor of *always rectangles* (uniform notation), a class drawn
  as ≥1 rectangle sharing color+badge, tied by a rail — a class can be a
  hybrid (A,B boxed together, C boxed separately, same tag). The rectangle
  split is computed *after* layout (maximal contiguous same-class regions),
  so it falls out of ELK's placement rather than being decided up front.
- **Advanced non-laminar layout** (bias the layout so one rectangle spans a
  gap) — considered, deferred; the rail gets the same legibility for much
  less complexity.
- **Node status in the graph payload vs joined from the workflow mirror.**
  Joined. `summarize`/`GridSummary` already enumerates every grid (internals
  + checkpoints included) with kind/axes/status counts keyed by path, so the
  overlay carries only edges + correspondences + extents — smaller payload,
  one source of truth for status, and the graph watch fires far less often
  than the status-count ticks.
- **watch_graph whole-value replace vs keyed patches.** Whole-value replace
  (the overlay changes rarely). Keyed patches only if graphs grow large.

## ELK option candidates (B3 tuning loop)

- `elk.algorithm = layered`, `hierarchyHandling = INCLUDE_CHILDREN`.
- containment ⇒ compound children (hard). Class contiguity ⇒ soft hints to
  try: `layoutPartition` / `partitioning.activate`, `nodeGroup`, position/
  layer constraints, or ordering by class. Expect iteration; worst case =
  more rectangles + rail, still correct.
- ports for input grants: fixed-side ports on task nodes; `portConstraints`.
- run async (elk is promise-based); memoize on a structural hash of
  (workflow-mirror structure + graph-mirror) so status-count ticks don't
  relayout.

## Loose ends / open

- Observed extents (A2): cheap-ish but touches coord parsing; fine to land
  as a follow-up within the chunk or skip for v0. The plate badge shows axis
  name(s) regardless.
- Heterogeneous-name plate label: show all distinct names; pick an ordering
  (task-side first? first-appearance?) — decide when rendering.
- Edge-family visibility toggle defaults: unknown until we see real density;
  start all-on, revisit.
- Collapse-internals-to-button mode: rule is "re-anchor internal edges to
  the task node"; build only if inline internals prove noisy.
