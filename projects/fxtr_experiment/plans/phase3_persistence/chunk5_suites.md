# Chunk 5 — the suites

**Status: complete.** (230 passed + pyright clean;
`tests/test_at_rest_e2e.py`. The plan's separate round-trip /
re-derivation / variety bullets consolidated into the **messy-state
round-trip** — one state carrying errored + interrupted + orphaned +
dangling facts, internals grids, and a checkpointed step, asserted
verdict-for-verdict (`LintReport` and `CellReport` equality across the
import) and byte-for-byte on re-export, with the flip-time probe — and
the **real-git e2e** landed exactly as scoped: commit A partial, commit
B mixed cached/new with the A→B diff asserted file-precisely, then
`git reset --hard` + force-import in both directions — restore-to-
partial re-runs only what A lacked and regenerates B's values
(byte-equal everywhere except the re-run generations' fresh uuids),
restore-to-completed serves with zero re-runs. The `.git` directory
rides inside the export dir for free — dot-entries are invisible to the
ownership validator, so the export dir literally is the repo.)

The phase's end-to-end claims, pinned. (Unit coverage lands with each
chunk; this chunk is the cross-cutting behavior.)

## Scope / tests

- **Round-trip byte identity**: `export ∘ import ∘ export` is
  byte-identical, across a rich at-rest state (multi-cell task grids,
  dataframes, handles, checkpoints, subtrees/internals).
- **Re-derivation equivalence**: after export → force-import (and
  separately across a session reopen + import into a fresh db backed
  by the same store): fresh generations re-derive *fresh with no
  re-runs* (task-fn call counts unchanged), and the post-import
  `LintReport` equals the pre-export one.
- **At-rest variety round-trips**: errored (failure records intact),
  interrupted (unresolved request edges intact), ungrounded facts,
  dangling sourcing records and targets — any at-rest state
  serializes; nothing is normalized away.
- **The real-git e2e** (the collaboration story, both restore flavors
  in one arc). Pipeline `data → task1 → task2` with task1 multi-cell:
  1. Write the data cells; run a *subset* of task1's cells; export;
     `git init` + commit (**commit A** — a partial grid: some cells
     with generations, some without, task2 empty).
  2. The larger run: demand task2 (and the remaining task1 targets) —
     task1's done cells **serve from state** (call counts pin no
     re-run), the extra task1 cells execute, task2 computes over the
     mix; export; **commit B**. The A→B diff is row-level and
     meaningful (new `generations.tsv`/`cells.tsv` rows; untouched
     rows byte-identical).
  3. Restore-to-completed: checkout B + force-import → everything
     serves fresh with zero re-runs (**result fidelity**: the read
     side of imported state).
  4. Restore-to-partial: checkout A + force-import → the hard-reset
     flow onto the partial state; re-run the larger flow — task1's
     imported subset serves without re-running, the rest re-executes,
     and every value equals commit B's (content-addressed equality;
     **definition fidelity**: imported rules/wiring/intent are
     *executable*, not merely readable — mint, gates, and cross-task
     reads all run over imported rows, task2 reading task1's imported
     partial results).
  Note: regenerated generations mint fresh uuids, so
  `generations.tsv` is never byte-compared across a re-run — byte
  identity is only ever claimed for exports of the *same* state;
  value/cell equality carries leg 4.

Notes/log (2026-07-09, at landing):

- **Bindings survive an in-process import** (a pleasant discovery worth
  knowing): the registry keys connectors by handle entity id, and
  imported handle cells carry the same content-addressed keys — so
  re-binding is only needed across a process boundary, exactly like a
  session reopen. The git e2e leans on this (no re-bind between
  restores).
- The restore-to-partial leg re-creates `t2` via the same `open_task`
  call (commit A predates the grid); identical rule ⇒ identical config
  ⇒ the re-demand regenerates B's values with content-addressed
  equality doing the verification.
- Verdict-for-verdict equality across import is a strong assertion:
  `LintReport` dataclass equality covers dangling statuses, target
  findings, the ungrounded inventory *and their diagnosis strings*;
  `CellReport` equality covers status/currency/reasons/results. Both
  held with no normalization anywhere.

Stress follow-up (same day, post-landing review): three more scenarios
closing the gaps the first pair left —

- **Divergent versions** (same paths, different bytes): V1 → V1_stale
  (input overwritten, nothing re-run) → V2 (cleared + re-run). Exact
  differing-file sets per pair — including that V1→V1_stale touches
  *only* the input files, `generations.tsv` byte-identical: staleness
  is derived, never stored, visible in the bytes — and row-level
  assertions (the untouched sibling cell's row identical across all
  three, the re-run cell's row changed). A **second workflow database**
  imports V2 first (so V1 was never its session state), then
  force-imports V1 and reads the old value — the flip can only have
  come from git; a junk mutation before the force-import proves the
  wipe wipes. Stale-at-rest round-trips with its verdict and diagnosis;
  clear + re-run converges to V2's value.
- **Cross-database collaboration**: B imports A's partial commit,
  extends it (the rest of task1 + task2), commits; A re-imports and
  serves the extension with zero re-runs — then overwrites an input,
  clears, re-runs: imported state is live, not read-only.
- **Resume-after-import** (the crashed-here-resumed-there story): a
  generation interrupted *after* a completed checkpoint step, exported,
  imported into a fresh db; the missing input supplied; re-demand
  resumes — the step replays from the *imported* checkpoint row and the
  counter proves it executed exactly once across fault → export →
  import → resume → completion, safety replay included.
- One test-construction gotcha worth remembering: import-into-empty
  must precede handle opens/binds in a fresh workflow (`open_handle`
  creates the grid, making the db non-empty and the no-force import
  correctly refuse); post-import binding attaches, and the same-value
  handle writes are idempotent.
