# fxtr_experiment frontend — local code overview

The workflow **dashboard** renderer: a workstation FullPanel (plus
link-slot pills) keyed on the `fxtr_experiment` user capability's
service type, driving `experiment-workflow` directly over the
workstation invoke layer. The design contract (architecture, UI
structure, status inventory, doctrine) is
`../notes/monitoring_ui.md`, and the watch/event mechanics are
`../notes/event_system.md`; this document is the guide to *editing
the package* — layout, conventions, and the implementation decisions
that live only here.

pnpm workspace member `@fxtr-experiment/dashboard` (repo-root
`pnpm-workspace.yaml`): typechecked by the root `tsc --build` graph,
linted by the root biome config, tested by the root vitest. Imports
are restricted to relative paths plus the curated externals (react,
jotai, jotai-effect, `@fxtr-ui/{core,state-util,react,react-dispatch,
workstation-dispatch}`, and `elkjs` — the plate view's layout engine,
the one non-`@fxtr-ui` runtime dependency) so the module can be
esbuild-bundled by the repo nix lib's renderer slice.
The module is nix-packaged by the repo lib:
`nix/lib/experimentRenderers.nix` (`lib.fxtr.experimentRenderers`)
esbuild-bundles `src/` into one dispatch module — elkjs bundled in
from a pinned npm tarball (keep its version in sync with what pnpm
resolves), the type entities from the `nix/fxtr_lib_type_ids` stub
venv — and the experiment demo's workdir preset merges the slice by
reference, so all four subtypes serve through real dynamic dispatch.

## The read path in one sentence

Every changing thing the panel shows is a **watch mirror**: the panel
subscribes to the capability's `watch_*` methods, folds the delivered
event patches into always-current plain-data mirrors, and reads those
mirrors — no invoke-slot queries, no refetch, no invalidation planning
(`../notes/event_system.md`). Mutations stay ordinary invokes.

## Layout

```
frontend/
├── package.json            # pnpm member; peer deps = the curated externals
├── tsconfig.json           # composite member of the root tsc --build graph
├── src/
│   ├── index.tsx            # dispatch entry: {experiment_workflow / frame_v0 / grid_config / slice_template: {key-uuid: Component}}
│   ├── panel.tsx            # the FullPanel: 4 watch drivers, watch specs, the Tree/Plates tab, runAction
│   ├── panel_state.ts       # the versioned ownStorage shape: watch slots (incl. graph) + action log + ui (incl. tab)
│   ├── watch_driver.ts      # the generic per-watch subscription FSM (atomEffect)
│   ├── header_view.tsx      # workflow header: lint badge + targets/sourcing/bindings/lint lists
│   ├── tree_view.tsx        # Tree tab, left pane: key-path tree, rollup badges, legend, internals reveal
│   ├── grid_view.tsx        # Tree tab, right pane: grid header (+ grid-level actions) over the cells table
│   ├── cell_drawer.tsx      # selected cell: verdicts, per-kind content, generation report, log lines
│   ├── frame_table.tsx      # frame-entity rendering: pure decode + table; drawer embed + dispatch renderers
│   ├── config_entities.tsx  # stored-config entity renderers: the grid_config / slice_template subtype maps
│   ├── plate_view.tsx       # Plates tab: the SVG plate-notation renderer (nodes/plates/edges, click-to-navigate)
│   ├── plate_layout.ts      # the elkjs compound layout for the plate view (containment nesting + routed edges)
│   ├── actions.tsx          # the mutation log: step chains, typed allocateStep, result rendering
│   ├── links.tsx            # BlockLink / InlineLink / HoverPreview (a labeled pill)
│   ├── derive/
│   │   ├── badges.ts        # shared cell-status badge glyphs/colors (tree + plate views)
│   │   ├── tree.ts          # summary rows → the grid tree (rollup aggregation, internals marking)
│   │   ├── cells.ts         # cells-table filter/sort fold; coords text = row id + targets join key
│   │   ├── actions.ts       # the mutation-affordance doctrine as a pure function
│   │   ├── folds.ts         # watch events → per-family mirrors (the fold functions + mirror types)
│   │   ├── plates.ts        # pure plate derivations: grant-classes (union-find) + edge-dedup classification
│   │   └── generation.ts    # drawer merges: step-checkpoints (report+log) + observations/requests
│   └── wire/
│       ├── types.ts         # TS mirrors of the wire dataclass forms (result + watch-event unions)
│       ├── proto.ts         # the typed ServiceProto declaration (the mutation + demand subset)
│       └── helpers.ts       # viewSel / boundByPosition / lint flags
└── tests/
    ├── frame_table.test.ts  # the pure frame-entity decode + display formatting
    ├── dialect.test.ts      # the live python↔TS dialect round-trip (see below)
    ├── _fixture_harness.ts  # spawns the Python fixture backend (READY <url> handshake)
    ├── tree.test.ts         # tree fold: aggregation, internals marking, malformed rows
    ├── cells.test.ts        # table fold: join-key form, filtering, sort keys
    ├── actions.test.ts      # the affordance doctrine per status/intent combination
    ├── folds.test.ts        # the per-family watch folds + panel-state shape check
    ├── generation.test.ts   # the drawer's step-checkpoints + observations/requests merges
    ├── plates.test.ts       # grant-class closure + the edge-dedup classification (pure)
    └── helpers.test.ts      # boundByPosition → viewSel sorted-pairs form
```

