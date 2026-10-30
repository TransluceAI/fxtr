# Chunk 5 — the linter + the suites

**Status: complete.** (209 passed + pyright clean: `lint.py` +
`WorkflowSession.lint` — one lock hold, grounding-only — + the
capability's `lint` → `LintReport` with the sound/clean properties and
the `Workflow` pass-through; `tests/test_lint.py` carries the seven
per-removal-kind unit tests and both lint-arc scenarios, and the RPC
smoke drives `lint()` over the real transport. The canonical writeup
landed as the linter section of `notes/grounding.md`. See Notes at the
bottom for the one scenario-shape correction discovered while
implementing.)

The clean-state inventory and the tests that pin the semantics
end-to-end. Explicitly *not* here (phase-scoping decision): a
delete-everything-derived replay sweep over a fixed scenario (redundant
with direct state assertions) and any randomized property harness
(possible future work, against the project's scenario-test style).

## Scoping resolutions (2026-07-09)

- **The linter is structural: groundedness and dangling intent only —
  staleness is deliberately out** (the 2026-07-09 re-scope). Lint
  findings mean "this experiment appears to not be constructed
  properly: these concrete problems prevent it from working" — problems
  you *fix* (re-source, re-target, adopt, delete). Staleness is the
  other axis: a normal operational state you *rerun*, already per-cell
  queryable (`inspect_cell` / `cell_currency`) and better served by a
  status view later; folding it into lint would make red routine
  (every mid-run or edited-input workflow) and kill the signal.
  Consequently the linter checks the **structural half** of the replay
  contract's hypothesis (everything grounded, no dangling intent); the
  freshness/completeness half is currency's, verified by other means.
  The clean state is still two-level:
  - **Sound**: intent is coherent (no dangling sourcing records or
    targets) and every attached target's *materialized* fact is
    grounded — task-cell targets: the current generation's
    `grounded_task_cell`; data-cell targets: the populated cell's
    verdict (the checked-read-root rule of `notes/grounding.md`).
  - **Clean**: sound *and* the ungrounded inventory is empty — the
    structural half of the contract's global hypothesis (a grounded
    task cell is necessarily wanted, so "everything grounded" ≡ sound
    plus nothing outside the demanded cone that a replay would not
    re-create).
- **An unmaterialized target is pending intent, not a finding**: a
  target on a cell with no generation (or an unpopulated data cell) has
  no fact to grade — the remedy is simply to demand it, which is
  operational, not structural. (This case is what forced the re-scope:
  grading it required completeness, completeness dragged in freshness,
  and neither is a construction problem.)
- **Per-target root checks *are* the cone check.** Both derived axes are
  cone-transitive at their roots — freshness requires well-founded fresh
  support, and `grounded_task_cell` conjoins its requested inputs'
  verdicts recursively — so the linter checks exactly the target roots
  and never walks the cone itself; the diagnosis chains are what itemize
  the actual break.
