# Stage 4 — Service-break notifications

## Motivation

A workstation brokers services between fxtr-RPC peers (sandboxes, agent
attached services) and high-level clients (browser UIs, the agent MCP).
When a peer registers a service, the workstation bridges it into a
workstation-owned `service_scope` and hands short-id'd references to
clients. Internally that bridge holds a **lease** on the source
service's lifetime token. If the grant backing that lease breaks — the
peer process dies, the RPC connection drops, or the source revokes —
the workstation loses its lease and every operation on that service
will now fail. Nothing on the `/client` surface tells anyone this
happened.

Today a client has **no way to learn that a service it holds has gone
dead**. This bites hardest with subscriptions: a subscriber hands an
`EventSource` a callback and then simply stops receiving events when the
source goes away — the *only* observable signal is that the paired
unsubscribe service breaks, which the client can't see. If the client
could detect that break, it could treat it as "resubscribe me." More
broadly, every debug/introspection view that lists services currently
has no way to show whether a listed service is still alive.

This stage adds a notification path so that any client holding (or
tracking) a service that breaks finds out, and so list/debug views can
render a third "broken" state.

## Scope

- **Services only.** Entities are out of scope: the workstation copies
  every tracked entity into its own per-record storage, so an entity a
  client holds does not depend on a still-live grant the way a service
  does. (The workstation holds a grant on its own entity scope, but
  that's purely for its own use and never breaks out from under a
  client.) Entities have no client-visible lease to break.
- **One-way and informational.** A break is terminal: once a service is
  reported broken it stays broken. There is no "recovered" or "replaced"
  signal — a client that wants a working service obtains a fresh ref.
  The notification does **not** change the service's tracked status or
  lifecycle; it is purely a signal.

## Design overview

### Why this is a side channel, not a session event

The obvious move would be a new `NonStateSessionEvent` (a seq'd entry on
the session event log, replayed via the session snapshot). That is
**wrong** for this signal:

- A seq'd event is delivered, acked, and then **drops past the retention
  horizon**. A client that reconnects after acking, or a brand-new
  subscriber to a session that already tracks an already-broken service,
  would get a snapshot containing *no* break event and never learn the
  service is dead.
- Brokenness is persistent **state**, not a point-in-time event. We do
  not care *when* a service broke, only that it is *currently* broken.
- The frontend coalesces breaks into a single connector-level fact
  anyway (first-one-wins), so historical log ordering buys nothing.

So service breakage is modeled as an **out-of-band, sequence-less side
notification driven by current state**. It is re-derived and re-delivered
on every (re)subscribe, which handles the "broke while I was away / broke
before I subscribed" cases by construction. It never touches the
mutation reducer, the event log, `event_refs`, refcounts, or the
ack/retention machinery.

### Delivery model

A new server frame `ServiceBrokenNotification(subscription_id,
service_short_id)` is pushed to a session subscription when, from that
subscription's perspective, a service the session currently holds
(`service_refcounts[key] > 0` — explicit `TrackServiceOp` pins *and*
implicit refs from unacked / preserved event payloads / in-flight
deliveries) is currently broken and has not yet been announced to that
subscription.

Delivery is **per session-subscription**, deduped per subscription via
an `announced_broken: dict[int, set[str]]` (sub_id → set of
service_short_ids) on `ClientConnectionRecord`. This reuses the existing
`_session_subs` fan-out and the per-session `service_refcounts` as the
subscription's "known set" for free. A connection hosting two sessions
that both hold the same broken service receives two frames — acceptable:
they carry distinct subscription ids, and the frontend coalesces them
into one connector-level fact. (True connection-level dedup — one frame
per connection regardless of how many of its sessions hold the service —
is a possible later micro-optimization; it would require a
connection-level "known services" union index and buys little given the
frontend already coalesces.)

Delivery is triggered at exactly the moments the (subscription, broken)
pairing can come into existence:

1. **A held service breaks.** The lease-broken callback marks the record
   broken, then for every session with `service_refcounts[key] > 0`, for
   every subscription of that session, announce (if not already).
2. **A subscription's session newly holds an already-broken service.**
   When `service_refcounts[key]` goes 0→1 (explicit track commit, or an
   incref from a delivered payload referencing the broken service),
   announce to that session's subscriptions if the record is broken.
3. **A new session subscription is created.** After the snapshot is
   sent, scan the session's currently-held broken services and announce
   each. (Fresh subscription → fresh `announced_broken` set → re-announce
   from current state on every reconnect.)

### Resource-list "broken" state

`_build_service_record` gains a **visible** (non-debug) `broken: bool`
field, set from the record. This surfaces the third state to `services`
resource subscribers (the debug tables) independently of the session
side channel — a connector subscribed only to the services list still
sees brokenness, and the list table can render it (e.g. strikethrough).
Goes through the existing `ResourceChangeBuffer` so the flip from
alive→broken emits a normal `resource_changes` delta.

---

## Chunk 1 — Backend (`fxtr_workstation`)

