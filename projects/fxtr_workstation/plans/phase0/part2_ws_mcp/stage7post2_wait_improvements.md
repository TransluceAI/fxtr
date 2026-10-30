# Stage 7 post-2 — `run_script` wait knob + tool-logging decorator

Two ergonomics fixes for the agent-facing MCP, plus a small
housekeeping cleanup.

## Goals

1. **`run_script(script, wait_seconds=0.0)`** — let the agent ask
   `run_script` to block until every invoke it just scheduled has
   resolved (or the timeout elapses), so it can synchronously fetch
   results in the same tool response instead of doing a separate
   `wait_for_slot_notification` round-trip per slot.
2. **`@logged_tool("name")` decorator** — replace the inline
   `_verbose_log_call` / `_verbose_log_result` calls scattered
   through every tool body with one decorator applied inside
   `@mcp.tool()`. The decorator also logs `BaseException` exits
   (cancellation included) so a manual interrupt test can confirm
   nothing silently vanishes.
3. **Drop `load_pending`** from the codebase. The agent loads
   entities via their own `.load()` over the RPC connection — there
   is no workstation op that would create a `["load_pending", N]`
   slot today, and there are no plans to add one. The tag is dead.

## Non-goals

- Adding a fallback / replay mechanism for tool calls that were
  interrupted mid-flight. The decorator logs the BaseException so
  we can see what the loss looks like manually; if it turns out
  data is being silently lost, that's a follow-up.
- Reworking the existing 0.1s trail-delay on `connect` /
  `wait_for_slot_notification`. They keep their current behavior.
- Surfacing `wait_seconds` on `connect` or `open_session`. Only
  `run_script` schedules invokes.
- Auto-retrieving results inside `run_script` itself. The agent
  still calls `retrieve_result(slot_key)` in a follow-up script.
  The wait knob is purely a "don't return control to me until the
  events have actually landed in my response buffer" lever.

## Part 1 — `run_script(wait_seconds=...)`

### Tool surface

```
@mcp.tool()
@logged_tool("run_script")
async def run_script(
    script: str,
    ctx: Context,
    wait_seconds: float = 0.0,
) -> str: ...
```

- `wait_seconds == 0.0` (default) → current behavior preserved.
  After the mutation commits we still `anyio.sleep(0.1)` before
  draining, exactly as today. **Crucially, the 0.0 path never
  enters the wait branch and never emits a "Timed out after Xs"
  tail** — even if the script scheduled invokes that haven't
  resolved by the time the 0.1s sleep ends. The "timeout tail" is
  a wait-branch-only construct.
- `wait_seconds > 0.0` → after the mutation commits, **block
  until every invoke this script scheduled has promoted to a
  terminal value or the timeout elapses**, then drain the buffer.
  Replaces (does not stack with) the 0.1s sleep — the wait either
  resolves quickly via event arrival or we hold the full budget
  before timing out, and in both cases the buffer is drained at
  the end. The waited-on events flow through `OutgoingBuffer` the
  same way they do today, so they end up folded into the tool's
  response string.
- `wait_seconds < 0` → tool-error string ("must be non-negative").

### What "this script's invokes" means

The mutation builder consumes `runner_queued_ops` and turns
each `schedule_invoke` op into one `["invoke_pending", N]` slot
on commit. We surface the list of slot_keys we just placed into
`invoke_pending` state up to the tool layer:

- `_apply_script_mutation` walks `runner_queued_ops` for entries
  with `kind == KIND_SCHEDULE_INVOKE` and collects their
  `slot_key`s. On the commit-success path it returns these
  alongside the outcome string. On error paths (rejection,
  `SlotKeyAlreadyExists`, `ForgetMissingKey`, empty mutation) the
  list is empty.
- `_do_script_job` propagates the list up.
- `WorkstationConnection.run_script` returns a small dataclass
  (or named tuple) carrying `(body: str, scheduled_invoke_slot_keys:
  list[str])`. The string portion is unchanged from today; only
  the wrapper is new.

We deliberately scope the wait to *this* script's invokes rather
than "everything currently pending in the session" so a long-
running invoke scheduled in an earlier script doesn't delay a
later, unrelated `run_script(wait_seconds=...)`. The agent can
mix-and-match: schedule a long invoke without `wait_seconds`,
then schedule a short one with `wait_seconds=5` and have only the
short one block the response.

