# Stage 1 — Subscriber / EventSource refactor

Replace the polling-style `EventListenerProto` callback protocol with an
explicit subscribe/unsubscribe protocol family, defined as general fxtr2
primitives, and rework the workstation callback system (and every consumer
in the repo) on top of it.

Detailed reasoning / alternatives in
`stage1_subscriber_refactor_scratch.md`.

## Motivation

Today a peer that holds a callback ref only learns the listener is gone
when a `notify` returns `False` or errors — there is no release signal, so
sources hold dead listeners until the next broadcast, and durable
subscriptions can't be torn down promptly. The new design:

- **Explicit subscribe/unsubscribe** as fxtr2-wide primitives
  (`Subscriber` / `UnsubscribeHandle` / `EventSource`). Anyone subscribing
  for a duration is required to unsubscribe (e.g. in a `finally`); the
  unsubscribe doubles as the release signal so the source can drop its
  handle on the subscriber and clean up gracefully.
- **The workstation callback becomes one level removed**: a
  `WorkstationSessionEventInbox` that *takes an event source* and owns the
  subscription's lifecycle. The workstation unsubscribes when the inbox
  closes (close-callback op or session close) — session clients never
  manage unsubscribes themselves.
- Event **receipt** is unchanged: payloads still land as `CallbackEvent`s
  in the session event log; the frontend inbox machinery
  (`CallbackEvent` / `inboxLogs` / `CloseCallbackOp`) is untouched.

Decisions confirmed up front:

