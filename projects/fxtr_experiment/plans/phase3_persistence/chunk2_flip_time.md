# Chunk 2 — flip-time record persistence

**Status: complete.** (218 passed + pyright clean; schema v7. See Notes
for the two design calls made while implementing: the choreography
lives *inside* ``set_status`` — one choke point, every caller inherits
the invariant — and the startup crashed-run flip follows the same
gapless choreography per row, making terminal-with-NULL-record
unrepresentable; a repair-pass design was built first and dropped in
review.)

The maintenance invariant: **a generation in a terminal status always
has its record entity persisted and pointed at** (`record_id` on
`task_execs`), so export never assembles anything.

## Scope

- Schema bump: the `record_id` column (persist-then-record, the
  liveness pattern; no migrations — pre-release, state dbs recreated).
- Assembly wired into every flip out of `running`:
  - run end (the executor's terminal flip, after the safety pass);
  - the **startup crashed-run flip** (`running → interrupted` at
    session open — the record assembles from whatever the crashed run
    durably left, which is exactly the at-rest truth);
  - **retry re-flips** (a new run of the same generation re-flips
    terminal → re-assemble, re-store, repoint; the old record entity
    stays in the store, unreferenced — version-log food).
- Turnover/clear deletes the pointer with the row; entities are
  retained (v0 retains all entities).

## Tests

- Terminal flip writes the record; its decoded content equals a fresh
  assembly from the rows.
- Kill-and-resume: the startup flip produces a record for the
  interrupted generation (reusing the existing cancel/crash-resume
  harnesses).
- Retry re-stores: two runs of one generation → two distinct record
  entities, the row pointing at the latest.
- The invariant probe: no terminal `task_execs` row with NULL
  `record_id` after any suite scenario (a cheap whole-db assertion
  usable by other tests).

Notes/log (2026-07-09, at landing):

- **The choreography lives inside `set_status`**, not in a separate
  `finish_generation` the executor calls: terminal flips assemble the
  record *at the target status* (the row still says running —
  `_build_generation_record` takes a status override), re-attach the
  referenced ids (`encoding.generation_record_ids`), persist, then
  update status + `record_id` in one transaction; flips to running
  clear the pointer in the same transaction. Every caller — executor,
  white-box tests — inherits the invariant with zero call-site changes,
  and the biconditional (`record_id` non-NULL ⇔ terminal) is global,
  not runtime-paths-only.
- **The startup crashed-run flip is the same gapless choreography, per
  row** — terminal-with-NULL-record is *unrepresentable*. First build
  had a bulk sync flip followed by a session-open "repair pass" over
  terminal-NULL rows; review caught that the only mess the pass could
  ever clean up was the mess its own flip-to-record gap created (and
  repeated mid-open crashes would even stack duplicate RunnerCrash
  rows). The fix inverts the ordering problem: the RunnerCrash fact is
  *synthesized into the assembled record in memory*, and the one final
  transaction inserts that exact failure row + flips status + points
  `record_id` together — a crash at any point leaves the row `running`
  with nothing written, retried cleanly and idempotently at the next
  open. The stored record still equals a fresh assembly of the
  post-transaction rows (the probe checks content, not just presence).
- **The invariant caught a real latent bug on landing**: one chunk-1
  test seed recorded a fn id without persisting it (a liveness-invariant
  violation that would have surfaced as a coverage crash after a
  reopen); the choreography's re-attach turns that class of violation
  into an immediate, named `StateDbError` at flip time. Every other
  existing test seed was already clean — the combined persist-then-record
  helpers did their job.
- The whole-db probe (`tests/test_flip_time.py::assert_record_invariant`
  — biconditional + stored-record ≡ fresh-assembly content) is
  importable by other suites; the executor e2e runs it over a bare
  reopen, which doubles as "the repair pass finds nothing to do".
