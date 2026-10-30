# Monitoring UI — design note

Standing design note for the workflow **dashboard**: the top-level UI for
monitoring and manipulating grid state, built on the fxtr workstation's
view layer (`projects/fxtr_workstation/frontend_lib`). This describes the
contract the UI is being built toward; terms used without definition
(grids, cells, generations, currency, grounding, targets, sourcing) are
defined in `view_spec.py`, `notes/execution_semantics.md`, and
`notes/grounding.md`. How live state reaches the UI — the watch
subscriptions, their event schemas, and the delivery contract — is
`notes/event_system.md`; this note consumes that surface.

## Architecture: the renderer drives the capability directly

The dashboard is a workstation **FullPanel renderer keyed on the user
capability's service type id**, driving a resolved
`ServiceRef[UserCapabilityProto]` through the workstation invoke layer.
There is deliberately no UI-specific gateway service in between: the
capability remains the complete user surface (anything the dashboard can
do, any client can do through the same protocol), and the pieces the UI
needs all cross the existing boundary:

- **Typed args/results.** fxtr2's typed service dispatch decodes wire-form
  arguments into the protocol's dataclass parameters impl-side and encodes
  dataclass returns symmetrically, so the renderer sends the plain JSON
  wire form of a `ViewSel` (token + sorted `[position, value]` pairs) and
  receives `CellReport` / `KeyInfo` / `TargetInfo` as plain JSON objects.
  Ids and refs inside args/results cross as the workstation's ref stubs.
- **Reads are watches; grants ride their payloads.** The panel's read
  path is the path-addressed watch surface (`notes/event_system.md`):
  it holds no attach cache and no token-heal logic. Mutations still
  speak `ViewSel`, built from the grants the `watch_cells` /
  `watch_cell` payloads carry (the watched grid's own grant + its
  parent's token, minted through the same walk logic as attachment —
  internals read-only-forced). Tokens are ephemeral per capability
  mount, and a mount death breaks every subscription, so a resubscribe
  always delivers fresh tokens — nothing token-shaped is treated as
  durable. (`root()` / `attach` / `attach_internals` remain the
  programmatic attachment surface; `attach_internals` accepts open
  positions — the grid-level internals window — with fully bound
  selections as the per-cell browse.)
- **Records round-trip.** Introspection results that are *records*
  (`TargetRecord` inside `TargetInfo`) are passed back verbatim into the
  record-taking mutations (`demand_target`); the wire forms are symmetric,
  and an early smoke test pins the browser-side round trip.
- **Boundary discipline holds.** Every new wire method below that returns
  ids re-attaches them (`WorkflowSession.ensure_loadable`), so entity
  loads from the browser (cell values, results, configs) resolve through
  the workstation's tracked refs back to the workflow's entity store.

Long-running mutations (a demand that executes tasks) are ordinary invoke
slots: the button fires the invoke, the slot stays pending, and the table
updates meanwhile via the live watch mirrors — the UI never blocks on the
result to show progress.

## UI structure

Filesystem metaphor: a folding **tree** on the left, a **detail view** on
the right, and a **cell drawer** inside the detail view. Everything is one
FullPanel renderer with internal navigation (breaking it into independently
movable sub-panels is out of scope); every entity and bound service the
workflow references renders as a proper `RefLink`, openable in its own
panel via the standard shell affordances.

### The tree (left)

Nodes are **key-paths**, i.e. grids — not cells. Regularity (one config
per subtree key) makes the namespace a clean tree: a subtree key's
children are its child keys. A subtree grid is both a directory *and*
cell-bearing (its own cells — the prefix coordinate tuples — show in its
detail view when selected). Per node, at a glance:

- name (key), kind glyph, own axis names;
- populated-cell count; **running** count; **errored/interrupted** counts
  (shown only when nonzero); stale count;
- grounding tint (grounded / unanchored / orphaned) and a target dot when
  targets exist on the grid.

Task internals are per-*cell* (each task cell owns a key-space), so they
do not hang off the tree by default: the drill-down lives in the task
cell's drawer ("browse internals" via `attach_internals`), with an option
to pin one cell's internals subtree into the tree while exploring it.

