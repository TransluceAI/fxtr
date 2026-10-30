# Stage 7 post-1 — crash-loop + slot-overwrite fixes

Two bugs uncovered by callback testing, plus a recovery tool so the
agent can escape a poisoned session without restarting the MCP. See
`stage7post1_crash_fixes_scratch.md` for the bug trace and the
design alternatives considered.

## Goal

1. **Prevent silent slot-key overwrites.** A second
   `create_callback("foo")` against an existing
   `["callback_open", N]` slot today silently leaks the old slot
   number out of `pending_slot_index`, leaving the workstation
   side's callback orphaned. When the orphaned slot fires, the MCP
   raises `ProtocolError`. Fix: reject any script op that would
   overwrite an existing `data_by_key` entry; force the agent to
   `forget()` first.
2. **Stop the post-crash reconnect spam.** Once the MCP is in a
   state where `subsequent_events` replay reliably crashes the
   worker, the supervisor's outer retry loop reconnects → crashes
   → reconnects → … forever, sending a `supervisor_crashed`
   channel frame each time. Fix: park the supervisor immediately
   on any post-ready non-wire crash. Wire blips retain their
   existing backoff-retry behavior; non-wire crashes (bugs)
   require an explicit agent tool call to wake the supervisor.
3. **Give the agent a way out.** Even with parking, the bad
   `_EventJob` from `subsequent_events` re-enqueues every cycle
   and crashes the worker before any agent job drains, so the
   agent can't `close_session()` to recover. Add
   `close_session(force=True)`: open a fresh out-of-band /client
   websocket, subscribe, immediately send `CloseOp` without
   processing any events, clear the toml. The agent gets clean
   recovery; the supervisor's stale subscription dies of
   `session_gone` on its next operation and naturally returns to
   no-active-session state.

## Non-goals

- Reworking the supervisor's reconnect-backoff model in general.
  The existing exponential-backoff + max-attempts behavior stays
  for pre-ready failures and for normal post-ready wire blips.
- Skip-bad-event semantics. The MCP doesn't attempt to repair a
  crashed cycle by ignoring the problematic event — the agent
  uses force-close to start over instead. (Skipping silently
  would mask future bugs.)
- Closing tracker pin leaks from leaked slots. The
  `SlotKeyAlreadyExists` check stops the bug at the source; old
  data in already-corrupted sessions has to be cleaned via
  `close_session(force=True)` + a fresh `open_session()`.

## Part 1 — `SlotKeyAlreadyExists` check

### Where the check lives

`mutation_builder._apply_script_op` runs each queued op against a
copy of the workstation's pre-state, mutating it in place. After
phase 1 (pending changes) and any prior phase-2 ops, the running
`state["data_by_key"]` reflects exactly what the slot_key map would
look like at op time.

Before each kind that writes to `data_by_key`, we add a guard:

```
if slot_key in state["data_by_key"]:
    raise SlotKeyAlreadyExists(slot_key, kind, running_value)
```

Applies to: `KIND_EXPORT_ENTITY`, `KIND_EXPORT_SERVICE`,
`KIND_SCHEDULE_INVOKE`, `KIND_CREATE_CALLBACK`. Does **not** apply
to `KIND_FORGET` (which removes), `KIND_FORGET_CALLBACK_NOTIFICATION`
(which writes to `callback_notifications`, not `data_by_key`), or
to `_apply_pending_change` (event-driven sets legitimately replace
`invoke_pending` with `invoke_result`).

Because `_apply_script_op` mutates state in-place, a `forget` op
earlier in the same mutation removes the key before subsequent ops
run — the cross-script forget+create case works. (Same-script
forget+create is already blocked by the sandbox's `used_slot_keys`
set, so this stays a multi-script pattern.)

### Error class

New exception in `session_state.py`, sibling of `ForgetMissingKey`:

```
class SlotKeyAlreadyExists(ValueError):
    slot_key: str
    kind: str           # e.g. "create_callback"
    running_value: list # what's currently at data_by_key[slot_key]
```

