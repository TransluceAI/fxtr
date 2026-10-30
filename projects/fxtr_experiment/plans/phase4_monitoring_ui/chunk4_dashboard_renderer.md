# Chunk 4 — the dashboard renderer

**Status: complete** (2026-07-11) — stages 1–7 committed (stage 3
includes the review-feedback round, 2026-07-10; stage 4 landed as two
commits, 2026-07-11; the interleaved demo-driver scenarios landed as
`fxtr expt 4.4post1`, stage 5 as `fxtr expt 4.4: the cell drawer`,
stage 6 as `fxtr expt 4.4: mutations`, and stage 7 as `fxtr expt 4.4:
subscription-driven live updates`, 2026-07-11; the LOCAL_CODE_OVERVIEW
was pulled forward between stages 6 and 7). Stage 8 (the nix slice +
demo preset wiring) was **deferred out of the chunk at close** — split
to [renderer_consolidation.md](renderer_consolidation.md) with the gathered
implementation context, to keep the dev-mount HMR loop as the fast
iteration path for the UI-feel work that follows. Until it lands, the
demo's dev mount is the panel's only mount and the renderer has no nix
build coverage (bit-rot guards: repo tsc/biome/vitest + the dev
mount).

> **Verification-approach note (2026-07-14):** this chunk describes a
> headless-Chrome CDP harness as the self-verification path (below).
> That approach is **no longer recommended for new stages** — it is
> fiddly to stand up, throwaway (never a regression test), and its
> value overlaps a quick human look. For UI work the preferred check
> is a **human-operated browser pass** against the demo (the operator
> drives scenarios and watches behavior directly), with the automated
> tsc/biome/vitest + live dialect suite covering the mechanical
> correctness. The CDP text here is kept as historical record of how
> chunk 4 was verified.

## Session handoff (2026-07-11 — resume here in a fresh context)

**Commit state**: committed are stage 1 (`fxtr expt 4.4: dashboard
package scaffold + the HMR dev-mount loop`), stage 2 (`fxtr expt 4.4:
the wire dialect layer + the live round-trip harness`), stage 3
(`fxtr expt 4.4: tree + workflow header` — browser-reviewed twice:
initial round + the feedback resolutions below), stage 4 as two
commits: the upstream additions (`fxtr expt 4.4: attach_path +
grid-level internals attach` — python + tests + TS proto + doc
updates) and the UI (`fxtr expt 4.4: grid detail + the cells table`,
incl. the browser-review snapshot-reconciliation fix), the interleaved
demo-driver scenarios (`fxtr expt 4.4post1` — see the interleaved
work items section), stage 5 (`fxtr expt 4.4: the cell drawer`), and
stage 6 (`fxtr expt 4.4: mutations`), the pulled-forward
LOCAL_CODE_OVERVIEW + top-level CODE_OVERVIEW rewrite, and stage 7
(`fxtr expt 4.4: subscription-driven live updates`). The chunk closed
with stage 8 deferred to [renderer_consolidation.md](renderer_consolidation.md).
Stages 5+ are self-verified via the headless-CDP harness and committed
green per the settled cadence; Daniel's browser review happens in
batches.

### Stage-7 implementation state (2026-07-11 — committed)

Subscription-driven refetch, wired end to end against the live demo
(the conventions are documented in `frontend/LOCAL_CODE_OVERVIEW.md`'s
"Live updates" section — kept current per the standing rule):

