# Chunk 1 — base facts: schema + session ops

**Status: complete.** Landed with the scoping resolutions below (schema
v4; suite + pyright green — new coverage: sourcing CRUD + the maintained
invariant incl. cascade revoke, sourcing surviving grid deletion,
assertion recording on hit and miss + generation-cascade + the no-assert
cases (read grants, handle opens), grid_refs lifecycle, user
auto-sourcing, the un-sourced write/target rejections, and the adoption
flow). One deviation from the scope as written: no separate depth-1
orphan/unwanted probe methods were added — the claims are queryable
through `load_assertions` / `list_user_sourcing` / the observation
indexes, and the probes' real consumers (the local verdicts) land with
chunk 2's evaluator, which will define their exact shape.

## Scoping-round resolutions (2026-07-06)

- **Grid-before-cell is a maintained DB invariant, not an add-time
  guard**: a cell-level intent record (cell sourcing, target) requires a
  live grid sourcing record for its grid, *at all times*. Enforced in
  the session ops (not expressible as a sqlite FK — the grid row is the
  ``coords=''`` row of the same table): adds check the grid row exists;
  **revoking a grid's sourcing rejects while cell-level intent rows for
  that grid exist**, unless the caller opts into the cascade variant
  (deletes the dependent cell records/targets in the same transaction —
  the primitive under the user surface's future ``force`` toggle).
  Sourcing records are *not* FK'd to ``grids``: like targets, they are
  intent and survive grid deletion as dangling records.
- **User writes into un-sourced grids are rejected** (option (b)): the
  cell-sourcing upsert enforces the invariant, so a user write into a
  grid with no user sourcing record raises ("source/adopt the grid
  first"). A ``force``/``adopt`` keyword on the user-level write UX
  (auto-adopting next to ``overwrite``) is the chunk-4/UX-round
  affordance; the session/capability layer stays strict.
- **Assertions get their own table** (``grid_assertions(exec_id,
  version_id)``, insert-or-ignore) rather than a flag column on
  observations: the monotone-OR accumulates as idempotent inserts —
  including on record *hits* — without bending the "hits never touch the
  record" contract; the safety pass compares the assertion set like any
  other set (the frozen engine collects, never inserts).
- **Sourcing table shape confirmed**: ``(grid_path, coords DEFAULT '',
  fingerprint)``, PK ``(grid_path, coords)``; one record per subject;
  upsert replaces the pin; dangling derived at read time.
- **Reverse rule-ref index stores terminal paths only** (the grids the
  rule's slices actually address); enclosing-subtree breakage is covered
  by ``grounded_grid``'s parent clause — and rmdir-style ``delete_grid``
  cannot remove a parent while the referenced grid exists, so the
  terminal row is always hit first.
- **Targets gain a denormalized ``grid_path`` column** (the invariant
  checks and lint queries need targets-by-grid without decoding slice
  entities).

The extensional layer of `notes/grounding.md` — the root facts and
record-derived claims, queryable through `WorkflowSession`. No derived
predicates, no gates, no user-capability wire methods (chunk 4).

## Scope

- **Sourcing-records table** (targets-shaped, per the design): grid rows
  pinned to a config fingerprint, cell rows pinned to a value
  fingerprint; unique per subject identity; dangling derived at read time
  (pinned fingerprint vs live state), never auto-dropped. Session ops:
  add / list / revoke, plus the **auto-upsert** hook the user surface
  calls on user writes and creates (the write/create paths in
  `user_service.py` switch from `_user_sourced` responsibility rows to
  this).
- **Would-create recording** on `grid_config` observations (chunk 0's
  settled rule: the bit is "the open ran through write authority" —
  opens carry no mode — recorded **monotone-OR per generation**, so two
  opens of one grid through different-authority chains accumulate rather
  than mismatch, and the safety pass compares the final record). This
  makes `asserted_grid` derivable from the op record.
- **Reverse rule-reference index**: grid → referencing task grids
  (rule inputs, the task_fn slice, enclosing-subtree edges are derivable
  from `parent_path`), maintained on grid create/delete in the same
  transaction.
- **Drop `responsibility`** (table, actors, `record_responsibility`, the
  ops-engine and user-service call sites; update the turnover-cascade
  test): task claims are read off the op record, user claims off the
  sourcing table.
- Read-side session ops the later chunks need: claims-by-subject lookups
  ("who asserts g / who wrote d's current value / who reads c's
  outputs"), i.e. the depth-1 orphan/unwanted probes, expressed over the
  existing indexes + the new tables.

## Tests

- Schema + session-op unit tests (add/list/revoke, auto-upsert on write
  and create, uniqueness per subject, dangling on config drift and value
  overwrite).
- Lifecycle: generation turnover withdraws record-derived claims
  (inherited cascade), revocation withdraws user claims; the depth-1
  probes flip accordingly.
- Reverse-index maintenance on create/delete, including recreate.

Notes/log: (fill in during the chunk)
