# Chunk 6 — watch subscriptions: the renderer rewrite

**Status: complete** (2026-07-14). Design:
`notes/event_system.md` (client integration section) + the
watch-subscriptions section of `notes/monitoring_ui.md`. Depends on
[chunk5_watch_backend.md](chunk5_watch_backend.md). The panel's read
path moves from invoke-slot queries + hint-driven refetch to
**folded watch mirrors**: per view state, one inbox per watch, events
folded into always-current mirrors, drop-the-inbox as the unsubscribe.

Decisions settled at kickoff (2026-07-14 round):

- **Single-entry watch state, not keyed maps**: with
  drop-on-navigate-immediately (no LRU in v0) there is only ever one
  live cells watch and one live cell watch, so panel state holds
  `watches: {workflow, cells, cell}` — each entry carrying its spec
  (path / path+coords); the driver replaces an entry whose spec moved.
  The keyed-map generalization returns with the LRU follow-up if ever.
- **`proto.ts` trims harder than first listed**: `root` / `attach` /
  `attach_internals` / `list_keys` go too (declared but never driven —
  only `attach_path` was); a comment points at `user_api.py` as the
  protocol's source of truth for methods a future frontend may add.
- **Header "refetch" → "resubscribe"**: drops all three watch entries
  (fresh inboxes → fresh snapshots + fresh grants). Per-watch subscribe
  failures park in their own entry with a local retry.
- **Recomputing UI**: an "updating…" marker — header-side for the
  workflow watch, table-bar-side for the grid watch; existing mirror
  data stays visible; "loading…" only before the first snapshot batch.
- **Files**: `watch_driver.ts` (the generic FSM) + `derive/folds.ts`
  (pure per-family folds, incl. the client-side 2000-entry log cap
  mirroring the buffer bound) replace `subscription.ts` +
  `derive/hints.ts`.
- **Mirrors persist in `ownStorage`** (the standard stub-safe path);
  any move to non-persisted fold state is a separate discussion, not
  this chunk.

## Scope

1. **Wire layer** (`src/wire/`): TS mirrors of the `watch_api.py`
   event unions in `types.ts`; `proto.ts` drops the methods the panel
   no longer calls (`summarize`, `list_cells`, `inspect_cell`,
   `inspect_generation`, `list_observers`, the intent/bindings lists,
   `lint`, `attach_path`) and keeps the mutation surface +
   `read_cell_entity` / `demand_target`. Subscribes go through
   `subscribe_to(capability, "watch_*", [args])` on the inbox handle
   (the chunk-5 workstation extension).

2. **Generic watch driver** (replacing `subscription.ts` and
   `derive/hints.ts`, both deleted): one FSM shape parameterized by
   (method, args, fold) — idle → allocate fresh inbox +
   `subscribe_to` in one tick → subscribing → running (drain events,
   fold into the mirror, re-anchor `inbox.sliced(n)`); reset to idle
   with a fresh inbox on `connectionState: "broken"` or a capability
   stub change; a failed subscribe parks errored with a retry. Folds
   are pure per-family functions in `derive/` (patch batches →
   keyed-map upserts/removes, `replace` sub-topics, `recomputing`
   flag, `subject_gone`, log append/reset).

3. **Panel state** (`panel_state.ts`, `PANEL_KIND` bump + structural
   check grown in the same edit): `queries`/`gridQueries`
   (`CellsEntry`/`DrawerEntry` token pinning, `mapSlotAtom`
   allocate-on-subscribe slots) are replaced by watch entries —
   always-on `workflow`; keyed `cells[path]` / `cell[cellKey]` entries
   holding `{inbox, subscribeOp, mirror, phase}`. Navigation drops
   entries immediately (no LRU in v0 — follow-up if painful); the
   reducer's `CloseCallbackOp` on unreferenced inbox slots is the
   unsubscribe. The attach cache, token-invalid heal, refetch buttons,
   and summarize-coalescing flag are all deleted; a manual
   "resubscribe" escape hatch in the header replaces "refetch".

4. **Components**: tree + header read the workflow mirror (recomputing
   indicator on the slow sub-topics; lint badge unchanged); grid
   detail + cells table read the cells mirror (`subject_gone` renders
   an explicit "grid no longer exists" state); the drawer reads the
   cell mirror (report + generation + observers + the **log lines
   section** for task cells, reset on the new-run marker). Mutations
   build `ViewSel`s from the watch payloads' grants (own grant +
   parent token) — `deriveCellActions` inputs adjust accordingly.

