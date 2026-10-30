# Step 1 Post-6: Edge Case Test Coverage

Edge cases not currently covered by the test suite, organized by the invariant or subsystem they stress.

## Subslot key normalization

1. **Int and string subslot keys are equivalent.** `slot.subslot(42)` and `slot.subslot("42")` should return the same object with the same pin_key. A run in `subslot(42)` should be findable via `subslot("42")` on resume. Also test this for `ctx.cache_slot(42)` vs `ctx.cache_slot("42")`.

## Split state integrity

2. **Cannot subslot a Partial slot.** Create a slot, start a run (so it becomes Partial), then try to access subslots on a fresh reference to the same slot. Should raise `CacheSlotError`.

3. **Cannot subslot a Finished slot.** Create a slot, finish a run, then try to access subslots on a fresh reference. Should raise `CacheSlotError`.

4. **Cannot run in a Split slot.** Create a slot, split it with subslots (write into a subslot so Split is persisted), then try to run directly in it. Should raise `CacheSlotError`. (The test `test_write_once_violation_different_key` tests running with a different key, but not running in a split slot.)

## Pending forwarding

5. **Pending forwarded as arg to a sub-step.** Step A dispatches step B, gets a Pending. Before awaiting, it passes that Pending as an arg to step C. The runtime should resolve the Pending before C executes. Verify C sees the resolved value.

6. **Pending from sub-step forwarded to sibling sub-step.** A parent step dispatches two sub-steps into different subslots; the second takes the Pending from the first as an arg. Both should complete correctly, and the second should see the first's resolved result.

## Cache key edge cases

7. **Same step, different args, same slot → write-once violation.** Currently tested for different steps; add a case for the same step with different args in a finished slot.

8. **StepHandle as arg distinguishes cache keys.** Pass different step handles as args to the same step. The cache keys should differ (StepHandleMarker includes identity). Verify that running with step_handle_A caches, and running with step_handle_B does not hit the cache.

9. **Empty args.** Run a step with no positional args and no kwargs. Should work, produce a valid cache key, and cache correctly.

10. **Bytes and float args.** Run steps with `bytes` and `float` values. Verify cache key computation and round-trip through storage. (Covers the full SerializableTypes range.)

## Deeply nested structures

11. **Deep subslot path.** `slot.subslot("a").subslot("b").subslot("c").subslot("d")` — four levels deep. Verify the run executes, the entire Split chain is persisted, and the result can be loaded on resume.

12. **Deeply nested args.** Pass `[{"key": [1, 2, {"inner": slot_b}]}]` as an arg to a step. Verify the CacheSlot is found at the correct structural position, tracing works, and replay check passes on resume.

## Replay check edge cases

13. **Multiple dispatches into different subslots of the same foreign arg.** A step receives `foreign_slot` and dispatches runs into `foreign_slot.subslot("x")` and `foreign_slot.subslot("y")`. On replay, both should be checked. Changing data in just one subslot should fail the check.

14. **Foreign subslot passed as arg.** Top level pre-fills `slot_b.subslot("data")`. A step receives `slot_b` and passes `slot_b.subslot("data")` (a subslot, not the root) to an inner step. The inner step dispatches into it. On replay, the subslot's data change should be detected through the subslot derivation path.

15. **Purely internal dispatch — no tracking overhead.** A step creates `ctx.cache_slot("work")` and dispatches a sub-step into it with no foreign cache-slot args. Verify the outer step's `slot_deps` has zero tracked runs (the dispatch is purely internal and should be skipped by the tracker).

16. **Replay with extra subslots added.** First run: step dispatches into `foreign.subslot("x")`. Between runs, add new data into `foreign.subslot("y")` at the top level (expanding the foreign slot tree). The replay check should still pass because the observed subslot (`"x"`) hasn't changed.

## Error propagation edge cases

17. **Nested abort propagation (3 levels).** Step A dispatches step B, which dispatches step C. C raises an exception. Verify the abort propagates through B up to A as `StepAbortError`, and that both A's and B's slots remain Partial.

18. **Multiple sub-steps abort simultaneously.** A step dispatches two sub-steps that both fail. The containing step should still get a StepAbortError (poisoning), and the result should not be cached.

19. **Sub-step spawned but not awaited.** A step dispatches a sub-step via `slot.run()` but never awaits the Pending — just returns its own result. The containment model (`wait_for_all_substeps`) should still wait for the sub-step before caching the parent. If the un-awaited sub-step fails, the parent should be poisoned.

## Resume edge cases

20. **Resume a Partial slot with the same cache key re-executes.** A step fails partway through, leaving the slot Partial. On resume with the same args, it should re-execute (not error), and completed internal sub-steps should be reusable from their own cache.

