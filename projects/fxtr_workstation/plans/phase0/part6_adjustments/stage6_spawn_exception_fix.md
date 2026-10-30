# Stage 6: Un-blinding opaque spawn / invoke errors

## Observed behavior

Spawning a git-ref service whose package has no committed `uv.lock`
fails the nix build at eval (`Path 'packages/<pkg>/uv.lock' does not
exist in Git repository`). The nix wrapper raises a `RuntimeError`
with that full stderr in the message — but over the agent-facing MCP
the failure surfaces only as:

```
IRException(' (1 sub-exception)')
```

i.e. an empty-messaged exception group, with the real stderr nowhere
to be found — undebuggable from the agent side, and lost even when
inspecting the raised value by hand. The error is mangled host-side,
before the MCP ever sees it. (The underlying build error itself is
visible by building the attr directly: `nix build
.#fxtr.services.<svc>.sitePackages`.)

This was surfaced while building
`v2/plans/phase3/stage13_output_channels/part4post1_scheduler_caching.md`
(followup item (a)); it is workstation/spawner plumbing, not the
caching feature.

## Root cause — two independent bugs

**Issue 1 — spawner collapses the message into an empty group.**
`_contain_failures_after_started` (the spawn blast-radius guard in
`projects/fxtr_sandboxing/.../spawner.py`) is written with `except*`:

```python
try:
    yield status
except* Exception as eg:
    if not status.was_started:
        raise          # <-- re-raises eg (the GROUP), not the leaf
    logger.warning(...)
```

The nix build runs inside this CM *before* `status.started()` is
reported. A bare `raise` inside an `except*` clause re-raises the
synthesized `ExceptionGroup`, not the original `RuntimeError`. A group
wrapping a single child has an empty message, so `str(group)` is
`' (1 sub-exception)'`. `env_tg.start()` re-raises that group to
`spawn()` un-rewrapped (hence "1 sub-exception", not nested). The real
`RuntimeError` survives only as `eg.exceptions[0]`.

**Issue 2 — workstation renders invoke errors with `repr()`.** When the
agent invokes a service via `schedule_invoke`, the workstation runs the
method in `_invoke_worker_body` (`projects/fxtr_workstation/.../state.py`).
A raised exception (the spawn failure arrives here as a wire-decoded
`IRException`) is caught and delivered as:

```python
except Exception as exc:
    _deliver_invoke_error(state, session_id, slot, repr(exc))   # <-- repr
```

`repr(IRException)` renders only the constructor arg (`.message`) —
dropping `.category` *and* `.traceback_info`. The wire path actually
*does* preserve the full nested traceback: `wrap_exception` runs
`traceback.format_exception` over the group (recursing into the leaf
`RuntimeError`) into `traceback_info`, and the json codec carries it
across. So the stderr reaches the workstation intact and is then thrown
away by `repr()` at the very last step before the slot. This is not a
documented "summary" — `InvokeErrorEvent.message` is an unbounded
`str`, and `repr(exc)` is just the house convention for these message
fields (load errors, callback setup, all invoke-error sites). The only
deliberate summarizer (`_format_script_error` + `get_full_exception`
LRU) is a *different* path — script-body errors, not invoke errors.

The two bugs are complementary: Issue 1 empties the headline message;
Issue 2 discards the traceback even when the message is good. Either
fix alone helps; both together get the nix stderr fully to the agent.

## Fixes (each: red test → green)

**Issue 1 (`fxtr_sandboxing/spawner.py`).** Replace `except*` with a
plain `except Exception` so a pre-`started` failure re-raises the
*original* exception unchanged. `except Exception` still catches
`ExceptionGroup`s (ordinary exceptions) for the after-`started`
log-and-swallow path, while `BaseException`/cancellation still
propagates.

- Red test (`tests/test_spawn_build_error_propagation.py`, new): drive
  `_contain_failures_after_started` via `tg.start`, raise a
  `RuntimeError(<nix-stderr-like>)` before `started()`, assert a
  `RuntimeError` (with the message) propagates — not an
  `ExceptionGroup`. Fast; no nix/eryx.
- After-`started` containment stays covered by
  `test_spawner_blast_radius.py` (run it to confirm no regression).

**Issue 2 (`fxtr_workstation/state.py`).** Deliver the full traceback
instead of `repr`. Add a small helper
`_format_invoke_error(exc) -> "".join(traceback.format_exception(exc))`
and use it at the four invoke-error `repr(exc)` sites
(method-raised + result-delivery + the two arg-resolution sites). Leave
the load-error / callback-setup sites on `repr` (out of scope). Full
traceback flows unbounded into the slot `["invoke_error", msg]` →
durable event log + frontend mirror → agent as `RuntimeError(msg)`;
nothing downstream truncates.

- Red test: strengthen the existing
  `test_invoke_dispatch_error_delivers_invoke_error_event`
  (`tests/test_sessions_3c3b.py`) — `boom()` raises `RuntimeError`;
  assert the delivered `InvokeErrorEvent.message` contains the category
  (`"RuntimeError"`) and `"Traceback (most recent call last)"`, neither
  of which appears in the current `repr` output. (Existing substring
  assertions like `"boom" in message` keep passing — the traceback is a
  superset.)

## Implementation steps

- [x] Issue 1: red test (`fxtr_sandboxing`).
- [x] Issue 1: fix `_contain_failures_after_started` (`except*` →
      `except Exception`); rerun new test + `test_spawner_blast_radius`.
- [x] Issue 2: red test (strengthen `test_sessions_3c3b`).
- [x] Issue 2: `_format_invoke_error` helper + swap the four invoke-error
      sites; rerun invoke-error tests.
- [x] Update CODE_OVERVIEW.md where relevant (added the new
      `fxtr_sandboxing` test to its tree; the workstation/sandboxing
      prose was already accurate — no behavioral contract changed).

## Notes / out of scope

- Load-error and callback-setup messages still use `repr`; a broader
  sweep to full tracebacks is deferred.
- The IR has no structured exception-group type, so a group is flattened
  to formatted text in `traceback_info` (lossless for the message,
  lossy for structure) — fine for now.
- Followup item (b) from `part4post1_scheduler_caching.md` (hard hang
  when batching invokes against a freshly-connected service) is a
  separate lifecycle/deadlock bug, not addressed here.
