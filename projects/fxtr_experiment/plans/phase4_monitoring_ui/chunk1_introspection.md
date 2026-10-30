# Chunk 1 — introspection additions + delete_cell

**Status: complete.**

## Scope

- **Session queries** (`state_store.py` + implementation modules beside
  `currency.py` / `lint.py` where a query has real substance):
    - `summarize`: one lock hold; every grid row (key-path, kind, own
      axes, grounding verdict + reason, sourcing status, config id)
      with per-grid rollups — populated count; running / complete /
      errored / interrupted counts (task grids); stale / unsettled
      counts (force-evaluating unstamped cells via the existing
      currency evaluation, which stamps as it goes); targeted count.
      SQL-side aggregation; no per-cell python loops beyond the
      currency recursion it already pays.
    - `generation_report`: one snapshot for one task cell — current
      generation status, failure records, observations (subject path /
      coords / output, kind, recorded fingerprint, **per-edge
      current-match flag**: matches / changed / missing, reusing the
      fingerprint-vs-settled-value comparison the currency evaluation
      performs), checkpoint entries (coords, recorded step type id,
      completed). This absorbs the deferred step-entry introspection
      affordance.
    - `list_observers`: reverse edges over the existing reverse
      observation index (`subject_versions` identity index +
      `observations_by_version`) — observer cells (grid path, coords,
      current status/currency) of one subject cell (optionally one
      output).
    - `delete_cell`: remove one populated data/handle cell's value.
      Marks: observers of `(g, c)` (deletion is a settle event — the
      existing write/delete trigger rows generalize); grounding marks
      per the sourcing-record flip. The cell's sourcing record becomes
      dangling (preserved). Task cells refuse (typed error;
      `clear_cells` is their counterpart). Bindings are untouched
      (runner config, not grid state).
- **Capability surface** (`user_api.py` result dataclasses +
  `user_service.py` impl + `Workflow` passthroughs):
    - `summarize() -> WorkflowSummary` (`GridSummary` rows).
    - `config_id: FxtrID` on `KeyInfo` and `GridSummary` (the canonical
      config entity — re-attached).
    - `inspect_generation(sel) -> GenerationReport`
      (`ObservationInfo` / `FailureInfo` / `CheckpointInfo`).
    - `list_observers(sel, output=None) -> tuple[ObserverInfo, ...]`.
    - `list_bindings() -> tuple[FxtrID, ...]` — **served from the
      binding registry, not the session** (bindings are ephemeral
      runner state; the capability impl already holds the registry);
      re-attached. Plus `bound: bool | None` on `CellReport` for
      handle cells.
    - `delete_cell(sel) -> None`.
    - All introspection presence-class (records nothing; documented on
      each method); every id-bearing return re-attaches
      (`ensure_loadable`) with membership-rationale comments.

## Tests

- Rollup correctness: drive one of the existing axis-semantics
  scenarios and assert `summarize` counts across the arc (populated /
  running (parked task) / errored / interrupted / stale / targeted);
  stamps memoize (second summarize after no change does no evaluation —
  assertable via the stamp rows).
- Generation report: a staleness arc where exactly one input changed —
  per-edge flags show the one `changed` edge, `missing` after a
  subject delete; failures and checkpoint entries listed for an
  errored / a checkpointed task.
- Observers: matches the observation records for a small fan-in;
  per-output filtering.
- `delete_cell`: observers flip stale; sourcing record dangles
  (`list_sourcing`); task-cell refusal; handle-cell delete leaves the
  binding live; grounding verdicts re-derive.
- Boundness: `list_bindings` + `CellReport.bound` across
  bind → release → re-bind.
- RPC smoke: the new methods through the existing capability-over-wire
  harness (typed decode both directions).
- Pyright clean.

Notes/log:

- The sync bodies + session-level rows landed as the new
  `introspect.py` (the `lint.py` shape); the session composes them with
  the currency/grounding evaluations under its lock.
- **`delete_cell` seeds its grounding mark *before* the row delete**:
  the citer walk needs the still-present row (clearing its stamp is
  what reaches reader generations transitively, and prune-at-unstamped
  is only sound while the fact exists) — delete-first would strand
  stamped readers, which is exactly why `_delete_grid` enumerates its
  seeds pre-cascade. The e2e test pins the observable consequence (the
  reader flips to `unanchored`, not a stale `grounded` stamp).
- **`bind_handle` now persists the handle entity at bind time** (its
  closure rides the call's arena): bindings stay ephemeral, but
  `list_bindings` serves the ids back and served ids must re-attach.
  A registry-direct `BindingRegistry.bind` (in-process library use)
  that skips persisting raises from `list_bindings` — documented at
  the call site. Also recorded in `notes/monitoring_ui.md`.
- **Rule-input grants record no `grid_config` observation** (holding ≠
  observing), so a grant-fed task's generation report shows only
  `cell_value` edges (input reads + the prologue's task-fn read);
  config edges appear when the task itself opens grids. The
  per-edge-flags test documents this.
- `delete_cell` accepts `dataframe` cells alongside data/handle.
  Noticed in passing: `revoke_cell_sourcing` refuses `dataframe`
  (kinds `("data", "handle")` only) — looks like a pre-existing
  oversight since `write_cell_frame` sources dataframe cells; flagged
  for review, not changed here.
- Summary rows enumerate grids that exist; intent records on missing
  grids stay visible via `list_targets` / `list_sourcing` / `lint`
  (documented on `GridSummary`). Subtree grids report `populated=0`
  (their cells are implicit; the UI aggregates rollups up the tree).
