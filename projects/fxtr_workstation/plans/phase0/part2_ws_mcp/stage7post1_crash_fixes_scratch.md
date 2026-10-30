# Stage 7 post-1 — scratch notes

Working notes for `stage7post1_crash_fixes.md`. Not part of the spec —
these are the design rationales, alternative considered, and the bug
trace that motivated the work.

## The bug trace

The triggering scenario (from a real agent session):

1. Script 1: `create_callback("my_callback_slot")` — slot 1, never used,
   never closed. `data_by_key["my_callback_slot"] = ["callback_open", 1]`.
2. Script 2: `create_callback("demo_callback")` — slot 3. Registered
   with the `callback-tester` service, but then `fire_notify` hit a
   race condition and failed.
3. Script 3: `create_callback("demo_callback")` — slot 6. Workstation
   accepts the CreateCallbackOp at slot 6; nothing wrong server-side.
   But the MCP's mutation builder's `_emit_set` writes
   `data_by_key["demo_callback"] = ["callback_open", 6]` as a `replace`
   (because the key already exists). Slot 3 is now orphaned —
   `pending_slot_index` only has 6 → "demo_callback" after the next
   `apply_workstation_view` rebuild.
4. Script 3 also registers slot 6's callback with the service and
   fires `fire_notify`.
5. Service emits a CallbackEvent. But — based on the workstation debug
   UI — the service's registration list still has slot 3 (from script
   2's `register_callback(cb)` call). The event lands at slot=3.
6. `_apply_state_effect`'s callback branch tries
   `pending_slot_index.get(3)` → `None` → `ProtocolError`.
7. Supervisor crashes. Outer retry loop reconnects. `_do_subscribe`
   re-enqueues `subsequent_events` (which still includes the bad
   CallbackEvent). Worker drains it. Same ProtocolError. Loop.

Two distinct bugs:

- **Silent overwrite** of `data_by_key[slot_key]` lets an agent leak
  the old slot's `pending_slot_index` entry.
- **Crash loop** spams notifications and starves agent tool jobs out
  of the worker queue (`_EventJob`s from subsequent_events always
  drain first).

## Why slot-overwrite detection belongs in the mutation builder

Three places we could enforce "no silent overwrite":

1. **Sandbox builtins** — `create_callback`, `export_entity`, etc.
   Would need to check `data_by_key` on every call via an RPC to the
   host. Adds a round-trip per builtin. The sandbox already has
   `used_slot_keys` for in-script collisions but no view of the host
   mirror.