5. **Dialect harness**: the fixture backend grows canned watch events
   (every union variant); decode pins in `dialect.test.ts`. Spec args
   are path/coords strings — nothing new to encode-test.

6. **Demo verification** (headless CDP + browser pass): live table
   updates with NO refetch and no flash under driver mutations; crash
   → revive heals via resubscribe; delete-grid under an open detail
   pane shows the tombstone; step-log lines stream into the drawer
   (add a driver scenario step that `ctx.log()`s if none does — the
   slow checkpointed task is the natural host).

## Tests

- Vitest: per-family fold functions (snapshot fold, upsert/remove,
  replace, subject_gone, log reset); updated derive tests
  (`tree`/`cells` folds now read mirrors — shapes unchanged);
  storage-shape validity (stale `ownStorage` resets).
- The dialect decode pins above.
- Removed with their subjects: `hints.test.ts`.

## Steps

- [x] Wire mirrors + proto update + dialect pins *(2026-07-14; the
      fixture backend serves every union variant per family, encoded
      per-event exactly as the engine publishes)*
- [x] Generic driver + per-family folds *(2026-07-14 —
      `watch_driver.ts` + `derive/folds.ts`; drains fold in any live
      phase since deliveries can race the subscribe ack's fill)*
- [x] Panel state rewrite (kind bump v8) + entry lifecycle
      *(2026-07-14; single-entry slots per the kickoff decision — no
      keyed maps, no GC pass: spec changes replace the entry)*
- [x] Components on mirrors (incl. tombstones, recomputing, log lines)
      *(2026-07-14)*
- [x] Mutations off watch-payload grants; delete the attach cache
      *(2026-07-14; grid actions gate on `meta.parent_token !== null`
      — the root grid naturally offers none)*
- [x] Demo verification battery — **human-operated browser pass**
      (2026-07-14), not the CDP harness (dropped as the recommended
      approach; see below): live table updates with no refetch/flash
      under driver mutations, crash→revive resubscribe heal, delete-grid
      tombstone, step-log lines streaming into the drawer.
- [x] Rewrite `frontend/LOCAL_CODE_OVERVIEW.md` (watch conventions,
      driver, folds); update the top-level `CODE_OVERVIEW.md` pointer;
      remove the status TODOs from `notes/event_system.md` and
      `notes/monitoring_ui.md` *(2026-07-14)*

Notes/log:

- **The subscribe-arg-order bug + the `**kwargs` ergonomics fix**
  (2026-07-14, found in the live pass): the first cut kept chunk 5's
  `watch_cells(path, subscriber)` order, but the workstation's
  `subscribe_to` forwards as `method(subscriber, *args)` — subscriber
  first — so the forwarded subscriber landed in the `path` slot and
  the path string in `subscriber` (an `IRException` decoding
  `subscriber` from `'["combined"]'`). `watch_workflow` worked only
  because it has no extra args, and the backend tests missed it (they
  call the methods directly, never through `subscribe_to`). Fixed by
  making the watch methods **subscriber-first with keyword-only spec**
  AND reworking `subscribe_to` from positional `*args` to `**kwargs`
  (source/method positional-only): the spec forwards as
  `method(subscriber, **spec)`, order-independent and self-documenting.
  Touched `fxtr_workstation` (`protocols.py`, `callback_impl.py`, its
  `subscribe_to`-forwards test + `KeyedSourceProto` fixture, CODE_
  OVERVIEW) and the experiment watch surface (proto/impl/wrapper +
  event_system.md). The workstation full suite (387) confirms the
  shared-infra change is safe.
- **Known-gone `recomputing` fix** (2026-07-14): a `_CellsWatch` whose
  grid stays deleted was still slow-flagged, so each dirty pass
  published a `recomputing` marker that never settled (recompute
  short-circuits when gone). `slow` now excludes the known-gone state;
  the pass that *discovers* the deletion still announces, and its
  `subject_gone` tombstone is the settle signal (`watch_api.py`
  contract updated + a stronger tombstone test).
- **Demo `ctx.log`**: added a `ctx.log()` line to the demo's
  `slow_step` so the drawer's run-log stream has a task-authored line
  to show during the crash/resume scenario.
- **Two follow-ups noticed in the live pass** — the multi-watch update
  ripple (Q1) and generation-report streaming granularity (Q2) — are
  scoped in `chunk6post1.md` (deliberately out of this chunk).
