# Chunk 4 — currency evaluation in the database

Scope: replace the v0 in-process currency walk (one support-DAG recursion
per change epoch, memoized per process) with materialized dirty bits +
lazy re-verification *in the state db*. This file carries the design
round: the proposal below (one preferred design; alternatives noted where
they were considered), the gather-compatibility check, and the remaining
discussion points. Once settled, the outcome gets written up as current
state (an `execution_semantics.md` section or standing note) as part of
landing the chunk.

## What we know (carried from the deferral note + chunk 6 design)

- **The semantics do not change.** Fresh / stale / unsettled, well-founded
  freshness, value-pinned reports, the strict demand gate — all identical;
  chunks 2–3's suite is the regression bed, and the
  `currency.CurrencyReport` interface was designed to survive this swap
  unchanged (only the evaluator's internals move).
- **Crash safety for free**: marks are written in the same transaction as
  the mutation that caused them (single-writer session, one transaction
  per op), so dirty state can never under-report across a crash.
- **Running-generation record traffic doesn't mark** (today's epoch rule,
  preserved): a running generation's record inserts can only affect its
  own verdict, and stamps are restricted to settled generations (below),
  so nothing cached depends on an in-flight record.
- One evaluation = one snapshot: composite read bodies keep running under
  `run_readonly`; value pinning is unchanged.

## The proposal: generation-level dirty bits, eager transactional marking

### The two invariants

- **I1 (the dirty invariant).** For every generation `G` with a stamped
  verdict: if any settled, currency-relevant change since `G`'s stamp
  could alter `G`'s verdict, then `G.dirty = 1`. Contrapositive — the
  whole point: `dirty = 0` ⇒ the stamped verdict is current, serve it
  with no walk. Dirty is a conservative over-approximation; *stamps are
  ground truth only while clean*.
- **I2 (no fresh edges onto dirty support).** A new task-output
  observation edge only ever attaches to a *verified-fresh* upstream:
  every record-miss task-output read goes through the demand hook, whose
  strict gate verifies the upstream (clearing its dirty bit) before
  serving. This is chunk 6's freshness invariant doing double duty — it
  is what makes marking prunable (below).

### Granularity: marks per generation, precision per edge

Cells (generations) are the unit of execution status and turnover, so
they are the unit of *marking*. Per-output precision is not lost — it
already lives in the **observation edges**, whose subjects are
`(grid, coords, output)` triples with recorded fingerprints:
*verification* compares edge by edge. Consequence: marking never needs to
know which output changed. A rerun that leaves output B byte-identical
conservatively marks B's observers dirty, and their next verification
compares fingerprints and stamps them right back to fresh — no rerun,
same mechanism as value-level resurrection today. (The rejected
alternative — per-edge validity bits — buys nothing verification doesn't
already provide, and multiplies write amplification by the edge count.)

### Schema deltas

- `task_execs` gains: `dirty` (default 1 at insert), `verified_currency`
  (`fresh|stale|unsettled`, NULL until first stamp), `verified_reason`
  (the diagnosis, NULL iff fresh). A `verified_at` revision/timestamp is
  observability metadata only — correctness never consults it (I1 makes
  the dirty bit the sole invalidation channel; the global epoch counter
  and the in-process evaluator memo are **deleted**, not layered).
- An index on `subject_versions (grid_path, coords)` — marking resolves
  "who observed any version of this subject", joining through the
  existing `observations_by_version`.

### Marking events (each in the mutation's own transaction)

| mutation | subjects whose observers get marked |
| --- | --- |
| data/handle cell write (value actually changes) | `(g, c, "")` |
| grid create / delete | `(g, "", "")` (config observers) + on delete every subject with `grid_path = g` |
| generation turnover (`delete_generation` / clear) | `(g, c, *)` — all output subjects of the cell |
| terminal status flip (incl. startup crash-flip) | `(g, c, *)`, and self: `G.dirty = 1` |
| re-run of an existing generation (status → running) | self only (`G.dirty = 1`; observers were marked when it left its previous terminal state, or are gated on the flip to come) |