- `src/subscription.ts` (new) — the driver `atomEffect` (the agent
  panel's state-machine shape): idle → allocate a fresh inbox + the
  callback-target `subscribe_to(capability)` in one tick →
  subscribing → running (drain batches, apply the plan, re-anchor
  `inbox.sliced(n)`). Recovery: `connectionState: "broken"` and
  stubKey mismatch (runner revive hands the panel a fresh stub) both
  reset to idle with a FRESH inbox (a live inbox refuses a second
  `subscribe_to`; verified against the workstation parker's
  contract — it owns the unsubscribe lifecycle, nothing is torn down
  renderer-side). A failed subscribe parks in `errored` with a
  header retry.
- `src/derive/hints.ts` (new) — `planInvalidations`: hint batches →
  one plan (summarize / intent / bindings flags + deduped cell
  paths); `bindings` also drops every drawer snapshot (the bound
  badge rides `inspect_cell`; hints carry no boundness addressing).
  Pinned by `tests/hints.test.ts`.
- **Summarize coalescing deviates from the approved "trailing ~400ms
  debounce" in mechanism, not intent**: timers don't fit atomEffect's
  cleanup-per-rerun model, so summarize refetches are gated to
  at-most-one-in-flight with a trailing `summarizeWanted` flag that
  refires when the in-flight one settles — deterministic, timer-free,
  same bounded-rate + trailing-correctness goal.
- `panel_state.ts` — `PANEL_KIND` v7 (`subscription` phase; the
  structural check grew in the same edit, per the convention).
  `panel.tsx` mounts the driver; `header_view.tsx` gains the live
  indicator (`● live` / `connecting…` / `live updates failed` +
  retry). The manual refetch/refresh buttons stay as the
  at-least-once fallback.

**Verified live** (2026-07-11, headless CDP): `● live` on first
subscribe; driver-side input flips update the tree badges (stale /
unsettled) with NO manual refetch, and the reconciled view matches a
manual refetch exactly (a remaining stale badge was real backend
state — flaky's generation observed the other input version); the
crash → revive arc heals through the stubKey reset (dev mount hands a
fresh stub → resubscribe → live), and a post-revive mutation's
errored badge arrives via hints on the new subscription. Zero page
errors from the panel; one known wart, documented in the local
overview: a crash can log one shell-side `mutation rejected as
validation_failed` (an in-flight allocation racing the service
death) — the write rolls back and the FSM restarts cleanly.

### Stage-6 implementation state (2026-07-11 — committed)

The mutation surface, wired end to end against the live demo:

- `src/derive/actions.ts` (new) — `deriveCellActions`: the pure
  affordance list per kind × status × currency × intent state (the
  demand-affordance doctrine as data, pinned by
  `tests/actions.test.ts`). **The no-mutations gate is
  internals-ness, NOT grant mode** — found live: task grants are
  read-shaped by design, so a mode gate hides every task action;
  data/handle mutations additionally require a write grant.
- `src/actions.tsx` (new) — the panel-bottom **action log**: one row
  per gesture, a gesture being a short `ActionStep` chain (typed
  discriminated union → the typed `allocateInvoke` overload per
  method in `allocateStep`). The first step allocates on fire; each
  row advances its own chain when the current step fills (identity-
  guarded against double-advance); an errored step stops the chain
  and shows `errorGist` (the traceback's LAST line — the informative
  one) with the full text on the tooltip. Results render refs as
  `RefLink`s, `DeleteReport`s by referrer paths.
- `src/cell_drawer.tsx` — `ActionsRow` under the verdict line:
  buttons from `deriveCellActions`; `confirm`/`needsAdopt` actions
  expand inline first (delete/clear confirmations; the adoption
  prompt explains the grid-level ownership claim). "Run"/"Retry"
  demand **by record** (`demand_target` verbatim — the round-trip
  contract) when the targets join has one; "Target + run" chains
  `add_target` + the sel demand. **Task demands need an output
  selection** (found live: `read_cell_entity` rejects output-less
  task reads) — `demandSel` picks the grant's first declared output.
- `src/grid_view.tsx` — `GridActions` in the grid header (write
  parent grant + non-internals): source grid (adoption), revoke grid
  sourcing (plain / force-sweep variants in the confirm expansion),
  delete grid (confirm; the `DeleteReport` lands in the log). All
  addressed through the walk's parent grant (`grants[len-2]`) + key.
- `src/wire/proto.ts` — `read_cell_entity` declared (the user-demand
  channel). `src/panel_state.ts` — `PANEL_KIND` v6 (`actions` list);
  **`isPanelState` hardened to a structural check**: a mid-edit HMR
  window (kind bumped before `PANEL_INITIAL` grew) persisted a
  tagged-but-incomplete state through the auto-allocating query
  slots, which a tag-only check then accepted forever (boot crash).
  Bump kind AND grow the check together; the structural half is the
  safety net.
- Targets list (`header_view.tsx`): attached records get "run" —
  `demand_target` with the stored record passed back verbatim (the
  record round-trip smoke, exercised live); dangling records get no
  button (demand faults by design).

**Verified live** (2026-07-11, headless CDP): Retry on the errored
flaky cell (by-record demand → result ref); Clear + rerun on stale
`combined` b faulting first with the upstream diagnosis (strict gate
working — the fix: Clear upstream `shout` b, then Clear + rerun rides
the missing-support cascade to completion); Untarget → live verdict
flip to `unanchored (not wanted)` → Target + run re-completing;
the full adopt arc (revoke grid sourcing → prompt → adopt+target+run);
cell-sourcing revoke/adopt both ways; Delete cell (confirmed, row
gone); Delete grid on `echoed` (report: no referrers) + rebuild via
the driver; the targets-list verbatim run. Zero unexplained page
errors. Doctrine notes from the pass: an untargeted stale cell offers
no "Clear + rerun" (target first — two gestures; acceptable, watch
in review), and the force-revoke branch wasn't exercisable (no
refusal state on hand; same wire method, kwarg-typed).

### Stage-5 implementation state (2026-07-11 — committed)

The per-kind cell drawer below the cells table, wired end to end
against the live demo:

- `src/panel_state.ts` — `PANEL_KIND` bumped to
  `experiment-dashboard/v5`: `gridQueries` gains `drawer` (keyed by
  `cellKey(path, coordsText)` = `` `${path}::${coordsText}` ``; a
  `DrawerEntry` is token-pinned like `CellsEntry` and carries its
  grid `path` so pruning never parses keys). One entry holds
  `inspect` + (task cells only) `generation` + `observers` invokes,
  allocated together in one updater.
- `src/cell_drawer.tsx` (new) — `CellDrawer`: header bar (coords,
  kind tag, targeted dot, refresh + close) over the per-kind body.
  Verdict line always from the `inspect_cell` snapshot (the labeled
  channel; the table row only fills in pre-load), reason clamped to
  its first line (errored reasons embed whole tracebacks — the
  failures section is their home, `maxHeight` + scroll there).
  Sourcing rows joined client-side from the workflow-level
  `list_sourcing` (by grid path + coords text). Bodies: data/
  dataframe → value `RefLink`; handle → key ref + bound/unbound;
  task → per-output result refs, "browse internals…" (flips the
  tree's per-task reveal + un-collapses — no separate navigation),
  the generation report (id/status/fn ref; failures with kind/
  class/ts + capped message; observations table with per-edge
  match/changed/missing colors; unanswered requests; checkpoint
  entries with step/args/value refs), and reverse observers.
  Subtree cells get a namespace explainer (no inspect — subtree
  rows' own cells don't materialize in the live demo anyway).
  Token-invalid heal: same once-per-mount re-attach callback as the
  table, triggered off `inspect_cell`.
- `src/grid_view.tsx` — the stage-4 drawer stub replaced: a
  `DrawerProps` bundle (slot + internals-reveal state) threads
  GridDetail → CellsTable; GridDetail also derives the grid's
  sourcing rows from the workflow-level list.
- `src/panel.tsx` — `drawer` slot map wiring (`mapSlotAtom` over
  `cellKey`), GC extended (drawer entries pruned via their `path`
  field), `refetchAll` drops drawer entries too (snapshots; grants
  stay), and the internals-reveal toggle un-collapses the task node.
- `src/wire/helpers.ts` — `boundByPosition(coords)` (cell coords →
  the fully-bound `viewSel` map); pinned by `tests/helpers.test.ts`.

**Verified live** (2026-07-11, headless-CDP against the demo): data
drawer (verdicts + "user-sourced at pinned value" + value ref);
handle drawer bound (uppercase) vs unbound (reverse); task drawers —
`echoed` b **unsettled** with both observation edges `match` and the
support-stale diagnosis, `combined` b **stale** with the
`["inputs"] @ ["b"] changed` edge itemized, `shout` b observers
listing `combined`/`echoed` with their statuses, `planned` showing
the run-time creation trail (grid_config + wrote_cell edges) and
"browse internals…" revealing `draft`/`polish` in the tree,
`polish` (a task inside internals, read-mode grant via the walk)
with its own report and `["planned"] @ []` as observer, `slow` with
all six checkpoint entries, `flaky` errored with the failure record
(the traceback-swamping verdict line found and fixed in this pass);
`styled` table showing the flattened `v` prefix column; drawer
refresh/close; zero page errors all session. Not live-driven:
unanswered-requests rows (needs an interrupted run mid-state; decode
pinned by the dialect harness) and the drawer's token-invalid heal
(same callback path the stage-4 table heal exercised).

### Stage-4 implementation state (2026-07-11 — committed)

The grid detail pane + the value-free cells table, wired end to end
against the live demo:

- `src/panel_state.ts` — `PANEL_KIND` bumped to
  `experiment-dashboard/v4`: adds `gridQueries` (per-grid slot maps
  keyed by key-path text — `attach`: the grid's one `attach_path`
  invoke; `cells`: a `CellsEntry` = the `list_cells` invoke **pinned to
  the grant token its args were built from**, so a re-minted grant
  makes it stale by comparison) and `ui.selectedCell` (coords text;
  reset on grid navigation). New `mapSlotAtom(map, key)` — a slot atom
  over one key of a Record where missing keys read as `null` (so
  `makeInvokeResultAtom` allocates on first subscribe).
- `src/grid_view.tsx` (new) — `GridDetail`: header (path, kind tag,
  own axes, internals tag, `access: <grant mode>`, grounding verdict +
  reason, sourcing status, this grid's targets joined from the
  workflow-level `list_targets`, config entity as an inline `RefLink`
  — no Block-content dispatch key exists yet, so embedded config
  rendering waits for a config renderer) over `AttachedGridBody` (owns
  the `attach_path` subscription; error state has a "re-attach"
  button) → `CellsSection` (allocates/re-allocates the `list_cells`
  invoke when the entry is missing or token-mismatched) →
  `CellsTable` (coord columns named from the grant's axes — the full
  flattened frame —, status/currency/grounding/wanted columns,
  targeted `●` via the coords-text join, reason with ellipsis +
  tooltip, text filter + click-to-sort on coord/status headers, row
  click selects → a drawer stub box; the real drawer is stage 5).
  **Token-invalid heal**: a `list_cells` failure matching `unknown
  view token` re-runs the attach once per mount (backend restart
  under a live panel). **Checkpoint grids don't attach** (refused by
  design): their detail pane explains and points at the future
  drawer/generation-report affordance. **Snapshot reconciliation
  (browser-review find)**: the tables are snapshots like every query,
  so the header's "refetch" also drops every cells entry (grants
  stay — stable identities, not data) and each table has its own
  "refresh" button; stage 7's change-event hints make this selective
  and automatic.
