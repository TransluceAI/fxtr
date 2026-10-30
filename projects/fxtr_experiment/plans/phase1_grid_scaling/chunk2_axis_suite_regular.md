# Chunk 2 — axis-semantics suite: regular shapes

Scope: hand-written scenario tests exercising every coordinate-resolution
code path on *regular* demand structures, through the public surface
(user capability + executor), with exact assertions on results, record
contents, and freshness. Iotas via `CoordView`; one or two real data
grids per scenario. Fix whatever breaks — this chunk's deliverable is as
much the bug fixes as the tests.

Shapes (escalating):

- **Shared-axis coupling**: several inputs vmapped over one task axis
  (input_a[i] and input_b[i] both feeding cell i); assert per-cell
  isolation (overwrite input_a[2] → exactly cell 2 stale).
- **Diagonal extraction**: task[i] reads matrix[i, i]; staleness of
  matrix[2, 3] implicates nothing, matrix[2, 2] implicates cell 2.
  Settled (chunk 2 design round): the *sanctioned* form is dynamic — the
  task receives the matrix with both axes kept plus a coord view, and
  reads ``(r=i, c=i)`` per cell; per-cell observation records give
  exactly the wanted staleness. Static diagonal *wiring* is deliberately
  inexpressible: name matching can't say it (per-view name uniqueness),
  and the wire's duplicate-consumption rejection is already unit-tested
  (`test_view_spec.test_bad_wiring_rejected`). The one user-expressible
  near-miss — renaming both axes to the same name — trips the ordinary
  rename-collision error; asserted in the scenario as documentation.
- **Whole-slice inputs**: a kept axis arriving in the task (task[j]
  reads row[i] for every i in a loop over a small known set, plus the
  coord view for j); per-cell observations recorded, per-cell staleness.
- **3–4 axes in one grid**, mixed consumed/kept, including renames along
  the way (local names vs official names exercised deliberately).
- **Subtree-axis nesting**: a task grid inside a subtree with its own
  axes — flattened-prefix resolution (enclosing coords + own coords),
  vmap over the subtree axis, internals under a multi-axis task cell.
- **The MH-pilot shape** (`notes/example_mh_pilot.md`) end to end at toy
  scale: subjects × users rollouts, judges × providers judging, majority
  aggregation, top-level matrix — the shape the design was sized
  against, now actually run.

Also assert throughout: presence queries report exactly the computed
cells; the safety pass survives every shape (each scenario's demand runs
the pass implicitly); resume-after-reopen works on at least one
multi-axis scenario (kills in-process residue — the boundary-discipline
reopen rule).

Notes/log (implementation, 2026-07-05):

- **Bug found (the suite working as intended)**: ``attach_internals`` on
  a multi-axis task cell was rejected by ``open_child``'s full-frame
  check — the cell coordinates are root-frame literals, and the walking
  skeleton had only ever browsed zero-axis cells. Fixed with an
  internals-only carve-out in ``check_subtree_full_frame``, marked
  ``TODO(chunk3post1)``: the discussion settled that this is a narrow
  instance of "full-frame is a property of creation capability" —
  generalized (read-only ``at()``, ViewSel-carrying opens) in
  `chunk3post1_read_sliced_subtrees.md`, scheduled after chunk 3 so both
  suites cover the refactor.
- Everything else passed against the existing machinery: shared-axis
  coupling, the dynamic diagonal (via chunk 1's coord views), whole-slice
  loop reads, 3-axis grids with renames + ``local_names``, subtree-axis
  nesting (prefix resolution, per-cell internals notes), and the MH toy
  shape end to end — including the stale → unsettled propagation, the
  strict-gate refusal, the clear-slice → recompute-against-fresh-
  neighbors lifecycle, and the reopen serving everything from disk with
  zero task invocations.
- Test-side gotchas worth remembering: a *generator expression* with
  ``await`` inside is an async generator (``"".join(...)`` then fails at
  runtime — use a list comprehension; pyright flags it); the shared
  views' ``open_handle`` is the attach-only read shape, so re-binding
  after a reopen goes through the wrapper's write-mode ``open_handle``.
