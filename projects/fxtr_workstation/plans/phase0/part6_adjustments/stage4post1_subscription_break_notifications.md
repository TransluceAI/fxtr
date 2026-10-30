# Stage 4-post-1 — Callback-inbox subscription-break notifications

## Motivation

A workstation-minted callback inbox
(`WorkstationSessionEventInbox`) can be wired to an upstream
`EventSource` via `subscribe_to`. The workstation owns that
subscription end-to-end: a per-subscription **parker task**
(`_inbox_subscription_parker` in `callback_impl.py`) opens an isolated
subscription scope, bridges the source in (with an
`independent_lease` keep-alive), mounts an internal subscriber,
calls `source.subscribe(...)`, and parks until the inbox closes.

If the upstream source goes away ungracefully — the source's grant is
revoked, or its RPC connection drops — the keep-alive lease backing
the bridge breaks and the inbox simply **stops receiving events**.
Today nothing observes this: the parker stays parked, the dead
`sub_scope` leaks until the inbox closes, and the client that owns the
inbox is never told. The client's only signal would be "events stopped
arriving," which is indistinguishable from "the source has nothing to
say right now."

This stage makes the inbox's **subscription connection state**
observable to the inbox's owning session, so a client can react
("resubscribe me") and debug/UI views can show whether an inbox is
currently connected.

## Relationship to Stage 4 (service-break notifications)

Stage 4 surfaced breaks of **services a session holds** (a service-ref
lease breaking) as an **out-of-band, sequence-less side notification**
(`ServiceBrokenNotification`), re-derived from current state on every
(re)subscribe. That modeling was chosen because service brokenness is
**persistent, terminal state** — a broken service never recovers, and a
late/reconnecting subscriber must always learn it.

This stage is deliberately **different**. A callback-inbox subscription
break is **recoverable**: the client can `subscribe_to` a fresh source
on the same inbox slot and be connected again. The signal is therefore
modeled as a **normal seq'd session event** (a `NonStateSessionEvent`,
like `CallbackEvent`) describing a **state transition** of the inbox's
connection. The client reduces the event stream into a per-inbox
connection state exactly the way it already processes and stores
callback messages.

Consequences of the message-stream choice (all intended):

- Brokenness/connectedness is **derived by the client**, not held as
  workstation-side persistent state — the workstation just emits the
  transition.
- Dedup across reconnects falls out of the **existing per-seq
  notification machinery** (the MCP's `_notified_seqs`, the frontend's
  `lastProcessedSeq`) — no stage-4-style `announced_broken` index or
  re-derive-on-subscribe hook is needed.
- **Retention characteristic:** a transition event that has been acked
  and pruned past the retention horizon will not appear in a **cold**
  attach snapshot (same property callback messages have). A warm
  reconnect (same session object) keeps the reduced state; a client
  that needs the connection state to survive a cold attach preserves
  the latest transition event the same way it preserves callback
  messages. This is accepted for v1.

## Scope

- **Subscriptions on workstation-minted inboxes only.** Direct-push
  `notify` has no subscription and is unaffected.
- **One subscription per inbox** (newly enforced — see Design). This
  makes the inbox's connection state a single well-defined value and is
  why the transition event needs to identify only the inbox **slot**,
  not an individual subscription.
