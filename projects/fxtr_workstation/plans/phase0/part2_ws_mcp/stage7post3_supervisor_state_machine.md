# Stage 7 post-3 — supervisor state machine rewrite

The supervisor loop has grown three overlapping concerns —
state representation, error classification, and recovery
policy — that interact through five fields (`_ready`,
`_paused`, `_supervisor_error`, `_wake`, plus
`reached_ready` derived from `_ready.is_set()`). The atomic-claim
handshake in `wait_ready` makes the supervisor's state machine
half-owned by the tool layer, and a pre-ready non-wire crash
re-raises out of the supervisor task — which kills the lifespan
task group and tears down the whole MCP. See
`stage7post3_supervisor_state_machine_scratch.md` for the
specific bug trace that motivated this rewrite.

This stage collapses the state machine to one enum + two
events, makes the supervisor the only writer of its own state,
removes exponential-backoff entirely in favor of a much simpler
free-retry-budget model, and ensures the supervisor task only
exits on explicit shutdown or external cancellation. No
non-cancellation exception ever escapes the supervisor.

## Goal

1. **No MCP-wide tear-down from a connection error.** The
   supervisor task never re-raises a non-cancellation
   exception. Whatever happens to the workstation connection,
   the lifespan task group stays alive.
2. **One state representation.** A single `SupervisorState`
   enum plus a `pause_reason: str | None` field, mutated only
   by the supervisor task. Tools observe state via a
   condition-variable-style `state_changed` event.
3. **Uniform pause path.** Every failure — pre-ready, post-ready,
   wire, non-wire — eventually leads to PAUSED, except wire
   blips after the cycle reached READY which get a bounded
   number of free auto-retries.
4. **Simpler reconnection.** Replace exponential backoff +
   max-attempts with: "one free retry per ready→drop
   transition, capped by a small budget that refreshes on
   every successful tool-ready handshake." No backoff sleeps,
   no attempt counters.
5. **Trivial `connect` tool.** The supervisor starts in PAUSED
   immediately on construction (no resources held). `connect`
   is essentially just "wake and wait until ready." No lazy
   start, no `_ensure_connected` dance.

## Non-goals

- Changing the job-queue model, reader/worker split, mirror /
  replayable-events design, or self-commit flow. Those stay
  intact.
- Changing `force_close_session_one_shot` — it runs entirely
  outside the supervisor, unaffected.
- Surfacing wire-blip auto-retries to the agent. Successful
  silent recovery stays silent (no channel banner).
- Per-tool retry caps or backoff configuration. The retry
  cadence is driven by the agent's own tool-call cadence
  (after the initial in-call free-retry, the agent must call
  another tool to wake the supervisor).

## States and transitions

Four states:

```
PAUSED       # supervisor parked; needs an external wake signal
STARTING     # cycle in progress; not yet subscribed/ready
READY        # cycle subscribed; worker draining jobs
EXITED       # lifespan over; terminal
```

Initial state is `PAUSED` with `pause_reason = "(not started)"`.
The supervisor task is spawned by `start(tg)`; its first
action is to wait on `retry_requested` (or `shutdown`), so no
work happens until a tool calls `wake_and_wait_ready`.

Transitions (all driven by the supervisor task):

- `PAUSED → STARTING`: `retry_requested` fired by a tool.
- `STARTING → READY`: cycle subscribed (or no-active-session
  cycle reached the post-subscribe point); worker about to
  drain jobs.
- `STARTING → PAUSED`: cycle raised an exception before
  reaching READY. `pause_reason` describes the failure.
- `READY → STARTING`: cycle raised a *wire* exception **and**
  `free_retry_budget > 0`. Budget decrements. No state-event
  fires to the agent.
- `READY → PAUSED`: cycle raised any non-wire exception, or a
  wire exception with the budget exhausted.
- `* → EXITED`: shutdown fired or cancellation propagated;
  reached from the supervisor's outer finally.

`READY → STARTING` is the "free retry" path. Everything else
that isn't shutdown/cancellation routes through `PAUSED`.

## Connection-level fields

Replace the five-field state today with:

