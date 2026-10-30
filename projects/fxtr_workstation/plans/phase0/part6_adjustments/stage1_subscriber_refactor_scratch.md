# Scratch notes — stage1 subscriber refactor

Working notes behind `stage1_subscriber_refactor.md`. Not a spec; the plan
doc supersedes anything here.

## Current-state inventory (what touches EventListenerProto today)

Receive side (the thing being refactored):

- `fxtr_workstation/protocols.py` — `EventListenerProto` (`notify(payload) -> bool`,
  `listening() -> bool`) + `EVENT_LISTENER_TYPE_ID`.
- `fxtr_workstation/callback_impl.py` — `WorkstationCallbackImpl`
  (TypedServiceImpl[EventListenerProto]); `notify` runs the 3-phase
  `_deliver_result_as_event` into the session event log.
- `fxtr_workstation/state.py` — `CallbackRecord` (slot, impl, tracked,
  service_key, service_short_id), `callback_record_worker` (opens impl_scope,
  mounts impl, get_or_create_service_record, parks on `shutdown_event`),
  `CreateCallbackOp` pre-commit spawn, `CloseCallbackOp` + session-close sweep
  flipping `_closed`.
- `client_protocol.py` / `client_app.py` — `GetCallbackServiceRequest` →
  `service_short_id` lookup (used by the MCP today; the frontend will start
  using it too).

Send-side consumers (peers that hold a callback ref):

| Site | Pattern | Notes |
|---|---|---|
| `dynamic_dispatch` basic + nix impls | `subscribe_to_changes(listener)` + per-listener `_listener_worker` (skip-ahead `has_update` event, exit on notify-False/exception, `done`-flag reap) | the two impls are verbatim copies of each other |
| `agent_mcp_impl.py` | `watch_state(listener)` + `_watcher_worker` — same shape plus a per-watcher `latest` value (chain head) and atomic register-then-snapshot return | head returned at subscribe time |
| `ws_connection.py` | `workstation_inbox` bootstrap: `GetCallbackServiceRequest` → `get_service(short_id)` → `replace_service_type(EventListenerProto)`; `AgentMcpRunningSessionImpl.send()` calls `notify` **directly** (not a subscription) | direct-push pattern |
| demos (`notify_n` ×2, `notify_random`, callback-tester) | resolve ref in call scope, push N payloads inline, stop on notify-False | direct-push pattern |
| tests (`test_sessions_5d`, `_fixtures.CallbackInvokerImpl`, `test_basic_dynamic_dispatch_10c._RecordingListenerImpl`, `test_nix_configurable_dispatch` copy, `test_agent_mcp_impl._RecordingListener`, `test_callback_e2e._CallbackTesterImpl`) | mix of direct push + listener impls | |
| sandbox bridge `protocols.py`/`builtins.py` | sandbox-local wire-identical dup of EventListenerProto; `create_callback` returns `ServiceRef[EventListenerProto]` | dup needed because fxtr_workstation isn't in the sandbox dep closure |
| frontend TS (`dynamic_dispatcher.ts`, `agent-plugin-renderer/state_machine.ts`, demo apps) | `allocateCallback()` + `allocateInvoke(svc, "subscribe_to_changes"/"watch_state"/"notify_n", [inbox.handle, ...])` | receive machinery (CallbackEvent / inboxLogs / CloseCallbackOp) is untouched by this refactor |

Two distinct usage shapes fall out of this:

1. **Direct push** — short-lived, invoke-scoped, "here's a payload" (notify_n,
   MCP `send`, callback-tester). Subscription machinery would be pure
   overhead here. Decision (Q1): keep a `notify` method on the inbox, drop the
   bool return.
2. **Durable feed** — dispatcher reload notifications, agent log chain.
   These become EventSource subscriptions owned by the workstation inbox.

## Why @service_method markers are dropped from the user's sketch

`@service_protocol` discovers methods by walking the Protocol's async defs
(`_walk_protocol_methods`); no existing protocol (`EventListenerProto`,
`WorkstationDispatcherProto`, `PersistenceProto`, …) tags methods. The marker
is an impl-side convention (`SimpleAsyncServiceImpl`/`TypedServiceImpl`
dispatch). Generic protocols are fine: `SandboxConnectProto[T, U]` is
precedent; PEP 695 type params land in `__parameters__` and the wrapper
codegen handles substitution.

## Subscribe-flow alternatives considered (Q2)

