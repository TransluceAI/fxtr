# Chunk 4 — gather

**Status: complete (2026-07-17).** See Notes/log for deviations.

Design source of truth: `notes/dataframes.md` §Gather. The settled
constraints a fresh context must not lose:

- **One wire op, host-stitched**: the request is a *deduped key
  table* (arrow bytes, only the axis-binding columns) against the
  granted token; the host resolves the distinct addressed cells,
  runs record-check / demand / fault-on-missing, records one
  `cell_value` observation per distinct cell, and returns **one**
  stitched arrow stream (key columns + cell schema columns) in
  canonical order — sorted keys, each cell's rows in canonical row
  order, so input key order is irrelevant (this absorbs sender-side
  dedup nondeterminism).
- **The local key-join is view-runtime sugar** (framework code in
  the task's process): extract + dedupe keys → wire → join the
  stream back onto the caller's table; `column_prefix` and collision
  checks are local (they need the driving schema the host never
  sees); output order pins to driving-row order × stream order.
- Semantics: missing/errored cells fault; empty cells contribute
  zero rows (**join arity**: a driving row whose cell holds a
  zero-row frame produces zero output rows — inner-join shape, no
  null-padding; gather never manufactures nulls — loud docstring
  line); null key values are op errors; key columns must be str/int
  exactly matching axis kinds.
- The user capability gets the same op as an unrecorded interactive
  event; one host resolution shape serves both surfaces.
- Task code cannot observe where the join happens — the split is a
  transport property, revisitable without semantic change.

## Decisions settled at chunk start (2026-07-17)

- **Resolution is concurrent.** The op commits **all request edges up
  front** (request = observation: the gather asked for every
  addressed cell), partitions record hits from misses in memory, then
  resolves the misses in one anyio task group — task subjects through
  the demand hook, dataframe subjects through the gated read —
  recording each observation as it resolves. Nothing recorded depends
  on resolution order (observations/requests/safety-pass comparisons
  are all set-shaped; the response stream is sorted after the fact),
  the executor's in-flight map already dedups concurrent demands of a
  shared upstream cell, and gather fan-out is exactly where parallel
  demand pays. No v0 concurrency cap (task authors own their own rate
  limits; a global limit is a deferred executor-level scheduling
  knob).
- **Failure shape under concurrency** *(revised post-review — the
  original "re-raise an error-class-preferred representative" design
  was a smell: classification rode the escaping exception via pick
  logic, and concurrent multi-failures lost all rows but one)*:
  failure semantics are **per request**, matching k parallel single
  reads on the durable side — each failing cell records its own
  failure row at raise time, classified from its own typed exception
  (the generation's status then derives from the recorded set, error
  outranks fault, with no picking anywhere). The op raises one
  *synthesized* `GatherFailedError` naming every failed cell, chained
  from the exception group — a signal for already-recorded causes,
  itself never persisted (the `StepInvocationFailedError` precedent),
  and the uniform surface shape on the user capability (nothing
  records there; singleton unwrapping was considered and rejected —
  robust callers need the `GatherFailedError` branch regardless).
  Cancellation stays op-grain: sibling/external cancellation records
  nothing per cell (the per-cell asked-but-unanswered facts are the
  request edges); an externally cancelled gather records the op
  wrapper's single `RequestAbortedError`. Cancelled
  siblings leave asked-but-unanswered request edges: the normal
  interrupted shape; a resumed identical gather re-asks the same set,
  replays resolved cells as hits, retries the rest. Partial
  observations recorded before a fault stand (they are honest
  observations).
- **The batch is not one snapshot**: each cell's check-and-serve is
  individually value-pinned (the existing per-cell contract); cross-
  cell turnover mid-gather surfaces as ordinary staleness. Accepted —
  an atomic batch would hold the session lock across arbitrary
  upstream execution.
- **User-surface rule = the single-read rule, mechanically, per
  cell**: task-output cells require an attached target (before any
  demand, as for single reads); dataframe cells go through the
  grounding read gate. **No auto-target sugar** (no `demand_gather`
  wrapper): the target-expression DSL sketched in the discussion log
  (end-of-phase revisit) would obsolete it; users can add per-cell
  targets themselves meanwhile.
- **Wire form**: `gather(sel, keys: FrameWireType) -> FrameWireType`
  on both protocols. The key table's columns are named positionally
  (`"0"`, `"1"`, … for the grant's open positions in order — no axis
  names cross); `sel` may carry `at()`-bound positions and the keys
  bind the rest. The response carries the same positional key columns
  plus the cell schema columns, cells concatenated in
  sorted-coords-text order, each cell's rows in canonical order. The
  host re-validates authoritatively (column count vs open positions,
  exact dtypes vs axis kinds, null keys) — `ViewAxisError`
  (error-class); missing cells stay `CellFaultError` (fault-class).
  Duplicate keys: the host dedups (never rejects). An empty key table
  is legal (zero cells, zero observations, an empty correctly-typed
  stream); a zero-open-axes gather is legal (degenerate single-cell
  cross join).
- **Stitching**: host-side, per-cell projection bytes
  (`WorkflowSession.frame_bytes`) parsed and concatenated natively
  with literal key columns attached; one `frame_to_ipc` out. (A
  batched projection fetch belongs to the deferred persistent
  projection.)
- **The local join**: attach a row-index to the driving frame,
  inner-join on the key columns, sort by the index, drop it —
  driving-order × canonical-order by construction, polars-version
  drift immaterial (absorbed at the next write by the canonical sort,
  as decided for the split). `column_prefix` + post-prefix collision
  checks local; key columns type/null-checked locally too (better
  errors; the host re-checks).

## Scope

- **`runner_api.py` / `user_api.py`**: the `gather` wire op (contract
  docstrings; the request/response forms above).
- **`ops_engine.py`**: the effectful gather op (one `_effectful_op` =
  one obligation): kind checks via `frame_schema` (dataframe grants
  and frame-output task grants), key-table validation, read-side
  claim, upfront request edges, hit/miss partition, frontier guard on
  misses (the frozen safety-pass engine replays a recorded gather as
  pure hits), concurrent miss resolution, per-cell observations,
  stitched response. Shares the per-subject serve logic with `_read`
  (small refactor).
- **`user_service.py`**: the unrecorded interactive twin (per-cell
  target checks / gated reads, concurrent resolution, same stitch).
- **`view_runtime.py`**: `RuntimeDataFrameView.gather` — the `on=`
  name resolution to open positions, key extraction + local dedupe,
  the wire call, the local key-join + `column_prefix`/collision
  checks. `views.py` docstring updates as needed (the empty-cell join
  arity line).
- **`spec_registry.py`**: whatever small accessor the key→subject
  resolution needs beyond chunk 3's `frame_schema` (open-position
  axis kinds for validation).