```
state: SupervisorState                         # current state
pause_reason: str | None                       # set when state == PAUSED or EXITED
state_changed: anyio.Event                     # condition-var; fires on every state change, replaced after
retry_requested: anyio.Event                   # tool → supervisor: please leave PAUSED
shutdown: anyio.Event                          # lifespan → supervisor: exit
exited: anyio.Event                            # supervisor → lifespan: I'm done
free_retry_budget: int                         # decrements per free retry; refreshed by wake_and_wait_ready
```

Removed: `_ready`, `_paused`, `_supervisor_error`, `_wake`,
`_reconnect_backoff_initial`, `_reconnect_backoff_max`,
`_reconnect_max_attempts`, and the constructor params that
fed those last three.

A `SupervisorNotReady(RuntimeError)` exception class replaces
`SupervisorPaused`. It carries the pause reason string as its
message. Tools see this from `wake_and_wait_ready`.

The `WorkstationRejected` / `ConnectionLost` /
`SessionAlreadyActive` / `NoActiveSession` / `ProtocolError`
exceptions are unchanged in shape and usage — they describe
specific failure kinds emitted by per-job handlers and consumed
internally by the supervisor when building `pause_reason`. They
no longer escape the supervisor task.

## Atomic state update

The supervisor's `set_state(new_state, reason=None)` helper:

```pseudo
def set_state(self, new_state, reason=None):
    self.state = new_state
    self.pause_reason = reason
    ev = self.state_changed
    self.state_changed = anyio.Event()
    ev.set()
```

The current event reference is replaced *before* it is set, so
readers that captured the pre-update event reference see it
fire; readers that captured after the update get a fresh unset
event tied to the next state change. Standard condition-var
pattern using anyio Events.

## `wake_and_wait_ready` (renamed from `wait_ready`)

Tool entry point. Idempotent. Signals retry at most once per
call, then waits for the supervisor to transition to either
READY (return) or PAUSED (raise).

```pseudo
async def wake_and_wait_ready(self):
    requested_retry = False
    while True:
        # Capture event reference BEFORE reading state.
        ev = self.state_changed
        state = self.state
        if state == READY:
            self.free_retry_budget = MAX_FREE_RETRY_BUDGET
            return
        if state == EXITED:
            raise SupervisorNotReady(self.pause_reason or "supervisor exited")
        if state == PAUSED:
            if requested_retry:
                raise SupervisorNotReady(self.pause_reason)
            self.retry_requested.set()
            requested_retry = True
        # STARTING (or transient), wait for next state change.
        await ev.wait()
```

Properties:

- **Each call asks for at most one retry.** If the supervisor's
  first retry attempt also fails, the caller surfaces the
  error rather than looping inside `wake_and_wait_ready`.
- **Successful return refreshes the free-retry budget.** This is
  the only place the budget refreshes; it's tied to "the agent
  actually got useful work done through this connection."
- **No state mutation by the caller.** The supervisor is the
  only writer of `state` / `pause_reason` / `state_changed`.
  The caller only sets `retry_requested`.
- **Concurrent callers are safe.** Multiple callers signaling
  `retry_requested` is fine (Event.set is idempotent); each
  caller's per-instance `requested_retry` flag is per-call.

## Supervisor loop

```pseudo
async def _supervisor_loop(self):
    try:
        while not self.shutdown.is_set():
            # Park: wait for wake or shutdown. The first iteration
            # of the loop hits this branch directly (initial state
            # is PAUSED). Subsequent iterations either re-enter the
            # while body after a free retry (skipping this branch)
            # or hit this branch after entering PAUSED.
            if self.state == PAUSED:
                await self._wait_for_retry_or_shutdown()
                if self.shutdown.is_set():
                    return
                self.retry_requested = anyio.Event()  # fresh for next park

            # Start a cycle.
            self._reached_ready_this_cycle = False
            self.set_state(STARTING)
            try:
                await self._run_one_cycle()   # internally calls set_state(READY)
                                              # then waits on shutdown
                return                        # clean shutdown
            except BaseException as e:
                if isinstance(e, cancelled_exc_class):
                    raise                     # external cancellation propagates
                self._reset_per_cycle_state()
                reason = self._format_pause_reason(e)
                _log.exception("workstation supervisor cycle failed; %s", reason)

                wire = _is_wire_disconnect(e)
                if wire and self._reached_ready_this_cycle and self.free_retry_budget > 0:
                    self.free_retry_budget -= 1
                    continue                  # free retry: skip park, run another cycle
                self.set_state(PAUSED, reason)
                await self._maybe_notify_paused(reason)
                # Top-of-loop will hit the park branch on next iteration.
    finally:
        self.set_state(EXITED, "lifespan exited" if self.pause_reason is None else self.pause_reason)
        self._drain_pending_jobs_and_requests()
        self.exited.set()
```