A workflow-level header above the tree carries the **lint badge**
(sound / clean / findings count) plus entry points to the
workflow-level lists: targets, sourcing, bindings, and the lint report
itself. The tree and all of these ride the one `watch_workflow` mirror
(the same shapes `summarize` / `list_targets` / `list_sourcing` /
`list_bindings` / `lint` serve one-shot).

### Grid detail (right)

- **Header**: absolute path, kind, axes; the stored canonical config,
  rendered by dynamic dispatch on the config *entity* (task grids: rule
  wiring and declared outputs; dataframe grids: schema); grounding verdict
  + reason; sourcing status; targets on this grid; actions (source/revoke
  sourcing, delete grid — with the `DeleteReport` consequences surfaced).
- **Cells table**: one row per nonempty cell (the `watch_cells` mirror —
  the same rows `list_cells` serves), columns = coordinates + status
  badges (`ExecStatus`, currency, grounding, wanted, targeted — targeted
  joined client-side against the workflow watch's targets list) + the
  failure/verdict reason. **Value-free by doctrine**: presence-class
  rows carry no values; content is one click away. Filtering/sorting is
  client-side (server-side paging and filtering is a followup for large
  grids).
- **Cell drawer** (selected row): one `watch_cell` subscription serving
  the `CellReport` (content id WITH verdicts — the same shape
  `inspect_cell` serves) plus, for task cells, the generation report,
  reverse observers, and the live log stream. By kind:
    - *data / dataframe*: value ref rendered by dispatch, currency
      verdict + reason, grounding, sourcing record state, targeted;
      actions (source/revoke cell sourcing, target toggle, delete cell).
    - *handle*: the key ref, **bound/unbound** badge, grounding/sourcing;
      no bind/unbind actions (bindings require a live connector
      `ServiceRef`, which only programmatic clients can supply).
    - *task*: status + currency + reasons; per-output result refs (each a
      `RefLink`); the **generation report** (below) — observations with
      per-edge match status (the "why is this stale" view), failure
      records, checkpoint entries; **reverse observers** (who read this
      cell); internals browsing; actions (target toggle, run/retry/clear,
      clear).

### Status inventory

Every derived and declared status notion, and where it surfaces:

| notion | values | where |
| --- | --- | --- |
| execution status | pending / running / complete / errored / interrupted | tree rollups, cell rows, drawer |
| currency | fresh / stale / unsettled (+ diagnosis; well-foundedness failures surface as unsettled-cycle diagnoses) | tree stale count, cell rows, drawer |
| grounding | grounded / unanchored / orphaned (+ reason) | tree tint, grid header, cell rows |
| wanted | bool (+ reason) | cell rows, drawer |
| targeted | attached / dangling_missing / dangling_mismatched | grid header, cell rows, targets list |
| sourcedness | attached / dangling (per record) | grid header, drawer, sourcing list |
| handle boundness | bound / unbound (ephemeral) | handle cell rows + drawer, bindings list |
| structural health | sound / clean + findings | workflow header (lint) |

### Mutations

The v0 mutation surface, all existing or added below: demand/run,
retry, `clear_cells`, `delete_grid`, `delete_cell`, target add/remove,
sourcing CRUD (with `adopt` / `force` flows surfaced explicitly). Writing
data, creating grids, and binding handles stay programmatic-only.

**The demand affordance respects demand-requires-target** and never
auto-targets silently. The button is two-state: on an untargeted cell it
reads **"Target + run"** (an explicit `add_target` — prompting for `adopt`
when the grid is unsourced — then `demand_target`); on a targeted cell it
reads **"Run"**. Errored cells additionally offer **"Retry"** (demand
again; the demand policy retries an errored-fresh generation) and
**"Clear + rerun"** (`clear_cells` then demand). Stale/unsettled cells
surface "Clear + rerun" plus the diagnosis naming what to fix upstream.

## The monitoring surface on the capability

The introspection and mutation surface the dashboard rests on
(`UserCapabilityProto`, with the underlying queries on
`WorkflowSession`). All introspection here is presence-class: records
nothing, nothing downstream can depend on it, and locations in outputs
are absolute (root-frame transparency).

- **`summarize() -> WorkflowSummary`** — the one-call backing for the
  tree: every grid as a `GridSummary` (key-path text, kind, own axes,
  grounding verdict + reason, sourcing status, `config_id` — the canonical
  config entity, re-attached) with per-grid rollup counts (populated;
  running / complete / errored / interrupted for task grids; stale /
  unsettled; targeted). Currency rollups **force-evaluate** unstamped
  cells — the stamps make repeat summaries O(changes), and a cold
  namespace paying one full walk on first summary is accepted (watch-item
  for very large workflows).
- **`config_id` on `KeyInfo`** — same exposure for `list_keys`, so any
  subtree listing can dispatch-render stored configs.
- **`inspect_generation(sel) -> GenerationReport`** — the task-cell debug
  view, one session snapshot: the current generation's status, failure
  records, **observations** (subject path / coords / output, kind,
  recorded fingerprint, and a per-edge *current-match* flag — matches /
  changed / missing, the itemized form of the currency diagnosis), and
  **checkpoint entries** (coords, recorded step type id, completed) —
  which also delivers the deferred step-entry introspection affordance.
- **`list_observers(sel, output=None) -> tuple[ObserverInfo, ...]`** —
  reverse edges over the existing reverse observation index: the cells
  (grid path, coords, current status/currency) whose current generations
  observed this subject.
- **`list_bindings() -> tuple[BindingInfo, ...]`** — the live bindings
  (ephemeral runner state): per binding, the handle key (re-attached)
  joined to the handle cells currently holding it (one snapshot;
  possibly none — bindings survive cell deletion), so each binding has
  its grid-side context in the UI; plus a `bound: bool | None` field on
  `CellReport` for handle cells. Together
  with the handle grids' contents this is the workflow's complete
  external-authority surface, now auditable from the UI. To keep the
  re-attach total, `bind_handle` persists the handle entity at bind time
  (the one served-id source the state db does not record); the binding
  itself stays ephemeral.
- **`delete_cell(sel)`** — remove one populated data/handle cell's value
  (task cells refuse — `clear_cells` is their counterpart). Observers of
  the subject go stale (deletion is a settle event; the currency
  maintenance's grid-delete/write marks generalize). The cell's sourcing
  record, if any, becomes dangling (preserved — intent is never silently
  dropped). Deleting a handle cell does not release any live binding
  (bindings are runner config keyed by handle entity, not grid state).

### Watch subscriptions

Live state reaches the dashboard through the capability's **watch
methods** — `watch_workflow` (summary rows + lint + targets + sourcing
+ bindings; backs the header and tree), `watch_cells(path)` (one grid's
meta + cells table), and `watch_cell(path, coords)` (the drawer bundle,
including a running task cell's live log stream).

TODO: the plate view adds a `watch_graph` family carrying its relational
overlay (edges + axis correspondences) — design `notes/plate_view.md`,
plan `plans/phase4_monitoring_ui/chunk8_plate_view.md`. Each is a
path-addressed, presence-class subscription delivering an initial
snapshot as the first diff and keyed patches thereafter; the panel
folds them into always-current mirrors and never refetches. The whole
mechanism — event schemas, delivery/ordering contract, the
recompute-and-diff engine over the session's post-commit hint stream
(`change_hints.py`), and the inbox-per-watch client idiom (dropping the
inbox stub *is* the unsubscribe) — is `notes/event_system.md`.

Consequences the UI relies on:

- **No refetch machinery.** Unchanged state produces no events; changed
  state arrives as keyed patches, so tables update in place without
  clearing/reloading. A `recomputing` event precedes slow passes (cold
  summarize, lint) for a working indicator; its terminal batch is the
  settle signal.
- **Tombstones, not silence**: a watched grid/cell that is deleted
  emits `subject_gone`, rendered as an explicit "no longer exists"
  state; re-creation at the same path resumes the same watch.
- **Grants arrive with the data**: mutation `ViewSel`s are built from
  the grants carried by the `watch_cells` / `watch_cell` payloads —
  the panel holds no attach cache and no token-heal on the read path.

## Renderer packaging

The dashboard renderer is **in-repo, first-class** (not part of a demo
workdir): a multi-file TSX module living with the experiment project
(`projects/fxtr_experiment/frontend/`), esbuild-bundled against the
standard curated externals (elkjs, the one real npm dep, is bundled
into the module from a pinned tarball), and exposed through the repo
nix lib (`nix/lib/experimentRenderers.nix`, `lib.fxtr.
experimentRenderers`) so any workdir preset includes its slice by
reference — the same wiring shape as the builtin renderer slice (the
slice supplies the renderer drv plus the `(type, dispatch key)`
claims; the `forType` values are content-identical stub
re-declarations from `nix/fxtr_lib_type_ids`, eagerly validated by the
dispatcher). The package joins the pnpm workspace for typechecking and
component tests.

Beyond the capability's FullPanel, the module registers link-slot
renderers (BlockLink / InlineLink / HoverPreview) for the capability
service (a labeled pill), the frame-entity renderers (`frame_v0`), and
the stored-config entity renderers (`grid_config` / `slice_template`);
the demo adds ordinary entity renderers for its own data types.

## The demo (`examples/fxtr_experiment_demo`)

A sibling of `examples/fxtr_workstation_demo`, oriented entirely around a
**persistent** experiment backend:

- **Orchestrator** (patterned on `run_demo_workstation.py`): persistence +
  `WorkstationServer` + mounts + Vite frontend. The **workflow runner
  mount** is a host-side service CM holding per-mount state via the
  `_fxtr_mount_lifespan` pattern: the `WorkflowSession` (two sqlite files
  under the demo workdir's data dir — state survives relaunches), the
  runner scope, the binding registry, the executor, and the mounted user
  capability, registered under an external name (e.g.
  `experiment-workflow`). Relaunching the process demonstrates the
  startup running→interrupted flip and record-check resume in the UI.
- **Demo driver** (`demo_helper` pattern, e.g. `experiment-demo-helper`):
  a host-mounted service with scenario methods that drive the *same
  capability programmatically* — bind host-side task fns (connectors),
  create data/task grids, write inputs, add targets, demand cells — so an
  operator can build real state from the browser and watch the dashboard
  react. Scenario set (aiming to exercise every status): a small
  data → task → downstream-task pipeline; a slow multi-step checkpointed
  task (kill/resume shows interrupted + checkpoint replay); a flaky task
  (errored + retry); an input-overwrite scenario (stale + unsettled
  propagation); a handle grid with a bindable/releasable binding.
- **Sandboxed task fns** are flag-gated, exactly as in the workstation
  demo: the sandbox spawner is exposed as an external service and an
  MCP-driven agent can spawn task-fn services and bind them; the demo
  driver itself only binds host-side fns.
- The demo workdir's preset flake includes the dashboard slice from the
  repo lib plus renderers for the demo's own entity types.

## Deferred followups

- **Task-level output channels** (sketch, optional last work item for
  the phase — `plans/phase4_monitoring_ui/chunk7_task_channels.md`):
  step-level `log()` lines already reach the drawer through the cell
  watch's log stream (the runner's runtime-only per-cell buffer —
  `notes/event_system.md`); the deferred extension gives `TaskContext`
  `log()` / `open_channel()` over fxtr2 output channels
  (`fxtr2.common.channels`), with the cell drawer rendering a running
  cell's channel reader via the existing `rd-types-channel` machinery
  and step `log()` output routing into the owning run's channel as
  sub-entries. Runtime-only like the buffer (restarted per run, never
  persisted); safety-pass and replayed runs stay silent (hits don't
  re-invoke steps; log calls are not recorded ops and must not differ
  in effect between live and replay).
- **Queue/waiting visibility**: once scheduling grows a real notion of
  "demanded but waiting for an execution slot", surface per-grid waiting
  counts in the tree and a queue view — a natural follow-up once the UI
  exists to show it.
- **Bulk value previews**: the cells table stays value-free in v0; if
  previews prove necessary, the sanctioned shape is a batched
  `inspect_cells` (values *with* verdicts, one snapshot), not values in
  presence queries.
- **Server-side paging/filtering** for `list_cells` on large grids.
- **Sub-panel decomposition** of the dashboard (independently movable
  grid/cell panels).
- **Summarize cost** on very large cold namespaces (first-summary full
  currency walk) — measure before optimizing.
