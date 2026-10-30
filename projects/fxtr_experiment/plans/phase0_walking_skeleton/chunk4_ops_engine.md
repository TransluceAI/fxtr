# Chunk 4 — ops engine (run capability minus run_step)

Scope: the record-checked effectful ops, driven directly by tests (no task
execution yet — a test harness plays the generation).

- The per-generation **record** structure (in-memory, loaded via the
  forward query; keyed by subject location) + universal record-check: hit
  serves / miss goes live and records, hit-mismatch rules
  (NondeterminismError), per-subject coherence.
- Ops: `read_cell_entity`, `read_handle_key`, `write_cell_entity`
  (conflict vs nondeterminism split), the opens (create-or-attach on
  canonical config; `open_task` through the canonicalizer; `open_handle`
  attach-only; internals opens), `read-xor-write` per-grid op rule.
- Observation/responsibility recording through the state store; grid_config
  observations at the coords='' location.
- Error classification hooks (CellFaultError vs integrity errors) — the
  poisoning *persistence* lands with the executor (chunk 6); here ops just
  raise correctly.

Tests: record-check semantics (hit/miss/mismatch), write conflict vs
self-nondeterminism, open idempotence + config mismatch, regularity via
canonicalizer, read-xor-write violations, coherence under repeated reads.

Decisions at implementation start:
- `OpsEngine` is **per generation**: constructed with (session, registry,
  exec_id, generation, holder_coords) — the executing cell's flattened
  coordinates are the only cell-dependent input, used solely to resolve
  spec `Param`s to absolute locations at *op* time (never at
  canonicalization time, preserving P2).
- The in-memory record loads via the forward query at construction —
  resume is just re-constructing the engine.
- Task-output reads delegate to a pluggable **demand hook** (chunk 6's
  executor); with no hook, a not-complete target faults (CellFaultError).
  Staleness evaluation is also chunk 6; chunk 4 freshness = status only.
- New error class `ReadWriteExclusionError(WorkflowError)` for the
  per-generation per-grid read-xor-write violation (errored-classified).
- `state_store` grows `record_result` / `get_results` (task outputs were
  not yet persistable).
- The internals spec (opens_internals ViewSpec for `own_internals`) is
  built by the *caller* (executor in chunk 6; the test harness here).

Notes/log:
- Done (`ops_engine.py`, `tests/test_ops_engine.py`, 7 tests / 57 total).
- The tests drive the engine through *runtime view objects over a real
  WorkflowSession* — specs, wire values, record-check, canonicalizer, and
  storage all real; only the executor is played by the harness.
- Hit-kind rules sharpened during implementation: a read hitting your own
  ``wrote_cell`` record raises ReadWriteExclusionError (serving it would
  legitimize read-your-own-write); a write hitting a ``cell_value`` record
  likewise; a write hitting a ``wrote_cell`` record with a different value
  is NondeterminismError.
- View layer now gates writes on the shadow's *effective* mode (the
  task-side self-restriction), while the engine checks the grant's mode
  (authority) — the two-level enforcement the mode design intended.
- Properties covered: per-subject coherence incl. resume (external edit
  invisible to the generation; a fresh generation sees it), write conflict
  vs self-nondeterminism, read-xor-write both directions, open idempotence
  + own-record NondeterminismError vs live GridConfigMismatchError,
  handle attach-only, checkpoint-requires-internals, open_task through the
  canonicalizer with durable canonical config, task-output reads (absent →
  fault; complete → served; demand hook consulted).
