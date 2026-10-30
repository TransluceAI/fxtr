# Chunk 8 — the plate view

Goal: a second visualization tab in the workflow dashboard that draws the
grids as a **plate-notation graph** — a node per grid, plates for
replication axes (containment + grant-matched cross-tree merges), and typed
edges for the four grid-level relationship families. Design (the settled
contract): `notes/plate_view.md`. This plan is the implementation split;
anything design-relevant discovered during the work is promoted to the note
and `notes/discussion_log.md`.

Two halves: a small **backend overlay** (a new grid-graph introspection
query served one-shot and as a `watch_graph` family) and the **frontend
tab** (elkjs layout + the plate renderer). The backend is deliberately
minimal because nodes/axes/containment/status already live in the workflow
summary mirror — the overlay adds only edges, axis correspondences, and
optional extents.

## Background the reader needs

- The watch architecture (families, the recompute-and-diff engine over the
  hint stream, the grants-ride-payloads rule): `notes/event_system.md`,
  `watch_engine.py`, `watch_api.py`.
- The existing families are the template: `_WorkflowWatch` in
  `watch_engine.py` is almost exactly the shape `_GraphWatch` takes
  (slow-flagged, dirty on `grids`/`cells`, recompute-through-impl, diff vs
  last-sent).
- Introspection bodies live in `introspect.py` (SQL + row types), are
  composed under the session lock in `state_store.py`, surfaced on the
  capability in `user_api.py` / `user_service.py`, mirrored TS-side in
  `frontend/src/wire/` and folded in `frontend/src/derive/`.
- The rule/binding types the correspondence decode walks: `model.TaskRule`,
  `model.SliceTemplate`, `model.Param` / `KeepAxis` / `LiteralCoord`,
  `model.rule_reference_paths`. The reverse index is the `grid_refs` table
  (`storage.py`).

## Part A — the backend overlay

### A1. The graph payload types (`user_api.py`)

The overlay the wire carries. All fields are path / name / int text —
**entity-id-free**, so nothing re-attaches (call this out with a
boundary-discipline comment: no ids ⇒ no coverage obligation, unlike every
other watch family).

```python
# direction: read edges are subject→observer, write/create edges observer→target;
# the renderer applies the "reads in, writes out" convention. Backend emits raw.
@dataclass(frozen=True)
class GraphEdge:
    observer_path: str          # the task grid
    subject_path: str           # the read/written/asserted grid
    kind: Literal["read", "grid_config", "wrote_cell", "grid_assertion"]
    count: int                  # how many cell-level edges collapsed here

@dataclass(frozen=True)
class InputGrantEdge:
    task_path: str
    target_path: str
    arg_name: str               # the rule input name ('' for the task_fn slice)
    is_task_fn: bool
    mode: SliceMode             # read / write
    kept: bool                  # any KeepAxis in the slice (the "batched over" hint)

@dataclass(frozen=True)
class AxisCorrespondence:
    # one Param link, resolved to (grid, own-axis name) on both sides
    task_path: str; task_axis: str
    target_path: str; target_axis: str

@dataclass(frozen=True)
class GridGraph:
    edges: tuple[GraphEdge, ...]
    input_grants: tuple[InputGrantEdge, ...]
    correspondences: tuple[AxisCorrespondence, ...]
    # (a future `extents: tuple[AxisExtent, ...]` — observed plate
    #  cardinalities — lands with `collect_axis_extents`; omitted for now
    #  rather than shipped always-empty)
```

Design decisions baked in here:
- Nodes are **not** in the payload — the renderer builds them from the
  workflow mirror (`GridSummary`, which already carries path/kind/axes and
  all status counts, for every grid including internals + checkpoints).
- Edges collapse to grid pairs with a count (the count is display sugar —
  "some cell" is the semantic; the number hints density).
- Correspondences are raw `Param` links; the renderer computes grant-classes
  + laminarity (presentation logic).

### A2. The introspection body (`introspect.py`)