(a) **Normal invoke via GetCallbackServiceRequest** *(chosen)* — client asks
for the callback's send-side `service_short_id` after the create commits,
then `InvokeOp(slot', inbox_short_id, "subscribe_to", [["service", src]])`.
No new server surface; the subscribe occupies an invoke slot, so error
surfacing/result tracking come for free. Cost: one extra request round-trip
+ a two-tick client flow (allocate → wait ready → subscribe).

(b) **New mutation op** `SubscribeCallbackOp(slot, service_short_id)` —
self-contained session protocol, validated in the pipeline, subscribe runs in
async pre-commit like `CreateCallbackOp`. More new machinery; subscribe
failures entangle whole-mutation rejection semantics.

(c) **Let InvokeOp target a callback slot** — e.g. `InvokeOp.service` may be
`["callback", slot]`; validation resolves slot → `CallbackRecord` against
`pending_creates ∪ open_callbacks` (exactly like the existing arg-side
`["callback", slot]` lookup), the invoke worker reads
`record.service_short_id` after pre-commit. Removes the round trip AND
enables same-mutation create+subscribe. Moderate change (validation, worker
target resolution, TS protocol typing). Deferred — recorded as a TODO in the
plan; (a) is the starting point and (c) is a pure optimization on top.

Client-side ergonomics for (a): the session auto-issues
`GetCallbackServiceRequest` once a callback slot commits and exposes the
result on the inbox stub. From the consumer's perspective the inbox "becomes
invocable when ready", which is the abstraction Daniel wanted, without wire
changes.

## SubscriberHub — do the three sites actually unify? (Q6)

Differences:

| | dispatchers (×2) | agent MCP watchers |
|---|---|---|
| payload | fresh `ReloadEverythingNotification` per send (content-deduped) | per-watcher `latest` chain head |
| wake | `has_update` event, replaced before send (skip-ahead) | same |
| initial delivery | none | current head returned at subscribe time (atomic) |
| idempotency | by listener ServiceKey, no-op | same, returns current head |
| exit | notify-False or exception | same |
| reap | `done` flag, swept on next trigger | same, swept on next update_head |
| scope | isolated mountable child of impl scope, bridge listener from call scope | same |

So: identical except (payload constant vs. latest-value) and (initial
delivery). Both unify under a hub that holds a **latest value** per
subscription and a `publish(value)` broadcast:

- dispatchers: `publish(ReloadEverythingNotification().store(scope))` — the
  value is just "the latest", and since every notification is identical the
  latest-value semantics are exactly the old semantics.
- agent MCP: `publish(new_head)`; `deliver_current=True` at subscribe time
  replaces the atomic snapshot-return — the head arrives as the first event
  instead (Q4 answer). Atomicity argument: the hub appends the subscription
  entry with `latest = current` and pending-delivery set, with no await
  between snapshot and append (same no-await window the current
  `watch_state` relies on); the worker's first loop iteration delivers it.
  A `publish` racing in after the append just overwrites `latest` before the
  worker sends — newer head wins, no miss, no duplicate.

Entity reachability: payload entities (notification / chain head) must be
loadable through the per-subscriber scope. Both sites store them in the hub
owner's scope, and the per-subscriber scope is opened with
`include=[owner_scope]`, so this holds; the hub documents the requirement
("published values' entities must be reachable from the hub's parent scope").

The exit condition changes from notify-False to: (1) unsubscribe invoked,
(2) `on_event` raised (crash backstop, Q5), (3) task-group cancel. The
unsubscribe handle impl is mounted **in the per-subscriber scope**, so it
dies with the subscription — late unsubscribe calls fail with a transport
error, which callers treat as "already gone" (best-effort idempotency).

Idempotent re-subscribe returns the existing subscription's handle ref
(mirrors today's no-op; a dropped-but-unreaped entry doesn't block
re-subscribe, mirroring the current `done`-flag carve-out).

Type id for the hub's handle impl: Daniel's call — the protocols module
pre-declares no ids (ids belong to impls), and `subscription_util.py`
defines its own namespace UUID + `define_type` for the handle impl it
ships. No `FxtrID.builtin` shortcut.

## Workstation close-path mechanics

Subscriptions can't be torn down by parking on `tracked.shutdown_event`: a
peer holding `get_service(cb_short_id)` keeps the tracked record alive past
session close (`test_drained_but_alive_peer_bridge_survives_session_close`),
and `CloseCallbackOp` doesn't necessarily drop the last registration either.
The unsubscribe trigger is the `_closed` flip itself.

