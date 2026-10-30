# Stage 5 post-1 — bug fixes from first agent usage

Five issues surfaced while driving the stage-5 plugin from a real
Claude Code session. They're tracked individually in
`examples/basic_test_demo/agent_workdir_1_gitignore/feature_requests.md`;
this plan groups them and proposes an ordering.

## Issues

1. **Tool-layer exception handling is wrong.** Intended behavior was:
   `Exception` → set on the job's completion future, do not re-raise;
   `BaseException` (cancellation, SystemExit, KeyboardInterrupt) →
   set on completion, then re-raise so cancellation propagates.
   Current code in `_handle_script_job` re-raises any `BaseException`
   leaf, which includes regular `Exception`s — so a script that
   `raise`s tears down the worker, which tears down the supervisor,
   which crashes the MCP process.

2. **Supervisor doesn't recover from non-wire exceptions.** The
   outer retry loop in `_supervisor_loop` only reconnects on
   `_is_wire_disconnect(e)`. Anything else (an internal invariant
   violation, an `assert` tripping in some library, a
   `NotImplementedError` from an unhandled event kind) propagates
   up and tears down the MCP. The supervisor was supposed to be a
   safety net — once a cycle has become ready, any failure should
   log + reconnect, not bring down the process. The agent's
   session-side state survives the disconnect (the toml's
   `active_session` is preserved), so reconnect should re-subscribe
   to the same session cleanly.

3. **Internal MCP errors aren't visible in Claude Code's debug log.**
   When the MCP crashes mid-call, Claude Code only sees
   `MCP error -32000: Connection closed` — no traceback. We need to
   redirect the MCP process's stderr (and Python logging output) to a
   known file so we can debug post-mortem. Append to e.g.
   `/tmp/fxtr_workstation_mcp_debug.log`.

4. **Sandbox exceptions: where to wrap.** The current
   `runner.run()` boundary surfaces script errors as
   `ExceptionGroup('unhandled errors in a TaskGroup',
   [IRException("...")])`. Probably the right fix is to wrap the
   exception *inside* the sandbox so the agent sees a clean
   traceback, but we want to think about this carefully — defer
   until after the stability fixes land.

5. **Polish items from the feature-request doc.**
   - **`run_script` should surface captured stdout.** Currently any
     `print()` output is silently discarded. Capture via
     `contextlib.redirect_stdout` inside `script_runner`, return on
     `RunResult`, append to the host's summary string.
   - **`_fxtr_mcp_builtin` available during preamble.** Reorder
     `run_preamble_and_script` so the builtin module is installed
     before the preamble is exec'd. Update the canonical
     `examples/basic_test_demo/agent_workdir/preamble.py` to import
     the common builtins (`from _fxtr_mcp_builtin import
     retrieve_result, schedule_invoke, ...`).

## Order of attack

1. **Fix #3 first.** Without visibility into the actual traceback,
   we're guessing at root causes. The debug-log redirect lets us
   confirm (or refute) the script-exception theory for the
   stage-5-observed crash.
2. **Fix #2.** Make the supervisor's safety net actually catch
   things. After this, the kind of bug that brought down the MCP
   in the agent's session should at most cause one disconnect +
   reconnect cycle. The exception trace recorded by step 1 will
   land in the debug log either way.
3. **Fix #1.** Now that the supervisor is robust, do the
   tool-layer handling right: regular `Exception` → tool result,
   `BaseException` → set + re-raise. After this fix, the script
   exception that originally crashed the MCP becomes a clean tool
   result with no disconnect at all.
4. **Fix #5 polish items.** Independent, easy.
5. **Fix #4 (revisit).** Decide where the sandbox-exception
   wrapping should happen and what shape the surfaced error
   should take.

## Implementation notes per issue

### #3 — Debug log redirect

Done at the `.mcp.json` launcher level — no Python changes needed.
The launcher now wraps the MCP in a shell pipeline that appends
stderr to `/tmp/fxtr_workstation_mcp_debug.log`:

```
sh -c 'printf "\n==== %s MCP starting (pid=%s) ====\n" "$(date -u …)" "$$" >> /tmp/…log; exec uv run --directory … fxtr-workstation-mcp 2>> /tmp/…log'
```

`exec` replaces the shell so signal/EOF propagation to the
child stays clean. The session marker (timestamp + pid) makes it
easy to find where each MCP restart begins. Stdout is left alone
— that's the JSON-RPC wire Claude Code reads.

This catches everything stderr-bound: Python's default
unhandled-exception printer, `logging`'s default WARNING+ handler,
plain `print(..., file=sys.stderr)`, and stderr from any of the
MCP's subprocess-launched sandbox env workers.

### #2 — Supervisor safety net

In `_supervisor_loop`, treat the post-ready cycle exit error
generically:

```
except Exception as e:
    if not self._ready.is_set():
        raise   # never-ready cycle = startup error, propagate
    self._reset_per_cycle_state_on_disconnect()
    _log.exception(...)  # always log full trace
    # treat as a blip; reconnect
    attempt = 0
    backoff = self._reconnect_backoff_initial
    attempt += 1
    if attempt > self._reconnect_max_attempts:
        raise
    ...
```

The "if `_ready.is_set()`, treat as blip" path becomes the only
path; the wire-vs-non-wire distinction collapses (both reconnect
the same way; the only difference is the log line, which `exception`
covers).

Additionally: surface the crash to the agent via a channel
notification. Generalize the `ChannelNotifier` signature from
`(slot_key, kind, summary) -> None` to `(content, meta) -> None`
where `meta` is a `Mapping[str, str]`. Slot-promotion calls pass
`{"slot_key": slot_key, "kind": kind}`; the supervisor-recovery
call passes `{"kind": "supervisor_recovered"}`. This is a small
refactor across `channel.py` + `ws_connection.py`.

