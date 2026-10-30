# Chunk 6 post-1 — watch refinements from the live pass

Refinements noticed during (and just after) chunk 6's human-operated
browser verification. **Item 2 is complete** (2026-07-14, verified
live — the generation-report streaming) and **item 3 is complete** (the
grid/cell view simplification, folded in for convenience); **item 1 is
deferred**. Design context:
`notes/event_system.md`; mechanisms in `watch_engine.py`,
`state_store.py`, `ops_engine.py`, `run_log.py`.

## Item 1 — the multi-watch update ripple (DEFERRED)

**Symptom.** Changing an input updates the dashboard in visible steps
(tree/header, then cells table, then drawer) rather than one atomic
update.

**Root cause.** The three watches are independent subscriptions; the
engine's single worker recomputes the dirty ones **sequentially** per
window (`WatchEngine._worker`), publishing each as it finishes, and the
client folds each into its own mirror and re-renders as it lands.

**Why concurrency is blocked.** The session is a **single sqlite
connection behind one `anyio.Lock`** (`WorkflowSession._run`), so
recomputes serialize on the lock regardless — it's serialized DB/CPU
work, not overlappable I/O. True parallelism needs a multi-connection
WAL reader pool, which breaks the single-snapshot discipline.

**The lever if we want it: batch-publish.** Compute all dirty watches,
then publish in one no-await burst so the client folds them in one
React tick — trading fast-watch latency (tree/cells wait for the
drawer) for atomicity. Prototype and eyeball; not obviously better.
**Deferred** — the ripple is sub-second and cosmetic.

## Item 2 — generation-report streaming (COMPLETE)

**Symptom.** In a running task cell's drawer, log lines stream live,
but checkpoint entries and observations appear all at once at run end;
running/errored steps never appear in the checkpoint-entries table.

**Root cause.** The generation report (checkpoints, observations,
unanswered requests) rides the *diffed* cell watch via
`inspect_generation`, but none of the generation-scoped writes dirty
the **owning task cell's** watch mid-run: `_put_checkpoint` hints the
*checkpoint grid* (not the task cell), and `_record_observation` /
`_record_request_edge` emit no `cells` hint. And a `checkpoints` row is
written only on step *completion* — running/errored steps have no row.

**The model (for the record).** Checkpoints, observations, and
request_edges are three distinct generation-scoped tables keyed by
`exec_id` (NOT a request+response pair). `inspect_generation`
aggregates them. `request_edge`/`observation` are the cross-cell
dependency graph (feed currency + grounding); `checkpoints` are
intra-generation step memoization (feed resumability). The execution
docs already treat all three uniformly as the generation's *op record*
for the replay-determinism comparison (`notes/execution_semantics.md`),
which is the layer where their kinship is real; they correctly diverge
at the currency/grounding consumer layer. So we do NOT merge the
tables — we unify their *observability* only.

### The design (option 2 — no durable execution-state change)

Steps get running/complete/errored visibility without a durable status
field: the **run-log stream** (event-native, already live) carries
running/errored; the **streamed generation report** carries completed
checkpoints (with refs) and observations/requests. The frontend folds
the two into the drawer's tables.

1. **The owning-cell hint (Q2a).** A shared `state_store` helper
   `_note_generation_activity(conn, exec_id)` resolves the owning task
   cell (`SELECT grid_path FROM task_execs WHERE exec_id = ?`) and
   emits `note_change("cells", <task cell grid_path>)`. Called from
   `_record_observation`, `_record_request_edge`, `_record_checkpoint`,
   and `_record_grid_assertion` — so anything a generation records
   dirties its own report. Coalescing bounds the cost; diff suppression
   drops no-op emissions; the frozen safety-pass engine records nothing
   so it stays silent. Grounding marks are untouched (the hint is an
   additive watch signal, orthogonal to `mark_grounding`).
2. **Structured `StepRef` (drop the stringified step).** `watch_api`
   gains `StepRef {grid_path, coords}` (the checkpoint-entry identity
   vocabulary); `LogEntry.step` becomes `StepRef | None` (was a
   `f"{grid_path}{coords}"` blob). `ops_engine` threads structured refs
   through its `_note` calls + the `StepCapability` label. The frontend
   matches log entries to checkpoint entries by the `(grid_path,
   coords)` tuple — a clean keyed join, no parsing.