Design (Daniel's call — don't rely on open-here/close-elsewhere scopes):
each `subscribe_to` spawns a **per-subscription parker task** on
`state.background_tg` via `await background_tg.start(...)` (mirrors
`callback_record_worker`). The task owns the subscription scope inside a
normal `async with`:

```
impl_scope (callback_record_worker-owned, parks on shutdown_event)
└── subscription_scope (parker-task-owned; isolated+mountable,
    include=[impl_scope])
    ├── bridged event_source ref (from the subscribe_to call scope)
    ├── mounted _InboxSubscriberImpl (on_event → 3-phase delivery)
    └── bridged UnsubscribeHandle ref (returned by source.subscribe)
```

Task body: open scope → bridge source from the `subscribe_to` call scope →
mount subscriber → `source.subscribe(sub_ref)` → bridge the returned handle
in (`bridge_service_from_scope`, before the dispatch call scope drains) →
`task_status.started()` → park on a per-entry `unsubscribe_requested`
event. On wake or cancel: guarded `move_on_after(timeout)` around
`handle.unsubscribe()`, log on timeout/error, exit (scope drains, bridges
drop). Failures before `started()` propagate out of the
`subscribe_to` dispatch to the caller.

Close path stays fully sync: `CloseCallbackOp` commit / session-close sweep
flip `_closed` and set every entry's `unsubscribe_requested` event —
idempotent, no `start_soon` needed; the parker tasks do the async work.

### Forceful teardown (Daniel's call) + the cross-pin problem

Original concern: if the workstation's unsubscribe times out (source alive
but flaky), the source holds a live subscription to a closed inbox; with
the bool return gone and silent-drop semantics, nothing ever reaps it.
Daniel's resolution: after the guarded unsubscribe attempt, the parker
task tears down forcefully — resources clean up immediately, and a source
that delivers again gets a dispatch error automatically (its crash
backstop then reaps). No raise-when-closed logic needed; teardown *is*
the signal. The internal subscriber still silently drops during the brief
`_closed`-flip → teardown window.

Why forceful (not graceful drain): the per-subscription scopes pin each
other across the wire. `bridge_service_from_scope` pins the *source*
scope until the *dest* drains, so:

- source's per-sub scope holds a bridge to our subscriber → pins our
  subscription_scope until the source side drains;
- our subscription_scope holds a bridge to the source's unsubscribe
  handle → pins the source's per-sub scope until we drain.