Idempotent same-value writes mark nothing (no change). Result-recording
during a run marks nothing (the completion flip is the visibility event).

### The marking algorithm, and why it's cheap

Mark = transitive closure up the observer DAG, **pruned at
already-dirty generations**:

```
frontier = observers of the changed subjects          (via the indexes)
while frontier:
    G = pop()
    if G.dirty: continue          # prune: its cone is already dark
    G.dirty = 1
    frontier += observers of (G.grid, G.coords, *)
```

Soundness of the prune rests on I2: while `G` is dirty, no *new* edge can
attach to its outputs without a demand first verifying `G` (which clears
the bit) — so everything downstream of a continuously-dirty `G` was
already marked when `G` was. (Edges recorded by *running* observers are
covered because running generations are dirty-at-insert and stamp-less by
rule.) Cycles terminate for free — a cycle member reached twice is
already dirty. And the prune gives the amortized bound: each generation
pays one mark per verify cycle, so total marking work is bounded by total
verification work, which is bounded by demand traffic.

Eager (write-time) propagation is the right side of the trade here:
writes are rarer than currency queries, the transactional coupling is
what makes I1 crash-safe with zero extra machinery, and a lazy scheme
would reintroduce query-time closure walks — the thing this chunk exists
to remove.

### Verification (the read path)

`cell_currency(g, c)`, one `run_readonly` body:

1. No generation → `None` (unchanged).
2. **Settled generation, `dirty = 0`, stamped** → serve the stamp +
   current results (value-pinned in this snapshot). O(1).
3. Otherwise derive, exactly the v0 recursion (config edges, cell edges,
   task-output edges: settledness + fingerprint + recursive support
   freshness; in-progress marker parks cycles at unsettled) — **but
   recursing only into support that is itself dirty/unstamped**; clean
   support contributes its stamp in O(1). Then stamp: write
   `verified_currency`/`reason`, clear `dirty` — **only for settled
   (terminal-status) generations**. Running generations always derive
   live: their record is still growing, record inserts don't mark, and
   stamping them would cache a moving target. (Their support is small to
   re-walk and typically clean.)