- `src/derive/cells.ts` (new) — pure `deriveCellRows(cells, filter,
  sort)` + `CellSort`/`CellSortKey`/`sameSortKey`; `coordsText =
  JSON.stringify(coords)` is the row identity AND the targets join
  key (compact JSON matches `encode_coords`). Covered by
  `tests/cells.test.ts` (join-key form, filtering, both sort keys).
- `src/panel.tsx` — rewired: `gridQueries` sub-atoms + `mapSlotAtom`
  per selected path, `GridDetail` keyed on the path, selection resets
  `selectedCell`, and a GC effect prunes both slot maps of paths a
  summary refetch no longer knows.

**Verified and closed out** (2026-07-11): full battery green (243
pytest + pyright, demo suite, 13 vitest, tsc + biome repo-wide,
`jj pre`); driven live against the demo both via Daniel's browser
pass (which found the snapshot-reconciliation gap, fixed above) and a
headless-Chrome CDP harness (a scratchpad script clicking the dev
panel — the approach: `--remote-debugging-port` +
`Target.createTarget`/`Runtime.evaluate` clicks + console/exception
capture; rebuild it if needed, it's ~80 lines): `inputs` (data grid:
3 cells, write access), `combined` (task grid: statuses, fresh/stale
currency with the stale reason, wanted ✓, targeted ● per row),
internals reveal → `["slow","steps"]` (checkpoint note + internals
tag).