3. **`step_errored` log kind.** Added to `LogEntryKind`, emitted (step-
   tagged) on the step-failure path in `run_step`. `step_completed` is
   deliberately NOT added — completion is already signaled by the
   checkpoint row streaming into the report (and it carries the
   args/value refs a log line wouldn't). So the log only covers the one
   state with no checkpoint: errored.
4. **Frontend drawer folds.**
   - **"Step checkpoints"** (renamed from "checkpoint entries"): the
     completed checkpoints from the report, merged with running steps
     (a `step_started` in the log with no checkpoint yet) and errored
     steps (a `step_errored`), keyed by `(grid_path, coords)`, with a
     status column (running / errored / complete). Complete rows carry
     the step-type/args/value refs; running/errored rows carry the
     log-derived status (+ the error gist for errored).
   - **"Observations"**: the observations merged with the unanswered
     requests (both already in `GenerationReport`), requests shown as
     **pending** rows. Answered reads appear as their observation; a
     pending read appears as a pending row until its observation lands.

Purely additive: no schema change, no execution-semantics change, no
at-rest-format impact (the durable checkpoints table is untouched; the
log stream stays runtime-only).

### Steps

- [x] `watch_api.py`: `StepRef`, `LogEntry.step: StepRef | None`,
      `step_errored` kind (+ docstrings). *(2026-07-14)*
- [x] `run_log.py`: structured `step` through `note` / `LogEntry`.
      *(2026-07-14)*
- [x] `ops_engine.py`: structured `StepRef` in `_note` + step label;
      `step_errored` note on the failure path. *(2026-07-14)*
- [x] Q2a: `_note_generation_activity` helper + the four record-write
      call sites (`state_store.py`). *(2026-07-14)*
- [x] Wire mirrors (`wire/types.ts`: `StepRef`, `LogEntry.step`,
      `step_errored`) + dialect fixture + pins. *(2026-07-14)*
- [x] Drawer merges (`derive/generation.ts` + `cell_drawer.tsx`): the
      "step checkpoints" table + the observations/requests table;
      structured step rendering in the log lines. *(2026-07-14; the
      merges are a pure `derive/generation.ts`, unit-tested.)*
- [x] Docs: `event_system.md` (log-event vocabulary + the owning-cell
      hint), `watch_api.py`, `frontend/LOCAL_CODE_OVERVIEW.md`.
      *(2026-07-14)*
- [x] Verify: human browser pass (running → complete → errored steps
      streaming into the step-checkpoints table; pending requests in
      observations) + repo tsc/biome/vitest + experiment (250) suites.
      *(2026-07-14; a new `test_watches` case pins the structured
      `StepRef` + the streamed checkpoint, and `derive/generation.ts`
      has unit tests for both merges.)*

## Item 3 — grid / cell view simplification (COMPLETE)

Folded in for convenience while in the same code (2026-07-14). The grid
detail and cells table over-showed healthy defaults; the fix surfaces
only what's notable.

- **Grid header**: dropped `access: read/write` (a viewer-internal
  detail, not workflow state); grounding and sourcing now render only
  when abnormal (`unanchored`/`orphaned`; `dangling_*`), and the
  grid-level targets summary only when non-empty — a healthy grid shows
  just path/kind/axes + config.
- **Cells table**: the `currency` and `grounding` columns fold into a
  composite **status** column (`derive/cells.ts` `cellBadges`) that
  shows the exec status plus only abnormal currency/grounding (`fresh`
  and `grounded` dropped); the `wanted` and `target` columns combine
  into one **want** dot (`cellDemand`), a 2×2 language (color =
  targeted vs wanted, fill = populated vs not) matching the tree pane:
  targeted+run = filled green ●, targeted+not-run = hollow green ◌,
  wanted-by-a-reader = filled grey ● (always populated), nothing
  otherwise.
- **Targeted-but-not-run cells now appear**: the table shows only
  populated cells (`list_cells`), so `deriveCellRows` synthesizes a
  value-free **phantom** row for each targeted coord with no populated
  cell (faint, "not run", the hollow-green dot) — selectable, so the
  drawer opens with a Run affordance. (Wanted-but-not-targeted-and-not-
  populated cells are NOT discoverable frontend-side — nothing
  enumerates the wanted set for the UI — so they only appear once
  populated; would need a backend query, out of scope.)

All derivations are pure + unit-tested. Frontend-only (`grid_view.tsx`,
`derive/cells.ts` + `cells.test.ts`); no backend or wire change.

## Option 3 — durable checkpoint status (deferred future)

If historical/queryable per-step status is ever wanted (inspect a
long-dead generation's per-step outcomes; make step status
load-bearing), add a `status` column to the `checkpoints` table
(running written at step-request, complete/errored at step-end), with
only `complete` rows serving as replay hits. This is an
execution-semantics change (schema bump, replay/hit + crash-resume
handling, its own `execution_semantics.md` update); it composes with
item 2's watch plumbing. Not needed for the monitoring goal — option 2
covers running/errored visibility via the ephemeral log, keeping the
durable state simple.