The mutation builder raises it from `_apply_script_op`. The caller
in `_apply_script_mutation` catches it alongside `ForgetMissingKey`
and returns a tool-error string explaining what happened and how to
fix it:

```
Error: cannot {kind}({slot_key!r}) — slot already holds
{running_value!r}. Call forget({slot_key!r}) in a prior script
first. The mutation was not sent.
```

(Atomic-script semantics: no wire mutation goes out; the agent's
state is unchanged.)

### Test additions

`tests/test_mutation_builder.py`:
- `test_create_callback_rejects_existing_slot_key` — pre-existing
  `["callback_open", N]` value, create_callback raises.
- `test_export_entity_rejects_existing_slot_key` — same for
  exports.
- `test_schedule_invoke_rejects_existing_slot_key` — uniform rule.
- `test_forget_then_create_callback_same_mutation` — forget op
  earlier in same mutation clears the slot, create succeeds.

`tests/test_callback_e2e.py` (or a new test file):
- `test_recreate_callback_after_forget_succeeds` —
  multi-script: create, forget, create same slot_key in a later
  script. Each commit succeeds; no orphaned slot.
- `test_recreate_callback_without_forget_errors` — multi-script:
  create, then try to create same slot_key without forgetting.
  Second script's `run_script` returns the tool-error string;
  the workstation state is unchanged.

## Part 2 — Supervisor park-on-crash-loop

### Wire vs non-wire — the only distinction parking cares about

The supervisor already distinguishes wire issues
(`ConnectionLost` / `httpx_ws.HTTPXWSException`) from non-wire
crashes (`ProtocolError`, `NotImplementedError`, plain bugs) via
`_is_wire_disconnect(e)`. We use this split as the entire
parking gate:

- **Wire disconnects** are benign network blips; the existing
  exponential-backoff retry handles them unchanged.
- **Non-wire post-ready crashes** are bug signals. Reconnecting
  rarely helps because subscribe-time replay reapplies the same
  events, and if the bug *is* transient, surfacing it loudly via
  a forced reconnect is a feature, not a bug. **Park immediately
  on any non-wire post-ready crash.** No retry, no activity
  tracking, no consecutive-crash counting.

Pre-ready non-wire errors keep their existing behavior: they
propagate via the supervisor's outermost `raise`, so the agent's
in-flight `wait_ready()` surfaces them directly. They don't park
because the supervisor never reached an interesting state and
the agent's `connect()` will get a clear error message.

### State to add to `WorkstationConnection`

```
_paused: bool = False
    # True while the supervisor is parked waiting for an external
    # wake signal. Observed by wait_ready (resumes lazily).

_wake: anyio.Event
    # Set by wait_ready when transitioning out of paused. The
    # supervisor's paused branch waits on (shutdown | wake).
    # Re-allocated each time the supervisor un-parks so it's a
    # one-shot per park.
```

(No `_had_tool_activity` — the simpler design doesn't need it.
No flag-setting in `_handle_*_job`.)

### Supervisor loop changes

`_supervisor_loop`'s outer except (after `_run_one_cycle` raises):

```pseudo
cycle_was_ready = self._ready.is_set()
wire_disconnect = _is_wire_disconnect(e)
if not cycle_was_ready and not wire_disconnect:
    raise  # unchanged: pre-ready non-wire error propagates

self._reset_per_cycle_state_on_disconnect()
log + notify supervisor_crashed for non-wire (unchanged)

if cycle_was_ready and not wire_disconnect:
    # Any non-wire post-ready crash → park.
    await self._enter_paused_state(e)
    # Returns when wake or shutdown fires. If shutdown, the outer
    # while-loop exits us. If wake, the next iteration runs a
    # fresh cycle.
    continue

# Wire disconnect (pre- or post-ready), or pre-ready wire failure.
# Existing backoff retry path.
if cycle_was_ready:
    attempt = 0
    backoff = self._reconnect_backoff_initial
attempt += 1
# ... unchanged: max-attempts check, backoff sleep, loop.
```