2. **Host RPC** — `McpHostImpl.copy_entity_in` is called by the
   sandbox; we could surface a "slot already exists" check here.
   But not all writes go through copy_entity_in (create_callback
   doesn't), so we'd still miss cases.
3. **Mutation builder** — `_apply_script_op` walks queued ops against
   a running state. The check is one `if slot_key in state["data_by_key"]`
   guard. Catches every kind that writes to data_by_key.

(3) is the cleanest. Errors are surfaced via the same `ForgetMissingKey`-
style early-return path in `_apply_script_mutation` — the mutation
isn't sent, the script's queued ops are dropped, the agent sees a
clear error.

Pending changes (`_apply_pending_change`) don't get the check.
Invoke-result promotion legitimately replaces `["invoke_pending", N]`
with `["invoke_result", payload]`; CallbackEvent appends to
`callback_notifications`, not `data_by_key`.

## Why the crash loop happens and what fixes it

The supervisor's outer except in `_supervisor_loop`:

```python
if cycle_was_ready:
    # The cycle had become ready before the failure — count this
    # as a transient blip and reset the consecutive-failure counter.
    attempt = 0
    backoff = self._reconnect_backoff_initial
attempt += 1
```

The attempt counter only protects against pre-ready crashes. Post-ready
crashes always reset the counter, so a crash-loop pattern keeps the
counter at 1 forever.

The fix: track whether the agent has done *anything* tool-shaped
between cycle becoming ready and crashing. If a cycle crashes without
agent activity AND the previous crash also had no agent activity, park.

State machine:

```
                  cycle becomes ready
                  ↓
    [running] ─────────────────┐
       │                       │ tool-call dispatched
       │ crash                 │ → _had_tool_activity = True
       ↓                       │
   _had_tool_activity?         │
       │                       │
    ┌──┴──┐                    │
    True False                 │
    │     │                    │
    │     │ park               │
    │     ↓                    │
    │  [paused]                │
    │     │                    │
    │     │ wake (agent call)  │
    │     ↓                    │
    │  reset _had_tool_activity = True
    │     │                    │
    └─────┴────────────────────┘
   reset to False; reconnect with backoff
```

Initial state: `_had_tool_activity = True` so the very first
post-ready crash gets a "free retry". This matches the existing
behavior for transient wire blips during normal operation.

On resume from paused: `_had_tool_activity = True` so the resumed
cycle also gets one retry budget. Without this, every wake would
immediately re-park on the very next crash. With it, the agent gets
"one shot per engagement" — if the cycle stabilizes and they get to
do work, great; if not, back to paused.

## Why we need a force-close recovery path

Even with parking, the agent's tool jobs can't be drained: the bad
`_EventJob` from `subsequent_events` always queues first and crashes
the worker before any agent job runs.

Options considered for letting the agent escape:

1. **Skip subsequent_events on subscribe.** Too drastic — we'd
   silently lose events in non-recovery scenarios.
2. **A `recover()` tool that clears `_active_session_id` locally
   without telling the workstation.** Leaks the workstation-side
   session until timeout; ugly but easy.
3. **`close_session(force=True)`** — user's suggestion. Open a
   one-shot subscribe-then-close-then-drop, then clear the toml
   locally. Cleanly disposes of the workstation-side session.

(3) is the best UX. The implementation has one wrinkle: the
supervisor's existing `/client` connection might be open with the
old subscription, in which case we have two ways to send the
CloseOp:

- **Through the supervisor's existing connection** — requires the
  supervisor to allocate a fresh subscription_id and bypass the
  `_do_subscribe` event-enqueue. Means a new `_ForceCloseSessionJob`
  worker kind + a `_force_close_pending` flag that `_do_subscribe`
  reads to skip the subsequent_events enqueue.
- **Through a fresh out-of-band /client websocket** — completely
  bypass the supervisor's state machine; just open a websocket,
  subscribe, send CloseOp, close. The supervisor's existing
  subscription gets `session_gone` on its next op and crashes →
  reconnects → no active session → clean.

I lean toward the **out-of-band** approach because:

- Works regardless of supervisor state (parked, crashing, normal).
- No new job kind / state machine bolt-on.
- The supervisor cleanup happens naturally via its existing retry
  path once the session is gone server-side.

The cost: ~20 lines of subscribe-then-close-then-drop, duplicating
the wire setup from `_open_workstation_rpc` style helpers. Worth it
for the isolation.

One caveat: while force-close is running and the supervisor's
subscription is still alive, racing CloseOp's could land in either
order. The workstation will accept whichever lands first; the second
gets a `MutationRejectedResponse` (session already closed). The
out-of-band path treats that as success since the goal — close the
session — is met.

## Decisions made (in conversation with Daniel)

- Any tool call wakes the supervisor (matches existing
  `_ensure_connected` pattern, not "only `connect()`").
- First crash after initial connect: free retry. Second consecutive
  crash without activity: park.
- Slot overwrite check rejects every kind that writes to
  `data_by_key` (export_entity, export_service, schedule_invoke,
  create_callback). Even repeated `schedule_invoke` under the same
  slot_key must explicitly forget first — uniform rule beats per-kind
  special cases.
- Force-close opens a fresh out-of-band /client connection rather
  than going through the supervisor's worker queue.
