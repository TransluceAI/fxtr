# Chunk 3 — frame cell ops end to end

**Status: complete (2026-07-17).** See Notes/log for the (small)
deviations.

Design source of truth: `notes/dataframes.md` (write/read paths,
representations/storage). Key settled constraints a fresh context
must not lose:

- **Identity is minted host-side after config resolution** — the
  canonical entity embeds the *declared* schema (token → grid
  config; frame outputs use the task grid's declared `FrameOutput`
  schema). The task adapter ships raw arrow IPC bytes; no
  canonical-encoding code runs task-side.
- **Write validation = `tables.validate_and_align`** (names matched
  as a set — the error is a name-*set* mismatch; exact dtypes with the
  lone `Null`-column cast; no nulls in declared-non-nullable columns,
  checked natively). A frame carries no nullability flags to match
  against (polars can't express NOT NULL); the declared schema is the
  sole authority. Then `tables.frame_to_rows` →
  `frame_encoding.build_frame_entity` mints the id.
- **Frame entity ids never surface to task code**; observations,
  conflicts, record-serving all key on the frame entity id
  runner-side.
- **Reads serve canonical order** (deterministic by construction).
- The projection is pure cache: derived, rebuildable from the
  canonical entity, never exported.

## Scope

### The wire

- **`runner_api.py` / `user_api.py`**: `FrameWireType` becomes
  `bytes` — arrow IPC **stream** bytes (name kept; the alias was
  always documented as an opaque stand-in; docstrings updated).
  `read_cell_frame` / `write_cell_frame` / `run_task` frame-output
  slots carry it. The user surface keeps `overwrite` and **gains
  `adopt`** on `write_cell_frame` (parity with the entity write —
  the same grid-before-cell sourcing invariant applies);
  `read_cell_frame`'s docstring documents `NotTargetedError` for
  task-output reads, like `read_cell_entity`.
- **`read_batch` is deleted from both protocols** (and the
  `user_service` stub). Its documented role ("the gather substrate")
  lapsed when gather became a dedicated host-stitched wire op
  (chunk 4); a future `read_many` entity fan-out would be a new op
  anyway. Kills the `FxtrID | FrameWireType` union. Update the
  deferred-items note in `notes/dataframes.md` accordingly.
- **Output-kind checks on both read ops, both capabilities**: once
  `FrameOutput` slots are real, `read_cell_entity` must reject a
  frame-output selection (it would serve the frame entity id as
  data — "frame ids never surface") and `read_cell_frame` must
  reject an entity-output selection (the projection would try to
  rebuild a non-frame entity). Grid-kind checks already fall out of
  `spec.kind`; the *output* check consults the declared
  `OutputSpec`.

### The host paths

- **Shared canonicalization helper** (`tables.py`):
  `bytes + declared schema → FrameEntity` (ipc_to_frame →
  validate_and_align → frame_to_rows → build_frame_entity). One
  body serves the engine's write op, the user write, the executor's
  results loop, and the safety pass.
- **`ops_engine.py`**:
    - `write_cell_frame`: resolve the declared schema from the
      grant (a small `spec_registry` accessor — the registry's
      `_Entry` already carries `schema` for dataframe grants,
      sourced from the stored config) → canonicalize via the shared
      helper → then the *identical* record-check/conflict/
      `wrote_cell` logic as `write_cell_entity`, comparing ids
      (replay hits canonicalize the incoming bytes just to compare
      — accepted v0 cost). Persist the entity under the liveness
      invariant (`session.write_cell` takes the `Entity`).
    - `read_cell_frame`: reuse `_read` wholesale
      (`kinds=("dataframe", "task")`) — record-check, demand hook,
      grounding read gate, `ensure_loadable` all come for free —
      then materialize id → bytes via the projection. The id never
      crosses the wire.
- **`task_service.py`**: `normalize_outputs` accepts `pl.DataFrame`
  for `FrameOutput` slots (type-check only — no schema knowledge
  task-side) and ships `tables.frame_to_ipc` bytes; `run_task`'s
  return becomes `Mapping[str, FxtrID | bytes]`.
- **`executor.py`**:
    - The results loop canonicalizes frame-output bytes host-side
      under the declared `FrameOutput` schema, stores the entity
      into the run scope, and records the frame id via
      `record_result`. A validation failure is recorded as an
      errored-class failure naming the output (a task-output
      defect, not a generic `TaskRaisedError`).
    - **The safety pass** (`executor.py` output comparison,
      `outputs != get_results`): a replayed task fn legitimately
      reassembles rows in a different order (the whole reason for
      multiset identity), so raw-bytes comparison would
      false-positive `NondeterminismError` on every frame-writing
      task. The pass canonicalizes replayed frame-output bytes to
      ids first — pure compute (`.store` into a scratch scope to
      learn the id; nothing persisted).

### The projection

- **Seam**: a tiny cache object — `get(frame_id) -> bytes | None` /
  `put(frame_id, bytes)` — behind one session method
  (`WorkflowSession.frame_bytes(id)`, get-or-rebuild:
  `ensure_loadable` → a **trusting decode** of the frame entity
  (schema + rows as stored; no re-encode, no per-value type sweep —
  every recorded frame id was minted host-side by
  `build_frame_entity`, and the store is trusted here like
  everywhere else in the system) → `tables.make_frame` →
  `tables.frame_to_ipc` → `put`). The strict validating
  `load_frame` stays as the spec's read half (tests, tooling,
  future import-time verification of foreign stores); the rebuild
  path just doesn't route through its validation. The deferred
  persistent projection (lmdb or a sqlite blob sidecar; never
  exported, wiped on import, safe to delete) drops in behind the
  same seam later.