### `_enter_paused_state` helper

```pseudo
async def _enter_paused_state(self, exc):
    self._paused = True
    # Surface a paused-state error to anyone awaiting wait_ready
    # right now (e.g. a tool call that arrived mid-park).
    self._supervisor_error = SupervisorPaused(
        "Workstation MCP supervisor paused after a non-wire "
        "crash. Call connect() to retry; if the crash repeats, "
        "use close_session(force=True) to recover."
    )
    if not self._ready.is_set():
        self._ready.set()  # wake the awaiters with the error

    # Notify the agent once (best-effort).
    await self._notify_paused(exc)

    # Block until wake or shutdown.
    async with anyio.create_task_group() as tg:
        tg.start_soon(_wait_event, self._shutdown, tg)
        tg.start_soon(_wait_event, self._wake, tg)

    # Reset for the next park (if any).
    self._wake = anyio.Event()
    self._paused = False
    self._supervisor_error = None
    # _ready will be replaced with a fresh unset event by the
    # supervisor loop's top before the next cycle begins.
```

Where `_wait_event(event, tg)` is a tiny helper that awaits the
event and then cancels the task group so the other branch unblocks.

### `wait_ready` change

```pseudo
async def wait_ready(self):
    if self._paused:
        # Wake the supervisor — it'll un-park, swap in a fresh
        # _ready event, run a cycle, and set _ready when that
        # cycle becomes ready. Our await on _ready picks that up.
        self._wake.set()
    await self._ready.wait()
    if self._supervisor_error is not None:
        raise self._supervisor_error
```

Race note: when paused, `_supervisor_error` is non-None and
`_ready` is set, so a naive `wait_ready` would immediately
observe the paused error. By setting `_wake` first, the
supervisor's paused branch un-parks; it clears
`_supervisor_error` and the supervisor loop top swaps in a fresh
`_ready`. The caller's `await self._ready.wait()` then blocks
until the next cycle actually becomes ready.

There's a small race: between `_wake.set()` and the supervisor
clearing `_supervisor_error` / swapping `_ready`, the caller's
`await self._ready.wait()` could resume on the old set state.
We handle this by ordering inside `_enter_paused_state`'s wake
exit:

1. Clear `_paused` and `_supervisor_error`.
2. Return to the supervisor loop top, which swaps `_ready`
   immediately.

`anyio.Event.wait()` re-checks the event state on each resume,
so a caller blocked on the new (unset) event waits correctly.
Coordination is single-task-cooperative (no thread races); the
critical section is purely about ordering Python-level
assignments before any await.

### What if the agent re-triggers the same crash?

After park → wake → cycle becomes ready → bad `_EventJob` →
non-wire crash → park again. The notification fires again on
each park entry. If the agent keeps calling `connect()` against
a poisoned session they'll see one `supervisor_paused`
notification per attempt — acceptable cadence (it's agent-driven,
not auto-spamming). The escape valve is `close_session(force=True)`
from Part 3.

### Test additions

`tests/test_supervisor_park.py` (new file):
- `test_park_on_first_non_wire_post_ready_crash` — rig a
  `_do_event_job` that always raises `ProtocolError`; verify the
  supervisor parks immediately, no auto-reconnect attempt.
- `test_wake_on_tool_call_after_park` — once parked, call any
  tool that goes through `wait_ready`; supervisor wakes and runs
  a fresh cycle.
- `test_wire_disconnect_does_not_park` — close the websocket
  externally (simulating a wire blip); verify the supervisor
  retries with backoff, no park, no `supervisor_paused`
  notification.
- `test_park_notification_fires_each_park` — wake → re-crash →
  park again. Each park entry emits one `supervisor_paused`
  notification; no spam between parks.
- `test_park_clears_session_error_on_resume` — after park, a
  subsequent `wait_ready` call doesn't surface the stale paused
  error once the cycle has resumed and is ready.

## Part 3 — `close_session(force=True)`

### Wire-level behavior

