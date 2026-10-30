# Chunk 4 — the user surface

**Status: complete.** (191 passed + pyright clean; the
demand-requires-target rule's 25 intended test breaks were converted to
targets per each test's intent — all on user-created, auto-sourced
grids, so none needed adoption or inspect conversion — and the new
`tests/test_sourcing_surface.py` covers the CRUD lifecycle + force
cascade, both dangling statuses, adopt-on-write recording both records,
source_cell on task-written values, idempotent-write re-sourcing, the
NotTargetedError / auto-target flows, the internals refusal, the
DeleteReport, one-snapshot verdict surfacing, and the RPC smoke incl.
keyword args on the new proto methods surviving the real transport.)
**Runs before chunk 3** (2026-07-07 order swap:
landing the vocabulary first lets the gate chunk's test updates use
sourcing/adoption/auto-target language — though this chunk carried
one behavior change of its own, the demand-requires-target rule).

## Scoping resolutions (2026-07-07)

- **User demands require attached targets, capability-enforced** (the
  method declares the intent; state never substitutes — the
  wanted-via-another-reader channel is deliberately not a user-demand
  side door; introspection is how existing values are looked at). New
  typed interactive error (`NotTargetedError`). Corollary: internals
  outputs are never demandable from the top (internals grids are never
  sourceable → never targetable) — `inspect_cell`-only, as law.
- **Adoption pins what is stored**: `source_grid` pins the current
  config, `source_cell` the current value; there is no proposed-config
  sourcing (proposing shapes is what `open_*` is for).
- **`source_grid` refuses task-internals grids** (adopting a task's
  private state as user intent breaks ownership and would make replay
  install internals at phase 0).
- **`inspect_cell` serves one snapshot across both axes**: the combined
  currency+grounding+value session op lands here (non-faulting form);
  chunk 3 reuses the same composition for its serve-or-fail gate.
- **`delete_grid` returns a `DeleteReport`** naming the referrer grids
  whose grounding flips (pre-delete enumeration; proceed-and-report —
  the refuse/`force` toggle stays with the UX round).
- **`list_sourcing` mirrors `list_targets`**: pinned fingerprints +
  attached / dangling-missing / dangling-mismatched statuses; served ids
  re-attached per the boundary checklist.
- **`adopt=` spreads to `add_target` and the demand wrapper, default
  False everywhere** (auto-adopt-by-default considered and rejected:
  unlike auto-target — a cell-level record of a cell-level act —
  adoption is a grid-level ownership claim with replay consequences
  that a cell-level act must not manufacture silently; it would hollow
  out the grid-before-cell invariant on its own default path, and its
  friction is rare-and-informative rather than common-path ceremony.
  The explicit kwarg makes recovery one word; a
  ``Workflow(auto_adopt=True)`` construction knob is the escape valve
  if usage ever earns it).

The intent-maintenance API, per the "maintaining the root facts" section
of `notes/grounding.md`. New wire methods follow the checklist in
`notes/boundary_discipline.md`.

## Scope

- **Sourcing CRUD on the user capability**: add / list / revoke for grid-
  and cell-sourcing records (list includes dangling status, mirroring
  `list_targets`); auto-upsert on user writes/creates already lands in
  chunk 1 — this exposes the explicit surface, including **adoption**
  (sourcing a fact the user did not author).
- **Idempotent writes upsert too**: a user write of the value already
  present currently short-circuits *before* the sourcing upsert, so
  re-writing a task-written value does not claim it. Writing is sourcing
  regardless of whether bytes changed — move the upsert ahead of the
  idempotence return (the invariant pre-check already runs first).
- **The grid-before-cell invariant** (API-enforced, not semantic):
  `add_target` and cell-sourcing require a user sourcing record for the
  grid first.
- **Demand auto-target, default on** (the 2026-07-07 resolution): the
  `Workflow` demand wrapper adds the target by default (`target=False`
  opts out); the wire capability stays mutation-free (no read ever
  writes intent state at the capability layer; the event log records
  auto-adds). Auto-target respects the grid-before-cell invariant —
  demanding into a task-created grid faults with "adopt first", never
  silently adopts. There is **no separate launch-requires-target
  enforcement**: chunk 3's strict gate + non-self-vouching user demands
  make launch-without-target fault emergently.
- **Verdict surfacing**: grounding verdict + reason join `KeyInfo`
  (grids), `CellInfo`, `CellReport` (`inspect_cell` serves value +
  currency + grounding from one snapshot), and `TargetInfo` (dangling vs
  wanted-but-unanchored made distinguishable).
- **`delete_grid` consequence reporting**: the call reports the
  dependents whose grounding flips (via the reverse rule-reference
  index); refuse-vs-notify `force` behavior stays deferred to the UX
  round — v0 proceeds-and-reports.

## Tests

- Sourcing CRUD incl. adoption and revocation flows over RPC (the smoke
  pattern from the phase-0 user-surface work) and across session reopen.
- Invariant enforcement (target/cell-source without grid sourcing
  rejected; adoption unlocks it).
- Launch-requires-target: read-of-fresh needs nothing; execute-path
  demands refuse without a target; auto-target flag records one;
  cascaded interior cells exempt.
- Introspection snapshots carry coherent (value, currency, grounding)
  triples; delete reporting names the flipped dependents.

Notes/log: (fill in during the chunk)
