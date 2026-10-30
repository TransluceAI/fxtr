# Discussion log

Running co-design log for the fxtr_experiment project (formerly the
fxtr_workflow_3 spike). Decisions sections are
newest-first: each records decisions made, alternatives considered, and open
questions to come back to. This is a working document, not a polished design.

**Policy: this log is never the source of truth for the design.** The
source of truth is always the self-contained docs rooted at
`notes/design_overview.md` (which points at the detailed writeups and
module docstrings). Rounds may be written here freely as discussion
happens, but **by the time a commit lands, every decision recorded here
must also live in the canonical docs** — an entry here is a rationale
trail and change summary, never the only home of a decision. (This is what
keeps a later bulk-consolidation round from ever being needed.) Open
questions and deferred followups also live in `design_overview.md`. The
previous log (the full design-phase rationale trail, through the walking
skeleton's chunk 5) is archived at
`notes/archive/discussion_log_2026_07_04.md`.

## Decisions

### 2026-07-17 — phase-4 followup scoping: renderer consolidation + the repin/reload dev loop

Rescoped the deferred nix wiring into a consolidation plan (canonical
statement: `plans/phase4_monitoring_ui/renderer_consolidation.md`,
which replaces `deferred_nix_wiring.md`). Trigger: rendering reaches
the experiment demo through three channels (the nix preset — including
the stored-config renderer whose source lives in the demo workdir; the
static `DevDashboardPanel` dev mounts; and the orphaned `frame_v0`
exports), and the missing structural piece is the repo-lib esbuild
slice — the second of the two ways repo-owned renderer code can reach
a workdir preset (the first, the import-map-external shim shape, is
wrong for the dashboard: it would statically bundle it into every
shell). Decisions:

- **One repo-lib slice, `lib.fxtr.experimentRenderers`**: one esbuild
  module over `projects/fxtr_experiment/frontend/src`, four subtypes
  (`experiment_workflow`, `frame_v0`, `grid_config`,
  `slice_template`); the stored-config renderer source moves from the
  demo workdir into the repo package. Single-module over split-module
  for simplicity (config viewers ride the dashboard bundle; split only
  if weight ever matters).
- **Type entities via the stub-mirror route** — extend
  `nix/fxtr_lib_type_ids` with a `WORKFLOWS3_NS` block (the venv route
  stays rejected: full package closure for identical content-addressed
  output). The demo workdir's type-entities venv drops its
  `fxtr_experiment` dependency.
- **elkjs is bundled, not externalized**: `esbuildRenderer` gains a
  `nodeModules` arg; elkjs (zero runtime deps) comes from one pinned
  npm-tarball fetch. `new ELK()` runs main-thread (no `workerUrl`), so
  no worker concern is expected — smoke-checked at verification.
- **The dev mount is deleted, not flag-gated** — rejected keeping a
  two-class HMR special case in favor of solving the root problem: an
  in-place workdir **repin** (`fxtr_workdir_harness` grows
  `repin_workdir` + a CLI; jj `@` → mirror push → marked-line rewrite →
  `nix flake lock` → workdir commit) plus one demo script
  (`reload_renderers.py`) that calls the dispatcher's existing
  `set_preset`; the frontend's existing `ReloadEverythingNotification`
  subscription reloads open panels in place, with panel state surviving
  via `ownStorage`. `esbuildRenderer` sources get `builtins.path`
  content-addressed narrowing so a renderer-only edit rebuilds one
  slice; `mkVenv` narrowing is measure-first.

### 2026-07-17 — phase 5 (dataframes) complete

The whole dataframe subsystem is implemented and current-state
documented (`notes/dataframes.md` is the canonical statement; the
per-chunk rationale trail is `plans/phase5_dataframes/`). The
decisions that firmed up during implementation, beyond the 2026-07-16
round and the amendment below:

- **Frame cell ops** (chunk 3): `FrameWireType` = arrow IPC stream
  bytes; identity minted host-side through one funnel
  (`tables.canonical_frame_from_ipc`); the session-owned projection
  (bytes-budget LRU behind a persistable seam; rebuilds are a
  *trusting* decode of the session's own store — the strict loader
  stays for trust boundaries); the executor canonicalizes frame
  outputs in the results loop AND before the safety pass's output
  comparison (replays legitimately permute rows); `read_batch`
  deleted from both protocols.
- **Gather** (chunk 4): one wire op, positionally-named deduped key
  table in → one host-stitched canonical stream out; request edges
  committed up front; misses resolved **concurrently** (the in-flight
  map dedups shared demands); **per-request failure semantics** —
  each failing cell records its own failure row, and the op raises
  one synthesized `GatherFailedError` (never persisted; the group
  discipline catches only all-`Exception` groups). The user twin
  applies the single-read rules per cell, deliberately without
  auto-target sugar.
- **Frame rendering** (chunk 5): **no `CellReport` preview variant**
  (the earlier sketch was struck): a frame cell's report carries the
  frame entity id like any value, and the dashboard renders the table
  by loading the self-describing entity through ordinary entity
  access (`frontend/src/frame_table.tsx` — drawer embeds + `frame_v0`
  dispatch renderers, live once the deferred nix wiring registers
  them).
- **Flagged for a follow-up design round: targeting as expressions**
  (per-cell target records replaced by a demand-expression DSL,
  possibly demand-without-target + an unwanted warning) — sketch in
  `plans/phase5_dataframes/discussion_log.md`.

### 2026-07-17 — phase-5 amendment: the frame value is `pl.DataFrame`

Landed with chunk 2 (the table runtime). Amends the 2026-07-16 round
below: the `Table` wrapper ABC is **deleted** — relational data moves
through the calculus as a raw `polars.DataFrame` at every table
position (reads return one; writes / gather / task frame outputs accept
one). Canonical statement: the frame-value + write/read sections of
`notes/dataframes.md`; full rationale + the compat argument:
`plans/phase5_dataframes/discussion_log.md` (the 2026-07-17 amendment).
Highlights:

- **Why no wrapper.** Its carried schema was near value-free (the
  declared schema governs writes; a table's own flags were advisory),
  its interop surface just reimplemented polars, and it forced gather's
  driving-frame payload columns to be representable as storage scalars.
  Raw polars gives duckdb/arrow interop for free and unconstrains
  payload dtypes.
- **Compat is preserved by confinement.** `pl.DataFrame` lives only in
  the view layer (client-side of the wire, task and user side); the
  wire carries arrow bytes and identity is the polars-free canonical
  CBOR entity, so a future frame-type change is a client-side source
  change, never a runner/persistence break.
- **Landed shape.** `tables.py` is the conversion/validation layer
  (dtype map, `make_frame`, IPC↔frame, frame→rows, `validate_and_align`
  — names + exact dtypes + `Null`-cast + native null-legality); polars
  is the sole required table dep, used eagerly only there (core stays
  polars-lazy). **duckdb and pyarrow are opt-in, not runtime deps**
  (they sit in the dev group): the framework's frame handling and all
  native polars querying are pyarrow-free; pyarrow is needed only for
  `df.to_arrow()` / `duckdb.sql("from df")`, so a duckdb-using task
  author installs both while a polars-only task needs neither.

### 2026-07-16 — phase-5 design round: dataframes

Design round settling the dataframe subsystem (canonical statement:
`notes/dataframes.md`, new, pointed at by the design overview's
decision map; working log with the full alternatives trail:
`plans/phase5_dataframes/discussion_log.md`; implementation plan:
`plans/phase5_dataframes/overview.md`). Highlights:

- **Scalar-only columns** — `ColumnKind` drops `entity`, gains
  `bytes`; frames become leaf entities (opaque-bytes transport
  possible, no loadability obligations through relational ops);
  `DataView.gather` deleted (nothing left to return).
- **Three representations, one identity**: the owned canonical CBOR
  entity (fingerprints/conflicts/replay/export unchanged), arrow IPC
  as the never-identity-bearing transport, a polars-backed `Table`
  handle; the runner keeps a derived projection cache (v0 in-memory
  LRU). polars becomes the required table dependency;
  duckdb/pyarrow never required (IPC + `__arrow_c_stream__`).
- **Multiset identity kept, framework-owned canonicalization** —
  rejected delegating row-order determinism to programs (the
  concurrency doctrine blesses completion-order assembly; engine
  output order isn't stable across versions); the sort spec is
  trivial by construction: lex on canonical per-row encoded bytes.
- **The scalar encoding spec**: exact-type strict; single canonical
  NaN, `-0.0` preserved, shortest-form floats; int64 pin;
  per-column nullability defaulting to **non-nullable** (FrameSchema
  is a storage contract, not a computation layer).
- **Gather = host-stitched stream + thin local key-join**: deduped
  key table in, one canonically-ordered stitched stream out
  (observations per distinct cell recorded host-side); the local
  key-join carries no system-state weight. Frame entity ids never
  surface to task code, and canonicalization is host-side by
  necessity (the canonical entity embeds the *declared* schema) —
  no identity code in sandboxes.
- **Table API**: exact dtype mapping (no implicit casts near
  identity); a `Table`'s nullability flags are advisory (the grid's
  declared schema governs writes; validation = names + kinds + data
  legality); writes align columns by name; in-memory tables have
  definite deterministic order (multiset semantics live at the
  storage boundary only).

### 2026-07-12 — phase-4 redesign round: watch subscriptions

Design round replacing the wire-level hint-refetch flow with **watch
subscriptions** (canonical statement: `notes/event_system.md`, plus
the watch sections of `notes/monitoring_ui.md` and the design
overview's monitoring bullets; plans: chunks 5–6, with the task-log
chunk renumbered to 7). Motivation: the refetch flow caused visible
churn (whole-view reloads, flashes, redundant transfers) and pushed
clearing/debounce/replan logic into the renderer.

- **Hints stay; the wire changes.** The ambient hint collector and
  per-grid emission precision (the 2026-07-10 round) are kept
  unchanged as the internal change-detection layer. What was rejected
  is shipping hints to the browser: instead a per-mount **watch
  engine** recomputes hint-affected queries server-side, diffs against
  the last-sent state per subscription, and publishes keyed patches.
  Key insight making this sound with no new invariants: diff-vs-last-
  sent is state reconciliation, so at-least-once/over-triggered hints
  stay safe, and the initial snapshot is just the first diff from an
  empty mirror (no snapshot/stream handoff; the hub's `seed` param
  deliberately unused).
- **Delivery leans on existing contracts**: queue-mode `SubscriberHub`
  (FIFO, lossless, crash-backstop kills the subscription rather than
  skipping — so a live stream never has gaps), the workstation inbox
  parker, and the session reducer's `CloseCallbackOp` on unreferenced
  inbox slots — dropping a per-view inbox stub IS the unsubscribe, so
  the client's watch set is just its state. One small workstation
  extension: `subscribe_to` grows an `args` param (the `method` param
  precedent, from the channels frontend).
- **Three path-addressed families** (per-family methods over one
  union-spec method — more typed, self-documenting):
  `watch_workflow` (summary + lint + targets + sourcing + bindings —
  coalesced because all are workflow-level and always-visible; lint
  triggers widened to `grids`/`cells`, fixing an under-trigger the
  refetch flow had), `watch_cells(path)`, `watch_cell(path, coords)`
  (one method for all cell kinds; payloads kind-discriminated like
  `CellReport`). Absolute path/coords addressing (the introspection
  doctrine's root-frame vocabulary) over ViewSels — watches survive
  subject identity changes, with explicit `subject_gone` tombstones.
- **Grants ride the watch payloads** (own grant + parent token, minted
  through the attach walk logic): mutations keep speaking ViewSel, but
  the renderer's attach cache / token-heal layer is deleted — token
  lifetime aligns with watch lifetime by construction. Consequence:
  `attach_path` loses its only consumer and is deleted; the wire-level
  `subscribe` is deleted too (watches are the one subscription
  surface).
- **Event schemas are typed unions** in a leaf `watch_api.py`
  (mashumaro, `kind`-discriminated, per-variant docs), TS-mirrored and
  pinned by the dialect harness. Shared vocabulary: patch batches
  (keyed upsert/remove + scalar replace), `recomputing` + empty-batch
  settle (slow passes get a UI working indicator), `subject_gone`,
  log events.
- **Step logs pulled forward** from the task-log chunk: step
  `log()` today lands unaddressed and unread in the passive state-db
  `events` table — rerouted to a runtime-only per-cell buffer
  (restarted per run, retained after run end so errored runs stay
  inspectable, never persisted), streamed **event-native** through the
  cell watch (buffer contents in the first batch, appends live). This
  bakes the event-native seam the chunk-7 channel work
  (`fxtr2.common.channels` + `rd-types-channel` — already existing
  machinery) plugs into. Raw print capture rejected for v0 (task fns
  sit behind service boundaries; `redirect_stdout` is process-global
  under concurrent runs).
- **Run-log follow-round (same day)**: the buffer is
  **executor-owned** (the executor owns the run lifecycle, is keyed by
  the watch addressing, and is reachable from the capability impl for
  the engine's seam), with the write path a per-run **sink threaded
  through `StepHost`** — the safety pass's frozen engine gets a
  discarded sink, making replay silence structural. Entries are
  structured (ts, kind, optional step identity, message); besides
  step `log()` (tagged with its invoking step), the ops engine emits
  **lifecycle entries** from its effectful ops (wrote/read cell,
  demanded task output, step started / record-hit) so a running
  cell's stream shows progress without task-code cooperation. The
  `events` table stays untouched minus the step-log call: it was
  always meant as a jj-style **op-log** for durable mutation/lifecycle
  provenance (the importer's `import` row is its real current user;
  step output there was a miscommunication) — a TODO on the schema
  records the future op-log entries.
- **Renderer policy calls**: drop watches immediately on
  navigate-away (LRU cache only if it proves painful); the refetch
  buttons are replaced by a single resubscribe escape hatch.

Scoping call at chunk close (canonical statements: the deferral notes
in `plans/phase4_monitoring_ui/renderer_consolidation.md` — formerly
`deferred_nix_wiring.md`, rescoped in the 2026-07-17 renderer-
consolidation round above, which also carries the implementation
handoff — plus the TODO in
`notes/monitoring_ui.md`'s packaging section and in
`frontend/LOCAL_CODE_OVERVIEW.md`):

- **The renderer's nix slice + demo preset wiring was split out of
  chunk 4** rather than blocking its completion. Rationale: the next
  phase of work is iterating on how the dashboard *feels*, and the
  dev mount's Vite HMR loop is the fast path for that — a nix-mounted
  panel adds a monorepo-commit → re-pin → rebuild cycle per
  iteration. The packaging design itself is unchanged
  (`notes/monitoring_ui.md`); only the implementation is deferred.
- Accepted consequence, until it lands: no nix build coverage for the
  renderer package (bit-rot guards are the repo tsc/biome/vitest and
  the dev mount), and the demo dev mount is the only mount.
- Assessment recorded at deferral time: the work is well-precedented
  (the `esbuildRenderer` externals already cover every bare import
  incl. `jotai-effect`; the demo flake's merge seam is waiting; a
  `fxtr_lib_type_ids`-style stub mirror yields a byte-identical type
  entity if the real-venv route rabbit-holes).

### 2026-07-10 — phase-4 chunk 2 design round: the hint-emission mechanism

Settled mid-implementation, in discussion (canonical statements: the
`subscribe` docstring in `user_api.py`, `change_hints.py`'s module
docstring, and the change-events section of `notes/monitoring_ui.md`):

- **Ambient contextvar collector, not signature plumbing.** The plan's
  "the body accumulates hints; the session flushes post-transaction"
  had an implementation fork: threading touched-grid sets out of the
  stamp-clearing walks through every mutating body's signature was
  rejected (churn in the hottest code); an early proposal to instead
  *weaken* the hint semantics (name only the directly-mutated grid,
  making any hint mean "refetch everything visible") was rejected too —
  it forfeits selective refetch entirely. The chosen mechanism keeps
  the plan's per-grid precision at zero signature cost: a leaf module
  (`change_hints.py`) holds a ContextVar'd mutable hint set;
  `WorkflowSession._run` (and the flip choreography's final
  transaction) installs and post-commit-flushes it; any call site —
  session bodies, `mark_grounding`'s four clear branches,
  `_propagate_clears` — just calls `note_change(kind, path)` locally.
  anyio propagates context into `to_thread` workers, so worker-thread
  bodies write the same set the async side flushes; rollback drops
  hints structurally (an exception skips the flush).
- **A cleared stamp IS a hint.** The walks emit `("cells", grid)` for
  every grid whose currency/grounding/wanted stamps they clear —
  "displayed verdict may change" — so a mutation's transitive
  invalidation fan arrives as precise per-grid hints and the dashboard
  refetches only affected grids.
- **Deliveries are batches.** The hub's per-subscription "coalesce"
  mode keeps only the *latest* pending event (distinct hint kinds would
  be dropped), so the capability-side broadcaster window-dedupes
  (0.05s) and publishes one list-of-hint-dicts per flush over a
  queue-mode `SubscriberHub`, `origin_scope=None` (payloads carry no
  ids, so deliveries never depend on any workflow scope's liveness).
- **Emit-on-change**: idempotent mutations are silent — found by the
  silence test: a same-value write's sourcing re-upsert re-pins an
  identical fingerprint and must not emit `intent`.
- **Seam shapes**: `WorkflowSession.set_event_listener` (ONE listener;
  fan-out belongs to the hub behind it; called post-commit before the
  lock releases, must not block) and
  `BindingRegistry.set_change_listener` (the `bindings` hint —
  registry-side because bindings are not session state; auto-release
  emits too since release goes through the registry). Both are wired
  for the mount's lifetime by the capability's new
  `_fxtr_mount_lifespan`.
- **The subscriber teardown contract** (found by the RPC smoke test
  hanging at teardown, not in delivery): a live subscription
  cross-pins the two sides' scopes — the source side holds the
  subscriber's bridge, the subscriber side holds the unsubscribe
  handle — so a scope hosting both must not be gracefully drained
  while the subscription is live; it parks on the cycle forever. The
  sanctioned client patterns are explicit `unsubscribe()`, or holding
  the handle in a shorter-lived scope so its release (the hub's
  handle-drop teardown) precedes the subscriber scope's drain; the
  workstation inbox parker (`fxtr_workstation/callback_impl.py`)
  documents the same cycle. Canonical statement: the `subscribe`
  docstring's Returns section.

### 2026-07-09 — phase-4 scoping: the monitoring UI

Scoping round for the dashboard phase (canonical statement:
`notes/monitoring_ui.md`, linked from the design overview's decision
map). The shape: a workstation FullPanel renderer keyed on the user
capability's service type id — a key-path tree with rollup badges, a
grid detail view (config via dispatch on the stored config entity +
value-free cells table), and a per-kind cell drawer.

- **A UI-facing gateway service was considered and dropped.** The
  proposal (path-addressed facade re-shaping the capability for the
  browser) rested on two mistaken premises: that the workstation invoke
  layer can't carry typed dataclass args (it can — impl-side dispatch
  decodes wire JSON via the per-method args decoder, and returns encode
  symmetrically), and that grant tokens were awkward for panels (they
  aren't — panels persist server-side state, and tokens are treated as
  a re-derivable cache: re-root on invalid). The renderer drives
  `ServiceRef[UserCapabilityProto]` directly; new queries land on the
  capability itself, preserving the one-capability doctrine.
- **Capability additions settled**: `summarize` (one-call grid rollups;
  currency force-evaluates, riding the stamps), `config_id` exposure,
  `inspect_generation` (observations with per-edge match status,
  failures, checkpoint entries — absorbing the deferred step-entry
  introspection), `list_observers` (reverse edges), `list_bindings` +
  handle-cell boundness, `delete_cell` (data/handle; the one missing
  mutation — only `overwrite` existed), and `subscribe` (fxtr2
  `EventSource` contract, so the workstation's `subscribe_to` idiom and
  reconnect resilience apply for free).
- **Change notification = coalesced invalidation hints** (grids /
  cells-per-grid / intent / bindings), at-least-once, no payloads —
  refetch-to-reconcile. Chosen over renderer-side interval polling (no
  precedent in the frontend substrate; everything is push) and over a
  richer event vocabulary (hints make missed events safe by
  construction).
- **UI doctrine calls**: cells table stays value-free (bulk
  values-with-verdicts `inspect_cells` noted as the sanctioned future
  shape if previews prove needed); the demand button never auto-targets
  silently ("Target + run" vs "Run", `adopt` prompted explicitly;
  "Retry" vs "Clear + rerun" for errored cells).
- **Renderer lives in-repo** (`projects/fxtr_experiment/frontend/`,
  esbuild-bundled, preset slices reference it from the repo nix lib like
  the builtin slice) rather than in a demo workdir — this is the
  standard experiment dashboard, not demo code.
- **Demo topology**: `examples/fxtr_experiment_demo` hosts a persistent
  runner (session + executor + capability, sqlite under the workdir)
  host-mounted in the orchestrator via the mount-lifespan pattern, plus
  a demo-driver service whose scenarios build real state through the
  same capability; sandboxed task fns are flag-gated and agent-driven,
  exactly as in the workstation demo.
- **Task-level logs** (runtime-only per-run channels for tasks, steps
  routing into them) were sketched in the note but deferred to an
  optional last work item; queue/waiting visibility deferred until
  scheduling has a waiting notion.

### 2026-07-09 — phase-3 chunks 4 + 5 landed: import + the suites (phase complete)

Import landed per the pre-chunk calls (canonical statement: the
import-semantics section of `notes/at_rest_format.md`): three phases
with nothing written until everything is proved (strict parse with
file/row-named errors; store presence with all misses in one
`MissingEntitiesError`; checked-redundancy + cross-row laws against
decoded truth), then one locked destructive transaction
(empty-or-``force``; **the events log survives the wipe** — provenance
is history, not state — and the import appends its own event; fresh
exec_ids, re-interned versions, rebuilt derived tables, zero stamps).
Strictness extends to file canon: unsorted/duplicate rows and
present-but-empty TSVs refuse, so hand-edits must preserve the
canonical form byte-identity depends on anyway.

The chunk-5 suites pin the phase's contract end to end
(`tests/test_at_rest_e2e.py`): the messy-state round-trip — errored,
interrupted, orphaned, dangling, internals + checkpointed steps —
imports **verdict-for-verdict** (LintReport/CellReport dataclass
equality, diagnosis strings included) and re-exports **byte-for-byte**;
and the real-git arc — commit A partial task1, commit B mixed
cached/new (the A→B diff asserted file-precisely), `git reset --hard` +
force-import both ways: restore-to-completed serves with zero re-runs
(result fidelity), restore-to-partial re-runs only what A lacked and
regenerates B's values (definition fidelity; byte-equal except the
re-run generations' fresh uuids). Discoveries worth keeping: the
export dir can literally be the git repo (dot-entries are invisible to
the ownership validator, so ``.git`` rides along), and bindings survive
in-process imports (connectors key by content-addressed handle ids).

This completes phase 3; `notes/at_rest_format.md` is fully implemented.

### 2026-07-09 — phase-3 chunk 3 landed: export (+ the ownership/marker round)

Pre-chunk round resolutions (canonical statements in
`notes/at_rest_format.md`):

- **Directory ownership is proved, never assumed** — upgraded in-round
  from my manifest-presence check to a full allowlist walk: every
  non-dot entry must be a known layout member or the export refuses
  wholesale with nothing touched (`ExportLayoutError`). Misapplying the
  export-owned doctrine is a safe error, not a destructive sweep.
  Dot-entries (macOS droppings) are invisible — never block, never
  written, removed only with a removed grid dir.
- **The `EXPORT_IN_PROGRESS` marker** (written first, removed last,
  self-explaining text) makes a crashed export legible instead of
  engineering pseudo-atomic tree swaps; git is the recovery. Import
  will refuse on a present marker.
- Rode along: export returns counts (`ExportReport`, symmetric with
  import's future report); tomlkit for TOML writing (already an fxtr2
  dep); the snapshot is one locked session op
  (`WorkflowSession.export_snapshot`, `NotAtRestError` checked
  atomically) with rendering in a worker thread outside the lock.

### 2026-07-09 — phase-3 chunk 0 landed: the at-rest format spec

The format round (canonical statement: `notes/at_rest_format.md`;
resolutions also in the chunk file):

- **The git tree mirrors the grid tree** — one directory per grid,
  per-grid files (`grid.toml` + omit-if-empty coords-keyed TSVs), intent
  files per-grid too (they scale with cells). Motivated by row volume
  (one global `cells.tsv` mixing every grid would be unwieldy) and by
  the pleasing consequence that subtree/internals nesting is literal
  directory nesting.
- **The key-charset law**, enforced at creation from now on
  (`encoding.validate_grid_key` in `create_grid` — a chunk-0 code
  ride-along so the note states current truth): keys match
  ``[a-z0-9][a-z0-9_-]*``, maximally strict to start; lowercase-only
  avoids case-insensitive-filesystem collisions; no ``.`` makes
  reserved-filename collisions impossible. All existing keys conformed.
- **TOML for non-row files** (manifest, `grid.toml`), headerless sorted
  TSVs for rows; checked-redundant readable columns (kind, generation
  uuid, status, target coords) verified on import, never trusted — the
  uuid column kept specifically so retries read in git as the same
  generation with a new record id (turnover as a new generation);
  failure timestamps stay in the record.
- **Findings**: ``pending`` never exists at rest (generations insert
  running); ``step_type_id`` currently violates the extraction
  contract (`define_type` stores type entities into GLOBAL_SCOPE only)
  — the persist fix is assigned to chunk 1 and TODO'd in the note.

### 2026-07-09 — phase-3 scoping: persistence as (git repo + entity store)

Scoping round for the next phase (plan dir:
`plans/phase3_persistence/`, whose overview carries the settled
decisions until chunk 0 lands the standing note
`notes/at_rest_format.md`). The shape: bidirectional at-rest
import/export between the state db and a directory of sorted
canonically-encoded TSVs referencing the *same* entity store the
session runs against; git commits are the version roots (a
manual-granularity v1 of the deferred version log).

- **Own format, not the pin-namespace TSV format** — the existing
  `fxtr2` tsv-namespace is strictly two-column key→id with colon-free
  keys; our identity texts don't fit, and v0 doesn't need its tooling.
  Kept: the conventions (sorted rows, canonical encodings, byte-stable
  re-export). Replacing the closure-root role of pins: the
  **extraction contract** (every referenced id in a schema-declared
  id-typed column; the store's complete-subgraph invariant supplies
  closures). A hybrid standard-namespace layer was considered and
  dropped.
- **Flip-time generation records** (chosen over export-time assembly
  and lazy-on-request): one canonical autoincrement-agnostic entity
  per non-running generation, stored when status leaves ``running``
  (run end, startup flip, retry re-flips), pointed at by a
  ``record_id`` column. Motivation: export stays O(rows); ride-along:
  git history of ``generations.tsv`` keeps superseded record ids
  findable — history at commit granularity.
- **Import is a destructive capability method** (sets current state;
  empty-or-``force``; backup is doctrine — export first, git is the
  backup); counts-only ``ImportReport``; everything imported
  unstamped (verdicts are genuinely derived; `lint` is the
  what-did-I-just-load check); dangling outstanding grants accepted
  (the `delete_grid` analogy).
- **No snapshot root entity, no workflow-uuid check** — both conceded
  as redundant: git commits identify versions; the missing-entity
  validation already catches wrong-store imports. Manifest = format
  version only. Events stay local-only.
- **A root-state subset-import scenario was rejected** (import ≠
  replay-install: grids come from `grids.tsv`, not from sourcing
  records' pinned configs — that affordance belongs to a future
  replay/version-log phase; and the true root state is a row subset,
  not a file subset). Replaced by the real-git e2e: a
  partial-then-completed ``data → task1 → task2`` arc pinning both
  restore flavors — *result fidelity* (imported completed state
  serves with zero re-runs) and *definition fidelity* (imported
  rules/wiring/intent are executable: a hard reset onto a partial
  state re-runs the rest to content-addressed-equal values).

### 2026-07-09 — phase-2 chunk 5 landed: the structural linter + suites (phase complete)

Scoped in two rounds the same day (canonical statements: the linter
section of `notes/grounding.md`; the chunk file carries the trail):

- **The linter is structural — staleness is out.** First scoping had the
  target checks as complete + fresh + grounded ("contract-clean"); the
  unmaterialized-target question broke it (grading a never-run target
  needs completeness, completeness drags in freshness, and neither is a
  construction problem). Re-scoped: lint findings mean "not constructed
  properly" — grounding + dangling intent only, fixes not reruns;
  staleness stays currency's per-cell operational question. The linter
  checks the structural half of the replay contract's hypothesis.
  Corollaries: an attached target with nothing materialized is *pending
  intent*, never a finding; a lint mid-run is typically green (request
  edges self-vouch), so red genuinely means broken.
- **Two-level green**: *sound* (no dangling intent, no red targets —
  root checks suffice by cone-transitivity) / *clean* (sound + empty
  ungrounded inventory ≡ the global structural hypothesis). The
  inventory is neutrally "every non-grounded fact": prune candidates
  exactly when sound; while red it also carries the causes (no cone
  membership computed).
- **Shape**: one locked session op (`WorkflowSession.lint`; enumeration
  in the new `lint.py`, verdicts via the shared `_grounding_locked` —
  a ride-along refactor deduplicating `cell_snapshot` / `serve_check` /
  `gated_read_cell`), served as `UserCapabilityProto.lint` →
  `LintReport` (sectioned: dangling sourcing / dangling targets /
  target findings / ungrounded), `Workflow` pass-through, RPC-smoked.
- **Dropped**: the chunk's write-grant tightening hint (no lint check
  at all), and the adoption e2e scenario (covered by
  `test_sourcing_surface.py`; the new angle became the
  adopted-grid-with-unsourced-parent unit test).
- **The suites**: seven per-removal-kind unit tests + two lint-arc
  scenarios (`tests/test_lint.py`) — replay-in-place re-creation (a
  task re-creates its deleted grid on re-demand, fingerprints match)
  and ordering-fault convergence (side-written data; fault → demand
  writer → re-demand → clean). Implementation found one shape
  correction, recorded in the chunk notes: after delete + clear the
  state is sound-but-not-clean (the dependent's rule reference stays
  unanchored until the creator re-runs), and recovery must demand the
  creator first — the reader's mint faults before its token read could
  cascade.

This completes phase 2; `notes/grounding.md` is fully implemented.

### 2026-07-09 — phase-2 chunk 3 landed: request edges (option B) + the safety-pass correction

The gates landed in their final form after a design round across two
sessions. The stable parts match the standing resolutions (strict/off
knob on `Executor`/`open_local`; serve-or-fail single ops —
`WorkflowSession.serve_check` for task-output serves, `gated_read_cell`
for data/handle reads via an executor-provided `ReadGateHook`, the
run-prologue handle read *and the user surface's data/handle reads*
included; `UngroundedSubjectError` fault-class with diagnosis chains;
mint faults enriched with the grid's grounding reason — `ViewAxisError`
mint failures against an ungrounded grid convert to the fault-class
enriched form, since an unmintable-because-drifted world is a not-ready
dependency, not task error). Engines constructed without the executor
default to an ungated read path (`read_gate=None`), so white-box engine
harnesses are unaffected; executor-driven harnesses seed sourcing +
targets to run under the strict default.

**The self-vouching mechanism was redesigned mid-chunk: requests are
recorded at commitment (schema v6 ``request_edges``), replacing the
first implementation's executor-threaded pending-edge chain.** The
forcing problem (the worked example in
`plans/phase2_grounding/chunk3_gates.md`): in a fresh cascade nothing
has resolved, so under resolution-time recording every demander's
wantedness rests on an in-flight edge one level up, and the evaluator's
durable facts are silent about *why* a cascade member runs. Option (A)
threaded the whole in-flight chain through every demand/read hook, with
a derive-only evaluation mode that wrote no stamps and distrusted
negative ones. Option (B) — chosen — writes a durable
``request_edges`` row at record-**miss** time, before the op goes live:
`wanted` (and `grounded_task_cell`'s input conjuncts) read the request
record, the whole chain apparatus disappears, lint/UI can see in-flight
demand, crash behavior is uniform (edges are durable at commitment;
(A)'s call-stack chain simply vanished with the process), and the two
axes split cleanly — currency reads the resolved-value record,
grounding reads the request record.

**A safety-pass gap found while confirming (B)** (Daniel's catch): the
plan's claim "complete ⇒ requests ⇔ resolved observations, so the
safety pass needs no new comparison" was circular — the equivalence is
a determinism property across resumes, exactly what the pass verifies.
The failing case: a request aborts unresolved (edge, no observation),
the resumed run nondeterministically *skips* it and completes — the
dangling edge sat in no compared set and would survive as a phantom
``wanted`` fact. Fix: the pass compares ``request_edges`` against the
record's read-class cell projection (pure DB-side, write-free), under
the now-explicit invariant that **every durable per-generation fact is
compared at completion** (`notes/execution_semantics.md`). Regression
pair in `tests/test_executor.py`.

Rode along: stamped negative verdicts now serve their stored diagnosis
(the loaders carry ``grounding_reason``; reports no longer say
"(stamped)"), and `tests/test_gates.py` carries the seven gate
scenarios.

### 2026-07-07 — phase-2 chunk 4: the user surface (resolutions + landing)

Scoping resolutions (full statements in
`plans/phase2_grounding/chunk4_user_surface.md`; canonical writeups in
`notes/grounding.md` + `user_api.py`):

- **User demands require attached targets, capability-enforced**
  (`NotTargetedError`), superseding the previous round's "emergent from
  the gate" framing: the emergent version left a side channel (a cell
  wanted through another task's records would pass the gate for a demand
  the user never declared) — *the method called declares the intent; the
  state never substitutes*. Corollary: internals outputs are never
  demandable from the top (internals grids are never sourceable →
  never targetable); internals browsing is `inspect_cell`-only as law.
- **Adoption pins what is stored** (`source_grid` = current config,
  `source_cell` = current value; no proposed-config sourcing);
  `source_grid` refuses task-internals grids.
- **`inspect_cell` serves one snapshot across both axes** (the combined
  `cell_snapshot` op — value + currency + grounding + wanted; the gate
  chunk reuses the composition for serve-or-fail).
- **`delete_grid` returns a `DeleteReport`** (referrers from the reverse
  index, pre-delete; proceed-and-report).
- **`list_sourcing` mirrors `list_targets`** (attached /
  dangling-missing / dangling-mismatched).
- Rode along: `adopt=` on writes (explicit-opt-in adoption beside
  ``overwrite``), idempotent writes upserting sourcing (the chunk-1
  leftover — writing is sourcing whether bytes changed), the demand
  wrapper's auto-target default, and grounding verdicts in
  KeyInfo/CellInfo/CellReport/TargetInfo.
- **Auto-adopt-by-default rejected; `adopt=` kwarg spread instead** (to
  `add_target` + the demand wrapper): auto-target transcribes a
  cell-level act into a cell-level record, but adoption is a grid-level
  ownership claim (replay installs the grid by user fiat) that a
  cell-level act must not manufacture silently — it would hollow out the
  grid-before-cell invariant on the default path, with subtree-wide
  blast radius on revocation; and the friction it removes is rare
  (cross-ownership acts) rather than the common path auto-target fixed.
  One-word recovery via the kwarg; a construction-level knob is the
  future escape valve if earned.

### 2026-07-07 — gate-round resolutions: auto-target, two modes, chunk swap

Pre-implementation round for the gate work (canonical statements updated
in `notes/grounding.md`'s gate table + "maintaining the root facts" +
recorded alternatives; plan files reordered):

- **User demands do not self-vouch** (nothing durable justifies them —
  the principled asymmetry with task requests), so strict faults
  untargeted user demands of task cells. Resolution: **demands
  auto-target by default at the `Workflow` wrapper** (`target=False`
  opts out; the wire capability stays mutation-free; event-logged).
  The earlier auto-target rejection was about *inspection*, which the
  labeled channels now cover — demands are programmatic driving, i.e.
  intent. Accepted cost: target accumulation from exploration (visible,
  removable, linter-surfaced).
- **Launch-requires-target became emergent**: strict gate +
  non-self-vouching demands already reject launch-without-target
  (`target=False` on a never-run unwanted cell faults at the gate before
  executing); the separately-enforced precondition is dropped.
- **Two gate modes, not three**: `strict` (default) / `off`; the
  local-only middle dropped (protection beyond off ≈ authorless data
  only; relief versus strict ≈ tolerating downstream-bookkeeping flap);
  the orphaned verdict survives as diagnosis.
- **Serve-or-fail in one op**: no check-then-use window — one session op
  evaluates both axes under one lock hold and serves the value/results
  from the same snapshot or raises. Serve time = re-check-on-arrival
  (mid-run revocations fault the demander at arrival, like currency).
- **Chunks 3 and 4 swapped**: the user surface (pure vocabulary
  additions) lands before the gates (the behavior change), so the gate
  chunk's test updates can target/adopt/inspect with real API.

### 2026-07-06 — phase-2 chunk 2: the derived predicates (resolutions + landing)

Scoping resolutions (full statements in
`plans/phase2_grounding/chunk2_derived_predicates.md`; the canonical
writeup is `grounding.py`'s module docstring — the four-part correctness
argument (invariants; state ↔ datalog implications; per-mutation
obligations; per-query correctness) is itself a chunk deliverable):

- **Faithfulness bar**: implementation matches the datalog exactly;
  surprises pause the chunk. (None arose — the one open risk,
  `mint_compatible`, resolved by re-performing minting's own structural
  checks (`spec_from_binding` / `spec_from_task_fn` + declared-output +
  handle-kind checks) against config *entities*, whose immutability
  makes the two-phase load under one session-lock hold sound.)
- **Status-blindness written down**: a new "Execution status and the
  predicates" section in `notes/grounding.md` — running = younger
  monotone estimate; interrupted = deterministic prefix the fixpoint
  resumes; errored = deterministic prefix before a reproduced error;
  plus the sharpened estimate-honesty caveat (claims from non-complete
  generations rest on safety-pass-unverified determinism).
- **Bidirectional component, currency-hybrid shape**: Python frontier
  loader (heterogeneous node kinds) pruning at stamped facts; naive
  monotone iteration; stamp-back of the whole derived cone in the same
  transaction; running generations never stamp their own verdicts.
- **The I2-free prune invariant P** ("every stamped fact's justification
  cone contains only stamped facts and running generations") with the
  would_start fan as the one propagation subtlety — would_start is never
  stamped, so clearing its inputs fans to its dependents without
  consulting the generation's own stamp.
- Verdict shape as planned: tri-state stamps on the fact's own row;
  `wanted_stamps` location-keyed (turnover of a cell's own generation
  leaves its wanted stamp untouched — verified in the smoke).
- The evaluator's self-vouching seam for chunk 3 is deferred to chunk 3
  itself (the two-phase session entry point is where the pending-edge
  parameter will thread through; nothing in the evaluator's shape blocks
  it).

### 2026-07-06 — phase-2 chunk 1: base facts (scoping resolutions + landing)

Scoping resolutions (full statements in
`plans/phase2_grounding/chunk1_base_facts.md`; canonical schema writeup
in `storage.py`'s docstring, semantics in `notes/grounding.md`'s updated
status/claims sections):

- **Grid-before-cell is a maintained DB invariant**, not an add-time
  guard: revoking a grid's sourcing refuses while cell-level intent
  (cell sourcing, targets) exists, unless the same transaction cascades
  them — the primitive under the user surface's future `force` toggle.
  App-enforced (the grid row is the `coords=''` row of the same table —
  no FK can express it); intent rows survive grid deletion as dangling
  (targets doctrine, one level up).
- **User writes into un-sourced grids are rejected** (pre-checked before
  the write, so refusal leaves no orphan value); an `adopt`/`force`
  keyword on the write UX is deferred to the user-surface chunk.
- **Assertions are their own table** (`grid_assertions`, INSERT OR
  IGNORE) rather than a flag on observations: the monotone-OR
  accumulates as idempotent generation-level facts — record hits
  included — without bending "hits never touch the record"; the frozen
  engine collects without inserting and the safety pass compares the
  set (a new comparison point).
- **`responsibility` dropped entirely**: task write claims are the
  `wrote_cell` observations themselves; grid claims are the assertion
  rows; user claims are the sourcing records. One less dual-bookkeeping
  surface; `ResponsibilityRecord` deleted from the model.
- **Reverse rule-ref index stores terminal slice paths only**
  (`model.rule_reference_paths`); enclosing-subtree breakage rides
  grounding's parent clause, and rmdir-style deletes hit the terminal
  row first anyway. `targets` gains denormalized `grid_path`/`coords`
  columns for the invariant checks and by-grid queries.

### 2026-07-06 — phase-2 chunk 0: opens follow the chain

The open-modes design round (inventory + analysis + decision trail in
`plans/phase2_grounding/chunk0_open_modes.md`; canonical statements in
`notes/design_overview.md`'s views-and-wiring section and the module
docstrings). The inventory found mode doing two different jobs — plumbing
authority (restating what the grant chain already determines) and
declared write-intent in rule wiring (`SliceBinding.mode`, the one
identity-bearing site) — and the settled design separates them:

- **Opens carry no mode** (the `mode=` params are deleted from every
  open/attach on the view layer, the run capability, and the user
  capability); grants follow the chain's authority. All structural
  invariants were verified mode-independent (keyed on sliced-ness,
  internals-ness, kind, performed ops).
- **`read_only()` is a holder-local privilege drop**, enforced by the
  view layer, not capability-level; the `read_only()`-before-`at()`
  ritual is gone (`at()` sheds create authority itself). Received rule
  bindings remain capability-enforced (boundaries enforce; self-
  discipline advises).
- **Wiring populates from the local flag** — so the wiring default flips
  to write for undropped creator-side views. Accepted, with the
  full-frame check failing loud for subtree inputs and a chunk-5 linter
  hint ("write-granted input no generation ever wrote") for leaves;
  sticky-identity caveat noted.
- **`asserted_grid` = open through write authority**, recorded
  monotone-OR per generation (no per-op payload mismatch rule); a
  reader-through-write-chain is a faithful co-claimant. Grounding-note
  wording updated; chunk 1 implements the bit accordingly.
- **The explicit-affordance remnant**: user `open_handle` takes
  `create: bool = False` (authority minting stays deliberate); user
  `attach` loses its mode param. Alternatives considered and rejected:
  a `Write(...)` wiring-site wrapper with mode-less views (safer default,
  more API churn — the flipped default was judged acceptable given the
  mitigations), and keeping caller modes with only the would-create
  question settled (deferred-refactor variant).

### 2026-07-06 — grounding rounds: the intent axis (wanted / would_start / grounded)

Multi-round co-design of sourcedness, well-foundedness, and orphanhood,
consolidated into the new standing note `notes/grounding.md` (linked from
the design overview's reading order + a new "Intent and grounding"
decision-map section). Absorbs chunk 5 (grid well-foundedness — now the
dependency clause of `grounded_grid`; the chunk file records how each of its
open questions resolved) and resolves the `delete_grid`-vs-dependents
deferred item (reverse rule-reference index; refuse-vs-notify `force`
toggle deferred to a UX round). Highlights and alternatives:

- **Two orthogonal derived axes**: currency judges values; grounding
  judges intent/structure and is deliberately fingerprint-blind
  ("would a from-scratch replay re-create this", never "same bytes").
  Linter-clean = the conjunction = the replay contract's hypothesis.
  Rejected alternative (recorded in the note): grounding also requiring
  freshness.
- **Three mutually recursive predicates, one LFP** — `wanted` (demand
  intent, flows consumer→producer), `grounded` (existence, flows
  producer→consumer + grid-dep edges), with `would_start` as the macro
  carrying the split. A single merged "supportedness" was worked through
  and rejected: the sub-workflow pattern (parent creates + demands child;
  child reads parent-written args) is a derivation cycle under the merged
  predicate — demand must flow from generations that would *start*, not
  complete. Cycles of every kind are handled by LFP non-derivability, no
  special cases.
- **Local verdicts** (orphaned / locally-unwanted) are the depth-1
  approximations; tri-state parallel with currency (orphaned ≈ stale:
  definite no-claimant; ungrounded-but-claimed ≈ unsettled: intent flow
  no longer specifies the fact).
- **Gating is pure policy over stamped derived state**; requests are
  self-vouching (runtime counterpart of the would_start split — without
  it fresh cascades deadlock), so the gate only bites on data/handle-cell
  reads, the one channel demand doesn't flow through. **Strict default**
  (consistent with the strict currency posture), local-only
  ("upstream-only", never flaps on downstream churn) and off as levels;
  the two framing exhibits (cleared-driver window pro-strict; sibling-
  bookkeeping flap pro-local) are recorded in the note for the UX round's
  final call. Maintenance must stay sound with the gate off (no leaning
  on the strict-mode I2-analogue).
- **Root-fact surface**: sourcing records become first-class revocable
  targets-shaped state (auto-upsert on user writes/creates); grid
  sourcing before cell intent (API invariant); adoption via config-pinned
  co-assertion; **launch requires a target** (auto-add sugar;
  auto-target-on-read rejected — inspection channels cover looking);
  data-cell targets kept as checked read-roots.
- **`asserted_grid` from create-capable opens, not creation events**
  (creation is schedule noise; any asserter would re-create it) — schema
  change: record the requested mode in `grid_config` observations.
- **Replay contract** with two honest caveats: convergence is fixed-point
  iteration (side-written data; the blocking/parking mode would make it
  one-pass), and record-derived facts are estimates on dirty states,
  exact once fresh (safety pass).

Next: a phase-2 plan dir for the implementation (schema, stamps/marks,
gate seam, user-API surface) once the doc has settled.

### 2026-07-06 — chunk 4 round: currency in the db (stamps, not dirty bits)

Settled the remaining design points on the committed proposal and landed
the chunk (full resolutions + implementation notes in the chunk file,
`plans/phase1_grid_scaling/chunk4_currency_in_db.md`; canonical writeup
in `notes/execution_semantics.md`'s new "Currency maintenance in the
database" section):

- **Dirty ≡ unstamped**: the four (dirty, stamped) states never
  meaningfully come apart, so there is no dirty bit — just the nullable
  stamp (``currency_verdict`` / ``currency_reason`` / ``currency_as_of``,
  NULL together), and marking = clearing stamps up the observer DAG
  (prune at already-unstamped). Naming: ``currency_*`` + "verdict";
  ``settled_currency`` rejected for colliding with the settled/unsettled
  term of art.
- **`run_readonly` deleted; currency is session-owned.** No public
  run-under-my-lock seam remains: ``WorkflowSession.cell_currency`` is a
  typed op (derive + stamp in one transaction), `currency.py` is its
  store-internal conn-level implementation module, and the epoch counter
  + ``CurrencyEvaluator`` memo are deleted — the stamps are the memo.
- **Hybrid SQL-nativeness**: recursive-CTE bulk cone load; Python
  verdict recursion; stamps written back in the same body.
- Scale numbers moved the right way (walk ≈13ms → ≈6ms and now O(1) on
  repeat; suite ≈34s → ≈21s).

### 2026-07-06 — chunk 5 rescope: well-foundedness deferred, direction settled

Walked the design through three positions and deferred the chunk pending
its own scoping round (trail + open questions now live in the chunk
file, `plans/phase1_grid_scaling/chunk5_well_foundedness.md`):

- The original transitive stored flag corresponds to no crisp
  operational claim (complete+fresh input cells keep serving under
  holding ≠ observing, so a fresh run can succeed under a
  transitively-broken input grid).
- Direct existence is honest ("mint can succeed") but reduces to a
  dynamic diagnostic that doesn't actually resolve the `delete_grid`
  deferred item — rejected as not worth a chunk.
- **Settled direction: a strictness increase.** Fresh runs fail
  immediately when any input grid is not well-founded, making transitive
  well-foundedness mean "a run can succeed (structurally)" *by fiat* —
  a slight tightening (affected cells are usually stale anyway) that
  buys clear invariants and obviously-correct diagnostics, and justifies
  the stored transitive state + reverse rule-reference index. Open
  questions (existence cycles via delete+recreate, recompute-on-recreate,
  gate placement and error classification, config-compat refinement) are
  in the chunk file; the `delete_grid` deferred item stays open in
  `design_overview.md` until that round.

### 2026-07-06 — chunk 3.post1 followup: child mode ≤ subtree grant mode

Resolved (former) open question 7 the strict way: **child mode never
exceeds the subtree grant's mode**, sliced or not. `open_child` rejects
write-mode children under any read-mode subtree view; the 3.post1
"window is look-only" literal-scan special case collapses into this one
general rule (sliced specs are read-mode by construction after the
`bind_keeps` downgrade, so the sliced case is just an instance).
Consequences traced and closed with it:

- Opens that carry no caller mode — nested subtrees (engine + user
  surface + `attach`'s subtree branch) — now **inherit** the grant's
  mode instead of assuming write, so browsing descends through read
  grants (previously `open_subtree` under an *unsliced* read grant, e.g.
  a zero-axis task's internals view, silently returned a write grant —
  the escalation in its purest form, since subtree write is create).
- The user capability's `open_task` derivation asks for read (matching
  the run capability), keeping the attach half legal through read-mode
  grants; creates still require create access, and the canonicalizer's
  full-frame creation check is unchanged.
- `open_checkpoint`'s write request under a *forwarded* read-mode
  internals grant now rejects (previously it could attach an existing
  checkpoint grid writable through someone else's internals window).

### 2026-07-05 — chunk 3.post1 round: slicing sheds create authority

Implemented the read-sliced-subtrees plan; two refinements over the
chunk 2 round's sketch (full statements in the chunk file + the updated
canonical docs):

- **Slicing sheds create authority** (replacing "reject binds on
  write-mode subtree specs"): ``read_only()`` is a local shadow rewrite,
  so the runner-side true spec of a downgraded-then-sliced view still
  says write — instead of shipping effective mode on every selection,
  ``bind_keeps`` downgrades any bound subtree spec to read. Crafted wire
  selections become sound by construction; the view layer's explicit
  ``read_only()``-before-``at()`` stays as intent-visibility.
- **Windows are look-only all the way down**: children of a sliced view
  open read-only even when they exist (attach). The *unsliced* form of
  that escalation — write-mode child opens under read-mode subtree
  grants — was noticed in the process and is now open question 7.
- The chunk 2–3 suites rode through the ViewSel-opens wire conversion
  unchanged — the "schedule the refactor after the suites" bet paid off
  exactly as intended.

### 2026-07-05 — chunk 2 round: diagonals + the creation-capability rule

- **Diagonal extraction is dynamic, by design.** The sanctioned form is
  "receive the matrix with both axes kept + a coord view; read
  ``(r=i, c=i)`` per cell" — per-cell observation records give exactly
  the wanted staleness. Static diagonal *wiring* is deliberately
  inexpressible (per-view name uniqueness; the wire's consumed-twice
  rejection, already unit-tested); the only user-expressible near-miss
  is the rename collision, asserted in the suite as documentation.
- **Bug found by the suite**: ``attach_internals`` on a *multi-axis* task
  cell mints an internals view whose cell coordinates are root-frame
  literals, and ``open_child``'s full-frame check rejected it — the
  walking skeleton only ever browsed zero-axis cells. Fixed for now with
  an internals-only carve-out in ``check_subtree_full_frame``
  (read-only grants can't create, so nothing can broadcast).
- **The carve-out is a narrow instance of a general rule**, settled in
  discussion but deliberately deferred: *full-frame is a property of
  creation capability* — read-mode subtree views may be sliced. Tracing
  it through: sliced read subtrees as task args already flow (literals
  pass ``translate`` and bake into the stored binding, so callee grants
  need no wire change); slice-then-open in one holder is the part that
  needs surgery (the bare-token ``open_*`` wire encodes the very
  assumption being retired → ViewSel-carrying opens), plus read-only
  ``SubtreeView.at``. Scheduled as
  `plans/phase1_grid_scaling/chunk3post1_read_sliced_subtrees.md` —
  after the axis suites, so they cover the refactor; the carve-out
  carries a ``TODO(chunk3post1)``.

### 2026-07-05 — chunk 1 design round: CoordView (née EnumerateView)

Refined the chunk 1 plan (now `plans/phase1_grid_scaling/chunk1_coord.md`,
which carries the full statements):

- **Rename: enumerate → coord.** The settled slogan is "coordinates are
  structure, not data", and this view *is* the coordinate — `CoordView` /
  `CoordBinding` / view kind `"coord"`, constructors `int_coord(name)` /
  `str_coord(name)`. Rejected: `enumerate_*` (builtin clash; suggests
  receiving the whole sequence), `iota` (obscure), `loc`/`pos` (vague).
- **Received coord grants are token-backed.** They register true specs in
  the run's `SpecRegistry` like any grant, so forwarding a received coord
  view into a child `open_task` is a plain `WiredArg` translating through
  the mapped relation (batch-invariance rejection included). This closes
  the trap where forwarding a consumed coord would literalize a per-cell
  value into config identity. Only locally created coord views cross as
  the token-less wired-arg variant; engine ops reject coord tokens (no
  effectful op exists on them).
- **`ViewSpec` splits into disjoint classes** rather than growing a
  degenerate path (empty tuples collide with the root-subtree spec;
  marker steps are duck-typing hacks): a discriminated union of frozen
  dataclasses sharing the `kind` field (per-class restricted `Literal`
  types, so existing kind checks narrow for the typechecker) —
  `LeafViewSpec` (data/dataframe/handle/checkpoint), `TaskViewSpec`
  (the only home of `output`), `SubtreeViewSpec` (the only home of the
  internals flag), and the path-less `CoordViewSpec` (template + kept
  axis; walking its path is now inexpressible). Runtime checks stay at
  the wire dispatch points (tokens are untrusted); the narrowed types
  are what they return. The split lands first as its own
  no-behavior-change commit.
- Sync `value()` (pure — async would suggest an op); the consumed case
  hands the task a zero-axis `CoordView`; literal-bound coord views are
  allowed (a constant scalar input, literal identity-bearing).

### 2026-07-05 — phase 1 scoping round: grid scaling

Scoped `plans/phase1_grid_scaling/` (overview + five chunks). Decisions:

- **`EnumerateView` semantics**: coordinates are structure, not data —
  reading one is unrecorded-pure (no observation, no staleness edge, no
  record entry); no wire op at all (consumed-axis values ride the
  per-run grant, kept-axis values are local arithmetic); identity-bearing
  as a rule input (`EnumerateBinding`: kind + wiring + received name);
  1D, `str|int`, allowed kept (the identity function — silly but the
  calculus makes allowing it natural) and top-level.
- **Test style**: hand-written scenario tests only (semantics checks
  where readability is the value); no hypothesis-style property tests.
- **Dataframes deferred to phase 2 in full**, with the guardrail that
  chunk 4's currency design round settles the gather questions first —
  baseline expectation is "gather = a for-loop of reads, bulk-inserted",
  with slice-observation compression the one real question (design the
  schema to permit it later or reject it explicitly; don't build it).
  Frames' canonical encoding will stay the dumb sorted lists-of-lists;
  fast query formats are derived, never identity-bearing.
- **Currency-in-DB** starts with its own design round; the known shape
  and open questions (mark granularity, propagation timing, verified
  stamps, cycles, resurrection mechanics) are written up in the chunk
  file for a second-pass decision.
- **Well-foundedness**: grid-level, forward-looking (never touches
  completed generations' currency), maintained via a reverse
  rule-reference index; `delete_grid` stays allowed and flips
  dependents' flags; demand consults the flag for diagnosis while
  grant-mint failure remains the enforcement.

### 2026-07-05 — post-skeleton design-doc audit

Walked `design_overview.md` (and `execution_semantics.md`) against
everything chunks 7–9 landed. Consolidation, no new decisions:

- The user-surface section now carries chunks 8–9: wrapper/bootstrap
  placement, atomic clear, fully-bound unique targets + `demand_target`,
  the attach (trust-stored) vs open (assert-proposed) split, and the
  **introspection doctrine** stated as a principle — values arrive
  labeled or not at all (presence queries value-free; `inspect_cell` =
  content + verdict, one snapshot; internals browsable read-only; the
  only unlabeled channel is the gated read/demand).
- New decision-map bullets: task/step authoring (`__call__` callables;
  decorators are the adapters) and the served-id re-attach rule (the
  liveness invariant's serving-side twin).
- Bindings bullet gained the auto-release-on-drop semantics; deferred
  followups gained step-entry (checkpoint history) introspection.
- `execution_semantics.md`: the escaped-exception classification now
  matches the executor (runner-side typed `CellFaultError` → interrupted
  carve-out).
- **New standing note `notes/boundary_discipline.md`** (added to the
  reading order): the one genuinely cross-cutting lesson of the
  walking-skeleton phase is that every seam is a service boundary and
  the same four rule families apply at each — id coverage
  (liveness + re-attach; "tracked ≠ pinned"; the reopen-masking residue
  trap), persistence scopes (pin from the scope holding the closure,
  before it closes), ref lifetimes (never durable; call-scope mounts for
  caller-interest cleanup; RPC peers drain concurrently), and error
  transit (informational; typed reconstruction at consumers; signals ≠
  poisonings). Each rule was paid for by a real bug or near-miss this
  phase; the note ends with the new-wire-method checklist.

### 2026-07-05 — chunk 9 round: user-surface followups from the chunk 8 retrospective

Four fixes (rationale + notes in the chunk 9 plan file; contracts in the
module docstrings), plus two explicit no-changes:

- **`attach`** (new wire op): a grant for an *existing* grid from its
  stored config alone — the opens assert a proposed config (what task
  determinism needs); attach asserts nothing and trusts storage. The
  stored config carries everything a grant needs, task grids included, so
  `list_keys` + `attach` reconstructs any view after a reopen. Task
  internals stay structurally unreachable from the top level (no subtree
  grant reaches a task grid's key-space) — v0 has no internals browsing.
- **Typed re-raise** (`errors.typed_wire_call`): consuming boundaries
  (view ops, the `Workflow` wrapper) reconstruct documented exception
  types from transit `IRException`s — ergonomics on top of the
  informational-only doctrine, never an input to system state. Closes the
  cross-cutting item open since the task-side step wiring landed.
- **`_UserViewBackend`**: the view runtime's seam and the user wire
  protocol stop sharing shapes by pun; the adapter carries the divergence
  (attach-only `open_handle`, intentional errors for task-internals ops),
  and the proto's seam-driven `mode` default is gone.
- **Auto-release-on-drop for bindings**: the release impl's own
  `_fxtr_mount_lifespan` teardown releases, mounted on the dispatch call
  scope (alive exactly while the caller holds the returned ref). Intended
  semantics: drop granularity is wherever the caller's ref bridge lives;
  the guarantee is that a release capability stranded in a closed scope
  cannot wedge a binding — not promptness within one shared scope.
- Found writing 9.4's test: an unbound *task-fn* binding at demand time
  misclassified as `TaskRaisedError`/errored; it is a runner-side typed
  `CellFaultError` raise (the fn spawn, in-process — classifying from it
  does not violate the transit doctrine) → fault-class, `interrupted`.
- **Internals browsing** (`attach_internals`, added in the same round):
  a read-only internals grant per task cell — subtree transparency says
  the owner may *look*; the surface guarantees look-only (read-mode
  forced under internals roots; checkpoints list but don't attach —
  step-entry browsing deferred). Mechanically the task-cell spec
  re-tagged as the run-side internals pseudo-subtree with literals for
  Params, so `open_child` addressing lines up unchanged.
- **No change**, recorded: `ensure_loadable` stays a per-call-site rule
  (no automatic layer; the namespace scope tracks only what was
  explicitly retrieved); `write_handle` on the wrapper is the privileged
  split working as intended, not a wart.

### 2026-07-05 — chunk 8 pre-implementation round: user-surface decisions

Settled before implementing the user capability (contract fixes landed in
`user_api.py` / `model.py` / `errors.py` docstrings, the canonical homes):

- **Targets are fully bound and unique per slice identity.** The
  "positions may be open" phrasing on ``add_target`` was a documentation
  bug: an open-slice target has no coherent ``run_targets`` semantics
  without enumerating cells (open-map principle). v0 rejects open
  positions at add time; the future form is gather-driven ("target the
  cells a gather expression spans"), deferred with gather itself (new
  deferred-followup entry). One record per slice: ``add_target`` is
  idempotent on the identical record and raises ``TargetConflictError``
  (new) when a different-config record exists; ``remove_target`` matches
  on slice identity alone, ignoring the pinned config fingerprint — a
  dangling record must stay removable after drift, and intent changes
  stay explicit (never silently rebound).
- **`CellInfo` carries no values** (field dropped, data cells included):
  presence queries tell you what exists, reads tell you what it is —
  values flowing out of a bulk listing would smuggle unlabeled data past
  the value-pinned demand gate (a complete-but-stale cell's results
  observable without the currency check). Retrieval is always an explicit
  read/demand.
- **`inspect_cell` is the stale-results side channel** (follow-on
  question: how does a user look at a stale/unsettled-but-complete cell's
  value at all?). Not via the demand pathway — demand is a
  materialization *event* (it executes, awaits, resumes; introspection
  must trigger nothing) — but a separate ``inspect_cell(sel) →
  CellReport``: content and verdict from one session snapshot (riding the
  currency evaluation's existing value-pinning), so a value is never
  observable *as if fresh* — it always arrives labeled. Distinct from
  ``list_cells`` by being single-cell and explicit: the bulk listing
  stays value-free.
- **`clear_cells` is atomic**: any addressed cell running → the whole
  call refuses, clearing nothing (the no-partially-applied-states
  doctrine).
- **Bootstrap is a plain async CM, not a mount lifespan**: the capability
  impl stays dumb glue over already-running machinery (session, executor,
  bindings) — the runner's lifetime is not hostage to one mount, and the
  scope ordering (runner scope includes the session's entity scope) falls
  out of CM construction order. A "workflow server from db paths"
  deployment helper can later wrap the same CM inside a mount lifespan
  without wire changes.
- Implementation shape: a sibling ``user_service.py`` (mirroring
  ``task_service.py``) — own ``SpecRegistry``, reads via the executor's
  demand (materialization events), the shared canonicalizer for opens, no
  ``OpsEngine`` (no record/frontier/poisoning to tangle with). v1-cut
  methods (``read_cell_frame`` / ``write_cell_frame`` / ``read_batch``)
  raise ``NotImplementedError``.
- Decided while implementing (rationale in the chunk file's notes):
  **``demand_target`` joined the wire surface** — targets are absolute
  root-frame slices no granted token addresses, so the by-record demand
  is the honest primitive and ``run_targets`` stays wrapper sugar over
  it; ``open_handle`` grew a ``mode="read"`` default (shared views keep
  their three-argument call; creation stays deliberate via
  ``mode="write"``); the binding release capability's
  auto-release-on-ref-drop half is deferred (explicit ``release()``
  works; connector lifetime still bounds the binding).

### 2026-07-05 — executor robustness: no exit path leaves a generation `running`

Follow-up to a hole noticed during chunk 7.4: `mint_rule_grants` ran
*after* the generation row was minted/flipped to ``running``, so a rule
referencing a deleted grid (reachable because ``delete_grid`` doesn't
know about rule references, and holding a grant records no observation —
the deletion needn't stale the generation) stranded the row in
``running`` with no failure recorded, and on a resume also erased the
prior attempt's failure evidence (``clear_failures`` runs at flip time).
Stuck ``running`` self-heals on re-demand and gets crash-flipped across
restarts, but blocks ``delete_grid`` and misreports state. Decided:

- **Grants mint before the generation row is touched** — a missing
  rule-referenced grid is now a pre-run ``CellFaultError`` exactly like a
  missing task fn ("nothing was attempted, no generation state touched",
  which `_run_once`'s docstring had already promised).
- **The safety pass's setup (mint/load/record-load) moved inside its
  try**: the pass gates completion, so a setup failure (the same deletion
  landing in the run→pass window) records evidence and lands ``errored``
  rather than propagating with the row still ``running``. Errored (not
  interrupted) matches the existing precedent for environmental failures
  during the pass; the strict gate would fault such a cell anyway.

### 2026-07-05 — chunk 7.4 finding: served ids must be re-attached (ensure_loadable)

The crash-simulated resume flagship (first test ever to re-*run* a task
after a session reopen) exposed a coverage bug: an engine read serves a
state-db-recorded ``FxtrID``, and the service return packet can only
cover ids whose nodes are *tracked* in the dispatch scope's ancestry —
durable presence in the entity store is not enough. Within the writing
process this held by residue (the original persist left the closure
tracked under the entity scope); on a resumed session nothing had
re-tracked the node and the return-packet lift failed.

Decided fix: ``WorkflowSession.ensure_loadable`` — an untyped
``namespace.retrieve`` into the session's entity scope (lazy pin lookup;
idempotent; copy-free), making the class's existing contract ("ids
recorded in the state db stay loadable below" the entity scope) true
across sessions instead of by accident. The engine's shared read path
calls it on **every** served id, hit and miss alike (the miss's
``read_cell`` also returns a bare recorded id). The liveness invariant is
what makes this total: every state-db-recorded id was pinned
(pin key = id string), so the lookup cannot legitimately miss — a miss is
an integrity violation and raises. Consequence for tests/tools: writes
that bypass ``persist`` (raw db helpers) now fail at serve time; the one
test doing that was corrected to use the real ``write_cell``. Checkpoint
payloads were never affected (``load_step_payload`` already retrieves
through the namespace).

### 2026-07-05 — chunk 7.3 adjustment: decorators are the adapters

Reviewing 7.3's landed shape (fn → `_Wrapped*` → adapter — two wrapper
layers doing one job), decided a hybrid collapse:

- **`TaskFn`/`StepFn` contracts use `__call__`, not `run`**: a task/step
  fn is any async ``(ctx, /, **args)`` callable. One mental model — plain
  functions, class instances with ``__call__``, and the adapter impls
  themselves all satisfy the same protocol.
- **Decorating is wrapping**: ``@task_fn`` ≡ ``TaskFnAdapter(fn)`` and
  ``@step_fn(type_id=...)`` ≡ ``StepFnAdapter(fn, type_id)`` — the
  decorators construct the adapters directly; the ``_Wrapped*``
  intermediary is gone; manual adapter construction is preserved and
  identical (class-based fns keep working unchanged).
- **Adapters gain a non-service ``__call__`` passthrough** (dunders are
  excluded from service-method discovery), so a decorated fn still acts
  like the undecorated one locally; the wire surface stays
  ``run_task``/``run_step`` alone.
- **Decorators moved beside the impls** (`task_service.py` /
  `view_runtime.py`); `task_api.py` is pure protocols again. Task-author
  import ergonomics deliberately deferred.
- **Per-scope mount memoization** for step adapters
  (``StepFnAdapter.mount_on``, weak-keyed by scope): a task looping
  ``run_step`` reuses one mount per execution scope. Sanctioned by
  fxtr2's design — one impl instance may mount into many scopes
  (per-mount state rides each mount's lifespan); the concurrent
  double-mount race is benign (both mounts valid).

### 2026-07-04 — chunk 7 design round: steps join (seam, StepHost, policies)

Pre-implementation round for steps/checkpoints; the wire contracts were
already settled (runner_api / task_api / errors docstrings). Decided:

- **The checkpoint/observation seam**: one op record *conceptually*
  (subject-keyed hit/miss, hit-mismatch = nondeterminism, miss → frontier
  guard → live → record, safety-pass set membership, cascade turnover),
  two entry shapes *physically* (separate db tables and in-memory maps —
  payloads and consumers differ, and checkpoint entries do NOT
  participate in currency: they record the generation's own
  nondeterministic history, not observations of anything external).
  Unified discipline surfaces: one ``visited`` set, the same
  ``_guard_frontier``, safety-pass comparison over the union.
- **Host-style step execution**: the engine takes a small ``StepHost``
  (parent scope + binding registry — injected *authority*, no logic) and
  owns the whole run_step miss path (per-step scope, capability,
  invocation, persist-inside-scope, record, serve). Rationale vs a
  demand-hook-style seam: the demand hook delegates real *policy*; a step
  miss has none to delegate — only authority — and connect-authority
  checks need the engine's own record, which would make a hook a two-way
  dialogue. Revisit if host-side proves awkward in practice.
- **One ``run_step`` call per checkpoint cell per run** (new
  ``DuplicateStepError``, errored-class): a second call — concurrent or
  sequential — is an error, not something the engine dedups; requesting
  the same work twice is a task antipattern and dedup belongs in task
  logic. (Refined from an earlier concurrent-only rule: the flat
  one-call rule is a simpler mental model, catches the concurrent
  double-run/PK-violation case for free, and closes a safety-pass blind
  spot — the *set* comparison can't distinguish call multiplicity, so a
  replay calling a step twice where the live run called once would
  otherwise slip through.) Across runs the rule is per-run: a resumed
  run's single call is served from the recorded entry.
- **Step payloads**: one ``StepPayloadEntity`` mechanism for both the
  invocation args and the step result (canonical entity round-trip;
  contained FxtrIDs become real entity refs so persistence pins their
  closures — the reason for entity-encoding); ``ServiceRef``\\ s are
  *rejected* in both (refs aren't durable; recording one is meaningless
  on replay — handles cross as keys, per the decided model).
- **Author-side sugar**: user-facing task/step fns use ``__call__`` with
  decorator converters — ``@step_fn(type_id=...)`` (the declaration site
  owns the step's identity) and ``@task_fn`` for symmetry. Purely
  author-side; the runner-side protocols are unchanged, and variants can
  coexist (eventually inside the sandbox layer).
- **Errors**: ``StepRaisedError`` (errored-class) for errors escaping a
  step fn; ``HandleUnboundError`` (existing, fault-class) remains the
  connect-not-available type; connect poisoning uses a per-invocation
  poisoned flag the run_step body consults after the step returns
  (checkpoint entry withheld even if step code caught the error).
- Refined during 7.2 review: **poisoning is registered exactly once, at
  the raise site that originates the error**; a poisoned invocation
  surfaces to the task as a fresh ``StepInvocationFailedError`` — a
  signal chained from the cause, never recorded by the op wrapper (type
  contract: constructed only for an already-recorded cause). Replaces a
  first cut that re-raised the original exception object with an
  object-identity dedup — rejected as mixing the two events (the
  poisoning and the invocation-failure signal) into one object.
- **Flagship resume test both ways**: cancel-based (interrupt mid-step,
  re-demand, earlier step served) and crash-simulated (session reopen +
  startup flip).
- Piece plan: 7.1 encoding/storage plumbing → 7.2 engine (record seam,
  run_step, capability) → 7.3 task side (view wiring, decorators) →
  7.4 executor/safety pass + the property-test battery.

### 2026-07-04 — chunk 6 design round 4: value-pinned currency; efficiency plan

Review of the first currency implementation (a per-query Python recursion
returning only a verdict):

- **Currency is a property of a (generation, results) pair at a snapshot,
  never of a cell's ambient state.** A verdict-only API invites a
  check-then-read race (gate validates generation G; a concurrent clear +
  re-run turns the cell over; the caller's separate read serves a value
  the gate never checked). Redesigned: the whole evaluation runs as one
  sync body under the session's writer lock (`run_readonly` — every
  mutation shares that lock, so the walk and the values it returns are
  one consistent snapshot), the report carries the validated
  ``(generation, status, results)``, the demand hook contract becomes
  "return the validated results", and the engine serves + records from
  exactly that map with no second read. Fallout fix: with a hook
  installed, *every* record-miss task-output read now goes through it —
  previously already-complete upstreams were served status-only,
  bypassing the currency gate entirely (a freshness-invariant hole).
- **Epoch memo** (v0 in-process): the session exposes ``change_epoch``,
  bumped by currency-relevant mutations only (cell writes, grid
  create/delete, generation lifecycle, results) — record traffic for a
  running generation deliberately doesn't bump, since it can only affect
  that generation's own verdict, which is gated behind its status flip.
  `CurrencyEvaluator` caches all verdicts a walk visits while the epoch
  is unchanged, collapsing fan-out demand storms to one walk per epoch.
- **Efficiency plan (consolidated to design_overview followups)**: v0
  stays derive-on-demand; the post-v0 replacement is materialized dirty
  bits + lazy re-verification in the database (dirty marks propagated
  over the reverse observation index on settled changes; verified
  verdict + revision stamped per generation — Shake/Salsa-style verified
  traces). A pure recursive CTE was considered and rejected as the whole
  answer: fresh requires "all children fresh", a hyperedge condition
  vanilla CTE recursion doesn't express. Revisit trigger: first
  realistic-scale exercise (gather/dataframes or the real MH example).
  The value-pinned interface survives the swap unchanged.

### 2026-07-04 — chunk 6 design round 3: cancellation never waits on committed work

Review of the first obligation implementation (which shielded the whole
op body so a committed op always ran to resolution): rejected — the shield
conflated keeping the *record* consistent with forcing the *work* to
finish, and would have made cancelling a runner block on e.g. a demanded
long upstream execution. Corrected semantics:

- The commitment point needs no shield at all (obligation registration is
  synchronous; anyio cancellation only lands at awaits).
- A committed op **cancelled before resolution** — the task dropped its
  request, or the run is being cancelled externally — records a
  fault-class ``RequestAbortedError`` failure and re-raises the
  cancellation immediately. The run can then only end interrupted; the
  aborted op recorded no observation, so a later run re-attempts it at
  the frontier. Request = observation is upheld by *refusing completion*,
  never by waiting.
- The one narrow shield is around the failure-record write itself (one
  bounded insert): it may run with a cancellation already pending, where
  an unshielded await would raise immediately and lose the row — the
  "keep two things in sync locally" case, and nothing more.
- Executor consequence (for the run-orchestration piece): a run cancelled
  *between* ops has no failure rows, so the executor classifies
  cancellation exceptions as interrupted directly (recording a
  fault-class failure itself) rather than falling through to the
  escape-means-errored rule.
- Follow-on (same day): **unexpected non-workflow exceptions inside an op
  poison too** (error-class, real class name in the row). Poisoning
  covers op failures of any type — without this, task code catching the
  wrapped internal error could complete cleanly around an op that failed
  mid-effect; the escape-means-errored backstop only covers the
  didn't-catch case. ``BaseException`` beyond cancellation
  (KeyboardInterrupt/SystemExit) is deliberately not caught — process
  teardown is the startup flip's job.

### 2026-07-04 — chunk 6 design round 2: the currency model (fresh / stale / unsettled)

Supersedes the two-state staleness closure previously in the
execution-semantics note (user's redesign, prompted by round 1's
observation-cycle question):

- **Stale is direct-only**: a definite, local fact — some observation's
  subject now holds a *settled value* different from the recorded
  fingerprint. Settled values are definite current values (user-written
  data; a completed generation's output — a stale task still has a settled
  value, just possibly not a current one). User actions (overwrite,
  delete) settle immediately; execution lifecycle (cleared / running /
  errored) leaves a task output without a settled value until a generation
  completes again.
- **Fresh requires well-founded support**: every observation matches its
  subject's settled value and every observed task cell is itself fresh,
  with the justification bottoming out in data cells and settled results.
  Observation cycles can never prove freshness — they park at unsettled
  (the swap-a-completed-task construction needs no enforcement; it is
  simply never rewarded with freshness).
- **Unsettled is the middle state**: nothing definitely changed, but the
  support DAG contains something stale / re-running / cleared / errored /
  cyclic; it resolves to fresh or stale when the support settles. This
  fixes the old closure's premature condemnation: B observing A does not
  become stale when A does — A's rerun may reproduce the value, in which
  case B resolves fresh (resurrection is the ordinary path, not a special
  case).
- **Naming**: ``fresh`` / ``stale`` / ``unsettled``; "settled" is the
  derived resolution concept (a cell is settled iff fresh or stale; a
  subject value is settled iff definite). "Up-to-date" is replaced by
  "fresh" (already the demand-policy vocabulary). Rejected: "settled" as
  the name of the good state — stale is *also* a settled outcome, and the
  direct-staleness definition needs "settled value" as a separate term.
- **Execution gating is pure policy over the derived currency; the v0
  default is maximally strict**: a new observation requires its subject
  complete + fresh; stale or unsettled → fault, with the diagnosis naming
  the roots the evaluation found. Planned-but-deferred policy options:
  permissive mode (observe anything — the completion derives as non-fresh
  automatically, no extra bookkeeping); await-instead-of-fault when a
  target is unsettled only because its support is running; and a derived
  **settling** state (would resolve fresh if every running task in the
  support resolves to what its observers recorded), enabling optimistic
  downstream parking when a stale task is retriggered.
- **Demand policy adjustments**: demanding a ``running`` cell awaits it,
  then re-checks currency *on arrival* (stale- or unsettled-on-arrival =
  fault); matrix rows renamed to "fresh"; new unsettled row (fault with
  diagnosis).
- Round-1 follow-ups confirmed: drop ``task_execs.reason`` in favor of the
  failure rows; add ``TaskRaisedError`` as the explicit executor-recorded
  failure class for exceptions escaping a run with no engine-recorded
  failure (classification stays rule-based, never wire-parsed).
- Implementation of the evaluation: memoized three-valued recursion; the
  in-progress memo marker *is* unsettled, so cycles need no special case.

### 2026-07-04 — chunk 6 design round 1: poisoning storage, executor ownership, request=observation

Grounded in a review of the abandoned first executor attempt (kept as a
conflicted WIP commit for reference); these supersede its choices.

- **Failures are stored, plural, engine-written.** New state-db table of
  *failures* observed inside a generation (a run may accumulate several —
  concurrent async branches can each error/fault). The engine inserts a
  failure row at raise time (it has session + exec_id at the raise site);
  the executor never classifies anything that crossed the wire. This
  makes "actively running but already poisoned by these things" a
  well-defined, queryable state that naturally transitions to
  errored/interrupted when the run actually stops. Status transitions
  stay executor-owned (single writer per fact: engine owns failure rows,
  executor owns status). Classification at run end from the failure set:
  any errored-class failure → ``errored``; only fault-class failures →
  ``interrupted``; escape-without-recorded-failure → ``errored`` (the
  task's own logic raised). Confirmed in round 2: ``task_execs``'s single
  ``reason`` column is dropped in favor of the failure rows.
- **The executor owns the per-run scope**; ``run_task_over_engine``
  dissolves into it (mount capability + resolve + invoke are three lines;
  the interesting ordering — persist results from the run scope before it
  closes — is executor lifecycle). Boundary mechanics stay unit-tested in
  ``task_service``; the run path is tested through the executor
  (integration-style, the more effective coverage).
- **Grants-from-rule reconstruction moves out of the executor** into the
  spec layer (``view_spec.py``/``spec_registry.py``) as a tested
  translation (stored rules are already holder-frame specs); proper
  ``isinstance`` checks, not ``type(x).__name__``.
- **Request = observation (new invariant, to be added to
  ``execution_semantics.md``).** A task observes a subject the instant it
  *requests* it, and the task is not complete until everything it
  requested has resolved to a fresh result — independent of what the task
  does with the result, including dropping/cancelling the request
  internally. Ignoring a result is equivalent to catching the exception
  and is not a separately-reasoned case: a run's completion gate is
  "all pending requests resolved + failure set empty", and a pending
  request that resolves to a fault poisons (interrupted). Consequence: a
  demand cycle can never resolve to anything — it can only fault (v0:
  block until user interrupt, per the standing scheduling note).
- **Staleness evaluation is a least fixpoint, not cycle special-casing.**
  Observation cycles across generations are legal (A completed; B read A;
  A cleared, re-ran, read B). ``is_stale`` = "some reachable recorded
  observation mismatches its subject's current value"; the evaluation
  memo's in-progress entries mean "no evidence yet", and any real
  mismatch anywhere in a cycle still propagates. Distinct from *demand*
  cycles (a runtime liveness matter, covered by request=observation
  above). Also made explicit: demanding a ``running`` cell awaits it and
  checks staleness *on arrival* — stale-on-arrival is a fault.
- **The task-fn read is an ordinary engine op in the run prologue.** Every
  run — including the safety pass — starts by reading the task-fn handle
  cell *through the engine* (``read_handle_key`` on a grant minted from
  the rule's ``task_fn`` slice), so it record-checks like everything
  else: first run records it, every replay hits it, and the safety pass's
  set comparison needs no exemption (the WIP's special-case subtraction
  disappears). No schema change; also aligns with connect authority
  (the fn key is then a key read *from a handle grant*).
