# stage7post3 — scratch

Working notes for the supervisor state-machine rewrite. The
plan doc has the final design; this captures the diagnosis,
alternatives considered, and decisions made during discussion.

## The triggering bug

Symptom: agent calls `connect` against a config with a stale
`active_session` UUID. The MCP dies and the tool call returns
nothing (or `CancelledError`, depending on race timing).

Trace:

1. `connect` tool → `_ensure_connected` → constructs
   `WorkstationConnection` → calls `connection.start(tg)` →
   `await connection.wait_ready()`.
2. Supervisor's `_run_one_cycle` opens `/rpc`, opens `/client`,
   enters `_run_inner`, calls
   `_do_subscribe(initial_session_id)`.
3. Workstation responds with `ErrorResponse(...)` because the
   session UUID is stale.
4. `_do_subscribe` raises `WorkstationRejected(...)` (a
   `RuntimeError`).
5. The exception propagates out of `_run_inner` →
   `_run_one_cycle` → into `_supervisor_loop`'s
   `except Exception as e:`.
6. Classification:
   `cycle_was_ready = self._ready.is_set()` is **False** (subscribe
   never reached the `self._ready.set()` line).
   `wire_disconnect = _is_wire_disconnect(e)` is **False**
   (`WorkstationRejected` is plain `RuntimeError`, not in the
   recoverable kinds).
7. Branch `if not cycle_was_ready and not wire_disconnect:
   raise` matches → re-raises the `WorkstationRejected`.
8. Outer `except BaseException as e:` catches it, sets
   `_supervisor_error = RuntimeError("supervisor loop exited
   unexpectedly: ...")`, calls `_ready.set()` so any awaiter
   unblocks, then re-raises.
9. `finally:` runs `_drain_pending_jobs_and_requests()` and
   `_exited.set()`, then the exception escapes the supervisor
   task.
10. The lifespan task group sees a child task fail with a
    non-cancellation exception → cancels all siblings,
    including the in-flight `connect` tool call.
11. Race: `connect`'s `await wait_ready()` either sees
    `_ready` set first (returns from the await, observes
    `_supervisor_error`, raises `RuntimeError("Workstation
    failed to become ready")`, caught by `except Exception`,
    formatted into the tool error string) **or** sees the
    sibling cancellation first (`CancelledError`, a
    `BaseException` not caught by `except Exception`,
    propagates out of the tool function uncaught).
12. Either way, the task group is now in failed state →
    lifespan exits → MCP dies.

## Root cause

Two design choices interact:

- **Pre-ready non-wire errors propagate out of the supervisor
  via `raise`** (line 868 in `ws_connection.py`). This was
  intended to "surface startup errors to the agent" but the
  mechanism (raise from a task-group child) tears the whole
  group down.
- **The supervisor's state is owned by both the supervisor
  and the tools.** `wait_ready` reaches in to clear
  `_paused` / `_supervisor_error` / swap `_ready` / set
  `_wake` in an "atomic claim" sequence. The supervisor's
  loop top reaches the other direction with the
  `if _ready.is_set(): _ready = anyio.Event()` swap.

A single-line fix ("just park instead of re-raise") would
patch the symptom. But the second issue keeps the design
fragile, so this stage rewrites both.

## Alternatives considered

### Alt A: Just delete the `raise` at line 868

Park pre-ready non-wire errors the same as post-ready. Smallest
diff possible. Keeps the existing `_paused` / `_wake` /
`_supervisor_error` / `_ready` machinery.

Pros: minimal change, low risk.

Cons: leaves the atomic-claim handshake in `wait_ready`
intact. The state machine is still distributed across five
fields and two actors. Maintenance burden persists.

Rejected: per the user's framing, the goal is to simplify the
design, not just fix the symptom.

### Alt B: Single state enum + `state_changed` event, keep exponential backoff

Collapse `_ready`/`_paused`/`_supervisor_error` into one
`SupervisorState` enum with a condition-variable-style
`state_changed` event. Keep the existing
`reconnect_backoff_initial` / `reconnect_max_attempts` config
for wire retries.

Pros: cleans up the state representation.

Cons: still carries two retry mechanisms (auto-backoff for
wire, park-and-wait for non-wire), with different cadences and
different failure semantics. The interaction between
"backoff sleep" and "agent's `wake_and_wait_ready` call" is
unclear — does a tool call interrupt the backoff sleep?

Rejected after discussion: the user proposed dropping
exponential backoff in favor of a single free-retry-budget
model.