21. **Multiple root cache slots in the same workflow.** Run two independent computations in two root cache slots within the same entrypoint. Verify they don't interfere, both cache independently, and both replay correctly on resume.

## Concurrency

22. **Concurrent dispatches to sibling subslots.** A step dispatches two sub-steps concurrently into sibling subslots of the same parent. Both should complete without lock contention or data corruption. (Tests that the per-slot lock doesn't serialize unrelated subslots.)

## Complex replay check integration tests

These tests exercise the replay check with deep nesting, many slots, and non-trivial threading patterns. The goal is to catch ordering/indexing bugs that only surface at scale.

23. **Large nested replay: many foreign and internal slots, non-sorted construction order.** Build a scenario with:
    - 3+ foreign slots passed to the top-level step, created in non-alphabetical order (e.g., `slot_z`, `slot_a`, `slot_m`) with data pre-filled via various subslot paths
    - The top-level step creates multiple internal cache slots (`ctx.cache_slot("w")`, `ctx.cache_slot("b")`, etc.) also in non-sorted order
    - The step dispatches into internal slots, passing subsets of the foreign slots to each
    - At least one inner step receives both foreign and internal slots, and dispatches further sub-steps that use some of the foreign slots via subslot paths
    - Some foreign slots are passed through but never actually used by the inner step (they appear in args but are not dispatched into) — the replay check should still pass because it only tracks dispatches, not arg presence

    Then verify:
    - **(a)** Replay with the exact same slot data → cache hit at every level
    - **(b)** Changing the data in a foreign slot that IS used at the deepest nesting level → replay check fails at the top level
    - **(c)** Changing the data in a foreign slot that is passed as an arg but never dispatched into → replay check still passes (it was never observed)
    - **(d)** Changing data in a foreign slot's subslot that was NOT accessed → replay check still passes (only the accessed subslot paths are tracked)

24. **Selective replay invalidation across nesting levels.** A 3-level call structure:
    - Top step receives `slot_x`, `slot_y`, `slot_z`
    - Dispatches `mid_step_1(slot_x, slot_y)` into internal slot "m1"
    - Dispatches `mid_step_2(slot_y, slot_z)` into internal slot "m2"
    - `mid_step_1` dispatches `leaf_1` into `slot_x.subslot("v")` and `leaf_2` into `slot_y.subslot("v")`
    - `mid_step_2` dispatches `leaf_3` into `slot_y.subslot("v")` and `leaf_4` into `slot_z.subslot("v")`

    Verify:
    - Replay with same data → passes
    - Change `slot_x.subslot("v")` data → fails (used by mid_step_1 → leaf_1)
    - Change `slot_y.subslot("v")` data → fails (used by both mid_step_1 and mid_step_2)
    - Change `slot_z.subslot("v")` data → fails (used by mid_step_2 → leaf_4)
    - Change `slot_x.subslot("other")` data (unaccessed subslot) → passes

25. **Foreign slot forwarded through multiple levels without use.** A foreign slot is passed as an arg through 3 levels of nesting but never dispatched into at any level. The slot_deps at each level should have `inner_slot_arg_mappings` entries for it (so the recursive replay check can resolve it), but no `TrackedForeignRun` entries observing it. Replay should pass even if the forwarded slot's data changes, since it was never observed. This specifically tests that arg_mapping completeness doesn't accidentally make the replay check too strict.

---

## Test file organization

### Current state

```
tests/
  test_smoke.py         (2 tests)  — import checks
  test_pending.py       (5 tests)  — Pending unit tests
  test_resolve.py       (9 tests)  — resolve_args, cache keys, collect_cache_slot_args
  test_workflow.py      (13 tests) — mixed: basic workflow, errors, resume, state persistence
  test_replay_check.py  (11 tests) — replay check scenarios
```

Problems: `test_workflow.py` mixes several unrelated concerns (basic operations, error propagation, resume, state persistence). Every test file re-declares `StepKeyEntity` and its own db-path helper.

### Proposed reorganization

```
tests/
  conftest.py                  # Shared fixtures: StepKeyEntity, db_path, make_ns, make_ep helpers
  test_smoke.py                # Unchanged (2 tests)
  test_pending.py              # Unchanged (5 tests)
  test_resolve.py              # Existing (9) + new cache key edge cases (#8, #9, #10)
  test_cache_slot.py           # NEW — CacheSlot state machine & subslot behavior
                               #   #1 key normalization, #2–4 state violations, #11 deep subslots
  test_workflow.py             # Existing basic workflow tests (trimmed) + new workflow tests
                               #   Keep: simple step, two-step, cache reuse, subslot tree, write-once,
                               #         state persistence, resume
                               #   Add: #5–6 pending forwarding, #7 same-step-diff-args write-once,
                               #        #12 deeply nested args, #21 multiple roots, #22 concurrent subslots
  test_error_propagation.py    # NEW — extracted from test_workflow + new error tests
                               #   Move: abort propagates, abort leaves partial, poisoning
                               #   Add: #17 nested 3-level abort, #18 multiple aborts,
                               #        #19 un-awaited sub-step
  test_replay_check.py         # Existing (11) + new replay edge cases
                               #   Add: #13 multiple foreign subslots, #14 foreign subslot as arg,
                               #        #15 purely internal no tracking, #16 extra subslots added,
                               #        #25 forwarded-but-unused
  test_replay_integration.py   # NEW — complex multi-level replay integration
                               #   #23 large nested replay, #24 selective invalidation
```

### Rationale

- **conftest.py** eliminates repeated `StepKeyEntity` definitions (currently 3 copies with different type-system UUIDs and names) and common helpers like `_db()`.
- **test_cache_slot.py** isolates CacheSlot state-machine invariants that don't need full workflow execution — these are tighter unit-level tests.
- **test_error_propagation.py** groups all abort/poisoning behavior, making it easy to see the error model's coverage at a glance.
- **test_replay_integration.py** houses the big multi-level tests (#23–24) separately since they're significantly more complex than the other replay tests.
- **test_workflow.py** stays as the home for "normal" end-to-end workflow scenarios.
- No subfolder — 8 files is manageable without nesting.

---

## Implementation steps

Each step below is one unit of work. Check off as completed.

### Phase 0: Reorganization

- [x] **0a. Create `conftest.py`** with shared fixtures:
  - `StepKeyEntity` (single definition, one UUID namespace)
  - `db_path(tmp_path)` fixture returning the SQLite URL string
  - `DummyBackend` and `make_dummy_slot` for resolve-level tests (currently in test_resolve.py)
- [x] **0b. Extract error propagation tests** from `test_workflow.py` into `test_error_propagation.py`. Update imports to use conftest fixtures. Keep the section headers for readability.
- [x] **0c. Migrate all existing test files** to use conftest fixtures (remove per-file `StepKeyEntity`, `_db()`, `_make_ns()` definitions). Run tests to verify nothing broke.

### Phase 1: Straightforward edge cases (low complexity, independent)

- [x] **1a. `test_cache_slot.py` — state machine tests (#1–4, #11)**
  - #1: int/string subslot key equivalence (both `subslot` and `ctx.cache_slot`)
  - #2: cannot subslot a Partial slot
  - #3: cannot subslot a Finished slot
  - #4: cannot run in a Split slot
  - #11: 4-level deep subslot path, verify state persistence on resume
- [x] **1b. `test_resolve.py` additions (#8–10)**
  - #8: StepHandle as arg distinguishes cache keys
  - #9: empty args — no positional, no kwargs
  - #10: bytes and float args round-trip through cache key
- [x] **1c. `test_workflow.py` — pending forwarding & simple edge cases (#5–7)**
  - #5: Pending forwarded as arg to a sub-step (resolved before execution)
  - #6: Pending from sub-step forwarded to sibling sub-step
  - #7: same step, different args, same finished slot → write-once violation

### Phase 2: Workflow integration edge cases

- [x] **2a. `test_workflow.py` — nested args, multiple roots, concurrency (#12, #21, #22)**
  - #12: deeply nested args with cache slot embedded in structure
  - #21: two independent root cache slots in one entrypoint
  - #22: concurrent dispatches to sibling subslots
- [x] **2b. `test_error_propagation.py` — new error tests (#17–19)**
  - #17: 3-level nested abort propagation (A→B→C, C fails)
  - #18: multiple sub-steps abort simultaneously
  - #19: sub-step spawned but not awaited — containment still waits, abort poisons parent
- [x] **2c. `test_workflow.py` — resume edge case (#20)**
  - #20: resume a Partial slot with same cache key re-executes (not error)

### Phase 3: Replay check edge cases

- [x] **3a. `test_replay_check.py` additions (#13–16, #25)**
  - #13: multiple dispatches into different subslots of same foreign arg
  - #14: foreign subslot (not root) passed as arg to inner step
  - #15: purely internal dispatch → zero tracked runs in slot_deps
  - #16: extra subslots added between runs → replay still passes
  - #25: foreign slot forwarded through 3 levels without use → replay passes even if data changes

### Phase 4: Complex replay integration

- [x] **4a. `test_replay_integration.py` — test #23** (large nested replay with many slots, non-sorted order, 4 sub-assertions)
- [x] **4b. `test_replay_integration.py` — test #24** (selective invalidation across 3 nesting levels, 5 sub-assertions)

### Phase 5: Wrap up

- [x] **5a. Run full test suite**, verify all pass, check for any flaky concurrency tests.
- [x] **5b. Update CODE_OVERVIEW.md** with the new test file layout.
