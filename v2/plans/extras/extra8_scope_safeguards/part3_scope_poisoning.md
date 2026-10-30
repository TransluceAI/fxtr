# Extra 8 part 3 — scope-context hygiene: poisoning + the task-start helpers

**Status: IMPLEMENTED** (`no_active_scope` / `NoActiveScopeError` in
`entity_ref_scope.py`; `fxtr2/common/task_utils.py`; tests
`v2/tests/test_task_utils.py`; doc mentions landed). Implementation
settlements: exception = `NoActiveScopeError(RuntimeError)`; the
audit found only the two accessor functions read `ACTIVE_SCOPE` in
core (both raise on the sentinel; `FxtrScope.get_active` routes
through `get_active_scope`); the helpers install context via
`as_active_scope()` / `no_active_scope()` inside a wrapper coroutine. Rationale trail:
`projects/fxtr_experiment/plans/phase6_sandboxing/chunk3post2_scope_safeguards.md`
(item 3); decisions below settled in the extra8 review round.

## Decisions

- **The poison is a sentinel value stored in `ACTIVE_SCOPE`, hidden
  from the supported interface.** A module-private sentinel object;
  `get_active_scope()` raises a pointed error on reading it (what the
  region's intent is — deliberately no ambient scope — and the remedy:
  pass scopes explicitly). Direct `ACTIVE_SCOPE.get()` is
  internal-unsupported, so no supported caller ever sees the sentinel.
  Free consequence: constructing a *non-isolated* `FxtrScope` in a
  poisoned region errors at construction (its `_init_common` capture
  goes through `get_active_scope()`) — the latent hazard turned into
  an error at the offending line. Note today's contextvar default is
  `GLOBAL_SCOPE`, so "no active scope" is a state this part
  introduces; the error message carries that novelty.
- **No poisoning inside core v2 for now.** The affordances land
  unused by fxtr2 itself; adopting them in core holder/serve tasks is
  deferred until wanted. The fxtr_experiment sandbox launcher's
  migration landed as its own followup commit: **poison won** — the
  holders start via `start_with_scope(scope=None)` (everything they
  touch is explicit post-root-fix, so the poison states the contract
  and turns regressions into `NoActiveScopeError` at the offending
  line), and the regression test split into a hazard-fake case (must
  fail to launch with the pointed error) plus a disciplined-fake case
  (full teardown, guarded by the drain report). Verified against the
  real gated e2e sweep.
- **Two helper variants, in `v2/fxtr2/common/task_utils.py`** (new
  small module): wrappers over `tg.start` and `tg.start_soon`
  (working names `start_with_scope` / `start_soon_with_scope`) that
  run the task with a chosen scope context installed at task top.
  `scope=None` means "no scope" — the poison sentinel. The `start`
  variant passes `task_status` through.
- **`no_active_scope()`**: a sync CM setting the sentinel for a
  block (the region form of the same thing; the helpers are the
  task form).
- **Doc mentions land with this part, in the doc's own register**:
  one sentence in scope-discipline's "Avoid Capturing Ambient Scope
  Context In Tasks" recommending the helpers as the packaged form of the
  reset idiom, plus the poison option — described as what it is for,
  not as more required than anything else in the doc. A line in the
  debugging-tips section if useful.

## To settle at implementation

- The exact error message shape (region intent, remedy, pointer to
  the discipline note) and the exception type (`RuntimeError` vs a
  dedicated class).
- Audit `get_active_scope()` call sites for any that must tolerate
  the sentinel (expected: none in core, since core never poisons and
  the sentinel only exists inside user-opted regions — but the error
  path should be checked against the load/index `as_active_scope`
  wrappers).
- Helper signatures against anyio's `start`/`start_soon` typing
  (varargs + `name=`), and whether the wrapper coroutine sets the
  contextvar directly (the launcher idiom) or via
  `as_active_scope()`.

## Verification sketch

- Unit tests: implicit use under poison raises with the documented
  message (both via `no_active_scope()` and `scope=None` tasks); the
  helpers install exactly the chosen context (and pass `task_status`
  through); non-isolated construction under poison errors; nested
  poison/activate interactions.
- Full v2 suite; the portable wheel needs no change (`common/` is
  already force-included) — verify by running one wasm e2e if
  anything else moves.
- Update `v2/CODE_OVERVIEW.md` (task_utils module line + a sentence
  in the debug section) and check off here + in `overview.md`.
