# Phase 0 — walking skeleton

Goal: the thinnest end-to-end path through every designed component, so
integration risk is retired before feature work. All design inputs are
settled (see `notes/design_overview.md`, `notes/execution_semantics.md`, and
the contract sketches in `fxtr_workflow_3/`); this phase makes them touch.

## The slice

Open a `Workflow` locally (state db + entity store + runner scope) → user
creates a handle grid and a data grid, writes an input cell, binds a task
fn (in-process connector), opens a task grid wired to read the input →
demands the task cell → the task fn executes **behind a ServiceRef**, reads
its input through the run capability, and returns an output → result lands
in the entity store + state db and comes back to the user. On top of that
one path, the skeleton proves the durable-workflow properties:

- kill-and-resume mid-task (replay hits; completed checkpoints served;
  steps not re-run),
- the safety pass on completion (strict by default),
- currency (overwrite the input → demand faults; clear → fresh
  generation; revert → resurrection; stale input → unsettled dependents),
- a target surviving across sessions,
- errored vs interrupted surfacing (one root cause, interrupted
  dependents).

## Chunks

Each chunk lands with tests and a CODE_OVERVIEW update; details, running
notes, and deviations live in the chunk's own file (promote anything
design-relevant to `notes/discussion_log.md`).

- [x] [chunk1_encoding.md](chunk1_encoding.md) — canonical JSON path/coords
      codecs; canonical entity encoding of configs and target slices.
      (`encoding.py` + `tests/test_encoding.py`.)
- [x] [chunk2_state_store.md](chunk2_state_store.md) — typed access layer
      over `storage.py`; pin-then-record entity-store helper; two-file
      session bootstrap. (`state_store.py` + `tests/test_state_store.py`.)
- [x] [chunk3_view_layer.md](chunk3_view_layer.md) — task-side shadows +
      real `views.py` objects; runner-side true specs (view_spec split).
      (`view_runtime.py` + `spec_registry.py`; creation ops now carry the
      holder's vocabulary — see the chunk file.)
- [x] [chunk4_ops_engine.md](chunk4_ops_engine.md) — run-capability ops
      minus `run_step`: record-check, reads/writes/opens, conflicts,
      observations, read-xor-write. (`ops_engine.py`, tested through view
      objects over a real session.)
- [x] [chunk5_task_service.md](chunk5_task_service.md) — the service
      boundary: task-side adapter, mounted run capability, `bind_handle` +
      spawner, grant push. (`task_service.py`; wire protocols decorated —
      two vocabulary findings in the chunk file.)
- [x] [chunk6_executor.md](chunk6_executor.md) — generation lifecycle for
      **deterministic-only tasks** (no steps yet): demand path, the full
      policy matrix (currency included), safety pass, first end-to-end
      test. (Landed as reviewed pieces 6.1–6.7 — see the chunk file; four
      design rounds refined the semantics along the way: currency
      tri-state, failure records, request = observation, value-pinned
      demands.)
- [x] [chunk7_steps.md](chunk7_steps.md) — checkpoints join: `run_step`,
      step capability (connect + authority), replay, step poisoning,
      resume-mid-task property test. (Landed as pieces 7.1–7.4 plus a
      7.3 adjustment collapsing the author decorators into the adapters;
      the crash-resume flagship surfaced and fixed a served-id coverage
      bug — see the chunk file's notes.)
- [x] [chunk8_user_surface.md](chunk8_user_surface.md) — user capability +
      `Workflow` wrapper; full-slice e2e through the public surface; RPC
      smoke test (stretch — landed, not skipped). (A design round refined
      the target/introspection contracts first: fully-bound targets unique
      per slice, valueless presence queries + `inspect_cell`, atomic
      clears, `demand_target`; see the chunk file.)

## Cross-cutting decisions

- **Deterministic-first split**: tasks run end to end (chunk 6) before
  steps exist (chunk 7) — pure-computation tasks exercise the whole outer
  loop, record, and safety pass without the checkpoint machinery.
- **ServiceRefs from day one, RPC later**: every boundary crossing goes
  through mounted ServiceRefs so nothing assumes same-process execution;
  tests run in-process, with one `open_rpc_scope` smoke test at the end as
  proof (nothing cursed should be needed for the wire to just work).
- **v1 cuts** (designed, deliberately not walked): dataframes / gather /
  `Table` (entity cells only — defers canonical frame encoding),
  the interactive policy object (hardcoded defaults: retry-once errored,
  stale-faults), intent-set batching / eviction (direct await), events
  beyond a stub, version log, UI/viz, `read_batch`, handle migrations, and
  recursive delete: `delete_grid` is **rmdir-style by spec in v1** — it
  deletes exactly one grid and *refuses* if child grids exist (indexed
  `parent_path` emptiness check) or a generation inside is running. Fully
  conformant with no partially-deleted states; recursion arrives later as
  a convenience layered over the primitive (parent-chain walk).
- Single runner process; WAL; one transaction per effectful op; the
  record-check runs against the generation's in-memory record.

## Status

**Phase 0 complete.** Chunks 1–8 done (encoding; state store; view
layer; ops engine; the service boundary; the executor — full demand
policy with the strict currency gate, poisoning/failure records,
request = observation, value-pinned demands, and the always-on safety
pass; steps + checkpoints — durable `run_step`, the step capability with
connect-authority and poisoning, kill-and-resume mid-task proven both
cancel-based and across a simulated crash/session reopen; the user
surface — capability + `Workflow` wrapper + `open_local`, the full slice
end to end through the public surface, durable targets across sessions,
and the RPC smoke test). Every durable-workflow property listed above is
tested. Next: phase 1 (`../phase1_grid_scaling/overview.md`) —
multi-cell grid semantics, CoordView, currency in the DB,
well-foundedness.