Detect the break, keep the service "known but unusable", and deliver the
side notification + resource delta.

### Break detection

In `service_record_worker` (`state.py`), after
`bridge_service_from_scope(..., keepalive_via="independent_lease")`
succeeds, grab the standing lease the bridge stored and register a
broken-callback on it. The `independent_lease` bridge stores an
`IndependentLocalBridge(lease=Lease[ref.handle.token])` in the scope's
`_known_handles` map (see `FxtrScope.attach_bridged_lowlevel_cap` /
`_build_independent_lease_bridge` in `entity_ref_scope.py`). Peeking the
scope's stored-lease map to recover that lease and calling
`lease.register_broken_callback(on_break)` is an internal-API reach but
an acceptable one here — it is exactly "subscribe to the lease that gates
our access". (Confirm the precise attribute path during implementation;
add a small accessor on `FxtrScope` if peeking private state directly is
too brittle.)

`register_broken_callback` gives a *reason* string and fires only on an
**ungraceful** break (revoke / RPC connection drop), not on an orderly
release — which is exactly the distinction we want. An orderly
unregister continues to flow through the existing registration-release
path and the normal resource-removal delta; it does **not** produce a
break notification.

### "Known but unusable" — the `forget_service` rework

Today `ShortIDAllocator.get_or_assign_service` (`short_ids.py`) wires
`key.handle.token.register_end_callback(lambda: self.forget_service(key))`.
When the source token ends, `forget_service` immediately *pops* the
`ServiceKey → short_id` mapping — even if a session still tracks the
service. That would (a) make `short_id_for_service` raise inside
`_build_session_debug_snapshot` for any session still tracking the now-
broken service, and (b) drop the short id from resolution, so the
service stops being "known".

Rework so breakage and forgetting are decoupled:

- On break we **mark the record broken** and keep the short id valid
  while the record still has trackers or registrations. The service
  stays in `service_records` (keyed by `ServiceKey`), the resource list
  still shows it (with `broken=true`), and `tracked.short_id` /
  `short_id_for_service` stay resolvable.
- The short id is forgotten only when the record is actually
  released (no trackers, no registrations) in `maybe_release_service_record`
  — at which point the dead handle can never recur, so the mapping is
  genuinely dead weight.

Invokes on a broken service then resolve the short id fine, await
`bridge_ready` (already set), and dispatch onto the bridged handle —
whose lease is broken — so the dispatch fails and the existing invoke
pipeline emits an `InvokeErrorEvent`. "Unusable but known" falls out
naturally; no special-casing of the invoke path is needed.

### State + delivery

- `TrackedServiceRecord` gains `broken: bool = False`.
- New `ServiceBrokenNotification(subscription_id: int, service_short_id:
  str)` frame in `client_protocol.py` (a top-level `ServerFrame`, not a
  `SessionEvent`).
- `ClientConnectionRecord` gains `announced_broken: dict[int, set[str]]`
  (sub_id → announced service short ids); cleared per subscription on
  unsubscribe, seeded empty on subscribe.
- A sync helper `_announce_service_broken_to_session(state, session)` /
  `_announce_break_for_record(state, tracked)` that walks the relevant
  `_session_subs` entries, checks `service_refcounts > 0` and the
  per-subscription `announced_broken` set, and `send_nowait`s the frame.
- Wire the three triggers: the lease-broken callback (mark broken +
  touch the `services` resource under a `mutation_context` + announce to
  all holding sessions); the `service_refcounts` 0→1 incref primitive;
  and `_handle_subscribe_session` after the snapshot send.
- `_build_service_record` adds the visible `broken` field; ensure the
  break callback `touch`es `("services", short_id)` so the alive→broken
  flip emits a `resource_changes` delta.

The break callback is sync and may fire from an arbitrary task (e.g. the
RPC reader applying a `BreakMessage`, or a revoke). It does only sync
registry work + `send_nowait`, consistent with the codebase's
GIL-atomicity / sync-registry-mutation discipline; it opens its own
`mutation_context` (the resource buffer pattern already supports this).

### Tests

- Register a service from a peer, track it in a session, break the peer
  side (drop the RPC connection / revoke), assert the tracking session's
  subscription receives `ServiceBrokenNotification` with the right short
  id, and that the `services` resource list flips to `broken=true`.
- A session that tracks the service *after* it broke gets announced on
  the track commit (trigger 2) and on a fresh subscribe (trigger 3).
- An invoke on a broken service yields `InvokeErrorEvent`.
- The short id stays resolvable and the debug snapshot does not raise
  while the broken service is still tracked; it is forgotten only after
  release.
- An *orderly* unregister produces a resource removal, **not** a break
  notification.
- Per-subscription dedup: a single break announces once per
  subscription; reconnect (fresh subscription) re-announces.

---

## Chunk 2 — Agent plugin (`fxtr_workstation_agent_plugin`)

Surface breaks to the agent as informative channel notifications —
best-effort "don't invoke on this service anymore".

