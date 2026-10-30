# Phase 1 — grid scaling: multi-cell semantics, currency in the DB

Goal: make the system solid for its core promise — distributing parallel
tasks across (high-dimensional) grids. The walking skeleton proved every
component end to end on mostly zero-axis grids; this phase stress-tests
and hardens the *coordinate* semantics (vmap coupling, whole-slice
inputs, irregular demand), adds the one missing primitive that makes
per-cell work ergonomic (`CoordView`), and then moves the two
scale-limited derived systems into the database (currency via dirty
bits; grid-level well-foundedness for `delete_grid`).

## Chunks

Each chunk lands with tests, a CODE_OVERVIEW update, and design-doc
updates as decisions become current state; details and deviations live in
the chunk's own file (promote anything design-relevant to
`notes/discussion_log.md` and the canonical docs).

- [x] [chunk1_coord.md](chunk1_coord.md) — `CoordView`: the path-less
      1D coordinate view ("iota"). Coordinates are structure, not data:
      unrecorded-pure, no staleness edges, value rides the per-run
      grant. Includes the disjoint `ViewSpec`-family split as a prestep.
- [x] [chunk2_axis_suite_regular.md](chunk2_axis_suite_regular.md) —
      axis-semantics scenario suite, regular shapes: shared-axis
      coupling, diagonals, whole-slice inputs, 3–4 axis grids,
      subtree-axis nesting, the MH-pilot shape end to end. Fix what
      breaks.
- [x] [chunk3_axis_suite_irregular.md](chunk3_axis_suite_irregular.md) —
      irregular demand structures (triangular etc.) + staleness surgery
      over them. Fix what breaks.
- [x] [chunk3post1_read_sliced_subtrees.md](chunk3post1_read_sliced_subtrees.md)
      — generalize the full-frame invariant to "creation capability ⇒
      total frame": read-mode subtree views become sliceable (read-only
      ``at()``, ViewSel-carrying opens), replacing chunk 2's temporary
      internals-only carve-out. Deliberately after the axis suites so
      their tests cover the refactor.
- [x] [chunk4_currency_in_db.md](chunk4_currency_in_db.md) — currency
      evaluation moves into the state db (currency *stamps* — the design
      round merged the planned dirty bits into stamp-nullity — cleared by
      mutations, written by verification). The chunk file carries the
      design round and its resolutions, including the
      gather-compatibility section (frames stay deferred; their currency
      story is designed).
- [x] [chunk5_well_foundedness.md](chunk5_well_foundedness.md) —
      **closed by absorption into the grounding design** (no phase-1
      implementation): grid well-foundedness became the dependency
      clause of `grounded_grid` in `notes/grounding.md`, and the open
      questions (existence cycles, recompute-on-recreate, gate
      placement, config compatibility) resolved there; the chunk file
      maps each question to its resolution. Implementation is scoped
      with the rest of the grounding work (the next phase), not here.
- [x] [chunk6_typecheck.md](chunk6_typecheck.md) — pyright gate for the
      package: pre-commit hook + fix the existing errors (inventoried in
      the chunk file); natural companion to an eventual spikes/ →
      projects/ move (move not scoped here).

## Cross-cutting decisions (settled in the phase-scoping round)

- **Dataframes / `Table` / `gather` stay deferred until a later phase.** Nothing
  here requires them: `CoordView` removes the trivial-scalar-grid
  pressure, and kept-axis loop reads cover slice consumption. The one
  coupling risk — dirty-bit machinery that gather's observation pattern
  would later break — is handled by requiring chunk 4's design round to
  settle the gather questions (see that file) before the schema lands.
- **Scenario tests, not property tests**: the suite is a semantics check
  where readability is the value — hand-written shapes with exact
  staleness assertions; no hypothesis-style randomization.
- **Well-foundedness is forward-looking and grid-level**: it never
  touches the currency of completed generations (holding ≠ observing is
  unchanged — a complete cell that never observed a deleted grid keeps
  serving). *(Partially superseded by the grounding design
  (`notes/grounding.md`): the "consult-only, no behavior change" half is
  retracted — the settled model gates structurally at mint time via
  `grounded_grid`; see the chunk 5 file for the mapping.)*
- Frames' eventual canonical encoding stays the dumb one (sorted
  lists-of-lists entity); efficient query formats (e.g. a sqlite/parquet
  projection) are a *derived* DB-maintained optimization, never
  identity-bearing. (Recorded here because chunk 4's gather section
  assumes it.)

## Status

Chunks 1–3 and 3.post1 complete (chunk 1: the ViewSpec-family split +
CoordView; chunk 2: the regular-shapes suite, which found the multi-axis
internals browse bug; chunk 3: the irregular suite — no system bugs, and
the scale smoke's timing numbers are recorded in its notes as chunk 4
input; chunk 3.post1: read-sliced subtree views — slicing sheds create
authority, ViewSel-carrying opens — plus the followup commit resolving
open question 7: child mode never exceeds the subtree grant's mode).
Chunk 4 (currency stamps in the state db) and chunk 6 (the pyright gate,
added to the plan during chunk 1) are complete. Chunk 5 closed by
absorption into the grounding design (`notes/grounding.md`; see the
chunk file) — **the phase is complete**. Phase 0 (the walking skeleton,
`../phase0_walking_skeleton/`) is complete, including the chunk 9
user-surface followups.
