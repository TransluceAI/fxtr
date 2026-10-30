# Chunk 4 — import

**Status: complete.** (228 passed + pyright clean: `importer.py` — the
three phases per the pre-chunk round: `read_layout` (strict parse,
file/row-named `ImportFormatError`s, marker/version refusals) →
`verify_and_decode` (every referenced id proved present, all misses in
one `MissingEntitiesError`; checked-redundancy + cross-row laws
verified against decoded configs/records/slices) →
`apply_import_body` as one locked transaction via
`WorkflowSession.apply_import` (`NotAtRestError` /
`StateNotEmptyError`; the wipe keeps the events log and appends the
import event; fresh exec_ids, re-interned versions, rebuilt
parent_path/grid_refs, zero stamps) — served as
`UserCapabilityProto.import_state(directory, force)` → `ImportReport`
counts + the `Workflow` pass-through; `tests/test_import.py`.)

## Scope

- **The destructive capability op**: sets the current state to the
  imported layout. Refuses if anything is running; refuses on a
  non-empty state db unless `force=True` (wipe + load in one
  transaction; the backup affordance is doctrine — export first, git
  is the backup). Event-logged.
- **The loader**: manifest version check first; grids → cells →
  generations (record entities decoded to rows; exec_ids assigned
  fresh, versions re-interned) → sourcing → targets.
- **Derived rebuilds**: `parent_path` from key paths, `grid_refs` from
  decoded configs, denormalized target locations from slice entities;
  **zero stamps imported** (everything conservatively unstamped —
  verdicts re-derive on demand; `lint` is the what-did-I-just-load
  check).
- **Validation**: missing entities → a typed error naming the ids
  (resolved against the shared store before any write); readable
  redundant columns checked against their derivations; malformed rows
  → typed errors with file/line.
- **`ImportReport`**: counts per fact kind, nothing else.
- Documented consequence: outstanding view grants may dangle after an
  import (the `delete_grid` analogy — ops fault at use time; the root
  grant survives).

## Tests

- Import into empty; the force wipe; the non-empty refusal; the
  running refusal.
- Missing-entity and version-mismatch errors (typed, named).
- Derived rebuilds correct (grid_refs match decoded rules; a
  post-import lint runs and matches expectations).
- Counts in the report match the layout.

Notes/log (2026-07-09, at landing):

- **The events log survives the force wipe** (the pre-chunk call):
  provenance is history, not state — wiping it as a side effect of
  restoring state would erase the record that an import ever happened.
  Correspondingly "empty db" means the fact/intent tables, events
  ignored.
- **Strictness extends to file canon**: import enforces sorted+unique
  rows, exact column counts, trailing newlines, and omit-if-empty
  (present-but-empty TSVs refuse) — hand-edits must preserve the
  canonical form byte-identity depends on anyway.
- Target locations are resolved by a local all-literals reader rather
  than `ops_engine.resolve_templates` (a runtime import would cycle
  through `state_store`); targets are fully bound, so anything
  non-literal is itself a format error.
- The record→rows expansion is what makes the flip-time invariant hold
  by construction post-import (the rows ARE the record); the
  `assert_record_invariant` probe runs in the round-trip test.