## The wire dialect layer (`src/wire/`)

The types are **hand-mirrored** from `fxtr_experiment`'s `user_api.py`
/ `runner_api.py` / `watch_api.py` and pinned by the round-trip
harness — drift on either side fails `dialect.test.ts`
deterministically. Conventions:

- Mirrors are **type aliases, not interfaces** (the `Method<>`
  constraints need implicit index signatures). Loose payloads are
  `ResultValue`, never `unknown`.
- mashumaro emits every field (`None` → `null`), so optional fields
  are `| null`, never `?:`. Python tuples arrive as arrays.
- Types describe the forms AFTER the transport's wire-escape decode:
  `FxtrID` leaves are `EntityRefStub`s, `frozendict`s plain objects.
- `ViewSel.bound` crosses as pairs sorted by position (int keys are
  not legal wire mapping keys). Never hand-build selections — use
  `viewSel()` / `boundByPosition()`.
- `TargetRecord` is **opaque and round-trips verbatim** into
  `demand_target` / `remove_target`; only the `*Info` wrappers carry
  display locations. A brand field keeps it from being fabricated.
- `LintReport.sound`/`.clean` are Python `@property`s — never on the
  wire; `lintSound()` / `lintClean()` implement them TS-side.
- **Watch events**: the three `*WatchEvent` unions (+ `GridMeta`,
  `LogEntry`) mirror `watch_api.py`, discriminated by `kind`. The
  folds narrow by `kind` and ignore unknown variants (a newer backend
  must not crash the panel).