All verdicts stamp, including `stale` and `unsettled`: a stale stamp is
invalidated by the revert (a write → mark → re-verify → fresh: Q5's
resurrection is just the ordinary cycle); an unsettled-because-running
stamp is invalidated by the status flip; an unsettled-because-cycle stamp
correctly persists until some member changes (a changeless cycle can
never resolve). The evaluation stays a Python-orchestrated recursion over
indexed queries inside one snapshot body — no monolithic SQL CTE for the
read path (the write path's closure is simple enough either way).

_Daniel note:_ I wonder how this interacts with the contract of `run_readonly` if we
are writing? Also, probably goes without saying but it would be good to have the
recursion be SQL-native maybe with a temporary table so that we avoid repeatedly
copying things back and forth. Possibly also worth clarifying how running generations
are actually represented in this currency table; do they get flipped to dirty when they
start running and then just stay dirty until they complete?

_Resolution (2026-07-06 round):_ all three settled —

- **`run_readonly` is deleted, and currency becomes a session-owned op.**
  The snapshot property came from the session lock, not read-only-ness;
  rather than a sibling "may write derived state" seam, there is *no*
  public run-a-body-under-my-lock method at all: `WorkflowSession.
  cell_currency` is a typed op whose body (implemented in `currency.py`,
  demoted to a store-internal conn-level module) derives *and* stamps in
  one transaction, and the session maintains its own invariants. The
  `CurrencyEvaluator` class, the epoch counter, and `_mutate` are all
  deleted — the stamps are the memo (verifying one cell stamps its
  settled support cone, so query fans hit stamps).
- **Hybrid SQL-native recursion**: one recursive CTE bulk-loads the
  support cone (expanding only through unstamped-or-running nodes) plus
  its records joined with current subject values; the verdict logic
  (well-founded freshness, cycle parking, diagnosis) stays in Python
  over the loaded cone. Verdict evaluation itself is deliberately not
  pushed into SQL.
- **Running generations**: unstamped from insert, re-marked (stamp
  cleared) on any flip back to running, never stamped while running
  (always derive live), and the first verification after settling writes
  the first stamp. `stamp present ∧ settled` is the serve condition.

### What this resolves (the former open questions)

1. **Granularity** — marks per generation, precision per edge (above).
2. **Propagation** — eager, transactional, prune-at-dirty.
3. **Stamps** — verdict + reason + dirty bit; no revision counter in the
   correctness story; epoch memo and `change_epoch` deleted.
4. **Cycles** — unchanged v0 parking in the read path; self-terminating
   in the write path via the prune.
5. **Resurrection** — mutations only ever *mark*; verification alone
   writes stamps and clears bits. A never-demanded dirty generation is
   fine (lazy); presence queries that report currency pay one
   verification per listed cell, memoized within the call's walk.

## Gather compatibility (design now, implement in phase 2)

`gather` is semantically a for-loop of cell reads plus one read of the
driving domain cell. Under this proposal that is *literally* its
implementation cost: one observation row per read cell (bulk-inserted in
the gather's transaction) + one for the domain cell. Marking and
verification need nothing gather-specific — a changed cell dirties its
observers through the same subject index; domain expansion is an ordinary
domain-cell write. Fault-on-missing + the open-map principle mean **no
absence subscriptions**: *adding* a cell never dirties anyone (only the
domain-cell write does), which is what keeps the reverse index bounded by
what was actually read.

**Grouped-observation compression** (if per-cell row volume ever hurts):
gathers address irregular, non-consecutive cell sets, so "slice bounds"
are the wrong shape — the plausible algebra is per-axis **point-set /
integer-range / cartesian-product** descriptors, matching how code
actually gathers. Checked against the proposal's invariants:

- *Marking*: a changed cell must find grouped edges containing it. Exact
  per-cell rows use the subject index; grouped rows are rare (one per
  gather per generation), so a scan of grid `g`'s grouped rows with a
  per-axis containment test is cheap — no exotic indexing needed. I1/I2
  and the prune are untouched (a grouped edge is still just an edge from
  an observer generation).
- *Verification*: a grouped edge stores an aggregate fingerprint over the
  set; re-verifying recomputes it with a bulk query (this is where a
  derived fast projection of frames/cells earns its keep — always
  derived, never identity-bearing; canonical encodings stay the dumb
  sorted forms).
- *Schema*: a new subject shape (descriptor instead of coords) —
  additive; nothing existing migrates.

Conclusion: design v1 with per-cell rows only; grouped rows are a future
additive optimization that changes no invariant or decision above. Do
not implement now.

## Remaining discussion points

- **The trigger table** above is the load-bearing list — review it for
  missing mutations (it is meant to be exactly today's `change_epoch`
  bump set, refined: idempotent writes and running-record traffic
  excluded, self-marking on lifecycle flips added).
  _Daniel note:_ Looks OK, but we should verify/audit this later and state
  the invariant clearly on the things that would modify it, so we can track
  down bugs later.
  _Resolution:_ the invariant (I1) is stated in `storage.py`'s module
  docstring, the canonical table lives in `notes/execution_semantics.md`,
  and every mutation body in `state_store.py` carries a "Marks:" line —
  including explicit "Marks: nothing, because ..." on the exclusions —
  so `grep -n "Marks:"` is the audit.
- **Stamp-only-settled**: the rule that running generations never carry
  stamps (always derived live) — confirm, since it is what lets record
  inserts stay mark-free.
  _Daniel note:_ Discussed somewhat above.
  _Resolution:_ confirmed; see the running-generation bullet above.
- **`list_cells` cost**: presence queries reporting currency verify each
  listed cell (memoized per call). Accept for now, or add a
  "no-currency" listing flavor for big grids later?
  _Daniel note:_ I think this seems fine for now, we can revisit if it
  slows things down noticeably.
  _Resolution:_ accepted — and the stamps make it cheaper than the old
  per-call memo (verified cells serve O(1) across calls, not per epoch).
- Whether `verified_at` metadata is worth the column (debuggability) or
  noise.
  _Daniel note:_ May as well keep it!
  _Resolution:_ kept as ``currency_as_of``; cleared together with the
  verdict (one coherent nullable stamp — no last-stamped archaeology).

Tests: the full existing suite unchanged (semantics identical); the
chunk 3 uncomfortable-scale scenario re-timed (the motivation number);
mark unit tests at the session level per the trigger table (including
no-mark for idempotent writes and running-record traffic); prune
correctness (diamond DAG: one change marks the cone once; second change
prunes); stamp lifecycle (stamp → clean serve → mark → re-verify;
resurrection; unsettled-running invalidated by the flip); crash between
mutation+mark and the next query (same transaction — simulated reopen
sees consistent state).

Notes/log (design-round outcome + implementation, 2026-07-06):

- **Dirty ≡ unstamped — the bit is gone.** Enumerating the four
  (dirty, stamped) states showed they never meaningfully come apart:
  (clean, unstamped) is unreachable, and (dirty, stamped) keeps a stamp
  verification never consults (debug archaeology only — dropped in favor
  of the simplification; the event log is the trail). So the schema is
  just the nullable stamp — ``currency_verdict`` / ``currency_reason`` /
  ``currency_as_of``, NULL together — and *marking is stamp-clearing*.
  The trigger table simplified with it: the self-marking entries reduce
  to "flip-to-running clears own stamp" and the terminal-flip row is
  observers-only.
- **Naming**: ``currency_*`` prefix; "verdict" (already the docs' word)
  over "verified"; ``settled_currency`` rejected (collides with the
  settled/unsettled term of art while the column can hold
  ``'unsettled'``); "stamp" survives as prose shorthand for the trio.
- **No new index needed**: the planned ``subject_versions (grid_path,
  coords)`` index already exists as the prefix of
  ``subject_versions_identity``.
- Implementation as resolved above: `currency.py` rewritten as the
  conn-level implementation module of ``WorkflowSession.cell_currency``
  (recursive-CTE cone load + Python verdict recursion + same-transaction
  stamp-back); marking helpers + per-body "Marks:" docstrings in
  `state_store.py` (the audit trail the trigger-table note asked for);
  ``run_readonly`` / ``change_epoch`` / ``_mutate`` /
  ``CurrencyEvaluator`` deleted; the id-blob converters moved to
  `storage.py` (shared with the currency module, avoiding an import
  cycle); schema version 2 → 3 (no migration — spike-stage dbs are
  fresh). Canonical writeup: the new "Currency maintenance in the
  database" section of `notes/execution_semantics.md`.
- **`_write_cell` now no-ops on idempotent same-value writes** (SELECT
  before write) — previously it re-wrote unconditionally; the check is
  what makes "idempotent writes mark nothing" literal.
- Tests: suite unchanged and green (the semantics tests needed only the
  ``session.cell_currency`` spelling); new stamp-lifecycle tests in
  `tests/test_currency.py` (stamp/serve/mark/re-verify, resurrection,
  idempotent-write and record-traffic no-marks, transitive clearing,
  unsettled-running invalidated by the flip, stamps surviving a session
  reopen). The epoch-memo test died with the epoch.
- **Timings**: the chunk 3 scale smoke (210-cell triangle) computes in
  ≈7.7s (unchanged — run cost dominates), and the post-edit currency
  walk dropped ≈13ms → ≈6ms *and now stamps* (subsequent queries are
  O(1) row reads). Full-suite wall time dropped ≈34s → ≈21s, mostly
  ``list_cells``-style query fans hitting stamps instead of re-walking
  per epoch.