The cycle helper `_run_one_cycle` does what it does today, but
also calls `self.set_state(READY)` and sets
`self._reached_ready_this_cycle = True` at the end of subscribe
(or at the equivalent post-subscribe point in the
no-active-session branch — see below). It then waits on
`self.shutdown` and returns cleanly when shutdown fires, which
exits the loop's while body via the `return` after `_run_one_cycle`.

### Subscribe vs no-active-session ready point

Today the inner block reads:

```pseudo
async with inner_tg:
    inner_tg.start_soon(reader_loop)
    if _active_session_id is not None:
        await _do_subscribe(_active_session_id)
    inner_tg.start_soon(worker_loop)
    self._ready.set()
    await self.shutdown.wait()
    inner_tg.cancel_scope.cancel()
```

The new version replaces `self._ready.set()` with
`self.set_state(READY); self._reached_ready_this_cycle = True`.
The READY transition still fires after subscribe (when
applicable), so the agent doesn't see a premature ready.

### Pause-reason formatting

`_format_pause_reason(exc)` produces a short, agent-readable
string. Examples by exception kind:

- `WorkstationRejected(...)` → `"workstation rejected: <message>"`
- `ConnectionLost(...)` /
  `httpx_ws.HTTPXWSException(...)` →
  `"connection lost: <repr>"`
- `ProtocolError(...)` → `"protocol error: <message>"`
- `NotImplementedError(...)` →
  `"unimplemented handler: <message>"`
- Anything else → `"unexpected error: <type>: <message>"`
- For `BaseExceptionGroup`, walk leaves and concatenate up to
  the first two with " | " between them.

The same string lands in (a) `pause_reason` (read by
`wake_and_wait_ready` to build `SupervisorNotReady`), (b) the
channel banner content, and (c) `_log.exception`'s extra arg.

### Channel banner

Fire one `notifications/claude/channel` frame on every entry
to PAUSED **except** the initial `PAUSED → STARTING → PAUSED`
chain triggered by a tool's own `wake_and_wait_ready`. Or
more concretely: fire on every entry to PAUSED, period. The
"my own tool call triggered this and I'll see the
`SupervisorNotReady` error anyway" case duplicates the info
across two channels, but that's harmless and avoids
state-tracking in the supervisor.

Keep the channel `kind` attribute as `supervisor_paused` (was
`supervisor_crashed` in stage 7 post-1 — that name was already
a "park, don't crash" misnomer; this rename completes the
correction).

The initial `PAUSED` state at construction time does not fire
a banner — the supervisor task has not yet emitted any
state-change, since `set_state` is only called from inside the
running supervisor.

## Lifespan and tool integration

### Connection creation moves to `set_working_dir`

Today the connection is lazily created the first time a tool
calls `_ensure_connected`. With the new model the supervisor
sits idle in PAUSED holding no resources, so we can construct
+ `start(tg)` it immediately when the agent calls
`set_working_dir`. This makes the connection's lifecycle line
up with its config dependency.

```pseudo
async def set_working_dir(path, ctx):
    # ... existing config-loading body ...
    app_ctx.connection = WorkstationConnection(rpc_url=..., client_url=..., ...)
    app_ctx.connection.start(app_ctx.task_group)
    return "Working dir set to ..."
```

### Tools call `wake_and_wait_ready` directly

