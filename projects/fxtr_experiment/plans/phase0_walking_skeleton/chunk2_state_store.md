# Chunk 2 — state store + two-file session

Scope: the typed durable layer the runner talks to.

- A `StateStore` class over `storage.py` (fxtr2 connection-pool utils; WAL;
  `PRAGMA foreign_keys=ON`; one transaction per logical op): grid
  create/lookup/delete, cell read/write, exec lifecycle (insert generation,
  status transitions, turnover-delete, startup running→interrupted flip),
  subject-version interning, observation/responsibility recording,
  checkpoint entries, targets, event append.
- Entity-store integration: open the fxtr sqlite store alongside;
  `persist_and_record(...)` helper enforcing the liveness invariant
  (pin closure — pin key = id — *then* record the id).
- Session bootstrap: open/create both files, schema init/version check,
  runner root scope wiring (entity store scope attached).

Tests: op-level store tests (most schema tests migrate to go through the
store), liveness-invariant ordering (crash window leaves orphan entity,
never dangling id — simulate by splitting the helper), startup flip.

Notes/log:
- Done (`fxtr_workflow_3/state_store.py`, `tests/test_state_store.py`,
  5 tests; 44 total green).
- `WorkflowSession.open(state_db_path, entity_db_path)` async CM: opens the
  fxtr `SQLiteFxtrNamespace` ("workflow" ns) + one dedicated state-db
  connection; schema init/version check; startup running→interrupted flip.
- **Deviation from plan**: skipped fxtr2's `ConnectionWorkerPool` (worker
  lifecycle ceremony) in favor of one dedicated connection guarded by
  `anyio.Lock` + `to_thread` — v0 is single-writer; the pool can slot in
  behind `_run()` later without API change.
- The liveness invariant is one method (`persist`: pin key = id string,
  `overwrite=True` = idempotent) composed into the combined helpers
  (`create_grid`, `write_cell`, `put_checkpoint`) so persist-then-record
  ordering is structural. Cross-session test proves recorded ids resolve
  from a fresh session.
- FxtrID blobs are stored as id-*string* bytes (round-trips via
  `from_id_string`); raw-bytes compaction is a later micro-optimization.
- The runner attaches to entity data via `session.entity_scope` (standard
  fxtr2 scope inheritance).