**Demo note**: the demo backend must be restarted after capability
changes (a stale orchestrator from a previous session may hold ports
3471/8643 — check `ps aux | grep run_experiment_demo`). After a
backend restart the browser needs its stale shell state cleared (the
shell offers the button) and the dev panel reopened once (the dev
session heals on the next mount).

### Stage-3 feedback resolutions (2026-07-10, settled in discussion)

1. **"Attached" chips dropped** — `AbnormalChip` in `header_view.tsx`
   badges only abnormal states (dangling_* / unanchored / orphaned);
   healthy defaults are silent.
2. **The workflow-level lists**:
   - *Sourcing rows* are labeled grid-vs-cell (`coords === ""` is the
     grid-level record) and say what the record means: "user-sourced
     at pinned config:" / "user-sourced at pinned value:" before the
     fingerprint ref. ("Vouch" deliberately avoided — in the grounding
     vocabulary it belongs to request-edge *self-vouching*, the
     non-user channel.)
   - *Bindings*: **upstream change** — `list_bindings` now returns
     `BindingInfo` (key + the handle cells holding it, one snapshot,
     via the new `WorkflowSession.handle_cells_holding` reverse query;
     joined against handle-kind grids only). The client-side join was
     impossible by doctrine (presence queries are value-free). Rows
     read "handle key <ref> at <cells>" (or "held by no handle cell").
   - *Targets*: **upstream change** — `TargetInfo` gained
     `grid_path`/`coords` (absolute, like `TargetFinding`; the targets
     table already had the denormalized columns, so
     `_list_targets` just returns them; the lint's dangling-target
     rows get locations too). The defensive TS `describeTarget`
     decoder was rejected (would duplicate `resolve_templates`
     client-side and drift).
3. **Tree badges**: populated stays a bare count (a `▦` glyph was
   tried and dropped in review) but gained a kind-aware tooltip (task
   = generations, data/handle = cells, checkpoint = entries; rollups
   include descendants). The target badge is `●n` targeted+populated /
   `◌n` targeted-unpopulated (n = target records, own + descendants) —
   colored red when any record dangles; the populated count stays (the
   dot only encodes target state × materialization). A symbol
   **legend** lives behind a "?" toggle at the top of the tree pane
   (`legendOpen` in ui state; `PANEL_KIND` bumped to v3). Iterate
   later if it reads confusing in practice.

### What exists (fresh-context file map)

The package — `projects/fxtr_experiment/frontend/` (pnpm member
`@fxtr-experiment/dashboard`; entry `src/index.tsx` default-exports the
`{experiment_workflow: {key-uuid: Component}}` dispatch map):

- `src/wire/types.ts` — TS mirrors of every `user_api` result
  dataclass + `ViewSel`/`ViewGrant`/`ViewToken`/`AxisSpec` + hint
  batches. `src/wire/proto.ts` — the typed `ExperimentWorkflowProto`.
  `src/wire/helpers.ts` — `viewSel` (sorted bound pairs),
  `lintSound`/`lintClean`, `asChangeHintBatch`.