- **Tri-state, recoverable.** Connection state is `idle` (no
  subscription has ever been established) / `connected` (a live
  subscription) / `broken` (the live subscription's source broke).
  `broken → connected` happens on a fresh `subscribe_to`.
- This stage also folds in an unrelated, additive generalization of
  `subscribe_to` (Chunk 4) that lets an inbox subscribe via a method
  other than `EventSource.subscribe`.

## Design overview

### The transition event

A new `NonStateSessionEvent` variant:

```
CallbackSubscriptionEvent(seq: int, slot: int, connected: bool, reason: str | None = None)
```

- `connected=True` — emitted when a subscription is **successfully
  established** (`subscribe_to` brought a subscription fully up).
  `reason` is `None`.
- `connected=False` — emitted when the live subscription **breaks**
  (its source's keep-alive lease fired the broken-callback). `reason`
  carries the lease's break reason string (revoke / RPC-drop
  description) so downstream surfaces can show *why* it broke.

`reason` is metadata hanging off the broken state — the client's
reduction stays a trivial latest-event-wins over `connected`; `reason`
rides along for display.

It carries no entity/service payload, so it appends through the
existing `_append_session_event` primitive with **empty** in-flight ref
sets (`event_refs` stays untouched for its seq). It rides
`event_log` / `event_seq` / retention / preserve exactly like
`CallbackEvent`, and replays through the session snapshot's
`subsequent_events` / `preserved_events`.

The client reduces the per-slot stream to a current state: the latest
`CallbackSubscriptionEvent` for a slot wins; `idle` is the absence of
any such event. (Emitting **both** directions keeps the state purely
event-derived — the client never has to correlate against its own
`subscribe_to` invoke result.)

An **orderly** inbox close (`CloseCallbackOp` / session-close sweep)
does **not** emit a `connected=False` event — that is a different
signal (the slot itself closing), and the existing close path already
covers it. The broken event is strictly for **ungraceful** source loss,
mirroring Stage 4's graceful-vs-ungraceful distinction.

### Single-subscription invariant

`WorkstationInboxImpl.subscribe_to` today dedups a repeat subscribe to
the **same** source key (idempotent no-op) but allows subscribing to
multiple **different** sources concurrently. We tighten this to
**exactly once**:

- **Any** `subscribe_to` while a live subscription exists **raises**
  `RuntimeError` (surfaces to the invoking session as an
  `invoke_error`) — including a repeat for the **same** source. A client
  is expected to subscribe an inbox exactly once; subscribing to the
  same thing twice is not a real use case, so erroring is simpler than a
  silent no-op. To subscribe again the client must `forget`/close the
  inbox slot, or wait for the current subscription to break, first.
- "Live" excludes entries that are `done` **or** `broken` (a broken
  entry is mid-teardown — a resubscribe is allowed immediately).

This drops the source-key dedup entirely: the check is just "is there a
live entry?", with no key comparison. `_InboxSubscription.source_key`
is therefore no longer needed for the check (drop it, or keep it only
for logging). It guarantees at most one live `_InboxSubscription` per
inbox, so the connection state is single-valued and the transition
event needs only the slot.

### Teardown on break

When the source lease breaks, the now-dead subscription must be torn
down (its `sub_scope` freed, its `_subscriptions` entry retired)
rather than left parked until inbox close:

- `_InboxSubscription` gains a `broken: bool` field.
- The broken-callback sets `entry.broken = True`, emits the
  `connected=False` event, then fires `entry.unsubscribe_requested` to
  wake the parker.
- The parker's teardown `finally` **skips** the best-effort
  `handle.unsubscribe()` when `entry.broken` (the source is dead — the
  call would only burn `UNSUBSCRIBE_TIMEOUT_SECONDS` against a corpse),
  then force-closes `sub_scope` and sets `done` as today.

---

## Chunk 1 — Backend (`fxtr_workstation`)

Detect the source-lease break on an inbox subscription, emit the
connection-state transition events, enforce single-subscription, and
tear the dead subscription down.

### Protocol + event

- [x] Add `CallbackSubscriptionEvent(seq, slot, connected: bool, reason:
  str | None = None)` to
  `client_protocol.py` as a `NonStateSessionEvent` (msgspec
  `tag="callback_subscription"`, `tag_field="kind"` — session events tag
  on `kind`, matching `CallbackEvent`; only top-level `ServerFrame`s tag
  on `type`; frozen). Added to the `NonStateSessionEvent` union (so it
  flows through `event_log`, snapshots, preserve, and
  `SessionEventFrame`). Unlike `ServiceBrokenNotification` it is **not**
  a top-level `ServerFrame` — it is a session event.

### Emit helpers (`state.py`)

- [x] A small sync helper `_emit_callback_subscription_event(state,
  session_id, slot, connected, reason=None)`: look up the
  `SessionRecord` (silent
  drop if the session is gone), bump `session.event_seq`, construct the
  event, and `_append_session_event(..., in_flight_e=set(),
  in_flight_s=set())`. Safe to call from an arbitrary task (sync
  registry work + `send_nowait`), consistent with the file's
  GIL-atomicity discipline — same context rules as the `CallbackEvent`
  append path.

### `subscribe_to` rework (`callback_impl.py`)

- [x] Enforce single-subscription: if any live entry exists
  (`not done and not broken`), raise `RuntimeError` — no source-key
  dedup (a repeat subscribe, even to the same source, is an error).
  Drop `_InboxSubscription.source_key` (no longer used for the check;
  keep only if wanted for logging). Optionally prune `done`/`broken`
  entries from `_subscriptions` here to bound list growth.
- [x] On a successful establish (after `background_tg.start(...)`
  returns — the subscription is fully up at that point), call
  `_emit_callback_subscription_event(state, session_id, slot,
  connected=True)`. (`subscribe_to` runs on the invoke worker task; the
  connected event is appended just before the method returns, ahead of
  the subscribe_to invoke's own `InvokeResultEvent`.)

### Break detection + teardown (`callback_impl.py`)

- [x] Add `broken: bool = False` to `_InboxSubscription`.
- [x] In `_inbox_subscription_parker`, after
  `bridge_service_from_scope(..., keepalive_via="independent_lease")`,
  peek the keep-alive lease and register a broken-callback — mirroring
  `service_record_worker`:

  ```
  keepalive = sub_scope._peek_independent_keepalive_lease(event_source.handle)
  if keepalive is not None:
      keepalive.register_broken_callback(
          lambda reason: _on_inbox_subscription_broken(inbox, entry, reason))
  else:
      _log.warning(...)  # break notifications disabled for this subscription
  ```

- [x] `_on_inbox_subscription_broken(inbox, entry, reason)` (sync,
  idempotent): no-op if `entry.broken or entry.done`; else set
  `entry.broken = True`, emit the `connected=False` transition event
  (passing `reason` through) for `inbox._session_id` / `inbox._slot`,
  and `entry.unsubscribe_requested.set()` to wake the parker for
  teardown.
- [x] In the parker's teardown `finally`, skip `handle.unsubscribe()`
  when `entry.broken` (force-close + `done` still run).

The broken-callback needs `state` — thread it through (the inbox
already holds `self._state`), so the lambda can reach
`_emit_callback_subscription_event`.

### Tests

- [x] Register a source `EventSource` from a peer, create an inbox in a
  session, `subscribe_to` it, break the source (drop the peer / revoke)
  → the session subscription receives a `CallbackSubscriptionEvent(slot,
  connected=False)` carrying a non-empty `reason`; the parker tears
  down (subscription scope gone; `entry.done`).
- [x] A successful `subscribe_to` emits `CallbackSubscriptionEvent(slot,
  connected=True)` ahead of the subscribe_to invoke result.
- [x] Resubscribe after break emits a fresh `connected=True`.
- [x] Single-subscription: any second `subscribe_to` while a live
  subscription exists raises (`invoke_error`) — including a repeat of
  the **same** source; resubscribe is allowed once the prior
  subscription is broken/done.
- [x] An **orderly** `CloseCallbackOp` / session close produces **no**
  `connected=False` event.
- [x] Snapshot replay: a session that attaches with un-acked transition
  events in `subsequent_events` reconstructs the latest state; preserved
  transition events survive an ack.
- [x] Update `CODE_OVERVIEW.md` (callback_impl.py + client_protocol.py +
  state.py sections: the new event, single-subscription invariant,
  break detection/teardown).

---

## Chunk 2 — Agent plugin (`fxtr_workstation_agent_plugin`)

Surface the broken transition to the agent as a best-effort "resubscribe
me" channel notification; track connection state in the mirror.

- [x] The `/client` reader / `_apply_state_effect`
  (`ws_connection.py`) learns the `CallbackSubscriptionEvent` kind. It
  **is** a `NonStateSessionEvent` (carries a seq, rides
  `_replayable_events` / retention), so it goes through the normal event
  path — no new top-level frame routing (contrast Stage 4's
  `_ServiceBrokenJob`, which existed only because
  `ServiceBrokenNotification` was a sequence-less side frame).
- [x] Per-kind dispatch: resolve `slot → slot_key` via
  `mirror.pending_slot_index` (callback slots are indexed there). Update
  a lightweight mirror field
  `callback_subscription_states: dict[slot_key, "connected" | "broken"]`
  (latest wins).
- [x] On `connected=False` only, emit an agent-facing channel
  notification (`kind="subscription_broken"`, content naming the
  `slot_key`, the break `reason`, and a hint to resubscribe) via
  `_maybe_notify` → `OutgoingBuffer`. Dedup is **automatic** via the
  existing
  `_notified_seqs` set (the event has a seq), so reconnect/replay does
  not re-spam the agent — no separate by-key dedup like Stage 4's.
  `connected=True` transitions update mirror state **silently** (no
  channel notification — non-actionable noise).
- [x] Defer a typed agent-log event for v1 (mirrors Stage 4 chunk 2's
  deferral of `ServiceBrokenLogEvent`); revisit if the operator frontend
  needs it.

### Tests

- [x] Drive a session that holds a subscribed inbox, break the source
  server-side, assert exactly one `subscription_broken` channel
  notification naming the slot_key and the break reason reaches the
  agent surface.
- [x] A `connected=True` event updates mirror state and emits **no**
  channel notification.
- [x] Replay / reconnect does not re-surface an already-notified break
  (rides `_notified_seqs`).
- [x] Update `CODE_OVERVIEW.md` (mirror field + the new per-kind branch
  in the callback/event-flow section).

---

## Chunk 3 — Frontend UI (`frontend_lib`)

Reduce the transition-event stream into a reactive per-inbox connection
state on the inbox stub.

- [x] `@fxtr-ui/connector-workstation` `protocol.ts`: add the
  `CallbackSubscriptionEvent` shape to the `SessionEvent` union (tag
  `callback_subscription`, fields `seq` / `slot` / `connected` /
  `reason?`).
- [x] `session.ts`: `applyEvent` handles the new kind — update a private
  `inboxSubscriptionStateAtom` (a `Map<slot, {state: "connected" |
  "broken"; reason: string | null}>`, latest-wins per slot), exposed
  read-only. Snapshot rebuild reduces the preserved + subsequent
  transition events per slot (alongside the existing inbox-log rebuild).
  This is a **state reduction**, not a log — no per-slot event
  accumulation like `inboxLogs`.
- [x] `@fxtr-ui/core` `stubs.ts`: add `connectionState: Atom<"idle" |
  "connected" | "broken">` and a sibling `subscriptionBreakReason:
  Atom<string | null>` to the abstract `CallbackInboxStub` surface (a
  future HTTP connector returns constants). Keeping the tri-state a
  plain string (reason in a sibling atom) keeps the common "is it
  broken?" check a plain equality.
- [x] `stubs.ts` (connector): `WorkstationCallbackInboxStub.connectionState`
  reads `session.inboxSubscriptionStateAtom.get(slot)` → `"idle"` when
  absent, else the stored `state`; `subscriptionBreakReason` reads the
  stored `reason` (`null` unless currently broken). The state lives on
  the **session** (callback slots are session-local), not on the
  connector's broken-services set.
- [x] Document the cold-attach retention characteristic in the relevant
  CODE_OVERVIEW section (a long-lived subscription whose `connected`
  event was pruned shows `idle` on a cold attach unless preserved).

### Tests

- [x] A `CallbackSubscriptionEvent(slot, connected=false, reason="...")`
  flips the inbox stub's `connectionState` from `idle`/`connected` to
  `broken` and populates `subscriptionBreakReason`; a subsequent
  `connected=true` flips it back to `connected` and clears the reason
  to `null`.
- [x] Snapshot rebuild reduces preserved + subsequent transition events
  into the correct current state.
- [x] Update `CODE_OVERVIEW.md` (core `CallbackInboxStub` surface,
  connector `session.ts` reducer + `stubs.ts` `connectionState`).

---

## Chunk 4 — Generalize `subscribe_to(method=...)` (additive)

Unrelated quality-of-life change folded in while the subscription path
is open. Let an inbox subscribe to a source whose subscription method
isn't literally `EventSource.subscribe`. **Default behavior is
unchanged.**

### Backend (`fxtr_workstation`)

- [x] `protocols.py`: `WorkstationSessionEventInbox.subscribe_to` gains
  `method: str = "subscribe"`. Update the docstring: the contract is
  "call `method(subscriber_ref)` on the source; it must return an
  `UnsubscribeHandle`-shaped ref (an `unsubscribe()` method)."
- [x] `callback_impl.py`: thread `method` from `subscribe_to` into the
  parker. The parker resolves the source as **`UnknownService`** and
  dispatches the named method dynamically, rather than the typed
  `EventSource` path:

  ```
  source_proxy = await event_source.replace_service_type(UnknownService).resolve(sub_scope)
  handle_ref = await getattr(source_proxy, method)(subscriber_ref)
  ```

  This mirrors how `invoke_worker` already dispatches arbitrary methods
  (`getattr(proxy, method_name)` on an `UnknownService` proxy,
  `state.py`). The strict `EventSource` typing is dropped; the bridge /
  subscriber-mount / unsubscribe-handle-resolve machinery is otherwise
  unchanged.

### Agent plugin (`fxtr_workstation_agent_plugin`)

- [x] Mirror the param on the **sandbox-side** inbox proto
  (`sandbox/.../protocols.py`'s sandbox-local
  `WorkstationSessionEventInbox`): `subscribe_to(event_source,
  method="subscribe")`, so the typed invoke-encode path accepts the
  kwarg and the agent can call
  `inbox.subscribe_to(source, method="watch")`. The untyped path passes
  kwargs through unchanged.

### Frontend (`frontend_lib`)

- [x] No structural change. Callers pass `method` as an optional invoke
  kwarg through the existing loose-typed handle-target invoke
  (`ctx.allocateInvoke(inbox.handle, "subscribe_to", [source], {method:
  "..."})`).

### Tests

- [x] Backend: a source exposing a subscription method named something
  other than `subscribe` can be subscribed via
  `subscribe_to(source, method="<name>")`; events flow into the inbox;
  break/teardown still works (the broken-callback path is method-
  agnostic).
- [x] Default (`method="subscribe"`) path is byte-for-byte unchanged
  (existing subscription tests still pass).
- [x] Update `CODE_OVERVIEW.md` (protocols.py `WorkstationSessionEventInbox`
  + sandbox-bridge proto note).

---

## Testing strategy

Each chunk is independently testable, mirroring Stage 4:

- **Chunk 1** against the workstation server directly (peer registers an
  `EventSource` over `/rpc`, a `/client` session subscribes an inbox to
  it, break the peer, assert the transition events + teardown +
  single-subscription rejection). Extends the existing callback test
  suites.
- **Chunk 2** against the MCP's in-memory harness
  (`test_mcp_in_memory`): break a subscribed inbox, assert one
  `subscription_broken` channel notification and the seq-based
  no-re-spam-on-replay behavior.
- **Chunk 3** as connector/session unit tests (mock WebSocket frames)
  for the per-slot connection-state reduction and `stub.connectionState`,
  plus a snapshot-rebuild test — mirroring the existing
  `callback_reducer` test shape.
- **Chunk 4** as a backend subscription test with a non-`subscribe`
  method name, plus a regression pass over the default path.

## Open implementation details to confirm

- Exact accessor for the subscription bridge's standing keep-alive lease
  on `sub_scope` (reuse `_peek_independent_keepalive_lease`, as in
  `service_record_worker`).
- Whether `register_broken_callback` reliably fires for the inbox
  subscription's source on both RPC-connection drop **and** explicit
  revoke (verify against the RPC lowering layer's break propagation —
  same question Stage 4 raised, expected to hold since the keep-alive
  bridge shape is identical).
- Where exactly to emit the `connected=True` event — from `subscribe_to`
  after `background_tg.start(...)` returns (chosen here, on the dispatch
  path) vs. from the parker just after `task_status.started()`. Both
  observe "fully established"; the former keeps the session lookup off
  the parker task.
- Whether `_subscriptions` should actively prune `done`/`broken` entries
  on each `subscribe_to` (bounded growth) or leave them (simpler;
  entries are tiny and an inbox is short-lived).