- `proto.ts` declares **only the mutation + demand surface** the panel
  invokes (`read_cell_entity`, `demand_target`, the intent/sourcing/
  delete mutations). The read path is watches, not invokes, so the
  `watch_*` methods are deliberately absent (driven through the
  workstation inbox's `subscribe_to` idiom), as are the grant surface
  and the one-shot introspection queries (the watch payloads carry
  grants; the mirrors serve the query shapes). `user_api.py` is the
  source of truth — mirror more methods there if a future frontend
  needs them.

**The dialect harness**: `dialect.test.ts` spawns
`examples/fxtr_experiment_demo/dialect_fixture_backend.py` (canned
values of every result dataclass AND every watch-event union variant,
served through the real dialect encode + escaping over the fxtr2 http
service layer) and connects with the real `@fxtr-ui/connector-http`.
Decode covers every mirrored family; encode is tested live via echo
methods (a TS-built `ViewSel` etc. decoded by the real typed
dispatch). To extend the pin: add the canned value in the fixture
backend, then assert its decoded form in the test. No checked-in wire
fixtures exist, by design. The dispatch *binding* layer (method
names/arg positions) is deliberately outside the harness — covered by
live demo use.

## Panel state and watch conventions (`panel_state.ts`)

- **`ownStorage` is versioned**: the panel state carries
  `kind: PANEL_KIND`; `isPanelState` checks the tag AND the top-level
  shape, and `atomWithDefault` resets anything failing it. On any
  breaking shape change, bump `PANEL_KIND` and grow the structural
  check **in the same edit** — under HMR a window where the kind is
  bumped but the shape check hasn't grown persists a
  tagged-but-incomplete state, and a tag-only check would accept it
  forever.
- **`watches` holds four single-entry slots**, not keyed maps: the
  always-on `workflow`, one `cells` (the selected grid), one `cell`
  (the selected cell's drawer), and `graph` (the plate view's overlay —
  live only while the Plates tab is visible, its spec collapsing to
  null off-tab so the driver drops the subscription). Because
  navigation drops a watch immediately (no LRU in v0), at most one of
  each is ever live, so a single entry per family suffices; the
  keyed-map generalization returns with an LRU follow-up if wanted. Each `WatchEntry` carries
  its `stubKey`, its `spec`, the `inbox` + `subscribe` invoke stubs,
  the `phase`, and the folded `mirror` (all `StateValue`s, so the
  whole thing persists in `ownStorage`).
- **No grant cache, no token heal.** Mutation `ViewSel`s are built from
  the grants that ride the `watch_cells` / `watch_cell` payloads (the
  `GridMeta`'s own grant + `parent_token`). Tokens die with the
  subscription; a broken subscription resubscribes and fresh tokens
  arrive in the fresh meta — nothing token-shaped needs healing.
- Identity/join keys: a cell's `coordsText` is `JSON.stringify(coords)`
  (compact JSON — the same canonical form as the Python `encode_coords`
  and the cells mirror's row key), used as the row id and the join key
  against `TargetInfo.coords` / `SourcingInfo.coords`.

## The watch driver and folds (`watch_driver.ts`, `derive/folds.ts`)

`makeWatchDriver` is one generic `atomEffect` FSM per watch slot,
parameterized by `(entryAtom, specAtom, method, specKwargs, sameSpec,
emptyMirror, fold)`. Panel mounts four of them (workflow / cells /
cell / graph). Working rules:

- **Spec drives everything.** `specAtom` derives what the current
  navigation wants watched (null = nothing): the cells/cell specs read
  the workflow mirror's rows so a non-watchable or deleted grid
  collapses to null. A `null` spec drops the entry (removing the inbox
  stub is the unsubscribe — the reducer's `CloseCallbackOp`). A spec
  change replaces the entry (dropping the old inbox → old subscription
  tears down server-side).
- **Subscribe** allocates a fresh inbox + a
  `subscribe_to(capability, method, ...spec kwargs)` invoke on the
  inbox handle in one tick. The spec rides as `subscribe_to`'s
  **kwargs** (4th `allocateInvoke` arg), which the workstation forwards
  to the watch method as `method(subscriber, **spec)` — the watch
  methods declare `path`/`coords` keyword-only, so order never
  matters.
- **Drain + fold**: in any live phase (deliveries can race the
  subscribe ack's fill), reduce delivered events through the family's
  pure `fold` into the mirror and re-anchor `inbox.sliced(n)` in the
  same write.
- **Recovery edges reset the entry to null** (the next run
  resubscribes fresh): `connectionState === "broken"` (the crash
  backstop — resubscribing for a fresh snapshot is the reconciliation),
  a capability-stub change (a runner revive re-registers the service),
  and a spec change. A failed subscribe parks `errored` with a retry
  (the header's / pane's retry writes the entry back to null).

The **folds** (`derive/folds.ts`) are pure `(mirror, event) → mirror`
per family, with the mirror types + empty values. Contract: every
mirror starts empty (`snapshotted: false`), the first batch is the
snapshot, `recomputing` flags a slow pass until its terminal batch
settles it, `subject_gone` resets to a tombstone (so a re-creation's
snapshot re-folds from empty), and cell-watch log events are
event-native (`log_reset` restarts the run's entry list, `log_append`
extends it, capped at `MAX_LOG_ENTRIES` to mirror the runner buffer's
own bound).

## Components on mirrors

- **Header + tree** read the workflow mirror (`header_view.tsx` shows
  the lint badge + lists and a `● live` / `updating…` indicator off the
  driver phase + the mirror's `recomputing` flag; `tree_view.tsx` folds
  the summary rows via `derive/tree.ts`).
- **Grid detail** (`grid_view.tsx`) reads the cells mirror: the header
  from the `GridMeta` bundle (grid facts + the grant), the table from
  the keyed `cells` map; `subject_gone` renders an explicit "grid no
  longer exists" tombstone. Grid-level actions show only when the meta
  carries a `parent_token` (a root grid has none — nothing to address
  through).
- **The drawer** (`cell_drawer.tsx`) reads the cell mirror: the report,
  and for task cells the generation report, observers, and the **run
  log** section (streamed live). Two of its tables merge the report
  with the log via `derive/generation.ts`: **step checkpoints**
  (completed checkpoints + running/errored steps keyed by the log's
  `StepRef`) and **observations** (observations + unanswered requests
  shown `pending`). `ViewSel`s are built from the cells mirror's grant.
- **Frame rendering** (`frame_table.tsx`): a dataframe cell's value is
  the self-describing canonical frame entity, so the drawer renders it
  with **no frame-specific wire surface** — `ctx.allocateLoad` on the
  ref the report already carries (slots in `PanelState.frameLoads`,
  capped), a pure decode of the tagged schema + rows, and a table with
  display-only truncation. Dataframe cells and `FrameOutput` results
  (gated by the grant's output specs carrying `schema`) embed it under
  their ref links. `FrameFullPanel` / `FrameHoverPreview` also export
  under the `frame_v0` subtype key in `index.tsx`, registered for the
  frame type id by the repo nix lib slice — bare frame refs dispatch
  to them (frame links deliberately fall back to the generic entity
  links).

## The plate view (`plate_view.tsx`, `plate_layout.ts`, `derive/plates.ts`)

The Plates tab (design: `../notes/plate_view.md`) draws the grids as a
plate-notation graph. It is a **read-only, invoke-free** view: nodes,
containment, own axes, and status badges come from the **workflow
mirror** (the same rows the tree folds), and the *relational overlay* —
edges, input grants, `Param` axis correspondences — comes from the
**graph mirror** (`watch_graph`, live only on this tab).

- `derive/plates.ts` is pure and unit-tested. `grantClasses` takes the
  transitive closure of the correspondences (union-find over
  `(grid, axis)` pairs). `classifyRelations` applies the edge-dedup
  grammar (a grant with a matching read → `used`, else `held`; an
  unmatched read → a bare arrow; writes/creations point the other way).
  `buildPlateForest` splices **one plate container per replication axis**
  into the containment tree, laminarly: every own axis gets a plate
  (shared axes span their grant-class members, solo axes are singleton
  plates), promoted greedily outermost-frame-first, and a task's children
  are wrapped in an **internals** container. A class whose members land
  under different parents yields several boxes tied by a **rail**
  (`railsByClass`).
- `plate_layout.ts` turns that forest into an **elkjs** compound graph —
  plate/internals/containment boxes are all real ELK containers ELK sizes
  to enclose their contents — and routes the classified edges on the root
  graph with `INCLUDE_CHILDREN`. A split class becomes a **rail node**: a
  small root-level ELK node with a dotted tie edge from its EAST ports
  into a NORTH port on each of the class's boxes, so ELK places rails and
  routes ties clash-free (no post-hoc overlay geometry). The one subtlety
  it handles: elkjs reports each edge's coordinates relative to its
  endpoints' **LCA** container, so edges are offset by the LCA origin
  (not treated as absolute). Layout runs in the browser (ELK's bundled
  worker); it is *not* in the unit tests.
- `plate_view.tsx` renders the positioned layout as SVG: nodes filled by
  **kind** with an intent border (targeted / grounding), axis plates
  (colored, labeled) + internals boxes (dashed grey) + rail nodes (styled
  like their class's plates, ties in the class color), edges styled
  per family (black grants — solid used / dashed held; grey reads; grey
  hollow writes; grey circle-terminated creations), status counts via the
  shared `derive/badges.ts` (same glyphs/colors as the tree), and slot
  labels revealed on edge hover at the arrowhead. The whole canvas
  **pans/zooms** (wheel-to-zoom-toward-cursor + drag-to-pan on a transform
  group; a drag suppresses the click, so panning never selects). Clicking
  a node switches to the Tree tab focused on that grid.

## Mutations (`actions.tsx`, `derive/actions.ts`)

- One user gesture = one `ActionEntry` in the panel-bottom log: a short
  chain of `ActionStep`s (e.g. "Target + run" = `add_target` then the
  demand). The first step is allocated when the gesture fires; each log
  row advances its own chain when the current step fills
  (identity-guarded against double-advance); an errored step stops the
  chain and shows the error's gist. Steps persist in `ownStorage`, so
  chains survive navigation.
- `ActionStep` is a **discriminated union**, so `allocateStep` goes
  through the typed `allocateInvoke` overload per method — adding a
  mutation means a new union arm + switch case, no loose strings.
- **Which buttons a cell offers is `deriveCellActions`** — the
  demand-affordance doctrine as a pure function, unit-tested per
  status/intent combination. Key invariants: the no-mutations gate is
  **internals-ness, not grant mode** (task grants are read-shaped by
  design); untargeted cells never get a bare "Run"; adoption and
  destructive steps expand to an inline confirm.
- Demand channels: "Run"/"Retry" use `demand_target` with the joined
  record **verbatim**; "Target + run" has no record client-side yet, so
  its second step is `read_cell_entity` (for task cells, an output
  selection — `demandSel` picks the grant's first declared output).
- Grid-level actions (source / revoke sourcing / delete grid) address
  the PARENT subtree token from the watch meta (`parent_token`) plus
  the child key.

## Rendering conventions

- Styling is plain inline React styles over the workstation theme's
  CSS custom properties (`var(--fxtr-…, fallback)`); no stylesheets,
  no external UI deps. Status/currency/grounding color scales are kept
  consistent between `grid_view.tsx` and `cell_drawer.tsx`.
- Every entity/service the workflow references renders as a `RefLink`
  (openable via the standard shell affordances); the stored config
  renders as an inline ref until a config renderer exists.
- Presence stays value-free (the table); content arrives only through
  the drawer's report, whose verdicts always accompany the value.
  Verdict-line reasons clamp to their first line — errored reasons
  embed whole tracebacks, whose home is the failures section.

## Dev loop and verification

- **The only mount is the dynamic dispatch path** (the repo nix lib
  slice in the demo workdir preset) — there is no static dev mount.
  The dev loop is `examples/fxtr_experiment_demo/reload_renderers.py`
  (its module docstring is the full contract): edit this package →
  `uv run python reload_renderers.py --workdir <the running demo's
  workdir>` → the script re-pins the workdir's fxtr input to the
  current jj `@` (uncommitted edits included), re-pushes the preset,
  and the open panels reload in place with their `ownStorage` state
  intact — no browser refresh. One iteration is ~45–50s (dominated by
  the workdir-side venv/lock rebuilds, not the esbuild slice; `mkVenv`
  source narrowing is the known deferred improvement).
- Run the demo from `examples/fxtr_experiment_demo`:
  `uv run python run_experiment_demo.py --workdir
  ~/.cache/fxtr_experiment_demo_workdir` → http://localhost:3471; the
  driver panel builds workflow state; open `experiment-workflow` from
  the Roots panel for the dashboard.
- Checks: `pnpm exec tsc --build projects/fxtr_experiment/frontend
  examples/fxtr_experiment_demo/frontend`, `pnpm exec biome check`,
  `pnpm exec vitest run projects/fxtr_experiment/frontend` (all from
  the repo root; the dialect test spawns Python from the demo's venv).
- **Live verification is a human-operated browser pass** against the
  demo (open the dashboard, drive scenarios, watch updates land with
  no refetch/flash, delete a grid for the tombstone, crash/revive for
  the resubscribe heal, open a running task cell for the streaming log
  lines). This is the preferred check for the perceptual behavior the
  automated tests can't assert.