`_ensure_connected` collapses to a thin gate:

```pseudo
async def _ensure_connected(app_ctx):
    if app_ctx.config is None:
        raise RuntimeError("Working dir is not set. Call set_working_dir(<path>) first; ...")
    await app_ctx.connection.wake_and_wait_ready()
```

(`app_ctx.connection` is guaranteed non-None once
`set_working_dir` has been called.)

### `connect` tool body

```pseudo
async def connect(ctx):
    app_ctx = _app_ctx(ctx)
    with app_ctx.buffer.tool_window() as drain:
        try:
            await _ensure_connected(app_ctx)
            cfg = app_ctx.config
            primary = f"connected to {cfg.workstation_url}; session={cfg.active_session or 'none'}"
        except SupervisorNotReady as e:
            primary = f"Error: {e}"
        except Exception as e:
            primary = f"Error: {e}"
        await anyio.sleep(0.1)   # trail-delay for subsequent_events drain
        return _combine(primary, drain())
```

The two `except` branches are equivalent in shape — separating
them is purely for readability so the most expected error
class shows up first in the body.

### Other tools

`open_session`, `close_session`, `run_script`,
`wait_for_slot_notification`: same pattern, replace
`wait_ready` calls with `wake_and_wait_ready`. `set_working_dir`
gets the new connection-creation hook. `close_session(force=True)`
unchanged.

## In-flight jobs across pause boundaries

We deliberately **do not** drain the job queue when entering
PAUSED. Rationale (from discussion):

- The job queue manages MCP-level work, not connection state.
  Tool calls submit jobs after a successful
  `wake_and_wait_ready`; the agent runs one tool at a time.
- A tool whose job was mid-flight when the cycle died gets its
  completion future failed by the per-job handler's
  `BaseException` block (existing behavior). The tool returns
  an error to the agent.