### Wait mechanics

In `_run_script_primary`, after `connection.run_script(...)`
returns:

1. If `wait_seconds == 0`: keep today's behavior — `await
   anyio.sleep(0.1)` and let the existing drain proceed. No
   wait-set computation, no `move_on_after`, no timeout tail.
2. If `wait_seconds > 0`:
   - For each `slot_key` in
     `scheduled_invoke_slot_keys`, peek
     `connection.peek_slot_key(slot_key)`. If the value is no
     longer `["invoke_pending", N]` (already promoted to
     `invoke_result` / `invoke_error`), drop it from the wait set.
   - For the remaining slot_keys, register a `watch_slot(...)`
     event each via `app_ctx.buffer.watch_slot(slot_key)` — this
     fires when the next notification with matching
     `attrs["slot_key"]` lands.
   - Wait for all of them under one
     `anyio.move_on_after(wait_seconds)` umbrella. The natural
     shape is an `anyio.create_task_group` with one child per
     watcher that just `await ev.wait()`s and exits.
   - On timeout, the cancel scope tears the children down; the
     `OutgoingBuffer.unwatch_slot` cleanup in `finally` releases
     any unfired watchers.
   - Drain the buffer either way.

The watch_slot peek-vs-register race is identical to the one in
`wait_for_slot_notification` (cf. CODE_OVERVIEW §"Race-safe
`wait_for_slot_notification`"): the peek and the
`watch_slot` registration are sync between awaits, so the
worker can't sneak a notification in between.

### Result formatting

- All-resolved within budget: just `_combine(primary,
  drain())` as today — the drained `<event …>` blocks reflect
  exactly the slot resolutions we waited for, plus anything else
  the buffer picked up in the same window.
- Timed out with N slots still pending: append a one-line tail
  like `Timed out after Ws waiting for invokes: [slot_a, slot_b].`
  after the drained text. The agent can decide whether to call
  `wait_for_slot_notification` per slot or move on.

We do **not** raise on timeout — the script committed
successfully, the wait is just a convenience layer.

### Edge cases

- **Script errored**: `_apply_script_mutation` never ran (sandbox
  zeros queued_ops on error). `scheduled_invoke_slot_keys = []`.
  The wait branch is a no-op.
- **Mutation rejected**: same — nothing committed, list is empty.
- **`wait_seconds=0` mutations with invokes scheduled**:
  unchanged behavior. The 0.1s sleep often catches the result
  anyway; the agent can wait_for_slot_notification later if not.
- **wait completes immediately** (all slots already terminal at
  peek time): we drain and return without sleeping. This is the
  "result landed during script send/commit" race-safe path; same
  spirit as the `wait_for_slot_notification` fast-paths.

### Docstring update

`run_script`'s docstring grows one paragraph at the bottom
documenting `wait_seconds`: default semantics, what "this
script's invokes" means, timeout behavior, "use this when you
plan to retrieve results in your next script and want to avoid a
separate `wait_for_slot_notification`" hint.

Also update the FastMCP top-level `instructions` blob if needed
(probably one extra sentence near the existing
`wait_for_slot_notification` reference).

## Part 2 — `@logged_tool` decorator

### Shape

```
def logged_tool(name: str) -> Callable[[F], F]: ...
```

Lives in `server.py` next to `_verbose_log_call` /
`_verbose_log_result` (one place for all verbose-logging concerns).
Applied inside `@mcp.tool()`:

```
@mcp.tool()
@logged_tool("run_script")
async def run_script(...): ...
```

(Inside, not outside, so FastMCP wraps the *logged* function — we
want every call FastMCP delivers to flow through the logger.)

### Behavior

- **On entry**: bind `(*args, **kwargs)` against the wrapped
  function's `inspect.signature`, drop `ctx`, pass the rest
  to `_verbose_log_call(name, **bound_args)`. The existing
  multiline-aware formatting handles `run_script`'s `script` arg
  cleanly.
- **On success**: `_verbose_log_result(name, result)` exactly as
  today.
- **On `BaseException`**: log a `[{name}] raised` fenced block
  with body = `f"{type(e).__name__}: {e!r}"` (no traceback —
  cancellation tracebacks are noisy and the type+repr is what the
  manual interrupt test needs), then `raise`. Catches both
  `Exception` and `BaseException` (CancelledError, KeyboardInterrupt,
  SystemExit). Always re-raises; this is observation-only.

The decorator is a no-op when `_VERBOSE_IO` is off, same as the
existing helpers (the underlying `_verbose_log_*` functions
already short-circuit). One sig-bind per call when verbose is on
is cheap; off-path we still bind but skip writing — acceptable.
If perf matters we can early-out on the `_VERBOSE_IO` flag.

### FastMCP signature footgun

FastMCP introspects the tool's signature to generate the
JSON-Schema for the agent. Our wrapper has `(*args, **kwargs)`
under the hood, so the `inspect.signature` path *must* recover
the wrapped function's real signature. `functools.wraps` sets
`__wrapped__`, and `inspect.signature` follows `__wrapped__` by
default, so this is expected to "just work." Implementation
checklist:

- After applying `@logged_tool("run_script")`, smoke-test
  `inspect.signature(run_script)` shows `script, ctx,
  wait_seconds` — not `*args, **kwargs`.
- If FastMCP relies on something lower-level that ignores
  `__wrapped__`, fall back to copying `__signature__` onto the
  wrapper explicitly (`wrapper.__signature__ = inspect.signature(fn)`).

### Perf early-out

The wrapper's first line is `if not _VERBOSE_IO: return await
fn(*args, **kwargs)` so the off-path skips the sig-bind entirely.
Verbose-on path pays the bind cost, which is what we want for the
diagnostic value.

### Removed code

Each tool body loses its inline `_verbose_log_call(...)` at the
top and `_verbose_log_result(...)` at the bottom, plus all the
intermediate `_verbose_log_result(tool_name, result)` calls on
error-path early returns (e.g. `set_working_dir`'s preconditional
errors). The decorator handles all of it.

The standalone `_verbose_log_call` / `_verbose_log_result`
helpers stay (the decorator delegates to them); only the call
sites collapse.

### Tools covered

All six MCP tools get the decorator:

- `set_working_dir`
- `connect`
- `open_session`
- `close_session`
- `run_script`
- `wait_for_slot_notification`

The outgoing-channel-frame logging in `lifespan` is independent
of the tool decorator and stays as-is.

## Part 3 — Remove `load_pending`

Mechanical cleanup. Touched files:

- `session_state.py`
  - `_is_pending_marker`: drop `"load_pending"` from the tuple.
  - Docstrings on `SessionMirror`, `_is_pending_marker`, and the
    `CALLBACK_OPEN_TAG` comment: drop the `load_pending`
    references.
- `server.py`
  - `wait_for_slot_notification`'s tag-dispatch tuple becomes
    `("invoke_pending",)`. Docstring + the "tags in
    `mirror.data_by_key`" preamble comment lose the
    `load_pending` mentions.
  - Any `_wait_for_slot_notification_inner` branch comments that
    name it.
- `tests/test_event_seq_redesign.py`
  - The test that injects a `["load_pending", 7]` value: drop
    that entry (or rename to a second `invoke_pending` slot). The
    test's intent is "pending_slot_index handles multiple pending
    markers"; that still holds with two `invoke_pending`s.
- `claude_plugin/skills/use_fxtr_workstation/SKILL.md`: drop the
  `load_pending` mention from the slot-tag explainer.
- `CODE_OVERVIEW.md`: scrub the four references (pending_slot_index
  description, `peek_slot_key` terminal-tag list, the race-safe
  `wait_for_slot_notification` per-kind list, invariant #5).
- Sandbox-bridge check: grep
  `projects/fxtr_workstation_agent_plugin/sandbox/` for
  `load_pending`; if `builtins.py` / `protocols.py` /
  `script_runner.py` reference the tag, scrub there too. The
  cleanup must be symmetric or the runtime will silently swallow
  a never-occurring case.

We leave the historical plan docs
(`stage3_post2_event_seq_redesign.md`, `stage6_callbacks.md`,
`stage7_better_notify.md`, `stage7post1_crash_fixes.md`)
unchanged — they're history.

If `load_pending` is removed, any future load-via-workstation
flow (none planned) will re-introduce a new tag of its choosing.

## Part 4 — Manual interrupt-loss test

After Parts 1–3 land, the user manually verifies that interrupting
a tool call mid-flight doesn't lose buffered events:

1. Start an MCP session with `FXTR_WORKSTATION_MCP_VERBOSE=1`.
2. Kick off a long `run_script(wait_seconds=30)` whose script
   schedules an invoke that resolves after a few seconds.
3. Interrupt the tool call (Ctrl-C / agent cancel) before the
   wait completes.
4. Confirm via the verbose-stderr replay that the `[run_script]
   raised: CancelledError: ...` entry shows up and that the next
   `run_script` (or `wait_for_slot_notification`) call still
   surfaces the invoke result — either inline in the drained text
   or via a debounced channel frame.

If the test shows data loss, that's a follow-up to design (likely
"persist the buffer across cancellation" or "re-flush on next
tool entry"). The decorator's BaseException log is the diagnostic
hook for that future work.

## Tests to add

Per-part test additions (specifics; mirror the style of existing
files in `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/`):

**Part 1 — `wait_seconds`**, in
`tests/test_invoke_e2e.py` (or a new
`tests/test_run_script_wait_seconds.py` if it grows):

- `test_run_script_wait_seconds_blocks_until_invoke_resolves` —
  schedule a `delayed-echo` invoke with a short delay (~0.2s),
  call `run_script(..., wait_seconds=5)`, assert the response
  contains an inline `<event slot_key="..." kind="invoke_result">`
  block for the slot. Without `wait_seconds`, the same call's
  0.1s sleep would still see the slot as `invoke_pending`.
- `test_run_script_wait_seconds_times_out` — delay longer than
  the budget; assert the response body contains the script's
  commit text *and* a tail like `Timed out after Xs waiting for
  invokes: [...]`.
- `test_run_script_wait_seconds_zero_no_timeout_tail` — explicit
  guard: schedule an invoke that won't resolve within 0.1s, call
  `run_script(...)` (default `wait_seconds=0.0`), assert the
  response does **not** contain "Timed out". Catches a future
  refactor that accidentally routes 0.0 through the wait branch.
- `test_run_script_wait_seconds_negative_errors` — input
  validation; tool returns an error string, mutation not
  attempted.
- `test_run_script_wait_seconds_only_waits_for_this_scripts_invokes` —
  two scripts: the first schedules a long invoke without
  `wait_seconds`; the second schedules a short invoke *with*
  `wait_seconds=5`. The second returns as soon as its own invoke
  resolves; the long invoke from script 1 may still be pending.

**Part 2 — `@logged_tool`**, in a small new
`tests/test_tool_logging.py` (verbose-mode only, captures
`sys.stderr`):

- `test_logged_tool_logs_call_and_result_when_verbose` — verbose
  on, call a tool, assert stderr contains the `[name] call` and
  `[name] result` fenced blocks.
- `test_logged_tool_logs_base_exception` — verbose on, force a
  tool to raise (e.g. `set_working_dir("/nonexistent")` raising
  inside, or monkey-patch); assert stderr contains a
  `[name] raised` block with the type name and re-raised exception.
- `test_logged_tool_no_overhead_when_verbose_off` — verbose off,
  call a tool, assert stderr does **not** contain any
  `=== [name]` fence markers. (Pairs with the perf early-out.)

**Part 3 — `load_pending` cleanup**: no new tests; existing tests
that referenced the tag get updated as part of the removal.

## Implementation steps

1. **Part 1 — `wait_seconds`**
   - [x] Define a small dataclass `ScriptOutcome(body: str,
         scheduled_invoke_slot_keys: list[str])` near
         `_do_script_job` in `ws_connection.py`.
   - [x] Update `_apply_script_mutation` to walk `runner_queued_ops`
         for `KIND_SCHEDULE_INVOKE` entries and include the
         collected slot_keys in its return value on the
         commit-success path (empty list on every other path).
   - [x] Update `_do_script_job` and
         `WorkstationConnection.run_script` return types
         accordingly. (`_handle_script_job` needed no change —
         it just forwards the result onto the typed Future.)
   - [x] Update `_run_script_primary` to unpack the new shape.
         Return type changed to `tuple[str, list[str]]` so the
         tool body has the slot_keys for its wait branch.
   - [x] Add the `wait_seconds: float = 0.0` parameter to the
         `run_script` tool. Validate (>= 0).
   - [x] Implement the wait branch: peek + filter, register
         watchers, wait under `move_on_after`, drain, append the
         timeout-tail if applicable. New helper
         `_wait_for_scheduled_invokes`.
   - [x] Update the tool's docstring + the FastMCP `instructions`
         blob.
   - [x] Update existing test `test_reconnect.py`'s
         `_erroring_do_script_job` return type annotation to
         `ScriptOutcome` (was `str`).
   - [x] Add five tests in
         `tests/test_run_script_wait_seconds.py` (uses a new
         `_DelayedEchoImpl` peer service with a configurable
         delay).
   - [x] Update CODE_OVERVIEW.md — new "Inline-wait on scheduled
         invokes (stage 7 post-2)" section under the
         outgoing-buffer area; trimmed the old "`run_script` and
         `connect` additionally sleep 0.1s" line to reflect the
         new branching behavior.

2. **Part 2 — `@logged_tool` decorator**
   - [x] Implement `logged_tool(name)` in `server.py`. Use
         `inspect.signature` to bind args; drop `ctx`.
         `functools.wraps` preserves the signature for FastMCP
         (verified via `inspect.signature` smoke check on all
         six tools post-decoration).
   - [x] BaseException branch logs type + repr + re-raises.
   - [x] Apply to all six tools; remove the inline
         `_verbose_log_call` / `_verbose_log_result` call sites.
   - [x] Quick verification: 4 unit tests in
         `tests/test_tool_logging.py` cover verbose-on call+result
         logging, BaseException logging + re-raise, verbose-off
         no-output, and signature preservation.
   - [x] Update CODE_OVERVIEW.md — added a brief mention of
         `logged_tool` in the server.py file listing.

3. **Part 3 — drop `load_pending`**
   - [x] Remove from `_is_pending_marker` + docstrings in
         `session_state.py` (`_is_pending_marker`,
         `CALLBACK_OPEN_TAG` comment, `SessionMirror`'s
         `pending_slot_index` docstring).
   - [x] Update `server.py`'s tag dispatch + comment block in
         `wait_for_slot_notification` + the terminal-slot
         comment.
   - [x] Update `tests/test_event_seq_redesign.py`'s
         `test_pending_slot_index_populated_from_state` to use
         two `invoke_pending` slots (intent preserved).
   - [x] Scrub `SKILL.md` (and fixed a stale "errors out on
         terminal" claim that stage7post1 had already
         invalidated).
   - [x] Scrub `CODE_OVERVIEW.md` — four references:
         `pending_slot_index` description, `peek_slot_key`
         terminal-tag list, race-safe
         `wait_for_slot_notification` per-kind list,
         invariant #5.
   - [x] Final grep confirms no live references remain (plan
         docs left untouched as historical).

4. **Part 4 — manual interrupt test**
   - User-driven; no code changes. Capture the result in a chat
     follow-up; if data was lost, add a follow-up bullet at the
     bottom of this plan.

5. **Final pass — Update CODE_OVERVIEW.md** so the file listing,
   tool-surface description, mutation-lifecycle section, and the
   `wait_for_slot_notification` / `run_script` per-kind text all
   read consistently with the new state. [x] Done — added
   "Inline-wait on scheduled invokes (stage 7 post-2)" section,
   updated trail-delay description, scrubbed all live
   `load_pending` mentions, listed the new test files in the
   "How tests work" section, mentioned `logged_tool` in the
   server.py file-listing line.

## Open questions / things to revisit

- **Default `wait_seconds` of `0.0` vs a small positive default.**
  Defaulting to 0.0 preserves every existing test and caller
  byte-for-byte; a small default (say, 0.1 or 1.0) might be
  friendlier in practice. Going with 0.0 for now and revisiting if
  the agent in practice always passes `wait_seconds`.
- **Interaction with `wait_for_slot_notification`.** If the agent
  uses `run_script(wait_seconds=10)` to wait for slot `r`, then
  immediately calls `wait_for_slot_notification("r", ...)`, the
  second call's race-safe terminal-slot fast-path kicks in (`r`
  is `invoke_result` by now) and it returns success without
  parking. Good — no churn needed.
- **Decorator + non-tool helpers.** Today's verbose logging covers
  only tool entry/exit. Internal helpers (`_run_script_primary`,
  `_do_force_close_session`, etc.) aren't wrapped. We're not
  changing that; if the manual interrupt test surfaces a gap
  there, we can add a sibling decorator for selected helpers.