Graceful mutual teardown only resolves if unsubscribe completes and both
sides unwind in the right order; if either peer never releases, a
graceful drain parks forever. Forceful teardown (the entity-record
`force_close` / cancelled-RPC-connection precedent — "drained-but-alive"
remote refs whose dispatches error) breaks the cycle on both sides. The
hub's worker exit gets the same treatment. Exact mechanics for a
mountable scope with live inbound bridges = implementation-time
verification item (in the plan's risks section).

### Hub delivery modes (Daniel: support stream semantics too)

`mode="coalesce" | "queue"` constructor param, one class. Per-subscription
delivery state is a strategy object: coalesce = latest-value slot
(skip-ahead, the original semantics); queue = FIFO of undelivered values,
drained in order, unbounded v1 (docstring TODO: bounds/backpressure —
drop-oldest vs block vs kill-subscription). `deliver_current` seeds the
slot / queue with the current value under the same no-await window.
Per-hub (not per-subscriber) mode because a source's event semantics are
uniform. No in-repo queue-mode consumer yet — it exists so stream-shaped
sources don't get built on coalescing by accident; implement + test both
modes in chunk 1 so the seam is real.

`_InboxSubscriberImpl.on_event` when `_closed` or session gone: silently
drop (return). The close path is already unsubscribing; raising would just
add noise to the source's backstop path. Direct-push `notify` on the inbox:
same — silent no-op when closed (callers that care probe `listening()`).

## Dispatcher proto shape (Q3)

`WorkstationDispatcherProto` / `NixDispatcherProto` re-declare

```python
async def subscribe(self, subscriber: ServiceRef[Subscriber[Any]]) -> ServiceRef[UnsubscribeHandle]: ...
```

— cannot subclass `EventSource[...]` (service-resolver registry rejects
subtype registration; same reason NixDispatcherProto re-declares today).
Wire dispatch is by method name, so the workstation inbox can
`replace_service_type(EventSource).resolve()` against the same ref and call
`subscribe` — **the method must be wire-named exactly `subscribe`**.
TODO comment at the proto: consider a `change_events` indirection
(`await dispatcher.change_events.subscribe(...)`) once there's a cleaner
property-style story for service-valued attributes; today it would read
`await (await dispatcher.change_events()).resolve(...).subscribe(...)`.

## Agent-side (MCP) inbox invocation gap

Confirmed: an agent today **cannot** invoke its own callback. Sandbox-side
callback refs carry `_OwnedCallbackHandleStub` (slot_key / slot_n only;
`request`/`dispatch` raise), which the wire walker only knows how to emit
as an *arg* tag. `schedule_invoke`'s target must carry a workstation
short_id (`_RemoteServiceHandleStub`) for the mutation builder to fill
`InvokeOp.service`. So `subscribe_to` on the agent's own inbox needs new
plumbing: the mutation builder, on seeing an owned-callback target with
`slot_n` set (cross-script), asks the host to resolve slot → short_id via
`GetCallbackServiceRequest` and emits a normal `InvokeOp`. Same-script
(`slot_key` only) can't work — the slot has no committed record, hence no
short_id at build time — so it raises with a "subscribe in a follow-up
script" message; lifting that is the server-side
InvokeOp-targets-callback-slot TODO.

Sandbox dep closure: fxtr2 **is** importable in the sandbox (per Daniel),
so the bridge imports `Subscriber`/`UnsubscribeHandle`/`EventSource` from
`fxtr2.services.subscription_types`; only `WorkstationSessionEventInbox`
gets a sandbox-local dup (fxtr_workstation stays out of the closure, same
as today's EventListenerProto dup rationale).

## Frontend flow details

New inbox-stub surface (core abstract + workstation concrete):
`CallbackInboxStub.sendService: Atom<ServiceRefStub<WorkstationSessionEventInboxProto> | null>`
(name TBD in implementation). Session behavior: when `reindexCommittedState`
first sees a committed inbox slot, fire `GetCallbackServiceRequest`; on
response, set slot → shortId in a session-level map backing the atom.
Re-attach path: snapshot rebuild re-fires the request for committed inbox
slots (idempotent server-side). The dead-sentinel inbox's atom is hardwired
null.

Dispatcher state machine: today allocate+subscribe happen in one tick. New
flow needs a phase: `reload_inbox` allocated → wait `sendService` non-null →
`allocateInvoke(inboxSvc, "subscribe_to", [dispatchServiceRef])`. The
subscribe-result slot is kept in userState as today
(`reload_subscribe_result`). Missed-reload window between create and
subscribe: same race exists today (subscribe invoke was already async);
dispatcher re-resolves everything on every notification, so no correctness
issue.

Agent panel: `init` allocates inbox → `subscribing` waits sendService +
emits `subscribe_to(agentServiceStub)` → `running`. The old
watch_state-return-head handling is deleted; the head arrives as the first
CallbackEvent (deliver_current). The `subscribing → running` transition no
longer waits for an invoke fill carrying a head — it can move to `running`
immediately after the subscribe invoke is allocated, since entries are
seeded purely from inbox events now.

`notify_n`-style demos: keep direct push; `notify` returns None now, so
`notify_n` returns `n` unconditionally (or becomes `-> None`); integration
tests asserting the count adjust. The "stops early when closed" behavior
disappears with the bool — the demo loop just fires n times (a closed inbox
silently drops).

## Chunking rationale (Q7)

Cut along package/test-suite boundaries so each chunk has a runnable test
story:

1. fxtr2 primitives + hub (v2 tests run green standalone).
2. workstation core (workstation tests green; dispatchers/agent/frontend
   broken until their chunks — acceptable per Daniel).
3. dynamic dispatch (workstation dispatch tests green).
4. agent plugin incl. sandbox bridge (plugin tests green).
5. frontend TS + demo servers + integration tests (everything green).

Frontend last because it integration-tests against chunks 2–4's servers.

## Decisions taken without asking (flagged in plan)

- `notify` on closed inbox: silent no-op; `listening()` retained.
- `subscribe_to` dedups by source ServiceKey (no-op returns; mirrors
  today's listener idempotency).
- Hub idempotency returns the existing handle ref.
- Unsubscribe handles die with their subscription scope; unsubscribe is
  best-effort + timeout everywhere.