- Jobs queued behind it (rare — usually only an `_EventJob`
  could be queued during a tool's execution) sit in the queue.
  When the supervisor un-pauses and the new cycle's worker
  starts, it drains them. `_EventJob`s from the old
  subscription_id are routed via the reader against the new
  subscription — the old sub_id mismatch causes the reader to
  drop them, which is correct (subscribe-time replay rebuilds
  state).
- `_AckCommitJob` queued for a session that died is harmless —
  the new cycle's `_last_processed_seq` starts at 0, the ack
  job advances it to the old commit's seq, which is irrelevant
  to the new subscription. We could filter these out, but it
  doesn't matter for correctness; leave it alone.

The only drain that runs is `_drain_pending_jobs_and_requests`
in the supervisor's outer finally — at lifespan exit. That
fails all queued tool-call jobs with `ConnectionLost`, same as
today.

## Free-retry budget — operational picture

Default `MAX_FREE_RETRY_BUDGET = 3`. With the refresh-on-ready
semantics:

- Healthy connection, occasional wire blip: every blip
  consumes 1 from the budget; every successful tool call
  refreshes it. So as long as the agent is interacting with the
  workstation between blips, the budget is effectively
  unbounded.
- Pathological flap: workstation accepts subscribe then
  immediately closes the websocket. Cycle 1: ready → drop →
  budget 2. Cycle 2: ready → drop → budget 1. Cycle 3: ready →
  drop → budget 0. Cycle 4: ready → drop → no more free
  retries → PAUSED. Agent sees a `supervisor_paused` banner.
  Next tool call triggers a single retry attempt (via
  `wake_and_wait_ready`); same behavior — succeeds and
  refreshes, or pauses again.

The budget is a constant, not configurable. Operators who need
a different value can change the source; we shouldn't be
exposing tuning knobs for behavior the agent can't directly
observe.

## Tests

`tests/test_reconnect.py` is renamed/restructured into
`tests/test_supervisor_state_machine.py`. Some existing tests
adapt (reconnect-after-blip, force-close recovery); others
become irrelevant (backoff timing, max-attempts exhaustion).

### Tests to keep, adapted

- `test_reconnect_after_forced_disconnect` → assertion shape:
  the supervisor stays in READY after one cycle drop +
  successful free retry; no PAUSED transition; no
  agent-visible banner.
- `test_script_job_exception_does_not_crash_supervisor` →
  unchanged in intent; assertions update to use
  `state == READY` rather than `_ready is prior_ready`.
- `test_reconnect_after_disconnect_with_queued_call` → assert
  the queued job runs against the post-retry cycle and
  succeeds.
- `test_park_then_force_close_recovers_to_clean_state` →
  unchanged in intent; uses `state == PAUSED` /
  `wake_and_wait_ready` semantics.

### New tests

- `test_supervisor_starts_in_paused_state` — construct +
  `start(tg)`, assert `state == PAUSED` immediately,
  `pause_reason == "(not started)"`, no banner emitted.
- `test_connect_tool_wakes_supervisor` — call `connect`;
  assert one STARTING → READY transition, tool returns success.
- `test_wake_and_wait_ready_raises_on_repeated_pause` — rig
  the cycle to fail twice in a row; assert
  `wake_and_wait_ready` raises `SupervisorNotReady` after
  exactly one retry attempt.
- `test_free_retry_budget_consumes_per_drop` — rig the wire
  to drop immediately after READY; assert `MAX_FREE_RETRY_BUDGET`
  consecutive free retries before PAUSED.
- `test_free_retry_budget_refreshes_on_ready_handshake` —
  call `wake_and_wait_ready` between drops; assert budget
  resets so the next drop gets a fresh budget.
- `test_non_wire_post_ready_crash_pauses_without_free_retry` —
  rig `_do_event_job` to raise `ProtocolError`; assert
  immediate PAUSED, no retry attempt, banner fired.
- `test_pre_ready_failure_pauses_without_free_retry` — rig
  subscribe to raise `WorkstationRejected`; assert immediate
  PAUSED with the rejection message in `pause_reason`.
- `test_pause_banner_fires_on_every_pause_entry` — drive into
  PAUSED twice in a row (with wake in between); assert two
  banners delivered.
- `test_paused_state_carries_string_reason` — assert the
  `pause_reason` matches the expected formatted string for
  each exception kind.
- `test_state_changed_event_concurrent_waiters` — two
  concurrent `wake_and_wait_ready` callers both observe a
  successful transition; both refresh the budget on their own
  return (idempotent — budget stays at MAX).
- `test_lifespan_shutdown_transitions_to_exited` — shutdown
  fires; assert `state == EXITED`, `wake_and_wait_ready`
  raises `SupervisorNotReady("supervisor exited" / "lifespan exited")`.

### Tests to delete

- Anything asserting specific backoff timing
  (`test_reconnect_max_attempts_exhausted`, if present), tests
  for `_reconnect_backoff_initial` / `_reconnect_max_attempts`
  constructor args.

## Implementation steps

1. **Connection-level state plumbing.**
   - [x] Add `SupervisorState` enum and
         `SupervisorNotReady` exception class to
         `ws_connection.py`. Remove `SupervisorPaused`.
   - [x] Add the new fields (`state`, `pause_reason`,
         `state_changed`, `retry_requested`,
         `free_retry_budget`) to `WorkstationConnection.__init__`.
         Remove the deleted fields (`_paused`, `_wake`,
         `_supervisor_error`, `_ready`, backoff args). Keep
         `_shutdown` / `_exited` and constructor signature
         elsewhere unchanged where possible.
   - [x] Implement `set_state(new_state, reason=None)`
         helper that fires + replaces `state_changed`.
   - [x] Implement `_format_pause_reason(exc)` helper
         (module-level, alongside `_is_wire_disconnect`).
   - [x] Update `is_ready` property to read `state == READY`.

2. **Supervisor loop body.**
   - [x] Rewrite `_supervisor_loop` against the pseudocode
         above: initial park, main while loop, free-retry
         continue path, PAUSED entry, banner emission, final
         EXITED transition.
   - [x] Update `_run_inner` to call
         `set_state(READY)` and flip
         `_reached_ready_this_cycle = True` at the
         subscribe-complete point.
   - [x] Update `_maybe_notify_supervisor_paused` to take
         the formatted reason string instead of an exception;
         rename the channel `kind` attribute to
         `supervisor_paused`.

3. **`wake_and_wait_ready` (rename + rewrite).**
   - [x] Replace `wait_ready` with `wake_and_wait_ready`
         (pseudocode above).
   - [x] Delete `_enter_paused_state` and
         `_wait_for_wake_or_shutdown` — the new design folds
         their behavior into `_supervisor_loop` directly.
   - [x] Add `_wait_for_retry_or_shutdown` helper as the
         single park primitive used by the supervisor.

4. **Tool surface.**
   - [x] Update `set_working_dir` to construct +
         `start(tg)` the `WorkstationConnection`.
   - [x] Trim `_ensure_connected` to the config check +
         `wake_and_wait_ready` call.
   - [x] Update `connect` body per the pseudocode (the
         `except SupervisorNotReady` arm).
   - [x] Replace `wait_ready` callsites in `open_session`,
         `close_session`, `run_script`,
         `wait_for_slot_notification` with
         `wake_and_wait_ready`. (The renames happened inside
         `ws_connection.py`'s public methods themselves;
         tool code that goes through `_ensure_connected`
         picks it up automatically. The only callsite in the
         tool surface is the `_ensure_connected` body.)
   - [x] Update CODE_OVERVIEW.md "Two long-lived connections,
         one supervisor" section to reflect the new state
         machine (delete the atomic-claim handshake
         description; replace the pre-ready/post-ready
         classification text with the single-pause-path model).

5. **Tests.**
   - [x] Restructure `tests/test_reconnect.py` into
         `tests/test_supervisor_state_machine.py`. Move kept
         tests, adapt assertions, add new tests per the
         "New tests" list. Delete now-irrelevant tests.
   - [x] Update other test files referencing
         `connection._ready` (was `test_script_error.py`'s
         "supervisor didn't reconnect" assertion); replaced
         with `is_ready` checks against the new state.
   - [x] All 11 state-machine tests pass; existing e2e and
         in-memory tests still pass on the rewrite.

6. **Manual sanity check.**
   - [ ] Run the basic_test_demo flow:
         `set_working_dir`, `connect`, `open_session`,
         `run_script`, `close_session`. Verify the success
         path is unchanged from the agent's perspective.
   - [ ] Manually break it: point at an unreachable
         workstation URL, call `connect`, verify the error
         body reads cleanly ("Error: connection lost: ..."),
         verify the MCP stays alive, verify a second
         `connect` call surfaces the same error without
         crashing.
   - [ ] Manually trigger a stale-session case: set
         `active_session` in the toml to a non-existent
         UUID, call `connect`, verify the error body reads
         "Error: workstation rejected: subscribe_session(...)
         failed: ...", verify the MCP stays alive.

7. **Update CODE_OVERVIEW.md (final pass).**
   - [x] Rewrote the supervisor / parking section of
         `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`
         against the new state-machine model. Updated the
         invariants list (replaced #11 detail about
         supervisor exit-on-non-cancellation, added #13 +
         #14 covering the no-escape and string-reason rules).
   - [x] Updated the test-file listing under "How tests work"
         (added `test_supervisor_state_machine.py` in the
         direct-call group).

## Open questions / things to revisit

- **`MAX_FREE_RETRY_BUDGET = 3` is a guess.** Pick a number;
  revise if real-world flap exposes a better value.
- **Banner duplication on tool-driven pause.** Firing the
  channel banner on every PAUSED entry means a tool whose
  `wake_and_wait_ready` raised `SupervisorNotReady` *also*
  causes a banner. The agent gets the info twice (once as
  the tool error, once as the banner). Acceptable; revisit
  if it's noisy in practice.
- **`_AckCommitJob` against a dead cycle.** A queued ack-commit
  from before a cycle drop runs against the new cycle's
  `_last_processed_seq` (starts at 0). It'd advance the seq
  to a value from the old session, which is then irrelevant
  to the new subscription. Confirmed harmless in design but
  worth a verifying test if behavior surprises.