- The `/client` reader (`ws_connection.py`) learns the new
  `ServiceBrokenNotification` frame. It is **not** a session reducer
  event: it does not go through `_apply_state_effect`, carries no seq,
  and does not touch the mirror / replayable-events / pending-changes.
- On receipt, route it to the `OutgoingBuffer` as a standalone
  notification (`kind="service_broken"`, content naming the service
  short id and any slot_keys in `data_by_key` that resolve to it), so it
  folds into an open tool window or flushes as a debounced channel frame
  like every other agent-facing notification.
- **MCP-side dedup.** Because the backend re-announces from current state
  on every resubscribe, the MCP must not re-spam the agent on every
  reconnect. Keep a per-connection set of already-surfaced broken short
  ids (analogous to `_notified_seqs`, but keyed by short id since there
  is no seq); reset on session close. Surface each broken service to the
  agent at most once per session.
- Optionally record a typed `ServiceBrokenLogEvent` on the agent event
  log chain so the operator-facing frontend view reflects it. (Decide
  during implementation whether this is worth a new log-event entity or
  whether the channel notification suffices for v1.)

### Tests

- Drive a session that holds a service, break it server-side, assert one
  `service_broken` channel notification reaches the agent surface.
- Resubscribe / reconnect does not re-surface an already-announced break.

---

## Chunk 3 — Frontend UI (`frontend_lib`)

Make service-ref stubs reactively reflect brokenness, coalesced at the
connector level, and let list/debug views render the third state.

### Connector-level broken set

- `@fxtr-ui/core`: add a `ServiceState = "alive" | "broken"` type and an
  abstract `get state(): Atom<ServiceState>` on `ServiceRefStub`. (A
  future HTTP connector implements it as a constant `"alive"`.)
- `@fxtr-ui/connector-workstation`: the connector holds a private
  writable `brokenServicesAtom` (a `Set<string>` of short ids) exposed as
  a read-only view, plus a `markServiceBroken(shortId)` method
  (idempotent set-add → reactive notify). Service-ref stubs stay
  **connector-interned** (option B): brokenness is a connector/global
  fact, so the same interned `WorkstationServiceRefStub` instance reflects
  it across every session.
- `WorkstationServiceRefStub.state` reads
  `get(connector.brokenServicesAtom).has(this.shortId) ? "broken" :
  "alive"`. The stub gains the connector's broken atom (or a small getter)
  at intern time — mirroring how op-result stubs receive a session
  backref.

### Feeding the broken set

Two independent, idempotent sources:

1. **Session subscription frames (primary).** `WorkstationSession`'s
   subscription handler learns the `ServiceBrokenNotification` frame and
   forwards it to the connector via a `markServiceBroken` callback passed
   in its options (alongside the existing `lookupEntity` /
   `lookupService`). This is **not** a reducer event — it does not touch
   `committed` / `local` / slot machinery.
2. **Services resource list.** `ReactiveResourceList` for `services`
   already carries each record's fields; with the new `broken` field,
   records flow through unchanged and a connector subscribed to the
   services list also calls `markServiceBroken` for `broken` records. The
   debug/list table renders the third state (e.g. strikethrough) straight
   from the record field — no stub needed there.

### Debug views

Service-listing debug panels (`fxtr-ui-workstation`) render the `broken`
record field (strikethrough / badge). Any renderer holding a
`ServiceRefStub` can read `stub.state` to reactively show/disable a dead
service or trigger an effect (e.g. resubscribe).

### Tests

- A `ServiceBrokenNotification` on a session marks the connector broken
  set; a `WorkstationServiceRefStub` with that short id flips
  `state` from `"alive"` to `"broken"`.
- Coalescing: breaks reported via two different sessions for the same
  short id produce one connector-level broken fact; the shared interned
  stub reflects it.
- The services `ReactiveList` surfaces `broken` and feeds the connector
  set; the list table renders the broken state.

---

## Testing strategy

Each chunk is independently testable:

- **Chunk 1** against the workstation server directly (peer registers a
  service over `/rpc`, a `/client` session tracks it, break the peer,
  assert the side frame + resource delta + invoke-error + short-id
  survival). Extends the existing session/service test suites.
- **Chunk 2** against the MCP's in-memory harness (`test_mcp_in_memory`):
  break a held service, assert the channel notification and the
  resubscribe dedup.
- **Chunk 3** as connector/session unit tests (mock WebSocket frames) for
  the broken-set coalescing and `stub.state`, plus a ReactiveList test
  for the `broken` field, mirroring the existing `slot_reducer` /
  `connector` test shapes.

## Open implementation details to confirm

- Exact accessor for the bridge's standing lease on
  `service_scope` (peek `_known_handles` vs. a small `FxtrScope` helper).
- Whether `register_broken_callback` reliably fires for the remote
  (RPC) case on connection drop *and* on explicit revoke; verify against
  the RPC lowering layer's break propagation.
- Whether the agent event-log chain should carry a typed
  `ServiceBrokenLogEvent` in v1 or defer to a channel-only notification.