- `src/panel_state.ts` — the `ownStorage` shape
  (`experiment-dashboard/v4`, validity-check reset), workflow-level
  query slots + per-grid slot maps + ui state + `mapSlotAtom`.
  `src/panel.tsx` — composition (five `makeInvokeResultAtom` query
  slots, refetch, per-grid slot wiring + GC, layout).
  `src/derive/tree.ts` (+ `tests/tree.test.ts`) — the summarize→tree
  fold (client-side rollup aggregation; internals = any grid under a
  task grid's path). `src/derive/cells.ts` — the cells-table
  filter/sort fold (coords text = row id + targets join key).
  `src/grid_view.tsx` — the grid detail pane (see the stage-4
  implementation-state section). `src/tree_view.tsx`,
  `src/header_view.tsx`, `src/links.tsx`.
- Harness: `tests/dialect.test.ts` + `tests/_fixture_harness.ts`
  (spawn) + `examples/fxtr_experiment_demo/dialect_fixture_backend.py`
  (the Python fixture; canned values of every dataclass family) +
  the demo pytest smoke `tests/test_dialect_fixture_backend.py`.
- Dev loop: `examples/fxtr_experiment_demo/frontend/src/client/
  DevDashboardPanel.tsx` (the "+ dashboard (dev)" sidebar
  registration, static import → Vite HMR; persistent dev session in
  localStorage; capability stub found reactively by external name) +
  its `App.tsx` registration.

Background reading for a fresh context: `notes/monitoring_ui.md` (the
contract this implements), this plan's alignment-round + scope
sections, `frontend_lib/CODE_OVERVIEW.md` sections for
`react-dispatch` / `workstation-dispatch` / `state-util` /
`connector-http`, `panel-viewer`'s `panel/PanelWrapper.tsx` (the
ownStorage plumbing the dev mount replicates), and
`fxtr-ui-agent-plugin-renderer` (the FullPanel precedent).

**Dev/verify commands**: backend+frontend:
`uv run python run_experiment_demo.py --workdir
~/.cache/fxtr_experiment_demo_workdir` from the demo dir →
http://localhost:3471 (driver panel builds state; dashboard under
"+ dashboard (dev)"). Checks: `pnpm exec tsc --build --force
projects/fxtr_experiment/frontend examples/fxtr_experiment_demo/frontend`,
`pnpm exec biome check --write` on the same dirs, `pnpm exec vitest run
projects/fxtr_experiment/frontend`, and `uv run pytest tests/` in the
demo dir (3 passed + 1 xfailed expected).

### Learnings not written elsewhere

- Wire types must be **type aliases, not interfaces** (the `Method<>`
  constraints need implicit index signatures); loose fields are typed
  `ResultValue`, not `unknown`. mashumaro emits every field (`None` →
  `null`), so optionals are `| null`, never `?:`.
- `makeInvokeResultAtom` allocates on first subscribe when the slot is
  null; **refetch = write a fresh `ctx.allocateInvoke` into the slot**.
- `LintReport.sound`/`.clean` are Python `@property`s — never on the
  wire; the TS helpers implement them.
- Key-path text is a plain JSON array of key strings — internals grids
  are NOT syntactically marked; the tree derives internals-ness from
  having a task-grid ancestor (`derive/tree.ts`).