- Doc updates: `notes/dataframes.md` gather section becomes current
  state (wire form, concurrency, join arity); CODE_OVERVIEW.

## Tests

- The MH-aggregate-shaped scenario: domain-cross driving table,
  gather over an un-run judge task grid's frame outputs forces
  execution of exactly the addressed cells, one observation per
  distinct cell, aggregate written as a frame — deterministic result
  across re-runs; the safety pass passes over the gathering task.
- **Concurrent demand proof**: two gathered cells whose task fns
  rendezvous (each waits for the other to start, via a shared
  binding object) — completes under concurrent resolution, would
  deadlock sequentially.
- Fan-in dedup: many driving rows, few cells — observations and wire
  payloads scale with cells, not rows; duplicate request keys served
  once.
- Missing cell faults (fault-class, poisons interrupted); a fault
  mid-batch leaves earlier observations recorded and a resumed
  gather replays them as hits.
- Empty cell contributes zero output rows (its driving rows vanish;
  no null-padding); empty key table; zero-open-axes gather.
- Null key errors; kind-mismatch key column; prefix collision;
  `on=` not covering the open axes.
- Replay: a second identical gather in the same generation is a pure
  record hit (no demand); read-xor-write claims the grid.
- User-surface gather: dataframe-grid gather (gated), task-output
  gather requires per-cell targets; a gather through the RPC wire
  (small leg on the existing smoke).
- Pyright clean.

## Notes/log

- **Landed as planned.** The gather mechanics (key extraction/
  validation, host stitch, local join) live in `tables.py`
  (`extract_keys` / `decode_key_table` / `stitch_gather` /
  `gather_join`); the group-failure pick is
  `errors.batch_representative` (shared by both capabilities).
- Two small rules added during implementation:
  - **The sanctioned `Null` cast extends to key columns** (all-null —
    typically an *empty* literal driving frame — takes the axis
    dtype on both the request path and the local join; actual null
    values still error). Without it, gathering with an empty
    driving table built from literals fails on polars' Null-dtype
    inference.
  - **Each axis binds from a distinct driving column** (`on=` values
    must be unique — two axes from one column would collide in the
    join rename; users duplicate the column explicitly if wanted).
- **Bug found by the local join**: `RuntimeDataFrameView.schema` only
  handled dataframe grants; on an `output_frame()`-derived view the
  shadow's grant is the *task* grant (schema rides `outputs`, not
  `grant.schema`). Fixed to resolve all three grant shapes
  (dataframe grant / pre-selected rule-binding grant / task-side
  output selection).
- The engine op resolves per-key subjects by merging each key into
  `sel.bound` and reusing `sliced` + `_resolve_cell` — no new
  registry machinery beyond chunk 3's `frame_schema`.
- The rendezvous test (two cells whose task fns each wait for the
  other to start) pins the concurrency decision: it deadlocks under
  sequential per-cell demand.
