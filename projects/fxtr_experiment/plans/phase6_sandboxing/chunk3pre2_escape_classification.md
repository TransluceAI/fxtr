# Chunk 3 pre-commit 2 — one escape-classification rule for run and safety pass

*Standalone, self-contained commit in `fxtr_experiment`, landed before
the chunk-3 serving work. Rationale: the chunk-3 round in
`discussion_log.md` (this directory).*

## Why

The run path already has a classification rule for exceptions that
escape the run body with **no engine-recorded failure** (the
`except Exception` arm of `Executor._run_once`): a runner-side typed
`CellFaultError` means *the world wasn't ready* — fault-class failure,
generation `interrupted`, retryable — and anything else means the
task's own logic raised — error-class, `errored`. The safety pass has
no such rule: its `except Exception` arm records everything as a
`NondeterminismError` and the generation lands `errored`.

But the pass **re-invokes the task fn** (the documented
run-plus-pass double invocation), so its setup — grant minting, the
task-fn binding spawn — can hit exactly the not-ready conditions a run
can. Today, an ephemeral binding released between the run and the pass
lands `errored`, though nothing about the generation's determinism is
in question. Chunk 3 widens this surface substantially (a sandboxed
task-fn binding re-runs its dependency pin checks at the pass's
connect), so the rule gets fixed first, standalone.

## The rule (the from-scratch statement)

> An exception escaping the run or pass body without an
> engine-recorded failure is classified by its runner-side type:
> `CellFaultError` family ⇒ fault-class failure + `interrupted`
> (couldn't start/finish; retry after fixing the world); anything else
> ⇒ error-class + `errored` (`TaskRaisedError` in a run;
> `NondeterminismError` in the pass). Fault-class escapes never gate
> completion as nondeterminism — they mean "couldn't finish", never
> "finished differently".

The classification stays runner-side-typed-only (the error-transit
doctrine): task-side raises arrive as `IRException` transit forms and
fall through to the error-class arm, exactly as today.

## Design

- Factor the shared half into one helper on `Executor` (e.g.
  `_record_escape(exc, exec_id, *, error_label) -> TerminalStatus`):
  `isinstance(exc, CellFaultError)` → record
  `(FAILURE_FAULT, type(exc).__name__, str(exc))`, return
  `"interrupted"`; else record `(FAILURE_ERROR, error_label,
  str(exc))`, return `"errored"`. `_run_once`'s arm calls it with
  `error_label="TaskRaisedError"`; the pass's with
  `"NondeterminismError"`.
- `_safety_pass` returns a tri-state instead of `bool` — the terminal
  status it decided (`"complete"` / `"errored"` / `"interrupted"`),
  with the caller just setting it. Engine-op violations recorded
  raise-side (frozen frontier, hit mismatches) and the executor-side
  set/output comparisons stay `errored` exactly as today; only the
  escaped-exception arm changes.
- `_record_safety_failure`'s "unless already recorded raise-side"
  guard keeps working for both classes (an engine-recorded failure
  set already carries its own classification via
  `_status_from_failures` — a pass whose replay engine recorded
  failures keeps deriving from that set).
- Note the run-path arm keeps its `engine is None` /
  `InternalRunnerError` special cases unchanged — this commit only
  unifies the classification of typed escapes.

## Docs

- `executor.py` module docstring (steps 4–5): the pass's setup can
  fault like a run's, and faults map to `interrupted` in both.
- `notes/execution_semantics.md`, safety-pass section: state the rule
  and the reason (the pass re-invokes the task fn, so its setup
  shares the run's not-ready surface).

## Steps

- [x] `executor.py`: the shared classification helper; `_run_once`'s
      except arm rewritten over it; `_safety_pass` → tri-state with
      the fault-class arm; module docstring.
- [x] `notes/execution_semantics.md`: the safety-pass paragraph.
- [x] Tests (`tests/test_executor.py` or the safety-pass suite): a
      binding released mid-run (the task fn's closure releases it, so
      the pass's spawn faults) → generation `interrupted` with the
      fault-class failure row; re-bind + re-demand then completes.
      Existing safety-pass regressions unchanged.
- [x] Update `CODE_OVERVIEW.md` if any wording it carries about the
      safety pass needs the tri-state. (Checked: its safety-pass wording
      — "frozen-engine replay gating completion" — stays accurate.)