One sync function per piece, in the caller's transaction, returning
session-level rows (mapped to the `user_api` shapes by the session, as the
other bodies are). No verdicts here — purely structural.

- `collect_graph_edges(conn)` — three grouped aggregates:
  - reads / grid_config: `observations` JOIN `subject_versions` (subject
    grid) JOIN `task_execs` (observer grid, via `exec_id`), `GROUP BY
    observer grid_path, subject grid_path, kind`. The `task_execs` join
    restricts to current generations for free (turnover cascades old rows).
  - writes: same, kind `wrote_cell`.
  - creations: `grid_assertions` JOIN `subject_versions` (asserted grid)
    JOIN `task_execs`, `GROUP BY`.
- `collect_input_grants(conn)` — for each task grid, decode its stored
  `TaskRule` (the config entity; the decode helper `encoding.py` already
  uses for configs) and emit one `InputGrantEdge` per terminal slice
  (`task_fn` + each `SliceBinding` input). `grid_refs` gives the same
  referenced paths cheaply for a cross-check, but the *arg name / mode /
  kept* facts need the decoded rule, so decode is the source. Coord inputs
  (`CoordBinding`) are skipped (no target).
- `collect_correspondences(conn)` — for each task rule, for each input's
  `SliceTemplate`, walk the flattened templates; for every `Param(i)` emit a
  link resolving:
  - the owning side: position `i` of the *task's* flattened frame → the
    grid that owns that flattened position (an enclosing subtree's own axis,
    or the task's own axis) + that axis's canonical name;
  - the target side: the target grid's flattened position of this template
    slot → its owning grid + axis name.
  Both resolutions are frame walks over stored axis specs (the same frame
  arithmetic `view_spec.py` / canonicalization use — factor a helper if one
  is not already exposed).
- `collect_axis_extents(conn)` *(optional; can land in a follow-up within
  the chunk)* — per grid, parse populated cells' flattened coords, project
  to each own-axis position, count distinct. Cheap-ish over the `cells`
  table; guard behind a flag / skip if it complicates the query.

### A3. The session method (`state_store.py`)

`WorkflowSession.grid_graph() -> GridGraph`: compose the A2 bodies under the
lock into the `user_api` shapes. No `Marks:` / `Events:` (pure read, records
nothing — like `summarize`). No `ensure_loadable` (id-free payload). One
snapshot, like the other introspection.

### A4. The capability surface (`user_api.py`, `user_service.py`)

- `grid_graph() -> GridGraph` on `UserCapabilityProto` (presence-class
  one-shot, mirroring `summarize`'s docstring conventions) + the impl in
  `user_service.py` (`UserCapabilityImpl`) delegating to the session +
  a `Workflow` wrapper passthrough.
- `watch_graph(subscriber, ) -> ServiceRef[UnsubscribeHandle]` on the proto
  (no spec kwargs — it is workflow-wide like `watch_workflow`; same
  subscriber-as-sole-positional shape, delivery + teardown contracts by
  reference).

### A5. The watch event union (`watch_api.py`)

Whole-value replace (the overlay changes rarely; keyed patches deferred):

```python
@dataclass(frozen=True)
class GraphUpdate:
    graph: GridGraph
    kind: Literal["graph"] = "graph"

@dataclass(frozen=True)
class GraphBatch:                 # one recompute pass (empty = settle marker)
    patches: tuple[GraphUpdate, ...]
    kind: Literal["batch"] = "batch"

@dataclass(frozen=True)
class GraphRecomputing:
    kind: Literal["recomputing"] = "recomputing"

type GraphWatchEvent = GraphBatch | GraphRecomputing
```

### A6. The engine state (`watch_engine.py`)

`_GraphWatch` copying `_WorkflowWatch`, with **two independently-dirtied
sub-topics** (the sub-topic pattern the workflow watch already uses — and
the point of the split is that turnover must not re-decode rules):

- `structure` (input grants + correspondences + extents-structure): dirty
  on `grids` hints only — it derives from the immutable grid configs, which
  a generation turnover does not touch.
- `edges` (the four aggregated edge families + extents' counts): dirty on
  `grids` *and* `cells` hints.

`recompute` recomputes only the dirty half (caching the other half's last
result), reassembles a `GridGraph`, diffs against `self._graph`, and emits
`GraphBatch([GraphUpdate(...)])` on change else an empty batch (the wire
stays a whole-value replace; the compute is what splits). `slow` is true
when dirty. Add `WatchEngine.add_graph_watch(subscriber)` beside the other
three.

The engine recomputes through `impl.grid_graph()`; to let it recompute one
half, either give the session two composable methods
(`grid_graph_structure()` / `grid_graph_edges()`) or have `grid_graph()`
take a "which halves" argument. Prefer the two-method split — it keeps each
a clean single-snapshot query and mirrors how the summary's sub-topics call
distinct impl methods.

### A6a. Config-decode memoization (measure-first, shared)

The structural half decodes each task grid's stored `TaskRule` from its
config entity. Configs are **immutable and content-addressed**, so a decode
cache keyed by `config_id` is correct with no invalidation. This is not
graph-specific: `summarize` already decodes every grid's config for its
axes on every recompute (including `cells`-only passes where configs are
unchanged), so the cache belongs on the shared config-decode helper
(`encoding.py`) and speeds the existing workflow watch too. Land the decode
behind that helper now (so adding the cache is a one-line change), but gate
building the cache on a profile — the design overview already flags
summarize decode/currency cost as measure-before-optimize. (A further step,
not planned: the decode needs no DB lock — only the `grid → config_id` map
does — so the structural half could be computed outside the transaction;
more complexity than the current single-snapshot model warrants.)

### A7. Backend tests + CODE_OVERVIEW

- introspection unit tests: edge aggregation (all four kinds, current-only
  after turnover, the collapse count), input-grant decode (arg names, mode,
  `kept`, task_fn slice, coord inputs skipped), correspondence resolution
  (Param → resolved `(grid, axis)` on both sides, incl. a prefix-position
  binding through an enclosing subtree axis), extents if built.
- watch test: subscribe → snapshot; a grid create and a generation turnover
  each redeliver; diff-suppression on a no-op recompute; the id-free payload
  round-trips.
- Update `CODE_OVERVIEW.md` (introspect / watch surface lines) and
  `notes/monitoring_ui.md`'s watch-subscription list (add `watch_graph`).

## Part B — the frontend tab

Lands against the demo dev mount (elkjs works freely under Vite; production
nix bundling is `renderer_consolidation.md`). Package:
`projects/fxtr_experiment/frontend`.

### B1. Dependency + wire mirrors

- Add `elkjs` to the package's deps and to the curated-externals list the
  esbuild bundling allows (note in `LOCAL_CODE_OVERVIEW.md` that this is the
  first non-`@fxtr-ui` runtime dep; the nix slice must bundle/tree-shake it
  in — add that item to `renderer_consolidation.md`).
- `wire/types.ts`: TS mirrors of `GridGraph` + the `GraphWatchEvent` union.
- `wire/proto.ts`: no change (the read path is a watch, not an invoke).
- Extend the dialect harness: canned `GridGraph` + each `GraphWatchEvent`
  variant in `examples/fxtr_experiment_demo/dialect_fixture_backend.py`,
  asserted in `dialect.test.ts`.

### B2. The graph mirror + grant-class derivation

- `derive/folds.ts`: a `graph` family fold (whole-value replace on
  `GraphUpdate`; `recomputing` flag; empty-batch settle) → a
  `GraphMirror { graph, snapshotted, recomputing }`.
- `derive/plates.ts` (pure, unit-tested): `correspondences → grant-classes`
  (union-find transitive closure over `(grid, axis)` pairs), the display
  label per class (all distinct names), and the edge-dedup classification
  (grant × observation → used-port / held-only / read-without-grant). No
  layout here.

### B3. Layout (`plate_layout.ts`)

- Build the ELK compound graph from the **workflow mirror** (nodes,
  containment tree, own-axis plate metadata) + the **graph mirror** (edges,
  input-grant ports, class-grouping hints): containment = hard compound
  nesting (`hierarchyHandling: INCLUDE_CHILDREN`); input grants = ports on
  task nodes; class membership = soft grouping/ordering hints for
  contiguity.
- Run elk (async; a jotai-effect driver or a memoized async atom keyed on a
  structural hash of the two mirrors so layout re-runs only on structural
  change, not on status-count ticks).
- Post-process: for each grant-class, find maximal contiguous same-class
  regions in the laid-out coordinates → the rectangle set for that class
  (one when contiguous, several otherwise). Return positioned
  nodes/containers/edges + the per-class rectangle sets.

### B4. The renderer (`plate_view.tsx`)

SVG/React over the layout:
- **containers** (subtrees / task internals) as nested rectangles;
  **checkpoint grids** as minimal leaf nodes, no edges.
- **nodes** with chrome joined from the workflow mirror by path (kind glyph,
  status counts, grounding tint, target dot); **input-grant ports** labeled
  by arg name, read/write by style; **solo/unshared own-axis** as a corner
  plate badge on the node.
- **shared plates**: one rectangle per class region, class color + badge
  (all axis names, observed extent if present); **rails** tying a class's
  multiple rectangles (a colored through-line with branches).
- **edges** styled per the design-note table (dashed grant→port, solid
  observation, thin write, distinct creation) with the dedup grammar;
  per-family visibility toggles.
- **click a node → navigate**: set the selected grid in panel state and
  switch to the tree/detail tab (shared navigation).

### B5. Panel integration (`panel.tsx`, `panel_state.ts`)

- A tab/mode toggle in the FullPanel: `Tree` ↔ `Plates`. The workflow watch
  stays always-on (both tabs need it).
- A 4th watch driver for `watch_graph` whose spec is non-null **only when
  the Plates tab is visible** (dropping the tab drops the subscription — the
  spec-drives-everything idiom; no new lifecycle rules).
- Bump `PANEL_KIND` + grow the structural check for the new
  `watches.graph` slot + the `ui.tab` field, in the same edit
  (the versioned-ownStorage rule).

### B6. Frontend tests + overviews

- `plates.test.ts`: grant-class closure (chains, join-through-task,
  heterogeneous names), dedup classification, region splitting given a
  canned layout.
- `folds.test.ts`: the graph fold (snapshot / replace / settle).
- Update `frontend/LOCAL_CODE_OVERVIEW.md` (the new modules, the tab, the
  elkjs dep + the deferred-bundling note) and the top-level `CODE_OVERVIEW.md`
  frontend pointer if the module set changes materially.

## Verification

- Automated: the backend suites (A7), the frontend unit suites (B6), the
  dialect round-trip (B1), root `tsc`/`biome`/`vitest`.
- Live (the preferred perceptual check, as for the rest of the renderer): a
  human-operated browser pass against `examples/fxtr_experiment_demo` — the
  demo driver's pipeline scenarios should show a legible plate graph, shared
  plates across the rollout→judgment style stages, input-grant ports
  deduping onto observation arrows, internals inline, and click-to-navigate;
  turnover (rerun a cell) updates edges with no full reload.

## Steps

- [x] A1 payload types (`user_api.py`)
- [x] A2 introspection bodies (`introspect.py`)
- [x] A3 session method (`state_store.py`)
- [x] A4 capability + wrapper (`user_api.py`, `user_service.py`)
- [x] A5 event union (`watch_api.py`)
- [x] A6 engine state, structure/edges dirty split (`watch_engine.py`)
- [x] A6a config-decode behind a shared helper — satisfied by the existing
      `state_store.load_config` (shared with `summarize`); id-keyed cache
      still deferred (profile-gated)
- [x] A7 backend tests + Update CODE_OVERVIEW.md + monitoring_ui.md
- [x] B1 elkjs dep + wire mirrors + dialect pin
- [x] B2 graph fold + grant-class derivation (`derive/`)
- [x] B3 layout (`plate_layout.ts`)
- [x] B4 renderer (`plate_view.tsx`)
- [x] B5 panel tab + watch driver + ownStorage bump (`PANEL_KIND` v9)
- [x] B6 frontend tests + Update LOCAL_CODE_OVERVIEW.md
- [x] Live browser verification pass (human-driven; drove the visual
      refinements below)

## Notes / to revisit

Watch-items already known: ELK class-contiguity tuning (B3); whether the
observation graph is noisy enough to want the family toggles on by default;
observed-extent cost (A2, may defer); keyed graph patches if graphs grow
large.

Part A implementation notes (landed):
- **Config decode lives session-side** (`state_store.graph_structure`),
  *not* in the impl as `summarize` does it. Justified: the overlay is
  id-free, so there is no per-row `ensure_loadable` for the impl to do —
  the impl just maps rows to the id-free wire types. `graph_structure`
  snapshots the grid→config-id map under the lock, then loads configs
  (lock-free, immutable) and runs the pure `build_graph_structure`.
- **`_graph_structure` / `_graph_edges` are impl-private** async methods
  the watch engine calls for its split recompute; the wire `grid_graph`
  combines them. `_GraphWatch.slow` is true only when `structure` is dirty
  (the config-load pass), so frequent edge-only recomputes stay quiet.
- **`grid_graph` is two lock holds, not one atomic snapshot** (structure +
  edges). Deliberate — a slightly skewed edge (referencing a grid whose
  structure just changed) is benign for the overlay and the renderer
  tolerates it; the same reconciliation posture as the other watches.
Part B implementation notes (landed against the dev mount):
- **elkjs** is the first non-`@fxtr-ui` runtime dep; the nix renderer
  slice must bundle/tree-shake it — noted in `renderer_consolidation.md`.
  Until then the plate view mounts only through the demo dev mount (same
  as the rest of the renderer).
- **`grid_graph()` (the one-shot) has no frontend consumer** — the panel
  reads only `watch_graph`. Kept on the capability for parity/testability
  (per the A4 note); the dialect fixture pins it either way.
- **Refined during the live pass** (human-driven browser iteration): real
  ELK-container plates (one per axis, incl. singleton solo-axis plates)
  with laminar splitting + rails; the internals region as a real container;
  edge grammar reworked (black grants solid/dashed, grey reads, grey hollow
  writes, grey circle-terminated creations); `grid_config` edges dropped as
  redundant; kind-based node fill + intent border; status counts shared
  with the tree (`derive/badges.ts`); hover-revealed slot labels; and
  wheel/drag **pan-zoom**. Still open: fixed always-visible input ports and
  the observed-extent badge (both noted as future refinements); the
  nesting-order edge case for a grid with ≥2 own axes one of which is
  shared-at-a-different-position (set membership always correct).
- **Extents dropped from v0**: rather than ship an always-empty
  `GridGraph.extents` field (dead surface), the field is **removed** until
  extents are actually computed. Adding it later is additive (a new field +
  `collect_axis_extents`). The design note frames observed extent as a
  future badge, not a current payload member.

Addendum (post-chunk follow-up): **rails are now rail nodes in the ELK
layout**, not an overlay band. The original rails were post-hoc geometry —
a horizontal track above the diagram with a vertical drop to each box's
center — and with many vertically-stacked plates the drops landed on top
of each other. Now each split class gets a small synthetic ELK node
(`rail:<classKey>`, root-level, styled like the class's plates) with a
dotted class-colored tie edge from an EAST port into a NORTH port on each
of the class's plate boxes, so ELK handles placement and overlap
prevention. `Rail` / `bandHeight` are gone from `plate_layout.ts`
(`PositionedPlate` gains variant `"rail"`, `PositionedEdge` a `classKey` +
style `"rail"`); `derive/plates.ts` (`railsByClass`) is unchanged. The
design note's rail section was updated to match.