The implementation lives entirely outside the supervisor: a new
helper `force_close_session_via_one_shot_websocket(url, session_id)`
that:

1. Opens a fresh `httpx_ws.aconnect_ws(client_url)` (the same URL
   the supervisor uses).
2. Sends a `SubscribeSessionRequest(session_id=session_id)` and
   awaits the ack + snapshot frames. The snapshot is discarded —
   we don't care about its contents.
3. Sends a `MutateSessionRequest` with `ops=[CloseOp()]`,
   `ack_seq=0`, `patch=[]`, against the subscription_id from step
   2.
4. Awaits the `MutationCommittedResponse` or
   `MutationRejectedResponse`. Either way, the session is closed
   server-side (rejection typically means "session already
   closed", which is also success for our purposes).
5. Closes the websocket.

Error handling: if subscribe fails with `session_gone`, treat as
success (the session is already gone). If the websocket can't
open, surface the raw error.

### Tool surface change

`close_session` grows a `force: bool = False` keyword. Default
behavior unchanged. With `force=True`:

```pseudo
async def close_session(ctx, force=False):
    app_ctx = _app_ctx(ctx)
    cfg = app_ctx.config
    if cfg is None:
        return "Error: working dir not set."

    if force:
        if cfg.active_session is None:
            return "Error: no active_session in toml to force-close."
        session_id = cfg.active_session
        client_url = f"{cfg.workstation_url}/client"
        try:
            await force_close_session_via_one_shot_websocket(
                client_url, session_id
            )
        except Exception as e:
            return f"Error: force_close_session failed: {e}"
        # Clear the toml + tell the supervisor.
        cfg.set_active_session(None)
        if app_ctx.connection is not None:
            app_ctx.connection.notify_session_externally_closed()
        return f"Force-closed session {session_id}. Reconnect to start fresh."

    # ... existing close_session body, unchanged ...
```

### Supervisor-side `notify_session_externally_closed`

The supervisor's `_active_session_id` is set in-memory. After
force-close, the toml is cleared but the supervisor might still
be subscribed to the old session. We need to tell it to stop:

```pseudo
def notify_session_externally_closed(self):
    """Clear _active_session_id locally. If the supervisor is
    mid-cycle with an active subscription, it'll get session_gone
    on its next operation and crash → reconnect → no active
    session. If parked, the next wake-up cycle will see no
    active session and skip subscribe."""
    self._active_session_id = None
```

That's it — the supervisor's natural retry path handles the
cleanup. The `session_gone` error on the old subscription is a
wire-level rejection that drains through `_is_wire_disconnect`
or `WorkstationRejected` paths as appropriate (a `session_gone`
on a session that's been closed mid-flight surfaces as
`ProtocolError`-via-subscribe-fail today; that's a transient
post-force-close artifact, not a real bug).

Race note: between `notify_session_externally_closed` and the
supervisor noticing, the supervisor's worker might queue an op
against the dead session. The op gets rejected; the per-tool
handler surfaces the rejection. Acceptable — force-close is an
explicit "I'm wrecking state to recover" call.

### Test additions

`tests/test_force_close.py` (new file):
- `test_force_close_closes_session_on_workstation` — assert the
  workstation reports the session as closed after.
- `test_force_close_when_supervisor_paused` — drive into paused
  state, call force-close, verify success.
- `test_force_close_when_supervisor_running` — call force-close
  while supervisor is normally subscribed; verify the supervisor
  recovers to no-active-session state.
- `test_force_close_already_closed_session` — server says
  session_gone; tool returns success.
- `test_force_close_no_active_session_in_toml` — tool returns
  the "nothing to close" error path.

## Implementation steps

1. **Part 1 — `SlotKeyAlreadyExists`**
   - [x] Add `SlotKeyAlreadyExists` exception in `session_state.py`.
   - [x] Add the guard to `_apply_script_op` for export_entity,
         export_service, schedule_invoke, create_callback.
   - [x] Removed the dead `apply_ops` helper from
         `session_state.py`. It was a parallel walker duplicating
         `mutation_builder._apply_script_op`, only referenced by
         unit tests. Tests that exercised unique scenarios via
         `apply_ops` were rewritten against
         `build_mutate_request` (the production path).
   - [x] Update `_apply_script_mutation` in `ws_connection.py` to
         catch `SlotKeyAlreadyExists` and return the formatted
         error string.
   - [x] Update `tests/test_session_state_diff.py`:
         the existing `test_apply_ops_export_replace_existing_key`
         and `test_build_mutate_request_replace_when_key_exists`
         now assert the new error behavior. Added
         `test_apply_ops_create_callback_rejects_existing_key`,
         `test_apply_ops_schedule_invoke_rejects_existing_key`,
         `test_build_mutate_request_create_callback_rejects_existing_key`,
         `test_build_mutate_request_forget_then_create_callback_in_same_mutation`.
   - [x] Update CODE_OVERVIEW.md — added a new "Slot-key
         overwrite check (stage 7 post-1)" section near the
         callback-flow content. Done in the final-pass step.

2. **Part 2 — Supervisor park**
   - [x] Add `_paused`, `_wake` fields and a `SupervisorPaused`
         exception class on `WorkstationConnection`.
   - [x] Implement `_enter_paused_state` helper +
         `_wait_for_wake_or_shutdown` helper.
   - [x] Branch in `_supervisor_loop`'s outer except: call
         `_enter_paused_state` when the crash was post-ready and
         non-wire.
   - [x] Update `wait_ready` to atomically claim un-park (reset
         `_paused` / `_supervisor_error` / `_ready`, then signal
         `_wake`). The supervisor's un-park only resets `_wake`;
         the caller does the rest before any await.
   - [x] Add `_maybe_notify_supervisor_paused` (keeps the
         historical `kind="supervisor_crashed"` so existing
         channel-frame consumers don't need to learn a new
         identifier; content describes the park behavior).
         Removed the now-dead `_maybe_notify_supervisor_crash`
         (non-wire crashes go through the park path instead).
   - [x] Updated the existing
         `test_supervisor_recovers_from_non_wire_crash` test
         (renamed to
         `test_supervisor_parks_on_non_wire_crash_and_wakes_on_next_call`)
         to assert the new park-then-wake behavior. Other
         reconnect tests (wire blip cases) still pass unchanged.
   - [ ] Update CODE_OVERVIEW.md — note the new state machine in
         the supervisor / hard-error-boundaries sections.

3. **Part 3 — `close_session(force=True)`**
   - [x] Implement `force_close_session_one_shot` in
         `ws_connection.py` (named slightly shorter than the plan
         draft; semantics match).
   - [x] Add `notify_session_externally_closed` to
         `WorkstationConnection`.
   - [x] Wire `force: bool = False` into the `close_session` tool
         in `server.py`. Split into `_do_normal_close_session`
         and `_do_force_close_session` helpers for readability.
   - [x] Added tests in `tests/test_connect_open_close_session.py`:
         `test_force_close_session_closes_workstation_side`,
         `test_force_close_session_no_active_session`,
         `test_force_close_session_recovers_supervisor_to_clean_state`,
         `test_force_close_session_without_connect`.
         Plus an end-to-end park+force-close integration test in
         `tests/test_reconnect.py::test_park_then_force_close_recovers_to_clean_state`.
   - [x] Update CODE_OVERVIEW.md — tool surface header now
         mentions `close_session(force=True)`; supervisor section
         describes parking + the recovery escape valve; a new
         "Force-close recovery (stage 7 post-1)" section
         documents the one-shot websocket implementation.
         Hard-error-boundaries section updated to reflect that
         non-wire crashes now park (channel-frame `kind` stays
         `supervisor_crashed`; content describes parking).

4. **Update CODE_OVERVIEW.md** — final pass for cross-references
   and the new agent surface. [x] Done.

## Part 4 — Race-safe `wait_for_slot_notification`

Added partway through implementation: the tool's pre-existing
peek-at-call-time check errored when the slot was terminal, which
meant an invoke result that landed while the agent was composing
the tool call surfaced as a misleading error. For callback slots
the same race was even worse — the slot stays `callback_open`
across notifications, so a notification that landed during
compose-window left the slot pending; the tool would park on
`watch_slot` and time out because the watcher only fires on the
*next* event.

### Per-slot-kind behavior

- **`invoke_pending` / `load_pending`**: park on `watch_slot`
  (unchanged). `after_notif_index` is a misuse here → error.
- **`callback_open`**: consult `mirror.callback_notifications[slot_key]`:
  - No `after_notif_index` + non-empty inbox → return
    immediately with a hint pointing at the latest seq.
  - No `after_notif_index` + empty inbox → park.
  - `after_notif_index=K` and K not in inbox → error
    ("after_notif_index must be a previously-delivered seq").
  - `after_notif_index=K` and inbox has seq > K → return
    immediately, hint at latest seq.
  - `after_notif_index=K` is the latest in inbox → park for
    the next callback.
- **Terminal** (`invoke_result` / `invoke_error` / etc.) →
  return success immediately ("slot was already resolved"). If
  `after_notif_index` was passed, hint that it's callback-only
  and was ignored (agent's intent of "wait for resolution" is
  met regardless).
- **Absent slot** → error (unchanged).

### Atomic-claim race window

Between peeking the inbox and registering `watch_slot`, no
awaits run, so no notification can land in between. The
cooperative scheduler is what saves us — same single-task
guarantee that already protected the existing pending-state
peek.

### Implementation steps

- [x] Add `peek_callback_notifications(slot_key)` to
      `WorkstationConnection` (mirrors `peek_slot_key`).
- [x] Restructure `wait_for_slot_notification` in `server.py`
      into a thin tool-window wrapper + per-kind dispatch
      helpers (`_wait_for_slot_notification_inner`,
      `_wait_for_callback_slot`, `_park_on_slot_watch`).
- [x] Add the `after_notif_index: int | None = None` parameter.
- [x] Remove the now-dead `_is_slot_value_pending` helper.
- [x] Update the FastMCP instructions to mention the new
      behavior + the new parameter.
- [x] Update the existing
      `test_wait_for_slot_notification_already_terminal` test
      (was asserting the now-removed "already terminal" error;
      now asserts the success return). Added five more tests:
      terminal + after_notif_index hint;
      callback already in inbox returns immediately;
      after_notif_index returns immediately if newer;
      after_notif_index not in inbox errors;
      after_notif_index on non-callback errors (unit-style,
      since every test service in the repo returns immediately
      and would race the slot into terminal).
- [x] Update CODE_OVERVIEW.md (tool surface + new race-safe
      section).

## Open questions / things to revisit

- **Force-close on an already-closed session.** If the workstation
  rejects the subscribe with `session_gone`, the tool currently
  returns success (the goal is met). If we wanted symmetry with
  normal `close_session` we might surface the difference, but
  practical UX: force-close is an emergency tool, "session is
  gone" == success.
- **Paused-state notification cadence.** With parking we emit
  one `supervisor_paused` notification on entry to paused. If the
  agent calls `connect()` against a still-poisoned session,
  they'll see another one on the re-park. One notification per
  park entry is agent-driven and not spammy; if this turns out
  to feel noisy in practice (e.g. an agent rapidly retries
  connect), we can add per-cause dedupe.
- **What counts as "wire" for parking purposes.** Today
  `_is_wire_disconnect` is `ConnectionLost` and
  `httpx_ws.HTTPXWSException`. Anything else triggers parking —
  including `WorkstationRejected` if it ever escapes a per-tool
  handler. Per-tool handlers already route their expected
  rejections to the completion future, so this shouldn't fire in
  practice, but if a workstation-side issue surfaces as a
  rejection during a non-tool job (e.g. a subscribe-time
  rejection that isn't `session_gone`), we'd park. That's the
  right default: surface the bug rather than mask it.