### #1 — Tool-layer exception handling

In `_handle_script_job` (and mirror in `_handle_open_session_job`
/ `_handle_close_session_job`):

```
try:
    result = await self._do_*_job(job)
except Exception as exc:
    job.completion.set_exception(exc)
    return                              # do not re-raise
except BaseException as exc:
    job.completion.set_exception(
        RuntimeError(f"job cancelled: {exc!r}")
    )
    raise                               # propagate cancellation
job.completion.set_result(result)
```

The current code's "treat known tool errors specially, re-raise
everything else" pattern collapses — every `Exception` becomes a
tool error. The supervisor (issue #2's fix) is now the safety
net for any genuine state corruption.

Edge case to think about: `ProtocolError` from an event-job (not
tool-call). Today that raises out of `_dispatch_job` and tears
down the supervisor; after #2 the supervisor recovers via
reconnect, which re-subscribes and rebuilds from a fresh
snapshot. So the existing "tear-down on ProtocolError" behavior
is preserved through a different code path.

### #5a — Stdout capture in `run_script`

`script_runner.run_preamble_and_script`:
- Wrap the `exec` (preamble) + `exec`/`eval` (script) calls in
  `contextlib.redirect_stdout(io.StringIO())`.
- Capture and return the buffer's contents alongside the existing
  return.
- Surface through `RunResult.stdout: str`.
- `_summarize_mutation_outcome` (or the caller) appends
  `"\nstdout:\n{stdout}"` to the tool result string when stdout
  is non-empty.

### #5b — `_fxtr_mcp_builtin` in preamble

`script_runner.run_preamble_and_script`:
- Move the `sys.modules[BUILTIN_MODULE_NAME] = builtin_mod` install
  *before* the preamble exec.
- Move the corresponding `sys.modules.pop` to a `finally` that
  wraps both phases (so a preamble error still cleans up).

Then update `examples/basic_test_demo/agent_workdir/preamble.py`
to include the common imports
(`from _fxtr_mcp_builtin import retrieve_result, schedule_invoke,
export_entity, lookup_entity, lookup_service_by_name, lookup_entity_by_name`).

Note: `exec()` doesn't allow top-level await, so async builtins
can be imported (and called from the per-script body) but not
invoked from the preamble itself. Imports are sufficient for the
boilerplate-reduction use case.

## Implementation steps

- [x] **#3 — Debug log redirect.** `.mcp.json` launcher wraps the
      MCP in a shell pipeline that appends stderr to
      `/tmp/fxtr_workstation_mcp_debug.log` with a per-restart
      session marker.
- [x] **#2 — Supervisor safety net.** `_supervisor_loop` now treats
      any post-ready `Exception` as recoverable (wire or not): the
      cycle resets, the trace is logged, and a `supervisor_crashed`
      channel notification fires before the reconnect backoff.
      Startup errors (cycle never became ready) still propagate
      unless wire-disconnect. `ChannelNotifier` was refactored to
      a `Protocol` taking `(content, attrs)` so the system-event
      notification has a place. Test:
      `test_supervisor_recovers_from_non_wire_crash` in
      `test_reconnect.py` injects an exception into
      `_do_script_job` and verifies (a) the tool call surfaces
      the exception, (b) the supervisor reconnects, (c) a
      follow-up `run_script` succeeds.
- [x] **#1 — Tool-layer exception handling.** Unified all three
      `_handle_*_job` wrappers to the
      `Exception` → set-completion-and-return,
      `BaseException` → wrap-in-RuntimeError-and-reraise pattern.
      Test: `test_script_job_exception_does_not_crash_supervisor`
      injects an `Exception` into `_do_script_job` and verifies
      (a) the tool surfaces the exception, (b) the supervisor's
      `_ready` event does *not* swap (no reconnect — same cycle),
      (c) a follow-up `run_script` succeeds on the same cycle.
- [x] **#5 — Polish.** Stdout capture: `script_runner` wraps
      preamble + script exec in `contextlib.redirect_stdout`,
      returns the buffer; `RunResult` carries a `stdout: str`
      field; `_do_script_job` calls `_append_stdout(outcome,
      script_stdout)` so the tool result fences stdout with
      `--- stdout ---` / `--- end stdout ---` markers.
      Preamble-builtin reorder: `_fxtr_mcp_builtin` is now
      installed *before* the preamble exec, and the canonical
      `examples/basic_test_demo/agent_workdir/preamble.py`
      imports the common builtins.
- [x] **#4 — Sandbox exception wrapping.** Script errors are now
      caught *inside the sandbox* at the
      ``run_preamble_and_script`` boundary. Only ``Exception`` is
      caught (``BaseException`` still propagates so cancellation /
      ``SystemExit`` / ``KeyboardInterrupt`` reach the sandbox
      runtime). Caught exceptions are formatted via
      ``traceback.format_exception`` with a one-line headline
      (``{phase} raised {ExcType}: {message}``) and returned on
      ``RunResult.error``. Stderr is captured alongside stdout via
      a parallel ``contextlib.redirect_stderr``. On error, the
      sandbox zeros out ``queued_ops`` on the wire (script
      semantics are atomic — no partial side-effects). The host's
      ``_do_script_job`` checks ``result["error"]`` and routes
      through ``_format_script_error`` (skipping
      ``_apply_script_mutation`` entirely) instead of building +
      sending a mutation. Stdout/stderr now share a unified
      ``_fenced`` helper; both success and error paths use the
      same fence-marker format.
- [x] Update `CODE_OVERVIEW.md`.
