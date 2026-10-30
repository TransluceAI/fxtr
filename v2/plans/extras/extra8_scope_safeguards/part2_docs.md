# Extra 8 part 2 — the already-true doc additions

**Status: IMPLEMENTED.** Pulled forward ahead of the poisoning part
(review call): everything here documents facts that are true *today* —
task-context forking, the part-1 drain report, the parking contracts —
so only the mentions of part 3's new affordances stay deferred (they
land WITH part 3; see `part3_scope_poisoning.md`). Content settled in
`projects/fxtr_experiment/plans/phase6_sandboxing/chunk3post2_scope_safeguards.md`
(items 4–5).

## What landed

1. **`v2/notes/scope_discipline.md` — "Avoid Capturing Ambient
   Scope Context In Tasks"**: every `tg.start`/`start_soon` copies the calling
   task's context including the active scope; a task that outlives the
   context it was started from (above all: one started from a service
   dispatch) should not rely on its inherited active scope — create
   only isolated scopes downstream, or install a context you control
   at task top (capture at a known point, set at entry) — and install
   the intended scope before running *user-defined code* in such a
   task, since user code is entitled to use the active scope even
   when the library is not. Written in the doc's own
   prescriptive-but-even register; points at the drain report for
   diagnosis. Part 3 adds one sentence recommending the task-start
   helper once it exists.
2. **`v2/notes/scope_service_lifetimes.md` — "Debugging scope and
   lease hangs"**: structure-first (the drain report, the guard, the
   signal CM, the TB rerun flag); designed parking vs wedge (the
   park-on-exit contract stated); ambient context as an input a repro
   must copy (deliberate `as_active_scope()` at harness task starts;
   verify by comparing the report's parent-edge classifications
   between the real system and the repro); the one-sided
   `open_rpc_scope` mutual wait.

## Steps

- [x] The scope-discipline rule.
- [x] The debugging-tips section.
- [x] `v2/CODE_OVERVIEW.md`: no listing change needed (both notes
      already existed; the debug package was added in part 1).