- **Direct push survives.** The inbox keeps a `notify(payload)` method
  (bool return dropped) for invoke-scoped pushes (`notify_n` demos, the
  MCP's `send` → `workstation_inbox` path). Durable feeds use
  `subscribe_to`. `listening()` stays as the probe; `notify` on a closed
  inbox is a silent no-op.
- **Session clients trigger `subscribe_to` via an invoke targeting the
  callback slot directly** (`CallbackInvokeOp`, added in chunk 3.5 —
  resolved like the arg-side `["callback", slot]` tag). No
  `GetCallbackServiceRequest` round trip, and create+subscribe can ride
  one mutation. (`GetCallbackServiceRequest` itself survives for the
  agent MCP's `/rpc`-side `workstation_inbox` bootstrap.)
  *(Superseded an earlier decision to discover the send-side short_id
  via `GetCallbackServiceRequest` and invoke that; chunk 3.5 was scoped
  mid-stage once the server-side cost turned out to be small.)*
- **Crash backstop stays reactive**: a source also drops a subscription
  when `on_event` raises (peer died without unsubscribing); the
  workstation's unsubscribe-on-close is best-effort with a timeout.
- A shared `SubscriberHub` helper in fxtr2 replaces the three verbatim
  copies of the per-listener worker machinery (basic dispatch, nix
  dispatch, agent MCP watchers).

## New primitives (`v2/fxtr2/services/`)

### `subscription_types.py`

Three `@service_protocol` Protocols (no `@service_method` markers —
protocol methods are discovered by walking async defs, matching every
existing protocol):

```python
@service_protocol
class Subscriber[T: FxtrServiceData](Protocol):
    async def on_event(self, payload: T) -> None: ...

@service_protocol
class UnsubscribeHandle(Protocol):
    async def unsubscribe(self) -> None: ...

@service_protocol
class EventSource[T: FxtrServiceData](Protocol):
    async def subscribe(self, subscriber: ServiceRef[Subscriber[T]]) -> ServiceRef[UnsubscribeHandle]: ...
```

Contract (docstrings): subscribing for a duration obligates the subscriber
side to call `unsubscribe` when done; `unsubscribe` is also the release
signal letting the source drop its bridge to the subscriber. Sources should
additionally drop a subscription whose `on_event` raises (backstop for
peers that vanish). `on_event` has no return value — there is no in-band
"stop sending" signal anymore.

Type-id policy: the protocols module pre-declares **no** type ids — type
identity belongs to impls, which define their own id (and namespace)
reflecting what they actually support. (`subscription_util.py` below does
exactly that for the impls it ships.)

(The `change_events`-indirection TODO — service-valued attribute sugar
so protos don't have to re-declare `subscribe` inline — concerns the
protos doing the re-declaring, not `EventSource` itself, which would
just be the attribute's type. It's recorded at the dispatcher protos
in chunk 3, not in this module.)

### `subscription_util.py` — `SubscriberHub[T]`

Reusable broadcast helper for EventSource implementors; replaces the
copy-pasted listener-worker pattern. Surface (pseudocode):

```python
hub = SubscriberHub[T](scope=owner_scope, task_group=impl_tg,
                       mode="coalesce")  # or "queue"
hub.publish(value)                       # sync; record + wake workers
handle_ref = await hub.subscribe(subscriber_ref, source_scope,
                                 deliver_current=False)
```

- **Two delivery modes**, chosen per hub (a source's semantics are
  uniform across its subscribers):
  - `"coalesce"` — latest-value, skip-ahead: multiple publishes during one
    in-flight `on_event` collapse to one delivery of the newest value.
    Right for idempotent / self-superseding payloads (reload
    notifications, chain heads).
  - `"queue"` — every-event: each subscription holds a FIFO of undelivered
    values; the worker drains it in order. Right for stream semantics
    where every event matters. Unbounded in v1 — TODO in the docstring for
    bounds / backpressure policy (drop-oldest vs. block vs. kill).

  The per-subscription delivery state is a small strategy object so the
  worker loop is mode-agnostic.
- Subscriptions are **not deduplicated** (decision revised during
  chunk-1 review): every `subscribe` call creates an independent
  subscription with its own handle, so a duplicate subscriber just
  receives duplicates. The contract is simply "delivered to until its
  handle is unsubscribed".
- Per subscription, a worker task (spawned via `task_group.start(...)` so
  the bridge is up before `subscribe` returns) owns an isolated mountable
  scope (`include=[owner_scope]`), bridges the subscriber in, mounts a
  small `UnsubscribeHandle` impl **in that same scope** (the handle dies
  with the subscription; late unsubscribes surface as transport errors and
  callers treat unsubscribe as best-effort), and loops: wait for wake →
  replace the wake event → take the next delivery from the mode strategy →
  `on_event(value)`.
- Exit paths: `unsubscribe` invoked (sets a per-entry event), `on_event`
  raised (log + drop), task-group cancel. `done` entries are reaped on the
  next `publish`. **Exit tears the subscription scope down forcefully**
  (cancellation-style, not a graceful drain — see "forceful teardown"
  under the workstation section; the same cross-pin argument applies on
  the hub side, where the subscriber's scope is pinned by the peer's
  bridge to the unsubscribe handle).
- `deliver_current=True` covers the agent-MCP "current head as first
  event" semantics: the entry is appended with the current value pending
  delivery (queued / set-as-latest per mode), with no await between
  snapshot and append (same atomicity window the old `watch_state`
  relied on).
- Published values' entities must be reachable from `owner_scope`
  (documented requirement; all three call sites already satisfy it).
- The module defines its own stable namespace UUID plus a
  `define_type(...)` id for the unsubscribe-handle impl it ships (e.g.
  `SUBSCRIPTION_UTIL_NS` + `hub_unsubscribe_handle`) — impls own their type
  identity; the protocols module stays id-free.

Differences absorbed from the three current sites (basic dispatch, nix
dispatch, agent MCP watchers — all `mode="coalesce"`): dispatchers publish
a (content-deduped) `ReloadEverythingNotification` ref each time; the
agent MCP publishes the latest chain head and needs `deliver_current`.
Everything else (skip-ahead wake, ServiceKey idempotency, scope/bridge
layout, reap) was already identical. See the scratch doc for the
comparison table. `"queue"` mode has no in-repo consumer yet; it exists so
stream-semantics sources don't get built on coalescing by accident.

## Workstation core changes (`fxtr_workstation`)

### `protocols.py`

`EventListenerProto` + `EVENT_LISTENER_TYPE_ID` are **deleted** (no
back-compat shim) and replaced by:

```python
@service_protocol
class WorkstationSessionEventInbox[T: FxtrServiceData](Protocol):
    async def subscribe_to(self, event_source: ServiceRef[EventSource[T]]) -> None: ...
    async def notify(self, payload: T) -> None: ...     # direct push; silent no-op when closed
    async def listening(self) -> bool: ...
```

New type ids under `WORKSTATION_NS`: `session_event_inbox` (the inbox
service) and `session_inbox_subscriber` (the internal subscriber impl).

Deliberate invariant (docstring it): the inbox's push method is named
`notify`, **not** `on_event` — an inbox ref is structurally *not* a
`Subscriber`, so it can never be handed straight to
`EventSource.subscribe`. Every durable subscription is forced through
`subscribe_to`, where the workstation owns the lifecycle; if you have an
`EventSource`, use `subscribe_to`.

### `callback_impl.py`

`WorkstationCallbackImpl` → `WorkstationInboxImpl`
(`TypedServiceImpl[WorkstationSessionEventInbox]`):

- `notify`: unchanged 3-phase `_deliver_result_as_event` delivery, but
  returns `None`; closed / session-gone → silent drop.
- `subscribe_to(event_source)`: dedup by the source's `ServiceKey`
  (idempotent no-op). Otherwise it spawns a **per-subscription parker
  task** on `state.background_tg` (via `await background_tg.start(...)`,
  mirroring `callback_record_worker`) and records the entry on the impl.
  The parker task owns the subscription end-to-end:

  1. Open an isolated mountable subscription scope
     (`include=[impl_scope]`).
  2. Bridge the source ref in from the `subscribe_to` call scope; mount an
     internal `_InboxSubscriberImpl` (whose `on_event` runs the same
     3-phase delivery; silent drop when closed); call
     `source.subscribe(subscriber_ref)` (re-typing the ref to
     `EventSource`); bridge the returned `UnsubscribeHandle` ref into the
     subscription scope.
  3. Signal `task_status.started()` — `subscribe_to`'s dispatch returns
     only once the subscription is fully established (failures before this
     point propagate to the caller).
  4. Park on a per-entry `unsubscribe_requested` event.
  5. On wake (or cancel): `move_on_after(timeout)` around
     `handle.unsubscribe()` (mirroring `expose_service`'s guarded
     release), log on timeout/error, then **tear the subscription scope
     down forcefully** (cancellation-style, not a graceful drain).

  **Forceful teardown is load-bearing twice over.** (a) It's the error
  signal: once the scope is gone the mounted subscriber is gone, so a
  source that missed the unsubscribe (timeout, flaky peer) gets a dispatch
  error on its next `on_event` and reaps via the crash backstop — no
  zombie subscriptions delivering into the void. (b) It avoids a
  cross-pin deadlock: the two sides' per-subscription scopes pin each
  other (the source's scope holds a bridge to our subscriber; our scope
  holds a bridge to its unsubscribe handle), so a graceful drain can block
  forever on a peer that never releases. This mirrors the existing
  cancelled-RPC-connection force-teardown / drained-but-alive pattern.

  This also sidesteps any open-here/close-elsewhere scope questions: the
  scope lives entirely inside one task.

### `state.py`

- `CloseCallbackOp` commit and the session-close sweep (both sync) flip
  `_closed` as today **and** fire each subscription entry's
  `unsubscribe_requested` event (sync, idempotent — the parker tasks do
  the async unsubscribe work). Parking on `tracked.shutdown_event` is
  *not* sufficient — a drained-but-alive peer bridge keeps the tracked
  record open past session close; the `_closed` flip is the real trigger.
- `CallbackRecord` / `callback_record_worker` otherwise unchanged
  (subscription bookkeeping lives on the impl).

### `client_protocol.py` / `client_app.py`

Chunk 2 itself changes nothing here (`GetCallbackServiceRequest`
already exposes the inbox's send-side short_id); chunk 3.5 then adds
`CallbackInvokeOp` — see its section below.

## Dynamic dispatch changes

- `dynamic_dispatch_types.py`: on both `WorkstationDispatcherProto` and
  `NixDispatcherProto`, replace `subscribe_to_changes(listener)` with a
  re-declared

  ```python
  async def subscribe(self, subscriber: ServiceRef[Subscriber[Any]]) -> ServiceRef[UnsubscribeHandle]: ...
  ```

  (Cannot subclass `EventSource` — the service-resolver registry rejects
  subtype registration, same reason `NixDispatcherProto` re-declares
  today. Wire dispatch is by method name, so the method **must** be named
  exactly `subscribe` for `EventSource`-typed refs to reach it.) Add the
  `change_events`-indirection TODO comment here.
- Both impls (`basic_dynamic_dispatch`, `nix_configurable_dispatch`):
  delete `_ListenerState` / `_listener_worker` / the reap logic; hold a
  `SubscriberHub` (constructed with the impl's scope + `impl_tg`);
  `subscribe` delegates to `hub.subscribe(...)`; `trigger_reload()` becomes
  `hub.publish(ReloadEverythingNotification().store(self._scope))`.

## Agent MCP plugin changes

- `agent_mcp_proto.py`: delete `watch_state`. `AgentMcpRunningSessionProto`
  gains the same re-declared `subscribe(subscriber) -> handle`; the impl
  delivers the **current chain head as the first event** on subscribe
  (`deliver_current=True`), so the old atomic snapshot-return is subsumed.
  `get_state` stays for one-shot reads; `send` keeps its shape.
- `agent_mcp_impl.py`: delete `_WatcherState` / `_watcher_worker`; hold a
  `SubscriberHub` on `impl_scope` / `impl_tg`; `update_head` →
  `hub.publish(new_head)`.
- `ws_connection.py`: the bootstrap re-types the inbox ref to
  `WorkstationSessionEventInbox`; `send()` drops the bool handling around
  `notify`.
- Sandbox bridge (`fxtr_ws_mcp_sandbox_bridge`): fxtr2 **is** importable
  sandbox-side, so `Subscriber` / `UnsubscribeHandle` / `EventSource` are
  imported from `fxtr2.services.subscription_types` directly; only
  `WorkstationSessionEventInbox` needs a sandbox-local wire-identical dup
  (replacing the `EventListenerProto` dup — `fxtr_workstation` stays out
  of the sandbox dep closure). `create_callback` / `retrieve_result`'s
  callback default re-type to the inbox proto; `builtins` re-exports +
  `preamble.py` update.
- **Agent-side inbox invocation** (new capability): allow
  `schedule_invoke`'s *target* to be an owned-callback stub. Today
  `_OwnedCallbackHandleStub` refs can only ride as args; with chunk
  3.5's `CallbackInvokeOp` the mutation builder emits a callback-slot-
  targeted invoke directly for **both** stub forms — same-script
  (`slot_key` only: the builder allocates the create's slot number in
  the same mutation, so create+subscribe rides one script) and
  cross-script (`slot_n` set: the committed slot number goes straight
  into `callback_slot`). No host-side `GetCallbackServiceRequest`
  resolution, no "subscribe in a follow-up script" error case.

## Frontend (TS) changes

*(Rewritten after chunk 3.5: `CallbackInvokeOp` makes the inbox
invocable by slot, so the previously-planned inbox-service discovery —
auto-issued `GetCallbackServiceRequest`, a `sendService` atom on the
inbox stub, re-fetch-on-reattach wiring, and the two-phase
"wait until invocable" flows — is all unnecessary and is NOT built.)*

- `@fxtr-ui/connector-workstation`: `allocateInvoke` accepts a
  `CallbackHandleStub` as the *target* (in addition to
  `ServiceRefStub`); the flush walk emits a `CallbackInvokeOp` for
  those, resolving a same-tick pending callback slot the same way the
  arg-side `["callback", slot]` encoding already does. Cross-session
  handle smuggling rejected at allocate time (same rule as args).
- `dynamic_dispatcher.ts`: allocate inbox + subscribe in the **same
  tick** — `allocateCallback()` then
  `allocateInvoke(inbox.handle, "subscribe_to", [dispatchServiceRef])`.
  Teardown is unchanged (removing the inbox from state →
  `CloseCallbackOp` → workstation unsubscribes server-side).
- `agent-plugin-renderer/state_machine.ts`: same pattern against the
  agent service; delete the watch_state-result-carries-head handling —
  the head arrives as the first inbox event, so the state machine no
  longer waits on an invoke fill to seed entries.
- Demos / integration tests: `notify_n` & friends stay direct-push but lose
  the bool (return `n` / `None`); update the Python demo services and the
  TS assertions accordingly.

## Implementation chunks

Each chunk should be independently reviewable and leave its own test suite
green; cross-package consumers of not-yet-migrated chunks may be broken in
between, but everything is ported by the end of the stage.

### Chunk 1 — fxtr2 primitives + SubscriberHub

- [x] `v2/fxtr2/services/subscription_types.py`: the three protocols +
      contract docstrings (no type ids here; the `change_events`
      TODO lives at the dispatcher protos in chunk 3 instead).
- [x] `v2/fxtr2/services/subscription_util.py`: `SubscriberHub[T]` (both
      delivery modes behind a per-subscription strategy object) +
      per-subscription worker + internal `UnsubscribeHandle` impl, with the
      module's own namespace UUID + `define_type` id for the handle impl;
      queue-bounds/backpressure TODO in the docstring.
- [x] v2 tests: protocol round-trip over RPC-ish scopes (subscribe,
      events delivered, unsubscribe releases, `deliver_current` in both
      modes, coalesce-mode skip-ahead collapse, queue-mode in-order
      every-event delivery, idempotent re-subscribe, `on_event`-raise
      backstop, post-teardown dispatch errors cleanly, cancel teardown).
      (`tests/test_services/test_subscription_util.py`; also covers the
      "handle still works after the subscribe dispatch ends" risk item
      and entity-payload reachability through the per-subscription
      scope.)
- [x] Update `v2/fxtr2/services/LOCAL_CODE_OVERVIEW.md` and
      `v2/CODE_OVERVIEW.md`.

### Chunk 2 — workstation core

- [x] `protocols.py`: add `WorkstationSessionEventInbox` + new type ids;
      delete `EventListenerProto` / `EVENT_LISTENER_TYPE_ID`.
- [x] `callback_impl.py`: `WorkstationInboxImpl` (`notify` sans bool,
      `subscribe_to`, `_InboxSubscriberImpl`, per-subscription parker
      tasks on `background_tg`).
- [x] `state.py`: close paths fire the per-subscription
      `unsubscribe_requested` events (via `impl.mark_closed()`); TODO
      comment near `InvokeOp` (in `client_protocol.py`) re
      callback-slot invoke targets.
- [x] Update `tests/_fixtures.py` (`CallbackInvokerImpl`) and
      `tests/test_sessions_5d.py`; add coverage: `subscribe_to` against a
      test `EventSource`, events land as `CallbackEvent`s,
      close-callback / session-close trigger unsubscribe on the source,
      direct-push `notify` no-op after close. (Also: per-source dedup
      no-op, and subscribe-failure → `invoke_error` + not retained —
      the chunk-2 risk item.)
- [x] Update `projects/fxtr_workstation/CODE_OVERVIEW.md`.

### Chunk 3 — dynamic dispatch

- [x] `dynamic_dispatch_types.py`: `subscribe` replaces
      `subscribe_to_changes` on both protos; TODO comment
      (`change_events` indirection, on the base proto's `subscribe`).
- [x] Port both impls onto `SubscriberHub`; delete the worker copies.
- [x] Update `tests/test_basic_dynamic_dispatch_10c.py` /
      `tests/test_nix_configurable_dispatch.py` (listener impls become
      `Subscriber` impls; add unsubscribe-path coverage). Also: the
      old proto-level idempotency test became a
      duplicate-subscribe-is-independent test (hub has no dedup), and
      the notify-False reap test became an `on_event`-raise
      crash-backstop test.
- [x] Update `projects/fxtr_workstation/CODE_OVERVIEW.md` (dynamic
      dispatch section).

### Chunk 3.5 — `CallbackInvokeOp` (invoke-on-callback-slot)

Scoped mid-stage (after chunk 3 review): let a mutation invoke a method
*on* one of the session's own callbacks by slot, the way args can
already reference one via the `["callback", slot]` tag. This is what
lets a client call `subscribe_to` on its own inbox without first
discovering the send-side service short_id — removing the
`GetCallbackServiceRequest` round trip from both the MCP and the
frontend, and enabling same-mutation create+subscribe.

Design decision (Daniel): a **separate tagged op**, not optional fields
on `InvokeOp` — disjoint shapes shouldn't be encoded as pairs of
optionals. `CallbackInvokeOp(slot, callback_slot, method_name, args,
kwargs)` is wire-tagged `callback_invoke`; validation routes both ops
into the same `invoke_specs` pipeline, so everything downstream of
target resolution (in-flight refcounts, `invoke_worker`, debug
surface) is shared and unchanged.

Why it's cheap: the invoke pipeline is `ServiceKey`-keyed immediately
after validation, and phase 3 already resolves callback slots against
`pending_creates ∪ open_callbacks` for args — a callback *target* is
the same lookup, reading `cb_record.service_key`. Pre-commit brings
same-mutation callbacks fully up before phase 3, so create+subscribe
ordering comes for free; the in-flight target pin (commit step d
before the close sweep in step e) already covers
invoke-then-close-in-one-mutation.

- [x] `client_protocol.py`: add `CallbackInvokeOp` to the `MutationOp`
      union; drop the chunk-2 TODO on `InvokeOp`.
- [x] `state.py`: phase-2 validation branch (`callback_slot` validated
      against the working callback-slot set, same slot monotonicity);
      phase-3 target resolution to the record's `ServiceKey` (via a
      `_CallbackInvokeTarget` marker riding `invoke_specs`);
      downstream untouched.
- [x] `protocol.ts`: add `CallbackInvokeOp` to the `MutationOp` union.
- [x] Tests (`test_sessions_5d.py`): invoke-on-callback across
      mutations, same-mutation create+`subscribe_to`+publish,
      unknown-slot reject, close-before-invoke-in-same-ops reject
      (atomic — the close doesn't happen either),
      invoke-then-close-same-mutation (in-flight pin; dispatch
      observes the closed inbox).
- [x] Update `projects/fxtr_workstation/CODE_OVERVIEW.md`.

### Chunk 4 — agent MCP plugin

- [x] `agent_mcp_proto.py` / `agent_mcp_impl.py`: `subscribe` +
      `SubscriberHub` (`deliver_current=True`); delete `watch_state` and
      the watcher machinery. (The constructor seeds the hub with
      `initial_head` when non-None, so persisted heads ride the
      first-event path too.)
- [x] `ws_connection.py`: inbox re-typing; `send` bool drop.
- [x] Sandbox bridge: import the three protocols from fxtr2 (verified
      fxtr2-portable force-includes `fxtr2/services`, so the new
      modules ship automatically); dup only
      `WorkstationSessionEventInbox`; `create_callback` typing,
      builtins/preamble.
- [x] `schedule_invoke` targeting an owned-callback stub: new queued-op
      kind `ScheduleCallbackInvokeOp` (separate kind, mirroring the
      wire-level `InvokeOp`/`CallbackInvokeOp` split; the target rides
      the existing placeholder grammar — `["callback_handle", k]`
      same-script / `["callback", N]` cross-script) → the mutation
      builder emits a `CallbackInvokeOp`; documented in the builtins
      docstrings + the skill doc. Log layer records it as a
      `ScriptOpRecord(kind="schedule_callback_invoke", callback=...)`
      with the walked `OwnedCallbackPlaceholder` ref as the target.
- [x] Update `tests/test_agent_mcp_impl.py` (rewritten for the hub
      model; includes a same-script-create+subscribe-relevant
      deliver-current suite), `tests/test_callback_e2e.py` (+ a new
      end-to-end `test_same_script_create_and_subscribe_to` covering
      sandbox → `ScheduleCallbackInvokeOp` → `CallbackInvokeOp` →
      `subscribe_to` → hub source → `CallbackEvent` → `forget` →
      unsubscribe), `tests/test_nix_dispatch_preset.py`,
      `tests/test_typed_service_e2e.py`,
      `examples/basic_test_demo` (callback-tester, demo_pkg, preamble).
- [x] Update `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`.

### Chunk 5 — frontend + demo servers + integration

- [x] Connector: `allocateInvoke` accepts a `CallbackHandleStub`
      target (new overload on `FxtrSessionUpdateCtx` + the session) →
      flush emits `CallbackInvokeOp` via a separate
      `CallbackInvokeOpSpec` (the handle's slot integer is known even
      same-tick, so no deferred resolution is needed client-side);
      cross-session smuggling rejected at allocate time without
      burning a slot. No `GetCallbackServiceRequest` auto-fetch, no
      `sendService` stub surface (obsoleted by chunk 3.5).
- [x] `dynamic_dispatcher.ts`: same-tick allocate-inbox + subscribe
      (callback-target `subscribe_to(dispatchServiceRef)` on the
      fresh inbox's handle).
- [x] `agent-plugin-renderer`: subscribe flow + head-as-first-event
      (the `subscribing` phase is now just the `subscribe_to` ack;
      `proto.ts` drops `watch_state` entirely — the agent service
      rides as the event-source arg, never invoked directly).
- [x] Python demo servers (`_demo_workstation.py`, both
      `demo_workstation.py`s) — `notify` sans bool (`notify_n`
      returns `n` unconditionally; `notify_random` returns the value
      unconditionally).
- [x] Integration tests (`integration.test.ts` — 30/30 against the
      ported demo; `dynamic_reload.test.ts` exercises the full new
      chain live: callback-target invoke → `CallbackInvokeOp` →
      workstation parker → dispatcher hub → reload event) + TS unit
      tests (3 new callback-target cases in
      `callback_reducer.test.ts`; dispatcher + agent-panel
      state-machine tests rewritten for the new flows).
- [x] Update `frontend_lib/CODE_OVERVIEW.md`.

### Wrap-up

- [x] Sweep for stragglers: `grep -r EventListenerProto` /
      `subscribe_to_changes` / `watch_state` across the repo comes up
      empty (excluding plan docs), with three deliberate exceptions —
      historical-context prose in `agent_mcp_impl.py`'s module
      docstring ×2 and `test_agent_mcp_impl.py` ×1 that explain what
      the new design replaced.
- [x] Update CODE_OVERVIEW.md (final pass over all four touched overview
      docs for consistency).

## Risks / things to verify during implementation

- **Returned-handle bridging**: `source.subscribe(...)` returns a
  chain-bridged `UnsubscribeHandle` ref whose lifetime is gated by the
  dispatch call scope; every holder (the workstation parker task, any
  direct Python subscriber) must `bridge_service_from_scope` it into a
  scope it owns before the call scope drains. Easy to get subtly wrong —
  cover with a "handle still works after the subscribe dispatch ends"
  test in chunk 1.
- **`subscribe_to` failure surfacing**: the parker task fails before
  `task_status.started()` if the source's `subscribe` raises — confirm the
  error propagates cleanly to the invoking client as an `invoke_error`
  (not a torn session mutation).
- ~~**Frontend re-attach**: `GetCallbackServiceRequest` must be re-issued
  on snapshot rebuild~~ — obsoleted by chunk 3.5: there is no
  client-side short_id cache; `CallbackInvokeOp` targets the slot
  directly and slots are stable across re-attach by construction.
- **Hub `deliver_current` atomicity**: the no-await window between
  snapshotting the current value and appending the subscription entry is
  load-bearing (no-dup-no-miss); preserve it explicitly + test under a
  concurrent `publish` (mirrors
  `test_watch_state_no_dup_no_miss_under_concurrent_update`).
- **TS typed-protocol shapes**: `subscribe_to` takes a service-ref arg and
  returns nothing; check the `Method<...>` declarations + `escapeInvokeArgs`
  path need no new cases (service refs in args already work). With chunk
  3.5, `allocateInvoke`'s *target* type widens to accept a
  `CallbackHandleStub` — verify the typed-protocol method/arg inference
  still applies for the service-ref form and degrades to loose types for
  the handle form.
- **Forceful-teardown mechanics**: verify the exact cancellation /
  `force_close` recipe for a mountable per-subscription scope with live
  inbound bridges (the entity-record `force_close` and the
  cancelled-RPC-connection teardown are the precedents), and that a
  post-teardown `on_event` dispatch from the peer surfaces as a clean
  error rather than a hang.

## Decisions (confirmed with Daniel unless noted)

- Direct-push `notify` on a closed inbox: **silent no-op**; `listening()`
  retained as the probe. The internal subscriber likewise drops silently,
  but only during the brief window between the `_closed` flip and the
  parker task's forceful teardown — after that, peers get dispatch errors
  (teardown *is* the durable "you're done" signal).
- The inbox is deliberately **not** a `Subscriber` (push method named
  `notify`, not `on_event`): if you have an `EventSource`, you use
  `subscribe_to`; direct subscription of an inbox behind the
  workstation's back is structurally impossible.
- One `SubscriberHub` class with a `mode="coalesce" | "queue"` constructor
  param (rather than separate `CoalescingSubscriberHub` /
  `QueueingSubscriberHub` classes); queue bounds/backpressure is a
  docstring TODO.
- No per-source `unsubscribe_from` in v1 — inbox teardown (close) drops
  all of its subscriptions; switching sources = new inbox.
- Wire/op names keep "callback" terminology (`CreateCallbackOp`,
  `CallbackEvent`, …); only the service-protocol layer says "inbox".
- Invoke-on-callback-slot (chunk 3.5) is a **separate tagged op**
  (`CallbackInvokeOp`) rather than optional fields on `InvokeOp` —
  disjoint shapes shouldn't be encoded as pairs of optionals; both ops
  converge into the shared invoke pipeline right after target
  resolution.
- `subscribe_to` returns `None`; the invoke-result event is the client's
  ack. On re-attach to a live session the frontend does **not**
  re-subscribe — the workstation-side subscription survived, and preserved
  `CallbackEvent`s re-deliver through the snapshot rebuild.
- Not re-asked: `subscribe_to` dedups by source `ServiceKey` (idempotent
  no-op), mirroring today's listener idempotency; all unsubscribe calls
  are best-effort with timeouts. (Revised in chunk-1 review: duplicate
  `hub.subscribe` calls create independent subscriptions rather than
  returning the existing handle — the hub has no dedup.)

## Implementation notes (running log, chunks 1–5)

Observations / judgement calls made while implementing, for review:

- **Hub `unsubscribe_grace` (chunk 1).** The hub's per-subscription
  teardown is forceful per the plan, except the unsubscribe path gets a
  small bounded graceful drain first (`unsubscribe_grace`, default
  0.5s) — an immediate revoke races the in-flight `unsubscribe()`
  dispatch's own reply, which would make even clean unsubscribes
  surface as transport errors to the caller.
- **`subscribe_to` on a closed inbox raises** (chunk 2) rather than
  silently no-oping — the silent-drop decision was scoped to `notify`;
  silently discarding a durable subscription request seemed wrong, and
  the raise surfaces cleanly as an `invoke_error`.
- **`deliver_current` semantics** (chunks 1/4): the hub retains the
  latest published value even with zero subscribers; with nothing ever
  published, `deliver_current=True` delivers nothing initially.
  Behavior change vs. `watch_state`: a pre-subscribe head is now
  *delivered as the first event* instead of riding the (deleted)
  return value — `test_agent_mcp_impl` was rewritten accordingly.
- **Pre-existing flaky test (not from this stage):**
  `test_supervisor_state_machine.py::test_free_retry_budget_refreshes_on_ready_handshake`
  fails its 5s ready-handshake budget when run after
  `tests/test_run_script_*` + `test_script_error` +
  `test_session_state_diff` + `test_stage7_buffer_e2e` (load-
  dependent). Verified the same failure with the same file combination
  at the pre-refactor baseline (chunk-1 commit) in a scratch
  workspace, and it passes in isolation on both — pre-existing,
  unrelated to this stage.
- **Forceful-teardown log noise:** tests that cancel live
  subscriptions sometimes log
  `INFO Arena ArenaKey(...) was finalized while open! (1 nodes live)`
  from the force-closed per-subscription scope's arena. Looks benign
  (force_close is exactly the "don't drain" path) but flagging it in
  case it warrants a quieter close in `FxtrScope.force_close`.
