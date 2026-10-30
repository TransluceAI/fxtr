# Chunk 3 — axis-semantics suite: irregular demand + staleness surgery

Scope: the suite's second half — demand structures where only a subset of
a grid's coordinate space is ever used, and precise staleness propagation
over those structures. Same style as chunk 2 (scenario tests through the
public surface; fixes are the deliverable as much as tests).

Shapes:

- **Triangular demand**: task[i, j] defined over the full square but only
  demanded for ``i <= j`` (driven by a downstream task or by the user
  demanding the triangle). Assert: no phantom work (undemanded cells have
  no generations — the open-map principle in action), presence queries
  show exactly the triangle, and demanding a below-diagonal cell later
  works fine (grids are unbounded; "irregular" is a demand pattern, not
  a schema).
- **Data-dependent selection**: a task that reads a small control cell
  and then reads only the input cells it names (the sanctioned
  data-dependent-read pattern, open question 2) — replay hits on resume,
  and changing the control cell reshapes the observation set on the
  fresh generation after a clear.
- **Staleness surgery over irregular structures**: on the triangle,
  overwrite one input feeding the diagonal → exactly the dependent
  diagonal cells stale, the rest fresh and serving; revert →
  resurrection of the whole set; clear one stale cell → only it
  recomputes; unsettled propagation through a two-stage irregular
  pipeline (stale root → dependent triangle unsettled → resolve both
  ways).
- **Mixed regular/irregular**: an aggregator over the triangle (kept-axis
  loop reads of only the computed cells) — its observation set is the
  triangle; a change *outside* the triangle does not implicate it.
- One scenario at deliberately uncomfortable scale (a few hundred cells)
  as a smoke check that nothing is accidentally quadratic in the
  in-process paths this phase keeps (the currency walk moves to the DB
  in chunk 4 — capture a rough before timing here as its motivation).

Notes/log (implementation, 2026-07-05):

- **No system bugs this chunk** — every scenario passed against the
  existing machinery: triangular demand (presence shows exactly the
  triangle; a below-diagonal cell demanded later is an ordinary new
  fact), diagonal-feeding staleness surgery (edit → exactly the diagonal
  stale; revert → whole-set resurrection with zero recomputation; clear
  one → only it recomputes), the two-stage pipeline (stale root →
  aggregator unsettled → resolved both by revert and by clear+rerun),
  data-dependent selection (the observation set follows the control,
  reshapes after a clear, and a control naming a missing cell interrupts
  then resumes with replay hits), and open-map non-implication (new
  facts outside the aggregator's triangle never touch it).
- The conditional diagonal read (``if i == j: read special``) doubles as
  a second data-dependent-read instance — chunk 1's coord views are what
  make both this and the selection scenario expressible.
- **Scale smoke (chunk 4's motivating numbers)**: a 210-cell triangle
  driven through one aggregator demand computes in ≈9s sequential /
  ≈7.5s with the reads fanned out concurrently (~20ms per generation,
  dominated by the run + safety-replay service round trips and sqlite
  writes, not by currency — concurrency in one event loop buys little
  wall time here; its value is the correctness coverage); one full
  derive-on-demand currency walk over the ~211-generation DAG after an
  edit takes **≈13ms** in-process. So at this scale the walk is cheap
  and the per-epoch memo holds up; chunk 4's dirty-bits motivation is
  the asymptotic story (walks re-derive per change epoch × per query,
  over DAGs that will be 100–1000× this) plus getting verdicts durable,
  not present-day pain.
- **Concurrency follow-up (same chunk)**: the aggregators now fan their
  reads out through ordinary anyio task groups (the sanctioned pattern —
  records replay-match as a set, so outer-level op order is free), and a
  dedicated scenario fans concurrent demands at the triangle *including
  same-cell fan-in*. Designing it exposed a real race: ``Executor.demand``
  awaited the currency gate between the in-flight check and its own
  registration, so two concurrent demands of one cell could both pass the
  check and double-run the generation. Fixed by registering before the
  first await (harmless on the serve path); the fan-in scenario pins the
  exactly-once behavior. Broader concurrent-write semantics remain open
  question 4 in `notes/design_overview.md`.