### Alt C (chosen): Single state enum + free-retry budget

Per discussion: one auto-retry per ready→drop wire transition,
capped by a small budget that refreshes on every successful
`wake_and_wait_ready`. No backoff sleeps. All other failures
(pre-ready, non-wire, exhausted budget) end up in PAUSED.

Pros: one retry policy, one state machine, clearly bounded.

The supervisor's only background activity is sitting in a
cycle or waiting on `retry_requested`/`shutdown`. No
intermediate "backing off" state.

## Design decisions from discussion

(Quoting answers user gave to the five open questions in the
prior message.)

1. **Free-retry cap with refresh on successful
   `wake_and_wait_ready`.** Decision: `MAX_FREE_RETRY_BUDGET =
   3` (constant, not configurable); refreshes when
   `wake_and_wait_ready` returns with state == READY. The
   refresh is tied to "agent actually got useful work done
   through this connection," which is a meaningful signal.

2. **Leave the job queue alone across pause boundaries.** The
   queue is MCP-level work, not connection state. Tools call
   `wake_and_wait_ready` before submitting; in-flight jobs at
   the moment of a crash get their futures failed by the
   per-handler `BaseException` block. Jobs queued behind drain
   normally against the new cycle.

3. **Rename `wait_ready` → `wake_and_wait_ready`.** More
   descriptive. The "wake" semantics is now first-class in the
   name.

4. **`pause_reason` is a string, not an exception.** The
   supervisor formats one human-readable string when entering
   PAUSED and uses it for both `SupervisorNotReady` (raised
   by `wake_and_wait_ready`) and the channel banner content.
   Avoids passing exception objects through the state machine.

5. **One free retry per ready→drop transition (not per
   connection lifetime).** Per-cycle scoping; if the retry
   succeeds and the next cycle also drops, that's another
   transition and gets its own free-retry attempt — capped by
   the budget.

Plus from the follow-up:

6. **Supervisor starts in PAUSED.** The constructor sets
   initial state to PAUSED with reason "(not started)".
   `start(tg)` spawns the task; the first thing the task does
   is wait on `retry_requested`/`shutdown`. No work happens
   until a tool calls `wake_and_wait_ready`. This also means
   the `WorkstationConnection` can be constructed eagerly in
   `set_working_dir` rather than lazily in
   `_ensure_connected` — no resource overhead.

## Things to check during implementation

- `_AckCommitJob` queued for the old session running against
  the new cycle. Should be harmless (the seq advance is local),
  but worth a test.
- `_EventJob` queued for the old subscription_id arriving at
  the worker after the new cycle's subscribe. The reader
  routes based on `frame.subscription_id ==
  self._active_subscription_id`, so old-sub events are dropped
  at the reader; nothing reaches the worker. But we should
  confirm the reader's drop path doesn't log noisily on every
  blip.
- `force_close_session_one_shot` calls
  `connection.notify_session_externally_closed()` which sets
  `_active_session_id = None`. The supervisor's next cycle
  (after a wake or auto-retry) skips subscribe. This still
  works correctly with the new model — no changes needed
  there.
- `SessionAlreadyActive` / `NoActiveSession` are raised by
  `_do_open_session` / `_do_close_session` per-job handlers
  and routed to the tool's completion future. They never
  reach the supervisor's main loop. Make sure that path is
  unchanged.

## Naming choices

- `SupervisorState` enum members: `PAUSED`, `STARTING`,
  `READY`, `EXITED`. Considered `IDLE` instead of `PAUSED` for
  the initial state, but settled on `PAUSED` because it's the
  same state as "parked after crash" — and the model is
  cleaner if every "supervisor not currently working" state is
  represented identically.
- `wake_and_wait_ready` vs `ensure_ready` vs
  `wait_for_ready`. `wake_and_wait_ready` is verbose but
  captures the two semantics; `ensure_ready` loses the
  "wake-from-paused" connotation; `wait_for_ready` doesn't
  hint that it'll trigger work. Going with
  `wake_and_wait_ready`.
- `SupervisorNotReady` exception name. Considered
  `SupervisorPaused` (matches the state name) but that's
  ambiguous with the EXITED case. `SupervisorNotReady` covers
  both PAUSED and EXITED.
- Channel `kind` attribute: `supervisor_paused`. Was
  `supervisor_crashed` in stage 7 post-1; the rename was
  already overdue (the supervisor doesn't crash, it parks).