- Never hand-build escaped wire JSON (a hand-guessed form 400'd in the
  pytest smoke; the vitest's real encoder was right on the first try —
  that asymmetry is the harness's whole point). The dispatch *binding*
  layer (method names/kwargs) is deliberately outside the harness —
  covered by live demo use.
- The `PANEL_KIND` v1→v2 bump exercised the stale-ownStorage reset
  convention cleanly.
- Root `pnpm check` needs the report-builder CLI installed via
  `pnpm install --ignore-workspace` (see the phase overview followup).
- **The pre-commit biome/tsc/pnpm-lock hooks' `files:` filters did not
  cover the new TS dirs** (`projects/fxtr_experiment/frontend/`,
  `examples/fxtr_experiment_demo/frontend/`, `examples/fxtr_http_demo/
  web/`), so stage 2 landed biome-unclean (`noNonNullAssertion` in
  `dialect.test.ts`) without any hook firing. Fixed in this working
  copy: filters extended, and the test uses a checked `at(arr, i)`
  helper instead of `!` (per-line biome-ignores would have drowned it).
- **The dev mount must gate on `lifecycle === "connected"`** (the
  shell's PanelWrapper contract): the panel's query slots
  `allocateInvoke` on first subscribe, which throws before the
  session's first snapshot. The persisted-session path made this
  deterministic — `attachSession` returns synchronously in
  "connecting" — surfacing as an uncaught AggregateError from jotai's
  subscribe flush. Fixed in `DevDashboardPanel.tsx` (render an info
  line until connected; "stale" reconnects unmount to the same line).
- **Updating the DRIVER renderer in a live workdir**: the runtime
  workdir is a scaffold-time copy, so template edits to
  `agent_workdir/renderers/experiment_driver/index.tsx` don't reach a
  running demo (and `--force` re-scaffolding wipes workflow state).
  The loop that works: copy the file into the workdir, `git commit`
  in the workdir repo, relaunch the demo (the dispatcher resolves the
  preset by the workdir's git HEAD at boot). Only the dashboard
  panel itself gets Vite HMR (the dev mount's static import).

### Special attention for the next stages

- Stage 6 (mutations): the proto declarations already exist
  (`wire/proto.ts` — add_target/remove_target/demand_target/
  clear_cells/delete_cell/delete_grid + the sourcing CRUD); the
  drawer + grid header are where the affordances land. Doctrine:
  "Target + run" vs "Run" (never auto-target silently; `adopt`
  prompted), "Retry" vs "Clear + rerun" on errored cells,
  `DeleteReport` consequences surfaced. The record round-trip smoke
  (`list_targets` → `demand_target` verbatim) belongs here. Demands
  are ordinary invoke slots — fire and let the table refresh show
  progress (manual refresh until stage 7).
- Stage 7's subscription: landed as designed (`subscription.ts`; see
  the stage-7 implementation-state section).
- The nix wiring (was stage 8) is deferred: everything it needs —
  the `forType` id string, the slice/type-entity routes, the
  iteration-loop mechanics, the verification plan — is written up in
  [renderer_consolidation.md](renderer_consolidation.md).
- `frontend/LOCAL_CODE_OVERVIEW.md` exists (pulled forward,
  2026-07-11, together with a guide-conformant rewrite of the
  top-level CODE_OVERVIEW) — **keep it current as stages 7–8 land**
  (the subscription conventions and the nix wiring section are its
  remaining gaps, marked TODO inline).

*(Swapped with the experiment demo in the chunk order, 2026-07-10: the
demo (chunk 3) lands first and is this chunk's live development
harness, manual-e2e vehicle, and bit-rot guard — the renderer slice
gets wired into the demo's preset the day it exists, so no standalone
nix check target is needed.)*

## Post-demo alignment round (2026-07-10, after chunk 3 landed)

- **Dev loop is the chunk's FIRST work item.** The in-repo renderer
  builds through the workdir's pinned `fxtr` flake input, so the
  nix/dynamic-dispatch loop needs a monorepo commit + input re-pin +
  `set_preset` per iteration — too slow for UI work. Investigate, in
  order: (1) a dev-mode **static mount** of the panel in the demo
  frontend (pnpm `workspace:*` import → instant Vite HMR against the
  live backend; needs a check that a static registration can overlay
  the dynamic dispatcher in one shell), (2) the runtime-compile
  dispatcher (`basic_dynamic_dispatch`'s `npx esbuild` path) over the
  in-repo `frontend/src`, (3) the scripted re-pin loop. The **nix
  packaging is the end state** and must land within the chunk (the lib
  slice + demo preset wiring); dev can drop it until the panel
  stabilizes. *(Resolved in stage 1: option (1), the static dev
  mount, worked — `DevDashboardPanel.tsx` is the loop; the nix slice
  remains stage 8.)*
- **Dialect testing re-settled: a live python↔typescript round trip,
  no checked-in fixtures.** Instead of golden files (whose id-leaf
  payload question kept resurfacing), the vitest spawns a tiny Python
  **dialect fixture backend** patterned on `examples/fxtr_http_demo`
  (the `fxtr2.experimental.http.service_*` layer; `READY <url>`
  handshake — TS-spawns-python precedent in that demo's
  `web/src/server.ts` and react-dispatch's `_integration_harness.ts`)
  and connects with the real `@fxtr-ui/connector-http`. The fixture
  serves representative values of the real `user_api` dataclass types
  through the real dialect encode + escaping; the TS side decodes them
  through the real connector into our wire types, and — the part
  golden files could barely cover — **the TS→python direction is
  tested live**: echo-style methods take a `ViewSel` (etc.) built by
  our typed helpers and the Python side decodes it through the actual
  typed dispatch, so an encoding bug fails loudly on the real
  boundary. Referenced entity ids in returns are loadable from the
  same server (the connector's entity path), so nothing needs mocking.
  Drift on either side fails the one live test deterministically.
  Settled at kickoff: the fixture **hand-rolls POST handlers** around
  the real dialect encode (`encode_to_service_data` + the real escape)
  — the http service layer doesn't host `TypedServiceImpl`s directly.
  Remaining kickoff verification: the CI story for vitest-spawned
  Python (follow `_integration_harness.ts`'s conventions). Note the http wire uses resolved-mode entity payloads
  while the workstation session wire uses short_ids — same tags, and
  our TS types are stub-typed at the leaves (mode-agnostic), so the
  pin (dataclass shapes, strategies, tag placement) is unaffected.
  The fixture backend lives beside the demo (it wants
  `fxtr_experiment` + the fxtr2 http layer, both in the demo's venv).
  Change-event hint payloads are plain dicts and stay covered by the
  chunk's demo e2e (or a trivial inline assertion), not this harness.
- **Panel identity across runner crash/revive**: a revive re-registers
  `experiment-workflow` as a NEW service record, so the old panel's
  stub is dead. v0: **the operator reopens the panel** (the demo makes
  this a one-click test); the workstation's service-substitution
  placeholder pattern is the noted followup if reopen proves annoying.
  (Grant re-rooting on a *live* panel — token-invalid after e.g. an
  import — is unchanged and still in scope.)
- **The `unsettled` verdict needs a pure chain to be visible**: a cell
  whose observations still match but whose support is stale is
  unsettled (not fresh — support isn't fresh; not stale — upstream
  might rerun to the same output). The demo's `combined` reads the
  root input directly, so input edits take it straight to stale; add a
  small driver task reading ONLY `shout`'s output when the drawer
  needs unsettled on screen (a persistent static state, unlike the
  running-support variant).

## Scope

- **Package scaffold**: `projects/fxtr_experiment/frontend/` — one pnpm
  workspace member (typecheck + vitest at the repo root; add the dir to
  `pnpm-workspace.yaml`), esbuild-bundled against the standard curated
  externals (no new import-map entries — so the panel code is fully
  bundled: relative imports + the standard externals only, unlike the
  demo's `builtin` slice which externalizes library packages). Layout:
  `src/index.tsx` is the renderer entry (the 2-level
  `{subtype_key: {dispatch_key_uuid: Component}}` default export);
  `esbuildRenderer { src = …/frontend/src }`; `tests/` outside `src/`.
- **Nix wiring**: expose the preset slice through the repo nix lib in
  the `fxtr_renderer.nix` module shape (a function of `keyIds`, so the
  demo's preset flake imports + merges it like its other slices); the
  slice's `forType` is the user capability's **service type id** with
  its type entity minted from the real `fxtr_experiment` package
  (`mkEntityFromPython` over a `mkVenv` of `projects/fxtr_experiment` —
  its pyproject likely needs the non-PyPI source redeclarations the
  `fxtr_nix_writer` convention uses; the `fxtr_lib_type_ids` stub is
  the fallback if that turns into a rabbit hole). Settle the exact lib
  attr names here. Wire the slice into the demo preset as part of this
  chunk (that is the build coverage; no separate check target).
- **Wire dialect layer**: TS types for the wire forms the panel speaks
  (`ViewSel` incl. the sorted-pairs `bound` strategy, grants, the
  report/summary dataclass shapes, hint events), a TS `ServiceProto`
  declaration for the capability, + thin typed helpers over
  `allocateInvoke`. **The dialect is pinned by a live
  python↔typescript round-trip harness** (the dialect fixture backend
  + `@fxtr-ui/connector-http` — the settled design and its kickoff
  verifications are in the post-demo alignment round above): both
  decode (fixture-served real dataclass values → our TS types) and
  encode (TS-built `ViewSel` etc. → real typed-dispatch decode via
  echo methods) run against the real machinery in one vitest; drift on
  either side fails it deterministically. No checked-in wire fixtures,
  no regen bookkeeping.
- **Grant management**: `root()` + `attach`-on-navigation with grants
  cached in versioned `ownStorage` (`atomWithDefault` validity-check
  pattern, `kind: "experiment-dashboard/v1"`); token-invalid (or fresh
  service stub) → re-root and re-derive. Treat every token as cache,
  never state. Internals paths route through `attach_internals`
  (read-only forced) instead of `attach`. *(As built, superseded by
  the stage-4 kickoff round: one `attach_path(keys)` walk per
  navigated grid — internals crossings included — and the storage
  shape is at `experiment-dashboard/v4`.)*
- **The panel** (FullPanel for the capability service, per
  `notes/monitoring_ui.md`; styling = plain React over the workstation
  theme's CSS custom properties, like the substrate packages):
    - workflow header: lint badge + targets / sourcing / bindings
      lists;
    - the key-path tree from `summarize` (rollup badges, grounding
      tint, target dots). Internals grids are ordinary summarize rows
      (their path steps carry the internals kind): the tree hides
      internals subtrees by default, and the task drawer's "browse
      internals" flips a per-task reveal toggle + navigates — one tree
      component, no separate internals viewer;
    - grid detail: header (config entity via `RenderDispatch`,
      grounding/sourcing/targets, grid actions) + the value-free cells
      table (client-side filter/sort; targeted joined against
      `list_targets`);
    - the cell drawer per kind (data/handle/task), incl. the
      generation report, reverse observers, internals browsing (the
      reveal toggle above), and the mutation affordances ("Target +
      run" / "Run" / "Retry" / "Clear + rerun", `adopt` prompts,
      delete confirmations surfacing `DeleteReport`);
    - subscription-driven refetch: one inbox `subscribe_to` the
      capability (the agent-panel state-machine shape: allocate inbox
      + callback-target invoke in one tick; resubscribe on the
      `connectionState: "broken"` edge); hints invalidate the matching
      queries (summary / one grid's rows / intent lists), debounced
      renderer-side.
    - link-slot renderers (BlockLink / InlineLink / HoverPreview) for
      the capability service: a minimal labeled pill, added early.
- **Record round-trip smoke**: `list_targets` → `demand_target` with
  the record passed back verbatim, exercised in the dialect harness
  (echo direction) plus one live invoke against the demo.
- **`frontend/LOCAL_CODE_OVERVIEW.md`**: the frontend package documents
  itself — layout, the wire-dialect layer, the grant-cache and
  storage-shape conventions, the component structure, how the dialect
  round-trip harness works (incl. how the fixture backend is spawned),
  and the nix wiring. The package is sizable and largely separate from
  the python package, so the detail lives here; the top-level
  `CODE_OVERVIEW.md` gets a one-entry pointer
  (`frontend/ … see its LOCAL_CODE_OVERVIEW.md`).
  *(Pulled forward — written after stage 6 and kept current per
  stage from here on; the nix-wiring section lands with stage 8.)*

Build order = **commit boundaries** (one `fxtr expt 4.4: …` commit per
stage, each green + browser-verified against the live demo; adjacent
stages merge if one lands thin):

1. scaffold + the HMR dev-mount loop (incl. link pills + the skeleton
   panel's live `summarize` dump);
2. wire dialect types/proto/helpers + the round-trip harness (one
   claim: the dialect is pinned);
3. tree + workflow header (typed summarize → key-path tree with rollup
   badges; lint badge + targets/sourcing/bindings lists);
4. grid detail + cells table;
5. cell drawer (per-kind; generation report; reverse observers);
6. mutations (demand/target/clear/delete/sourcing + prompt flows);
7. subscription-driven refetch (+ internals reveal if it stays small);
8. nix slice + demo preset wiring + LOCAL_CODE_OVERVIEW + chunk close.
   *(As closed: the LOCAL_CODE_OVERVIEW was pulled forward after stage
   6, and the nix slice + demo preset wiring was deferred out of the
   chunk — [renderer_consolidation.md](renderer_consolidation.md).)*

## Tests

- Vitest: the **dialect round-trip harness** (spawned Python fixture
  backend + real `@fxtr-ui/connector-http`; decode of every
  report/summary dataclass, encode of `ViewSel`/records via echo
  methods); state derivation (tree from summary rows incl. internals
  hiding; table filtering; drawer action enablement per status
  combination; hint → invalidation mapping); storage-shape validity
  checks (stale `ownStorage` resets, not crashes).
- Pytest (demo package): a smoke that the fixture backend boots and
  serves its shapes (so a Python-side break fails in the Python suite
  too, not only via the spawned vitest).
- Manual e2e against the chunk-3 demo throughout; note here anything
  that needed a workstation-side integration check instead.

## Stage-4 kickoff round (2026-07-11, settled in discussion)

- **`attach_path(keys) -> tuple[ViewGrant, ...]`** added to the
  capability: the dashboard's navigation primitive — every prefix grant
  from the root in one call, instead of a client-side sequential
  attach chain (which needed an in-flight-op driver state machine
  renderer-side). Task boundaries cross into internals transparently
  (the walk re-tags the task spec; the hop is not a returned prefix).
- **`attach_internals` accepts open positions** (the fully-bound check
  was a surface restriction, not machinery — read-mode subtree views
  are sliceable by design, and internals grids store cells under the
  task's flattened axes): open task positions stay kept on the grant,
  giving the grid-level internals window `attach_path` and the detail
  pane need. Consequence for the tree: internals grids stay ordinary
  rows behind the per-task reveal toggle — no virtual per-cell tree
  entries needed; per-cell browsing is the same call fully bound.
- Stage-4 UI decisions: lazy attach-on-navigation (cache accumulates);
  internals-grid detail panes work like any grid's (via the walk);
  cells-table filter/sort starts minimal (text filter + column sort);
  the targeted join key is `(grid_path, compact-JSON coords text)` —
  `JSON.stringify(coords)` matches `encode_coords`' canonical form.

## Interleaved work items (noted mid-chunk, schedule between stages)

- [x] **The demo driver exercises neither subtree grids nor
  tasks-that-create-tasks** (dynamic grid creation at run time), so the
  dashboard's subtree display and internals-with-nested-tasks browsing
  have no live test state. Add driver scenarios for both before the
  stages that need them (subtree rows in the tree/table; the drawer's
  internals browsing over a task that created grids).
  *(Landed as `fxtr expt 4.4post1`, 2026-07-11, together with the
  alignment round's unsettled-chain task: `scenario_subtree` — the
  `variants` subtree (axis `v`) with `cfg` data + `styled` task
  children; `scenario_nested` — the `planned` task whose run creates
  `draft` (data) + `polish` (a nested task) inside its own internals
  via the new `plan` standard fn (`ctx.own_internals().open_task`
  worked first try — no machinery changes needed); and
  `scenario_unsettled` — the `echoed` task reading ONLY `shout`'s
  output, so `overwrite_input` leaves its *b* cell statically
  unsettled. Smoke coverage in the demo's `test_scenario_arcs`.)*

Notes/log: (fill in during the chunk)