- **The inventory section is simply every non-grounded fact** (orphaned
  vs unanchored, each with its diagnosis) — "garbage" is not what the
  section *is*, only what it *means* once the report is sound: since
  grounding is cone-transitive, sound implies every inventory entry
  sits outside every demanded cone, so the entries are exactly the
  prune candidates ("garbage nobody needs"). While a target is red, the
  same section also contains the *causes* of the redness (the red
  root's diagnosis chain names them), and pruning those would be wrong.
  The linter deliberately does not compute cone membership to separate
  the two — the prune-candidate reading is exact once the targets are
  green, best-effort diagnosis before that (the standing dirty-state
  epistemics). Locally-unwanted task cells need no separate section:
  the tri-state mapping already renders them as orphaned task cells.
- **The write-grant tightening hint is dropped entirely** (the chunk-0
  ride-along idea — "write-granted rule input no generation ever wrote").
  Not worth a lint affordance; wiring-mode hygiene can be revisited if it
  ever bites.
- **The adoption e2e scenario is dropped**: adopt → target → demand
  mechanics are already covered by `tests/test_sourcing_surface.py`; the
  one genuinely new angle — the linter naming what an adoption pulls
  in — is the adopted-grid-with-unsourced-parent *unit* test below.
- **The remaining scenarios are lint arcs**: each starts lint-red with
  the expected finding, performs the documented user moves, and ends
  clean with values reproduced — pinning the *operational* half of the
  replay contract (the system re-creates what grounding says it would),
  which no existing test asserts.

## Scope

- **The lint affordance**: one locked session op (`WorkflowSession.lint()`
  style — one grounding snapshot; no currency evaluation anywhere),
  surfaced as `UserCapabilityProto.lint()` with a `Workflow`
  pass-through. Privileged presence-class operation (records nothing;
  nothing downstream can depend on it). Bulk verdict derivation
  piggybacks on the stamps, so the sweep pays one walk and repeat lints
  serve stamps. Sectioned typed report (easier to assert per-kind than
  one heterogeneous findings list):

  ```python
  @dataclass(frozen=True)
  class LintReport:
      dangling_sourcing: tuple[SourcingInfo, ...]   # status != attached
      dangling_targets: tuple[TargetInfo, ...]
      target_findings: tuple[TargetFinding, ...]    # attached targets whose
                                                    #   materialized fact is
                                                    #   ungrounded: location +
                                                    #   verdict + reason
      ungrounded: tuple[UngroundedFact, ...]        # fact kind + location +
                                                    #   orphaned|unanchored + reason
      # properties: sound (first three empty), clean (all four empty)
  ```

  Exact field shapes settle at review; served ids re-attach per the
  `notes/boundary_discipline.md` checklist.

## Tests

- **Linter unit tests, one per removal kind** (each asserting the
  specific report section and diagnosis, not one mutated mega-scenario):
  remove a target; revoke grid sourcing; revoke cell sourcing; clear a
  writer (side-written cells orphan); delete a dep grid (dependents
  unanchored, chain named); config drift dangling a sourcing record /
  target; the adopted-grid-with-unsourced-parent case (linter names what
  adoption pulls in).
- **Two replay-behavior scenarios** (lint arcs; new executor behavior,
  not restatements of final state):
  1. **Task-re-creates-its-grid replay-in-place**: delete a task-created
     grid → lint red (the reader's grid and generation unanchor — its
     rule references the missing grid — and its target reddens); clear
     the generations the deletion invalidated (the creator, whose
     record asserts/observes it, plus transitive readers — the clear is
     what lets them *rerun*, a currency fact). Cleared targets are
     unmaterialized intent, not findings, so the report turns *sound* —
     but not clean: the reader's broken rule reference stays in the
     inventory until the creator re-runs (see Notes). Re-demand the
     creator, then the reader; the creator re-creates the grid (the
     `asserted_grid` claim exercised as *behavior*), the cone rebuilds
     with matching fingerprints, and lint is clean. The closest thing
     to a real replay test before import/export exists.
  2. **Ordering-fault convergence**: demand a reader of a side-written
     cell before its writer ever ran → missing-cell fault, lint red
     (the reader's generation unanchored, the missing cell named);
     demand the writer; re-demand the reader → clean. Pins the
     contract's convergence-is-iteration caveat for side-written data
     specifically — distinct from read-through-staleness convergence
     and from the cleared-driver orphaned-value window, both already
     covered.

Notes/log (2026-07-09, at landing):

- **Scenario-1 shape correction** (vs the scoping round's sketch, which
  guessed "clear → clean-but-pending"): after delete + clear, the
  report is *sound but not clean* — the reader's grid stays unanchored
  ("rule reference … is not grounded") until the creator re-runs and
  re-creates the referenced grid. This also fixes the recovery order:
  the reader cannot simply be re-demanded first, because grant minting
  precedes the run — the reader's mint faults on the missing grid
  before its token read would ever cascade to the creator. Demand the
  creator, then the reader (or accept one extra fault-and-re-demand
  round — the iterate-to-quiescence texture). All three facts are in
  the scenario text above and pinned by the test, including the
  reader-first leg (the mint fault names the missing grid with the
  grounding-enriched diagnosis); the sound-vs-clean split is exactly
  what surfaces the residual damage. A related idiom limit worth
  remembering: the completion-token idiom fixes *cell* ordering but not
  *grid existence at mint* — minting precedes the first read.
- **Usage note**: the demand wrapper's auto-target pins the *output
  slice* (`t.output_entity()`), so `remove_target` must be called with
  the same slice expression — targets are unique per slice identity,
  and a task-view record is a different identity than its output view.
- **Ride-along refactor**: `WorkflowSession._grounding_locked` factored
  out of `_grounding`; `cell_snapshot` / `serve_check` /
  `gated_read_cell` now share it instead of duplicating the two-phase
  load-configs-evaluate block, and the lint sweep composes it per fact
  under one lock hold.
- Findings overlap by design: a red target's cell also appears in the
  ungrounded inventory (the finding names the *root*, the inventory
  names the *fact*); tests assert both.