- **v0 policy**: in-memory, **bytes-budget LRU** (entry cost =
  `len(bytes)`; an entry larger than the whole budget serves
  uncached), populated **lazily only** (rebuild-on-miss; writes do
  not populate — eager population would pay make_frame+to_ipc for
  frames that may never be read). Cleared trivially on reopen/import
  (it's in-memory).
- **polars import doctrine**: `state_store.py` reaches `tables.py`
  lazily (function-level import / lazily-constructed cache object),
  keeping `import fxtr_experiment.<core>` polars-free for
  sandboxes. (Nice-to-have, not load-bearing — noted in the module.)

### The user surface

- **`user_service.py`**: `read_cell_frame` (task outputs: the
  attached-target requirement + demand, exactly as
  `read_cell_entity`; dataframe cells: the gated read), served from
  the projection; `write_cell_frame` (canonicalize under the
  declared schema; sourcing upsert with the *frame id* as the
  value-pinned fingerprint; `overwrite` + `adopt` as for entity
  writes). Both unrecorded interactive events.
- **`view_runtime.py`**: `RuntimeDataFrameView.read_table` /
  `write_table` (`pl.DataFrame` ↔ IPC bytes at the boundary via
  `tables.frame_to_ipc` / `tables.ipc_to_frame`; framework-produced
  read bytes are trusted — no re-validation task-side;
  effective-mode gate on write as for entity writes) + the `schema`
  property (from the grant). Serves both surfaces —
  `_UserViewBackend` forwards the shared-shape methods.
- **`user_service.Workflow.demand_table(view, **coords) ->
  pl.DataFrame`**: the frame sibling of `demand` (auto-`add_target`
  + `read_cell_frame` + parse); `demand` itself stays entity-only
  (no union return).

### Non-breakage

- **Export/import**: frame entities already ride the entity-store
  export (the value entity is the whole fact); verify the round
  trip and that import starts with an empty projection and rebuilds
  lazily.
- **Introspection**: `inspect_cell` / the cell watch / `delete_cell`
  / `clear_cells` / sourcing over dataframe cells must not error
  once cells actually hold frame ids (the *preview* variant is
  chunk 5; this chunk only guarantees no crashes and sane id-level
  reports).

## Decisions confirmed at chunk start (2026-07-17)

- `read_batch`: **cut now**, both protocols (was: keep-or-cut in
  chunk 4).
- `FrameWireType` keeps its name; becomes `bytes`.
- No v0 wire-size guard for huge frames (deferred with chunked
  transport).
- User `write_cell_frame` gains `adopt`; `Workflow.demand_table`
  added.
- Projection: lazy-only population; bytes cached (not frames — bytes
  are the serving form and immutable; gather parses IPC natively).
- Rebuild uses a **trusting decode**, not `load_frame`'s full
  re-validation (initially planned; reversed on review): no path —
  wire or user — can record a frame id that wasn't minted host-side
  by `build_frame_entity`, and the rest of the system already trusts
  the store on read (`load_config`, `load_step_payload`, entity
  cells). Per-read re-proof of canonical order guarded only against
  "the database was corrupted", which breaks everything regardless.
  The strict loader survives as the spec's read half for tests /
  tooling / future import-time verification of *foreign* stores
  (where verification belongs at the trust boundary, once, not on
  every read).
- LRU budget: bytes-budget kwarg threaded
  `open_local` → `WorkflowSession.open`, default 256MB; an entry
  larger than the whole budget serves uncached.

## Tests

- Write/read round trip through a real task: canonical order served;
  permuted-multiset rewrite is an idempotent no-op; a different
  multiset conflicts; `NondeterminismError` on a record-hit payload
  mismatch.
- **Safety pass over a frame-writing task whose replay emits rows in
  a different order** (fails without host-side canonicalization of
  replayed outputs) — plus the ordinary safety-pass pass-through.
- Replay/restart: after session reopen (empty projection), a record
  hit still serves correct bytes (rebuild path).
- Frame outputs: per-output staleness (an idempotent rerun
  reproducing frame output A keeps A's observers fresh); a frame
  output failing validation records an output-naming errored
  failure.
- Output-kind mismatches: `read_cell_entity` on a `FrameOutput` and
  `read_cell_frame` on an `EntityOutput` rejected, both surfaces.
- Read-xor-write enforcement on dataframe grids; grounding read gate
  applies to frame reads.
- Null legality + name-set mismatch + kind mismatch rejected at
  write, from both surfaces; user `overwrite` and `adopt` work;
  sourcing pins the frame id; `demand_table` targets + serves.
- Projection LRU: bytes-budget eviction; oversize entry served
  uncached; hit serves without touching the entity store.
- Export/import round trip with populated frame cells.
- Introspection non-breakage over frame cells (`inspect_cell`, cell
  watch, `delete_cell`, sourcing).
- RPC smoke over a real wire (bytes cross intact); `read_batch` gone
  from both protocols (grep tests/docs for stragglers).
- Pyright clean.

## Notes/log

- **Landed as planned**, with the projection budget settled as the
  kwarg (`WorkflowSession.open(frame_cache_bytes=...)`, default
  256 MiB) and the rebuild's trusting decode as
  `load_frame(verify=False)` (the strict loader kept as the default
  and the spec's read half).
- The shared write-boundary funnel is `tables.canonical_frame_from_ipc`
  (parse → `validate_and_align` → `build_frame_entity`; normalizes
  parse failures to `EncodingError`). The engine/user write ops wrap
  it as `ViewAxisError`; the executor wraps it as `TaskOutputError`.
- The engine's write tail was factored into a shared
  `OpsEngine._write_cell` (entity and frame writes are one op past
  identity minting); reads reuse `_read` with a
  `_check_output_kind` pre-check. Consequence of check ordering: an
  *entity* read of a dataframe grid reports "read it as a frame"
  (the output-kind message) rather than the grid-kind message —
  clearer, kept.
- The executor's host-side output normalization
  (`executor._canonical_result_ids`) also closed a latent gap: the
  host previously trusted task-side `normalize_outputs` for the
  result-name set; it now validates names and wire types itself.
- `spec_registry.SpecRegistry.frame_schema(sel)` is the declared-
  schema/target-kind resolver (dataframe grants, wire-selected task
  outputs, and rule-binding pre-selected outputs).
- `FrameProjection` lives in `state_store.py` (it needs no polars);
  the polars-eager `tables.py` is imported lazily at the frame-op
  call sites (session rebuild, engine/user writes, adapter output
  encoding, view-layer parse) so core imports stay polars-free.
- Unplanned parity addition: `Workflow.overwrite_table` beside
  `overwrite`.
- Tests landed in `tests/test_frame_cell_ops.py` plus a frame leg in
  the existing RPC smoke (`test_user_surface.py`). The cell-watch
  path was verified structurally id-level (`CellReport` carries ids,
  the watch reuses the same snapshot body as `inspect_cell`) and
  covered via the inspect/summarize/lint/delete introspection test
  rather than a dedicated watch-harness test.
